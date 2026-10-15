"""Alerts of the parsers (OI, Funding, Dep / With, Whale perp, Спреды) read from their Telegram group.

The parsers live in their own project and post into the topics of a forum group. Bottom does not share a
database with them: the group is an ordinary Telegram source, collected into the Telegram store like any
other, and this module turns those messages back into structured alerts. A message is recognised by its
hashtags and layout, not by topic id, so the same code reads any chat that carries these formats.
"""

from __future__ import annotations

import re
import time

from .ultra import classify, parse_alert

FEEDS = ("oi", "funding", "spreads", "flows", "dex", "status")

_TAG_FEED = {"OI": "oi", "FUNDING": "funding", "SPREAD": "spreads", "INDEX": "spreads", "DEPOSIT": "flows",
             "WITHDRAWAL": "flows", "DEX": "dex", "TRANSFERS": "status"}
_TAG = re.compile(r"#(\w+)")
_MONEY = re.compile(r"([+\-−])?\$([\d.]+)([KMB])?")
_SCALE = {"K": 1e3, "M": 1e6, "B": 1e9}
_PCT = r"([+\-−]?\d+(?:\.\d+)?)%"
_FUND_ROW = re.compile(rf"^\s*[▲▼]?\s*([^\s$▲▼─].*?)\s+{_PCT}(?:/(\d+(?:\.\d+)?)ч)?\s+\$([\d.]+)", re.M)


def _num(s: str) -> float:
    return float(s.replace("−", "-"))


def money(text: str | None) -> float | None:
    """'$2.40M' / '-$132K' / '+$459.00K' → float."""
    m = _MONEY.search(text or "")
    if not m:
        return None
    v = float(m.group(2)) * _SCALE.get(m.group(3) or "", 1)
    return -v if m.group(1) in ("-", "−") else v


