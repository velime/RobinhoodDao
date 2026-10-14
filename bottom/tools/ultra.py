"""Ultra (OVERTAKE OI-bot) alerts: parse, aggregate series per ticker, classify by the playbook.

An alert looks like:
    $STG | Bybit
    OI: +36.37% за 4ч | +15.33% за 16м
    4.36M → 5.22M (+$312.6K)
    💲 Цена: $0.224540 (+16.37%)
    ⛽ Funding: -0.1130%
    ⚖️ L/S: 54.7% 🟢 / 45.3% 🔴
    🏷 Mark: $0.224550 (long)
    🏛 OI/MCap: 4.63%
    📕 Book Delta: -$27.4K
    🌊 A/D спот 4ч: +$261.4K ↑ (спот берут)  (перп +$2.92M ↑)
    🧱 Стакан Bybit:
    ↓ $0.228919 (+2.0%) — $34.9K (70x)
    12:35 UTC | #13 сигнал

Alerts arrive through the Telegram sources (the OVERTAKE channel and groups that
forward it). Classification is deterministic code — the model interprets, it does
not re-derive the pattern.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

_NUM = r"[+\-−]?\d+(?:\.\d+)?"
_HEAD = re.compile(r"\$([^\s|$]{1,24})\s*\|\s*(Binance|Bybit|OKX|Bitget|Gate|MEXC|BingX|Hyperliquid)", re.I)
_OI = re.compile(rf"OI:\s*({_NUM})%\s*за\s*(\d+)\s*ч(?:\s*\|\s*({_NUM})%\s*за\s*(\d+)\s*м)?")
_OI_ABS = re.compile(r"([\d.]+[KMB]?)\s*→\s*([\d.]+[KMB]?)\s*\(([+\-−])\$([\d.]+[KMB]?)\)")
_PRICE = re.compile(rf"(?:Цена:\s*|💲\s*)\$([\d.]+)(?:\s*\(({_NUM})%\))?")
_FUND = re.compile(rf"(?:Funding:|⛽️?)\s*({_NUM})%")
_LS = re.compile(r"(?:L/S:\s*|⚖️\s*)(\d+(?:\.\d+)?)%\s*🟢?\s*/\s*(\d+(?:\.\d+)?)%")
_MARK = re.compile(r"Mark:\s*\$([\d.]+)\s*\((long|short)\)", re.I)
_OIMC = re.compile(r"OI/MCap:\s*([\d.]+)%")
_DELTA = re.compile(r"Book Delta:\s*([+\-−])\$([\d.]+[KMB]?)")
_AD_SPOT = re.compile(r"A/D спот\s*\d+ч:\s*([+\-−])\$([\d.]+[KMB]?)")
_AD_PERP = re.compile(r"\(перп\s*([+\-−])\$([\d.]+[KMB]?)")
_WALL = re.compile(rf"([↑↓])\s*\$([\d.]+)\s*\(({_NUM})%\)\s*—\s*\$([\d.]+[KMB]?)\s*\((\d+)x\)")
_SIG = re.compile(r"(\d{1,2}:\d{2})\s*UTC(?:\s*\|\s*#(\d+)\s*сигнал)?")
_SCALE = {"K": 1e3, "M": 1e6, "B": 1e9}
_BLOCK = re.compile(rf"^(Binance|Bybit|OKX|Bitget|Gate|MEXC|BingX)\s*\|\s*OI\s*({_NUM})%", re.M | re.I)


def _f(s: str) -> float:
    return float(s.replace("−", "-"))


def _money(sign: str, s: str) -> float:
    mult = _SCALE.get(s[-1], 1)
    v = float(s[:-1] if s[-1] in _SCALE else s) * mult
    return -v if sign in ("-", "−") else v


def _fields(text: str) -> dict:
    """Price, funding, L/S, mark, OI/MCap, delta, A/D — from a whole alert or one exchange block."""
    a: dict = {}
    if m := _OI_ABS.search(text):
        a["oi_added_usd"] = _money(m.group(3), m.group(4))
    if m := _PRICE.search(text):
        a["price"] = float(m.group(1))
        if m.group(2):
            a["price_chg_pct"] = _f(m.group(2))
    if m := _FUND.search(text):
        a["funding_pct"] = _f(m.group(1))
    if m := _LS.search(text):
        a["long_pct"], a["short_pct"] = float(m.group(1)), float(m.group(2))
    if m := _MARK.search(text):
        a["mark_side"] = m.group(2).lower()
    if m := _OIMC.search(text):
        a["oi_mcap_pct"] = float(m.group(1))
    if m := _DELTA.search(text):
        a["book_delta_usd"] = _money(m.group(1), m.group(2))
    if m := _AD_SPOT.search(text):
        a["ad_spot_usd"] = _money(m.group(1), m.group(2))
    elif "спот n/a" in text:
        a["ad_spot_usd"] = None
    if m := _AD_PERP.search(text):
        a["ad_perp_usd"] = _money(m.group(1), m.group(2))
    return a


def _ex_name(s: str) -> str:
    return "OKX" if s.lower() == "okx" else "MEXC" if s.lower() == "mexc" else "BingX" if s.lower() == "bingx" else s.capitalize()


def parse_alert(text: str) -> dict | None:
    """One OI alert → dict, or None if the text is not one. Also '$X | Binance + Bybit' with a block per exchange."""
    head, oi = _HEAD.search(text), _OI.search(text)
    if not head or not oi:
        return None
    a: dict = {
        "ticker": head.group(1).upper(),
        "exchange": _ex_name(head.group(2)),
        "oi_chg_pct": _f(oi.group(1)),
        "oi_window_h": int(oi.group(2)),
    }
    if oi.group(3):
        a["oi_fast_chg_pct"], a["oi_fast_window_min"] = _f(oi.group(3)), int(oi.group(4))
    blocks = list(_BLOCK.finditer(text))
    if len(blocks) >= 2:
        per = []
        for i, b in enumerate(blocks):
            part = text[b.start(): blocks[i + 1].start() if i + 1 < len(blocks) else len(text)]
            per.append({"exchange": _ex_name(b.group(1)), "oi_chg_pct": _f(b.group(2)), **_fields(part)})
        a["exchange"] = "+".join(e["exchange"] for e in per)
        a["exchanges"] = [e["exchange"] for e in per]
        a["per_exchange"] = per
        for k, v in per[0].items():
            if k not in ("exchange", "oi_chg_pct"):
                a[k] = v
        for k, v in _fields(text).items():  # A/D etc. printed once for both
            a.setdefault(k, v)
    else:
        a.update(_fields(text))
    walls = [
        {"side": "above" if w.group(1) == "↓" else "below", "price": float(w.group(2)), "dist_pct": _f(w.group(3)),
         "usd": _money("+", w.group(4)), "x_avg": int(w.group(5))}
        for w in _WALL.finditer(text)
    ]
    if walls:
        a["walls"] = walls
    if m := _SIG.search(text):
        a["utc_time"] = m.group(1)
        if m.group(2):
            a["signal_no"] = int(m.group(2))
    return a


SCENARIOS = {
    (True, True): "OI растёт + цена растёт: приходят новые лонгисты — бычий тренд крепкий (лонг или держать)",
    (True, False): "OI растёт + цена падает: приходят новые шортисты — медвежий тренд крепкий (шорт или держать)",
    (False, True): "OI падает + цена растёт: шорты закрываются (шорт-сквиз) — технический отскок, осторожно с лонгом",
    (False, False): "OI падает + цена падает: лонги закрываются/ликвидируются — технический слив, может быть дно, "
                    "осторожно с шортом",
}


def combos(a: dict) -> list[str]:
    """Matches from the OI methodology (combos 1–10). Needs only the fields present in `a`."""
    oi, px = a.get("oi_chg_pct"), a.get("price_chg_pct")
    if oi is None or px is None:
        return []
    up, oi_up = px > 0, oi > 0
    d, lg, f, mc = a.get("book_delta_usd"), a.get("long_pct"), a.get("funding_pct"), a.get("oi_mcap_pct")
    per = a.get("per_exchange") or []
    out = []
    if oi_up and up and d is not None and d > 0:
        out.append("🟢 #1 OI↑ + цена↑ + Delta 📗 — деньги заходят, в стакане давление вверх")
    if oi_up and up and lg is not None and 45 <= lg <= 55:
        out.append("🟢 #2 OI↑ + цена↑ + L/S ~50/50 — тренд только начинается, толпа ещё не зашла")
    if oi_up and up and mc is not None and mc > 5:
        out.append("🟢 #3 OI↑ + цена↑ + OI/MCap >5% — серьёзное движение для монеты")
    if not oi_up and up and lg is not None and lg < 40:
        out.append("🟢 #4 OI↓ + цена↑ + L/S <40% — шорт-сквиз в действии, цену выносит вверх")
    if oi_up and up and len(per) >= 2 and all((x.get("book_delta_usd") or 0) > 0 for x in per):
        out.append("🟢 #5 Delta 📗 на обеих биржах + цена↑ — самый сильный бычий сигнал")
    if oi_up and not up and d is not None and d < 0 and lg is not None and lg > 60:
        out.append("🔴 #6 OI↑ + цена↓ + Delta 📕 + L/S >60% — идеальный шорт: лонги в ловушке")
    if oi_up and not up and f is not None and f >= 0.5 and lg is not None and lg > 65:
        out.append("🔴 #7 OI↑ + цена↓ + фандинг +0.5%+ + L/S >65% — перегруженные лонги платят, скоро ликвидации")
    if oi_up and not up and mc is not None and mc > 10:
        out.append("🔴 #8 OI↑ + цена↓ + OI/MCap >10% — сильное давление шортов")
    if oi_up and up and d is not None and d < 0:
        out.append("🔴 #9 OI↑ + цена↑ + Delta 📕 — расхождение: толпа покупает, а в стакане продают в лонгистов")
    if oi_up and up and lg is not None and lg > 75:
        out.append("🔴 #10 OI↑ + цена↑ + L/S >75% — толпа экстремально в лонге, скоро ликвидации сверху")
    return out


def classify(a: dict) -> dict:
    """Playbook pattern for one alert (or the latest state of a series) + methodology combos and scenario."""
    out = _pattern(a)
    oi, px = a.get("oi_chg_pct"), a.get("price_chg_pct")
    if oi is not None and px is not None and oi != 0:
        out["scenario"] = SCENARIOS[(oi > 0, px > 0)]
    out["combos"] = combos(a)
    return out


def _pattern(a: dict) -> dict:
    oi = a.get("oi_chg_pct", 0)
    px = a.get("price_chg_pct")
    f = a.get("funding_pct")
    lg = a.get("long_pct")
    flags = []
    pattern = "нет данных"
    if oi < 0 and px is not None:
        pattern = ("OI падает на росте — закрытие шортов, технический отскок" if px > 0
                   else "OI падает на падении — лонги выходят/ликвидации, возможно дно")
    elif px is not None and f is not None and lg is not None:
        if abs(px) < 2 and abs(f) < 0.02 and 45 <= lg <= 55:
            pattern = "шум: ротация позиций без направления"
        elif oi > 0 and px > 0 and f < 0 and lg < 50:
            pattern = "шорт-сквиз: шорты набираются против роста и платят — продолжение вероятно"
        elif oi > 0 and px > 0 and f < 0:
            pattern = "рост при отрицательном фандинге — шорты платят, возможен сквиз"
        elif oi > 0 and px < 0 and lg >= 65:
            pattern = "ловушка лонгистов: лонги набирают на падении — риск каскада ликвидаций вниз"
        elif oi > 0 and px < 0 and lg >= 58:
            pattern = "лонги усредняются на падении — толпа ловит нож, риск ловушки лонгистов"
        elif oi > 0 and px < 0 and lg < 50:
            pattern = "шорты набираются на падении — тренд вниз, но топливо для сквиза копится"
        elif oi > 0 and px > 0 and f >= 0.05 and lg >= 65:
            pattern = "перегрев лонгов: толпа в лонге и платит — риск ликвидаций сверху"
        elif oi > 0 and px > 0 and lg <= 55:
            pattern = "чистый лонг-моментум: новые деньги без перегрузки толпы"
        elif oi > 0 and px > 0:
            pattern = "лонг-моментум, толпа уже в лонге — вход только на откате"
        else:
            pattern = "неоднозначно"
    elif px is None and lg is not None and oi > 0:
        # some alerts carry the price without its change — judge by crowd and funding, direction from candles
        if lg <= 35:
            pattern = "шорты набирают позиции против рынка — топливо для сквиза, триггер нужен рост цены"
        elif lg >= 65:
            pattern = "лонги перегружены — риск слива; направление цены в алерте нет, смотри свечи"
        else:
            pattern = "в алерте нет изменения цены — направление смотри по свечам"
    if px is not None and abs(px) < 2 and oi >= 20:
        flags.append("OI растёт при стоящей цене — набор позиций перед движением")
    mc = a.get("oi_mcap_pct")
    if mc is not None:
        if mc > 1000:
            flags.append(f"OI/MCap {mc}% — спекулятивный пузырь, не торговать")
        elif mc > 300:
            flags.append(f"OI/MCap {mc}% — только скальп")
        elif mc > 100:
            flags.append(f"OI/MCap {mc}% — повышенная перегрузка деривативами")
    sp, pp = a.get("ad_spot_usd"), a.get("ad_perp_usd")
    if sp is not None and sp > 0 and (pp or 0) > 0:
        flags.append("спот и перп берут — органический спрос")
    elif sp is not None and sp < 0 < oi:
        flags.append("спот продают при растущем OI — рост держится на плечах")
    if pp is not None and pp < 0 < oi:
        flags.append("перп A/D отрицательный при растущем OI — признак дистрибуции")
    if f is not None and f >= 0.1:
        flags.append(f"фандинг +{f}% — перегрев (порог +0.1% уже сигнал)")
    if f is not None and f <= -0.5:
        flags.append(f"фандинг {f}% — шорты платят очень много")
    if (a.get("oi_fast_chg_pct") or 0) >= 10:
        flags.append(f"быстрый набор OI: +{a['oi_fast_chg_pct']}% за {a.get('oi_fast_window_min')} мин")
    if px is not None and abs(px) >= 15:
        flags.append(f"цена уже {px:+}% — движение во многом прошло до алерта, вход в погоню опасен")
    return {"pattern": pattern, "flags": flags}


def summarize(alerts: list[dict], now: float | None = None) -> dict:
    """Series of alerts for one ticker (oldest→newest) → compact state + pattern."""
    now = now or time.time()
    alerts = sorted(alerts, key=lambda x: x["ts"])
    last = alerts[-1]
    exchanges = sorted({e for a in alerts for e in (a.get("exchanges") or [a["exchange"]])})
    by_ex = defaultdict(list)
    for a in alerts:
        by_ex[a["exchange"]].append(a)
    # both venues alerting within 2 hours of each other = stronger confirmation
    dual = any(len(a.get("exchanges") or []) >= 2 for a in alerts) or len(exchanges) >= 2 and any(
        abs(x["ts"] - y["ts"]) <= 7200
        for i, e1 in enumerate(exchanges) for e2 in exchanges[i + 1:]
        for x in by_ex[e1] for y in by_ex[e2]
    )
    first = alerts[0]
    out = {
        "ticker": last["ticker"],
        "signals": len(alerts),
        "exchanges": exchanges,
        "both_exchanges_together": dual,
        "first_alert_hours_ago": round((now - first["ts"]) / 3600, 1),
        "last_alert_hours_ago": round((now - last["ts"]) / 3600, 1),
        "max_signal_no": max((a.get("signal_no") or 0) for a in alerts) or None,
        "oi_chg_pct_max": max(a["oi_chg_pct"] for a in alerts),
        "last": {k: last.get(k) for k in ("exchange", "oi_chg_pct", "oi_fast_chg_pct", "oi_fast_window_min",
                                           "price", "price_chg_pct", "funding_pct", "long_pct", "mark_side",
                                           "oi_mcap_pct", "book_delta_usd", "ad_spot_usd", "ad_perp_usd", "walls")
                 if last.get(k) is not None},
    }
    if first.get("price") and last.get("price"):
        out["price_move_since_first_alert_pct"] = round((last["price"] - first["price"]) / first["price"] * 100, 2)
    fs = [a["funding_pct"] for a in alerts if a.get("funding_pct") is not None]
    if len(fs) >= 2:
        out["funding_first_to_last"] = [fs[0], fs[-1]]
    out.update(classify(last))
    return out


def score(s: dict) -> float:
    """Rank tickers for 'best setup from Ultra': persistence, both venues, strength — and red flags."""
    sc = min(s["signals"], 12) + (4 if s["both_exchanges_together"] else 0) + min(s["oi_chg_pct_max"], 150) / 25
    if any("не торговать" in f or "ловушка" in f for f in s["flags"]) or "шум" in s["pattern"]:
        sc -= 8
    if any("прошло до алерта" in f for f in s["flags"]):
        sc -= 3
    return round(sc, 1)


def ultra_alerts(store, symbol: str = "", hours: int = 24, limit: int = 12) -> dict:
    """Alerts from the Telegram store: one ticker in detail, or the ranked overview."""
    since = time.time() - hours * 3600
    rows = store.db.execute(
        "SELECT ts, text FROM tg_messages WHERE ts >= ? AND text LIKE '%OI:%' ORDER BY ts", (since,)
    ).fetchall()
    seen = set()
    alerts = []
    for ts, text in rows:
        a = parse_alert(text)
        if not a:
            continue
        key = (a["ticker"], a["exchange"], a.get("utc_time"), a.get("signal_no"), a["oi_chg_pct"])
        if key in seen:  # the same alert forwarded into several groups
            continue
        seen.add(key)
        a["ts"] = ts
        alerts.append(a)
    if not alerts:
        return {"hours": hours, "error": "алертов Ultra (OVERTAKE) за период нет — проверь, что канал в источниках"}
    groups: dict[str, list] = defaultdict(list)
    for a in alerts:
        groups[a["ticker"]].append(a)
    if symbol:
        t = symbol.upper().lstrip("$").replace("USDT", "")
        series = groups.get(t)
        if not series:
            return {"hours": hours, "ticker": t, "error": f"по {t} алертов за {hours}ч нет",
                    "tickers_with_alerts": sorted(groups)[:40]}
        s = summarize(series)
        s["recent_alerts"] = [
            {k: v for k, v in a.items() if k not in ("ticker", "walls")} | {"hours_ago": round((time.time() - a["ts"]) / 3600, 1)}
            for a in sorted(series, key=lambda x: -x["ts"])[:15]
        ]
        for a in s["recent_alerts"]:
            a.pop("ts", None)
        return {"hours": hours, **s}
    summaries = [summarize(v) for v in groups.values()]
    for s in summaries:
        s["score"] = score(s)
    summaries.sort(key=lambda s: -s["score"])
    return {
        "hours": hours,
        "alerts_total": len(alerts),
        "tickers": len(summaries),
        "ranked": summaries[:limit],
        "note": "score — устойчивость серии, обе биржи, сила OI минус красные флаги; это фильтр, а не сигнал: "
                "проверь текущие данные (market_overview, derivatives, candles, orderbook) перед выводом",
    }