def _after(text: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*(.+)", text)
    return m.group(1).strip() if m else None


def feed_of(text: str) -> str | None:
    for t in _TAG.findall(text[-300:]):
        if t.upper() in _TAG_FEED:
            return _TAG_FEED[t.upper()]
    if parse_alert(text):  # an OVERTAKE-style alert carries no hashtag
        return "oi"
    return None


def _tags(text: str) -> list[str]:
    tail = text.strip().splitlines()[-1] if text.strip() else ""
    return _TAG.findall(tail)


def _oi(text: str) -> tuple[str | None, str | None, dict] | None:
    a = parse_alert(text)
    if not a:
        return None
    read = classify(a)
    keep = ("oi_chg_pct", "oi_fast_chg_pct", "price_chg_pct", "funding_pct", "long_pct", "book_delta_usd",
            "oi_mcap_pct", "ad_spot_usd", "ad_perp_usd", "signal_no")
    data = {k: a[k] for k in keep if a.get(k) is not None}
    data.update(ex=a["exchange"], pattern=read.get("pattern"), scenario=read.get("scenario"),
                combos=read.get("combos"), flags=read.get("flags"))
    return a["ticker"], a["exchange"], {k: v for k, v in data.items() if v not in (None, [], "")}


def _funding(text: str) -> tuple[str | None, str | None, dict]:
    bases = re.findall(r"[▲▼]\s*([A-Z0-9]{2,15})/USDT", text)
    head = re.search(r"([A-Z0-9]{2,15})/USDT", text)
    kind = "settlement" if "Расчёт через" in text else "jump" if "резкий скачок" in text else \
        "update" if "(обновление)" in text else "extreme"
    rows = [{"ex": m.group(1).strip(), "funding_pct": _num(m.group(2)),
             "interval_h": float(m.group(3)) if m.group(3) else 8.0}
            for m in _FUND_ROW.finditer(text) if not m.group(1).startswith(("Разница", "↔"))]
    trigger = [m.group(1).strip() for m in re.finditer(rf"^\s*[▲▼]\s*(\S.*?)\s+{_PCT}", text, re.M)
               if "/USDT" not in m.group(1)]
    base = None if kind == "settlement" else (head.group(1) if head else None)
    data = {"kind": kind, "trigger": trigger, "bases": bases if kind == "settlement" else [base] if base else [],
            "rows": rows}
    return base, None, data


_FF_HEAD = re.compile(r"#([A-Z0-9]{1,15})\s*\|\s*Spread:\s*([\d.]+)%(?:\s*\|\s*(NEW|[+\-][\d.]+)%?)?")


def _spread_ff(text: str) -> tuple[str | None, str | None, dict] | None:
    """The detailed futures-futures message: '#BRIC | Spread: 6.00% | +3.00%', Short/Long legs, funding, networks."""
    m = _FF_HEAD.search(text)
    if not m:
        return None
    data: dict = {"type": "perp_perp", "gross_pct": float(m.group(2))}
    if m.group(3) and m.group(3) != "NEW":
        data["delta_pct"] = float(m.group(3))
    if s := re.search(r"Short\s+(\S+)\s*:\s*\$([\d.]+)", text):
        data["dear"], data["short_price"] = s.group(1), float(s.group(2))
    if lg := re.search(r"Long\s+(\S+)\s*:\s*\$([\d.]+)", text):
        data["cheap"], data["long_price"] = lg.group(1), float(lg.group(2))
    fund = dict(re.findall(r"Funding\s+(\S+)\s*:\s*([+\-]?\d+(?:\.\d+)?)%", text))
    if fund:
        data["funding_pct"] = {k: float(v) for k, v in fund.items()}
    ivl = dict(re.findall(r"F/Interval\s+(\S+)\s*:\s*(\d+(?:\.\d+)?)H", text))
    if ivl:
        data["funding_interval_h"] = {k: float(v) for k, v in ivl.items()}
    if c := re.search(r"Перенос фандинга:\s*([+\-]?\d+(?:\.\d+)?)%", text):
        data["carry_8h_pct"] = float(c.group(1))
    if cap := re.search(r"Помещается\s*~(\$[\d.]+[KMB]?)(?:,\s*прибыль\s*~(\$[\d.]+[KMB]?))?", text):
        data["capacity"] = {"fits_usd": money(cap.group(1)), "profit_usd": money(cap.group(2))}
    nets = []
    for n in re.finditer(r"⛓\s*(\S+)\s+(\S+)\s*:\s*Dep:(🟢|🔴|⚪)\s*\|\s*W/d:(🟢|🔴|⚪)", text):
        nets.append({"ex": n.group(1), "chain": n.group(2), "deposit": n.group(3) == "🟢", "withdraw": n.group(4) == "🟢"})
    if nets:
        data["networks"] = nets
    return m.group(1), None, data


def _spread(text: str) -> tuple[str | None, str | None, dict]:
    ff = _spread_ff(text)
    if ff:
        return ff
    head = re.search(r"([A-Z0-9]{2,15})\s*·\s*(фьюч↔фьюч|спот↔фьюч|проверка индексов)\s*([+\-−]?\d+(?:\.\d+)?)?%?", text)
    base = head.group(1) if head else None
    kind = {"фьюч↔фьюч": "perp_perp", "спот↔фьюч": "spot_perp"}.get(head.group(2) if head else "", "index")
    data: dict = {"type": kind}
    if head and head.group(3):
        data["gross_pct"] = _num(head.group(3))
    if kind == "perp_perp":
        m = re.search(r"лонг (.+?) \$[\d.]+ → шорт (.+?) \$[\d.]+", text)
        if m:
            data["cheap"], data["dear"] = m.group(1), m.group(2)
    elif kind == "spot_perp":
        if m := re.search(r"схема:\s*(.+)", text):
            data["scheme"] = m.group(1).strip()
        if m := re.search(r"спот (.+?) \$[\d.]+.*?· фьюч (.+?) \$", text):
            data["cheap"], data["dear"] = m.group(1), m.group(2)
    if m := re.search(rf"перенос:\s*{_PCT}", text):
        data["carry_8h_pct"] = _num(m.group(1))
    elif m := re.search(r"(получаешь|платишь)\s+([\d.]+)%", text):
        data["carry_8h_pct"] = float(m.group(2)) * (1 if m.group(1) == "получаешь" else -1)
    if m := re.search(r"Держится:\s*(\d+)\s*мин(?:\s*·\s*макс\s*([\d.]+)%)?", text):
        data["held_min"] = int(m.group(1))
        if m.group(2):
            data["max_pct"] = float(m.group(2))
    if m := re.search(r"Помещается:\s*~(\$[\d.]+[KMB]?)(?:,\s*прибыль\s*~(\$[\d.]+[KMB]?))?", text):
        data["capacity"] = {"fits_usd": money(m.group(1)), "profit_usd": money(m.group(2))}
    if m := re.search(r"⚠️\s*(.+)", text):
        data["context"] = m.group(1).strip()
    if kind == "index":
        data["index_flags"] = [x.strip() for x in re.findall(r"^•\s*(.+)$", text, re.M)]
    elif m := re.search(r"🧭\s*(.+)", text):
        data["index_flags"] = [m.group(1).strip()]
    return base, None, {k: v for k, v in data.items() if v not in (None, "", [])}


def _flow(text: str) -> tuple[str | None, str | None, dict] | None:
    head = re.search(r"(Deposit|Withdrawal)(\s*🐋)?\s*\|\s*\$([A-Z0-9]{1,15})", text)
    if not head:
        return None
    tags = _tags(text)
    exchange = tags[2] if len(tags) > 2 else None
    vol = re.search(r"Объём:\s*([\d.]+[KMB]?)\s*\((\$[\d.]+[KMB]?)\)", text)
    data = {"kind": "deposit" if head.group(1) == "Deposit" else "withdrawal", "exchange": exchange,
            "chain": _after(text, "Network:"), "usd": money(vol.group(2)) if vol else None,
            "amount": vol.group(1) if vol else None, "whale": bool(head.group(2)),
            "frm": _after(text, "From:"), "to": _after(text, "To:")}
    if m := re.search(r"([\d.]+)% от суточного объёма", text):
        data["pct_vol"] = float(m.group(1))
    if m := re.search(r"([\d.]+)% от капы", text):
        data["pct_mcap"] = float(m.group(1))
    if "завёл весь запас" in text:
        data["wallet_left"] = 0
    return head.group(3), exchange, {k: v for k, v in data.items() if v is not None}


def _dex(text: str) -> tuple[str | None, str | None, dict] | None:
    head = re.search(r"\$([A-Z0-9]{1,15})\s*·\s*слив\s*·\s*(\S+)", text)
    if not head:
        return None
    sold = re.search(r"Продал:\s*(\$[\d.]+[KMB]?)\s*=\s*([\d.]+)%.*?·\s*(\d+)\s*сдел\S*\s*за\s*(\d+)\s*мин", text)
    wallet = re.search(r"\b(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b", text)
    data = {"net_sold_usd": money(head.group(2))}
    if sold:
        data.update(net_sold_usd=money(sold.group(1)), pct_of_liquidity=float(sold.group(2)),
                    sells=int(sold.group(3)), minutes=int(sold.group(4)))
    if wallet:
        data["wallet"] = wallet.group(1)
    return head.group(1), None, data


def _status(text: str) -> tuple[str | None, str | None, dict]:
    tags = _tags(text)
    base = tags[1] if len(tags) > 1 else None
    ex = tags[2] if len(tags) > 2 else None
    changes = [x.strip() for x in re.findall(r"^((?:🟢 открыт|🔴 закрыт)\s+.+)$", text, re.M)]
    return base, ex, {"changes": changes or [text.splitlines()[0]]}


_BOARD_ROW = re.compile(
    r"^\d+\.\s+\$?([A-Z0-9]{1,15})\s*·\s*(фьюч↔фьюч|спот↔фьюч)\s*·\s*(.+?)\s*→\s*(.+?)\s*\n"
    r"сейчас\s+([\d.]+)%\s*·\s*макс\s+([\d.]+)%\s*·\s*держится\s+(\d+)\s*мин(?:\s*·\s*влезает\s*~(\$[\d.]+[KMB]?))?",
    re.M)


_BOARD_TABLE_ROW = re.compile(
    r"^\d+\s*\|\s*\$?([A-Z0-9]{1,15})\s*\|\s*(фьюч↔фьюч|спот↔фьюч)\s*\|\s*(.+?)\s*→\s*(.+?)\s*\|\s*"
    r"([\d.]+)%\s*\|\s*([\d.]+)%\s*\|\s*(\d+)\s*\|\s*(.*?)\s*$", re.M)


def is_board(text: str) -> bool:
    return "#SPREADBOARD" in text[-200:] or "Актуальные спреды" in text[:120]


def parse_board(text: str) -> list[dict]:
    """The pinned 'actual spreads' message → one alert per row (current %, max %, minutes held)."""
    out = []
    for m in _BOARD_ROW.finditer(text):
        data = {"type": "perp_perp" if m.group(2) == "фьюч↔фьюч" else "spot_perp", "cheap": m.group(3),
                "dear": m.group(4), "gross_pct": float(m.group(5)), "max_pct": float(m.group(6)),
                "held_min": int(m.group(7)), "board": True}
        if m.group(8):
            data["capacity"] = {"fits_usd": money(m.group(8))}
        out.append({"feed": "spreads", "base": m.group(1), "ex": None, "data": data})
    for m in _BOARD_TABLE_ROW.finditer(text):  # the rich message flattened by the collector
        data = {"type": "perp_perp" if m.group(2) == "фьюч↔фьюч" else "spot_perp", "cheap": m.group(3),
                "dear": m.group(4), "gross_pct": float(m.group(5)), "max_pct": float(m.group(6)),
                "held_min": int(m.group(7)), "board": True}
        if m.group(8) and m.group(8) != "—":
            data["capacity"] = {"fits_usd": money(m.group(8))}
        out.append({"feed": "spreads", "base": m.group(1), "ex": None, "data": data})
    if m := re.search(r"Индексы расходятся:\s*(.+(?:\n[A-Z0-9, ]+)?)", text):
        for b in re.findall(r"[A-Z0-9]{1,15}", m.group(1)):
            out.append({"feed": "spreads", "base": b, "ex": None, "data": {"type": "index", "board": True}})
    return out


def parse(text: str) -> dict | None:
    """One group message → {feed, base, ex, data} or None when it is not a parser alert."""
    feed = feed_of(text)
    res = None
    if feed == "oi":
        res = _oi(text)
    elif feed == "funding":
        res = _funding(text)
    elif feed == "spreads":
        res = _spread(text)
    elif feed == "flows":
        res = _flow(text)
    elif feed == "dex":
        res = _dex(text)
    elif feed == "status":
        res = _status(text)
    if res is None:
        return None
    base, ex, data = res
    return {"feed": feed, "base": (base or "").upper() or None, "ex": ex, "data": data}


def load_alerts(store, chat_ids, hours: float = 24, limit: int = 3000) -> list[dict]:
    """Parsed alerts of the given chats, newest first: {ts, feed, base, ex, data, text}."""
    ids = list(chat_ids)
    if not ids:
        return []
    since = time.time() - hours * 3600
    marks = ",".join("?" * len(ids))
    rows = store.db.execute(
        f"SELECT ts, text FROM tg_messages WHERE chat_id IN ({marks}) AND ts >= ? ORDER BY ts DESC LIMIT ?",
        [*ids, since, limit]).fetchall()
    out = []
    for ts, text in rows:
        if is_board(text):
            out += [{"ts": ts, "text": text, **p} for p in parse_board(text)]
            continue
        p = parse(text)
        if p:
            out.append({"ts": ts, "text": text, **p})
    return out


def _brief(feed: str, d: dict) -> dict:
    if feed == "oi":
        keys = ("ex", "oi_chg_pct", "oi_fast_chg_pct", "price_chg_pct", "funding_pct", "long_pct", "book_delta_usd",
                "oi_mcap_pct", "ad_spot_usd", "ad_perp_usd", "signal_no", "pattern", "scenario", "combos", "flags")
    elif feed == "funding":
        return {"kind": d.get("kind"), "trigger": d.get("trigger"), "bases": d.get("bases"),
                "rates": {r["ex"]: r["funding_pct"] for r in d.get("rows") or []}}
    elif feed == "spreads":
        keys = ("type", "cheap", "dear", "gross_pct", "carry_8h_pct", "held_min", "max_pct", "capacity", "context",
                "index_flags", "flags")
    elif feed == "flows":
        keys = ("kind", "exchange", "chain", "usd", "amount", "pct_vol", "pct_mcap", "whale", "frm", "to", "wallet_left")
    elif feed == "status":
        keys = ("changes",)
    else:
        keys = ("net_sold_usd", "pct_of_liquidity", "sells", "minutes", "wallet")
    return {k: d.get(k) for k in keys if d.get(k) is not None}


def signals(store, chat_ids, feed: str = "", symbol: str = "", hours: float = 24) -> dict:
    """Tool: what the parsers posted — ranking of coins without a ticker, the whole series with one."""
    if store is None:
        return {"error": "Telegram-сборщик не подключён (TG_API_ID/TG_API_HASH) — группу парсеров читать нечем"}
    if not chat_ids:
        return {"error": "группа парсеров не задана — впиши её id в PARSER_CHAT_ID (его пишет setup_group.bat парсеров)"}
    feed = feed if feed in FEEDS else ""
    base = symbol.upper().lstrip("$").replace("USDT", "") if symbol else ""
    rows = [r for r in load_alerts(store, chat_ids, hours)
            if (not feed or r["feed"] == feed) and (not base or r["base"] == base or base in (r["data"].get("bases") or []))]
    now = time.time()
    if not rows:
        seen = store.db.execute(
            f"SELECT COUNT(*) FROM tg_messages WHERE chat_id IN ({','.join('?' * len(chat_ids))})", list(chat_ids)
        ).fetchone()[0]
        note = "за период сигналов нет" + ("" if feed else " ни в одной ленте")
        if not seen:
            note = "из группы парсеров ещё нет сообщений — проверь, что аккаунт Telegram в ней состоит, а сборщик запущен"
        return {"feed": feed or "все", "symbol": base or None, "hours": hours, "alerts": 0, "note": note}
    if base or feed == "funding":
        return {"feed": feed or "все", "symbol": base or None, "hours": hours, "alerts": len(rows), "recent": [
            {"feed": r["feed"], "hours_ago": round((now - r["ts"]) / 3600, 2), "ex": r["ex"], "base": r["base"],
             **_brief(r["feed"], r["data"])} for r in rows[:25]]}
    agg: dict[str, dict] = {}
    for r in rows:  # newest first
        a = agg.setdefault(r["base"] or "—", {"base": r["base"], "signals": 0, "feeds": set(),
                                              "last_hours_ago": round((now - r["ts"]) / 3600, 2),
                                              "last": _brief(r["feed"], r["data"])})
        a["signals"] += 1
        a["feeds"].add(r["feed"])
    top = sorted(agg.values(), key=lambda a: (-a["signals"], a["last_hours_ago"]))[:20]
    for a in top:
        a["feeds"] = sorted(a["feeds"])
    return {"feed": feed or "все", "hours": hours, "alerts": len(rows), "coins": len(agg), "top": top,
            "note": "это алерты наших парсеров (свои данные с бирж и DEX); перед выводом проверь свежие цифры"}


class _FlowRows:
    """Adapter: parsed Dep / With alerts shaped like the flows table `flows_summary` reads."""

    def __init__(self, rows: list[dict]):
        self.rows = rows

    def flows(self, base: str, days: float = 30) -> list[dict]:
        since = time.time() - days * 86400
        out = []
        for r in self.rows:
            d = r["data"]
            if r["feed"] != "flows" or r["base"] != base or r["ts"] < since or not d.get("usd"):
                continue
            out.append({"ts": r["ts"], "kind": d["kind"], "exchange": d.get("exchange") or "?",
                        "chain": d.get("chain") or "", "usd": d["usd"], "frm": d.get("frm") or "?",
                        "to": d.get("to") or "?", "tx": ""})
        return out


def exchange_flows(store, chat_ids, base: str, days: float = 30) -> dict:
    """Tool: deposits and withdrawals of a coin as the Dep / With parser reported them."""
    if store is None or not chat_ids:
        return {"error": "группа парсеров не подключена (TG_API_ID/TG_API_HASH и PARSER_CHAT_ID)"}
    res = flows_summary(_FlowRows(load_alerts(store, chat_ids, hours=days * 24)), base, days)
    res.setdefault("source", "алерты парсера Dep / With из Telegram-группы (только переводы выше его порога)")
    return res


def flows_summary(store, base: str, days: float = 30) -> dict:
    """Tool: deposits/withdrawals of a coin by exchange, net, biggest wallets, recent large moves."""
    rows = store.flows(base=base.upper(), days=days)
    if not rows:
        return {"base": base.upper(), "days": days, "note": "крупных переводов на биржи и с бирж за период не видел "
                "(или парсер Dep / With ничего не публиковал)"}
    by_ex: dict[str, dict] = {}
    senders: dict[str, float] = {}
    receivers: dict[str, float] = {}
    for r in rows:
        e = by_ex.setdefault(r["exchange"], {"deposits_usd": 0.0, "withdrawals_usd": 0.0, "n": 0})
        e["n"] += 1
        if r["kind"] == "deposit":
            e["deposits_usd"] += r["usd"]
            senders[r["frm"]] = senders.get(r["frm"], 0) + r["usd"]
        else:
            e["withdrawals_usd"] += r["usd"]
            receivers[r["to"]] = receivers.get(r["to"], 0) + r["usd"]
    dep = sum(e["deposits_usd"] for e in by_ex.values())
    wd = sum(e["withdrawals_usd"] for e in by_ex.values())
    return {
        "base": base.upper(), "days": days,
        "deposits_usd": round(dep), "withdrawals_usd": round(wd), "net_to_exchanges_usd": round(dep - wd),
        "read": "заводят на биржи больше, чем выводят — потенциальное давление продаж" if dep > wd * 1.5 else
                "выводят с бирж больше, чем заводят — накопление/уход с бирж" if wd > dep * 1.5 else "баланс",
        "by_exchange": {k: {kk: round(vv) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_ex.items()},
        "top_depositors": [{"wallet": a, "usd": round(u)} for a, u in sorted(senders.items(), key=lambda x: -x[1])[:5]],
        "top_receivers": [{"wallet": a, "usd": round(u)} for a, u in sorted(receivers.items(), key=lambda x: -x[1])[:5]],
        "largest": [{k: r[k] for k in ("kind", "exchange", "chain", "usd", "frm", "to", "tx")} |
                    {"hours_ago": round((time.time() - r["ts"]) / 3600, 1)}
                    for r in sorted(rows, key=lambda r: -r["usd"])[:8]],
    }


def spread_board(store, chat_ids, hours: float = 12) -> dict:
    """Tool: the pinned 'actual spreads' message as the parser last edited it — every row, with current and max %."""
    if store is None:
        return {"error": "Telegram-сборщик не подключён (TG_API_ID/TG_API_HASH) — закреп читать нечем"}
    if not chat_ids:
        return {"error": "группа парсеров не задана — впиши её id в PARSER_CHAT_ID"}
    boards = [r for r in load_alerts(store, chat_ids, hours) if r["data"].get("board") and r["data"].get("type") != "index"]
    if not boards:
        return {"hours": hours, "rows": [], "note": "закрепа спредов в группе за период нет (парсер spreads не писал "
                "или сборщик ещё не дошёл до группы)"}
    last_ts = max(r["ts"] for r in boards)
    rows = [r for r in boards if r["ts"] == last_ts]
    rows.sort(key=lambda r: -r["data"]["gross_pct"])
    return {"updated_min_ago": round((time.time() - last_ts) / 60, 1), "spreads": len(rows),
            "rows": [{"base": r["base"], "type": r["data"]["type"], "buy_at": r["data"]["cheap"],
                      "sell_at": r["data"]["dear"], "now_pct": r["data"]["gross_pct"], "max_pct": r["data"]["max_pct"],
                      "held_min": r["data"]["held_min"], "fits_usd": (r["data"].get("capacity") or {}).get("fits_usd")}
                     for r in rows],
            "note": "now_pct — спред сейчас, max_pct — максимум за время, пока он держится без перерыва; "
                    "данные парсера обновляются раз в ~3 минуты, перед выводом проверь цены (spread_check)"}
