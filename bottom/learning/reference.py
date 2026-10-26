"""Learning from a reference auto-caller (Telegram export of its calls topic).

    python -m bottom reference import <result.json>   # parse calls, watches, updates + people's results
    python -m bottom reference evaluate [N]           # outcome of each call on real 1h candles
    python -m bottom reference stats                  # what worked, by idea type and direction
    python -m bottom reference bundle <result.json> [name]   # maintainer: merge an export into the shipped calls

Outcomes use the same conservative evaluator as Bottom's own setups (fill at the worst edge, stop first in
the same candle, TP1 → breakeven). Stats by idea type go into Bottom's experience block, and the agent can
look up similar reference calls with the reference_calls tool. The reference is a teacher, not an oracle:
its numbers are checked against the market, never copied.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict

log = logging.getLogger(__name__)

DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "reference_calls.json.gz")
_P = r"\$\s?(\d[\d,]*\.?\d*(?:e-?\d+)?)"
PRICE = re.compile(_P)
RANGE = re.compile(_P + r"\s*[–\-—]\s*\$?\s?(\d[\d,]*\.?\d*)")
HEAD = re.compile(r"^(📣|👀|🔄|💨)\s*Maga AI\s*(call|watch|update)?\s*(—\s*обновление\s*)?(—\s*вход ушёл\s*)?—\s*\$?([^\s(]+)",
                  re.I)
TAGS = {
    "exhausted_squeeze": ("отработанный сквиз", "вертикаль", "mean-reversion", "mean reversion"),
    "squeeze": ("шорт-сквиз", "сквиз шортов", "лонг-сквиз", "сквиз"),
    "oi_flip": ("флип oi", "флип-oi", "флип ои"),
    "oi_alert": ("рывок oi", "oi +", "по алертам ультры — рывок", "oi:"),
    "onchain_distribution": ("раздач", "завез", "депозит", "дистрибуц", "сгружает", "несёт обратно"),
    "onchain_accumulation": ("вывод с бирж", "выводят", "накоплен"),
    "funding_payout": ("выплат", "расчёт фандинга", "расчёт позади", "после расчёта", "до расчёта"),
    "listing": ("листинг",),
    "news": ("новост", "анонс", "хак", "взлом"),
    "fvg_limit": ("fvg",),
    "range_extreme": ("24ч-хай", "24ч хая", "у хая", "суточного диапазона"),
    "spot_led": ("спот-ведом", "спот ведёт", "спот берут"),
    "tokenized_stock": ("акци", "stock"),
}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _text(m: dict) -> str:
    t = m.get("text")
    return "".join(x if isinstance(x, str) else x.get("text", "") for x in t) if isinstance(t, list) else (t or "")


def _setup_block(t: str) -> str:
    for mark in ("🎯 Сетап", "Сетап (", "Сетап:", "Сетап\n", "🎯 Что делать", "🎯 Зона", "Инструмент:"):
        i = t.find(mark)
        if i >= 0:
            return t[i:]
    i = t.find("🎯")
    return t[i:] if i >= 0 else t


def _direction(block: str, full: str) -> str | None:
    for src in (block, full):
        m = re.search(r"\b(лонг|long|шорт|short)", src, re.I)
        if m:
            return "long" if m.group(1).lower() in ("лонг", "long") else "short"
    return None


def _prices(line: str) -> list[float]:
    m = RANGE.search(line)
    if m:
        a, b = _num(m.group(1)), _num(m.group(2))
        return sorted([a, b])
    return [_num(x) for x in PRICE.findall(line)][:1]


def parse_setup(t: str) -> dict | None:
    """Direction, main entry (limit/pullback preferred), aggressive entry, hard stop, targets."""
    block = _setup_block(t)
    d = _direction(block, t)
    if not d:
        return None
    main = aggressive = None
    stop = None
    targets: list[float] = []
    for raw in block.split("\n"):
        line = raw.strip()
        low = line.lower()
        if not line or "$" not in line:
            continue
        bare = re.sub(r"\([^)]*\)", "", line)  # "(перед пулом $10.445)" is context, not a level
        if re.search(r"tp\d|цел[ьи]|\bt\d\b|т\d", low) and "стоп" not in low.split("tp")[0][:6]:
            if not targets or "tp2" in low or "цель 2" in low or "t2" in low:
                targets += [p for p in (_num(x) for x in PRICE.findall(bare)) if p not in targets]
            continue
        if ("стоп" in low and not re.search(r"стоп в (б/?у|безубыт|бу)|стоп.{0,3}→|→ стоп|стоп переносим", low)) \
                and stop is None:
            ps = [_num(x) for x in PRICE.findall(bare)]
            if ps:
                stop = ps[0]
            continue
        if re.search(r"агрессив|от текущих|рынком|хай-риск вариант", low):
            if aggressive is None:
                aggressive = _prices(line)
            continue
        if re.search(r"лимит|основн|консерв|по откату|откат|вход", low) and main is None:
            main = _prices(line)
    entry = main or aggressive
    if not entry or stop is None or not targets:
        return None
    lo, hi = entry[0], entry[-1]
    tp = targets[0]
    ok = (stop < lo <= hi < tp) if d == "long" else (tp < lo <= hi < stop)
    if not ok:
        return None
    risk = abs((hi if d == "long" else lo) - stop)
    return {"direction": d, "entry_low": lo, "entry_high": hi, "stop": stop, "targets": targets[:4],
            "aggressive": aggressive, "rr_tp1": round(abs(tp - (hi if d == "long" else lo)) / risk, 2) if risk else None,
            "limit": bool(main)}


def tags_of(t: str) -> list[str]:
    """Idea types, read from the idea section (the whole text mentions everything in passing)."""
    i = t.find("💡")
    j = t.find("🎯", i + 1) if i >= 0 else -1
    low = (t[i:j] if i >= 0 and j > i else t[i:] if i >= 0 else t).lower()
    head = t.split("\n", 3)[:3]
    low += " " + " ".join(head).lower()
    tags = [k for k, words in TAGS.items() if any(w in low for w in words)]
    if "exhausted_squeeze" in tags and "squeeze" in tags:
        tags.remove("squeeze")
    return tags


def source_of(path: str) -> str:
    """Source name of an export: the chat's name ("Maga AI calls" → maga-ai-calls), so exports of the same
    chat made at different times land in one source and their messages are deduplicated by id."""
    try:
        name = json.load(open(path, encoding="utf-8")).get("name") or ""
    except (OSError, ValueError):
        name = ""
    slug = re.sub(r"[^\w]+", "-", name.lower()).strip("-")
    return slug or os.path.splitext(os.path.basename(path))[0]


def parse_export(path: str) -> tuple[list[dict], dict]:
    data = json.load(open(path, encoding="utf-8"))
    msgs = [m for m in data.get("messages", []) if m.get("type") == "message"]
    by_id = {m["id"]: m for m in msgs}
    out, stats = [], defaultdict(int)
    caller = None
    for m in msgs:
        t = _text(m)
        h = HEAD.match(t.strip())
        if not h:
            continue
        caller = caller or m.get("from")
        kind = "entry_gone" if h.group(4) or h.group(1) == "💨" else \
            "watch_update" if h.group(3) else (h.group(2) or "call").lower()
        stats[kind] += 1
        rec = {"msg_id": m["id"], "ts": _ts(m), "kind": kind, "ticker": h.group(5).upper().strip("$"),
               "tags": tags_of(t), "text": t[:3000], "reply_to": m.get("reply_to_message_id")}
        if kind in ("call", "update"):
            s = parse_setup(t)
            if s:
                rec.update(s)
                stats[f"{kind}_parsed"] += 1
        out.append(rec)
    # people's reported results under a call: "12% чистого", "стоп"
    idx = {r["msg_id"]: r for r in out}
    for m in msgs:
        if m.get("from") == caller:
            continue
        target = idx.get(m.get("reply_to_message_id"))
        if target is None:
            parent = by_id.get(m.get("reply_to_message_id"))
            target = idx.get(parent.get("reply_to_message_id")) if parent else None
        if target is None:
            continue
        t = _text(m).lower()
        for x in re.findall(r"([+-]?\d+(?:[.,]\d+)?)\s*%\s*(?:чист|в карман|сюда|есть|от лимит)", t):
            target.setdefault("reported", []).append(float(x.replace(",", ".")))
        if re.search(r"\bстоп\b|по стопу|сняли по стопу", t) and "бу" not in t:
            target["reported_stop"] = True
    return out, dict(stats)


def _ts(m: dict) -> float:
    from datetime import datetime

    if m.get("date_unixtime"):
        return float(m["date_unixtime"])
    return datetime.fromisoformat(m["date"]).timestamp()


# ------------------------------------------------------------------------------- storage


def ensure_table(db) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS reference_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, msg_id INTEGER, created REAL, kind TEXT, symbol TEXT,
            direction TEXT, entry_low REAL, entry_high REAL, stop REAL, targets TEXT, rr_tp1 REAL, limit_entry INTEGER,
            tags TEXT, text TEXT, reported TEXT, status TEXT, outcome TEXT, r REAL, mfe_pct REAL, mae_pct REAL,
            closed_at REAL, UNIQUE (source, msg_id)
        );
        """
    )
    if "reviewed" not in {r[1] for r in db.execute("PRAGMA table_info(reference_calls)")}:
        db.execute("ALTER TABLE reference_calls ADD COLUMN reviewed INTEGER DEFAULT 0")
    db.commit()


def store(db, records: list[dict], source: str) -> int:
    ensure_table(db)
    n = 0
    for r in records:
        rep = {"pct": r.get("reported") or [], "stop": bool(r.get("reported_stop"))}
        cur = db.execute(
            "INSERT OR IGNORE INTO reference_calls(source, msg_id, created, kind, symbol, direction, entry_low, "
            "entry_high, stop, targets, rr_tp1, limit_entry, tags, text, reported, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source, r["msg_id"], r["ts"], r["kind"], r["ticker"], r.get("direction"), r.get("entry_low"),
             r.get("entry_high"), r.get("stop"), json.dumps(r.get("targets") or []), r.get("rr_tp1"),
             int(bool(r.get("limit"))), json.dumps(r["tags"], ensure_ascii=False), r["text"],
             json.dumps(rep), "open" if r.get("stop") else "no_setup"))
        n += cur.rowcount
        if not cur.rowcount and (rep["pct"] or rep["stop"]):  # a later export brought people's results
            db.execute("UPDATE reference_calls SET reported=? WHERE source=? AND msg_id=?",
                       (json.dumps(rep), source, r["msg_id"]))
    db.commit()
    return n


def load_bundled(db) -> int:
    """The reference calls shipped with Bottom go into the journal; on every start, so calls added to the
    bundle by an update reach an existing journal too (known ones are skipped). Returns how many are new."""
    ensure_table(db)
    if not os.path.exists(DATA):
        return 0
    with gzip.open(DATA, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    return store(db, data["records"], data["source"])


def merge_bundle(path: str, source: str | None = None) -> tuple[int, int, dict]:
    """Maintainer: add an export to the shipped bundle (same message id → the newer parse wins)."""
    recs, st = parse_export(path)
    for r in recs:
        r["text"] = r["text"][:1500]
    old: dict = {"source": source or source_of(path), "records": []}
    if os.path.exists(DATA):
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            old = json.load(fh)
    by_id = {r["msg_id"]: r for r in old["records"]}
    added = sum(1 for r in recs if r["msg_id"] not in by_id)
    for r in recs:
        prev = by_id.get(r["msg_id"])
        if prev and prev.get("reported") and not r.get("reported"):  # results replied to in an older export
            r["reported"], r["reported_stop"] = prev["reported"], prev.get("reported_stop")
        by_id[r["msg_id"]] = r
    merged = sorted(by_id.values(), key=lambda r: r["ts"])
    with gzip.open(DATA, "wt", encoding="utf-8") as fh:
        json.dump({"source": old["source"], "records": merged}, fh, ensure_ascii=False, separators=(",", ":"))
    return added, len(merged), st


def open_calls(db, limit: int = 30) -> list[dict]:
    ensure_table(db)
    rows = db.execute(
        "SELECT id, created, symbol, direction, entry_low, entry_high, stop, targets FROM reference_calls "
        "WHERE status = 'open' ORDER BY created LIMIT ?", (limit,)).fetchall()
    return [{"id": i, "created": c, "symbol": s, "direction": d, "entry_low": lo, "entry_high": hi, "stop": st,
             "targets": json.loads(tg)} for i, c, s, d, lo, hi, st, tg in rows]


def update(db, rid: int, res: dict) -> None:
    db.execute("UPDATE reference_calls SET status=?, outcome=?, r=?, mfe_pct=?, mae_pct=?, closed_at=? WHERE id=?",
               (res.get("status"), res.get("outcome"), res.get("r"), res.get("mfe_pct"), res.get("mae_pct"),
                time.time() if res.get("status") == "closed" else None, rid))
    db.commit()


async def candles_since(pool, symbol: str, since_s: float, hours: int) -> list[dict]:
    """1h candles of the coin's perp (or spot) starting an hour before the call.

    [] means no exchange lists the coin; when no exchange answers at all it raises, so the call stays open
    for a later run instead of being written off as no_data."""
    from ..tools.market.exchanges import find_market
    from ..tools.symbols import normalize_base

    base = normalize_base(symbol)
    reached = False
    for kind in ("perp", "spot"):
        for ex_id in ("binance", "bybit", "okx", "bitget", "gate", "mexc"):
            ex = await pool.get(ex_id)
            reached = reached or ex is not None
            found = find_market(ex, base, kind) if ex else None
            if not found:
                continue
            m, mult = found
            try:
                raw = await ex.fetch_ohlcv(m["symbol"], "1h", since=int((since_s - 3600) * 1000), limit=min(hours, 1000))
            except Exception:  # noqa: BLE001
                continue
            if raw:
                return [{"t": r[0], "o": r[1] / mult, "h": r[2] / mult, "l": r[3] / mult, "c": r[4] / mult,
                         "v": r[5] * mult} for r in raw]
    if not reached:
        raise RuntimeError("биржи недоступны")
    return []


async def evaluate(db, pool, limit: int = 30) -> int:
    """Outcome of open reference calls on real candles. Returns how many closed."""
    from .evaluate import FILL_WINDOW_H, MAX_OPEN_H, evaluate_setup

    closed = 0
    for c in open_calls(db, limit):
        hours = min(int((time.time() - c["created"]) / 3600) + 2, FILL_WINDOW_H + MAX_OPEN_H + 2)
        try:
            candles = await candles_since(pool, c["symbol"], c["created"], max(hours, 3))
        except Exception as e:  # noqa: BLE001
            log.info("reference %s: %s", c["symbol"], e)
            continue
        if not candles:
            update(db, c["id"], {"status": "no_data"})
            continue
        res = evaluate_setup(c, candles)
        update(db, c["id"], res)
        closed += res.get("status") == "closed"
    return closed


WIN = ("tp1_then_be", "tp2", "tp1_expired", "tp1_open")


def unreviewed(db, limit: int = 40) -> list[dict]:
    """Closed reference calls not yet shown to the lessons review, oldest first."""
    ensure_table(db)
    rows = db.execute(
        "SELECT id, created, kind, symbol, direction, entry_low, entry_high, stop, targets, rr_tp1, limit_entry, tags, "
        "outcome, r, mfe_pct, mae_pct, reported, text FROM reference_calls "
        "WHERE status = 'closed' AND COALESCE(reviewed, 0) = 0 ORDER BY created LIMIT ?", (limit,)).fetchall()
    cols = ("id", "created", "kind", "symbol", "direction", "entry_low", "entry_high", "stop", "targets", "rr_tp1",
            "limit_entry", "tags", "outcome", "r", "mfe_pct", "mae_pct", "reported", "text")
    out = []
    for row in rows:
        x = dict(zip(cols, row))
        x["targets"], x["tags"], x["reported"] = json.loads(x["targets"]), json.loads(x["tags"]), json.loads(x["reported"])
        out.append(x)
    return out


def mark_reviewed(db, ids: list[int]) -> None:
    db.executemany("UPDATE reference_calls SET reviewed = 1 WHERE id = ?", [(i,) for i in ids])
    db.commit()


def stats(db, min_n: int = 5) -> dict:
    """Win rate (TP1 reached), average R, fill rate — overall, by idea type and direction, limit vs market."""
    ensure_table(db)
    rows = db.execute("SELECT direction, tags, outcome, r, mfe_pct, limit_entry FROM reference_calls "
                      "WHERE status = 'closed'").fetchall()
    groups: dict[str, list] = defaultdict(list)
    for d, tags, outcome, r, mfe, lim in rows:
        keys = ["все", f"{d}", "лимитный вход" if lim else "вход от текущих"]
        keys += [f"{t} {d}" for t in json.loads(tags or "[]")]
        for k in keys:
            groups[k].append((outcome, r or 0.0, mfe or 0.0))
    out = {}
    for k, v in groups.items():
        filled = [x for x in v if x[0] != "not_filled"]
        if len(filled) < min_n:
            continue
        wins = [x for x in filled if x[0] in WIN]
        out[k] = {"calls": len(v), "filled": len(filled), "win_rate": round(len(wins) / len(filled) * 100),
                  "avg_r": round(sum(x[1] for x in filled) / len(filled), 2),
                  "not_filled_pct": round((len(v) - len(filled)) / len(v) * 100),
                  "avg_mfe_pct": round(sum(x[2] for x in filled) / len(filled), 1)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["filled"]))


def experience_lines(db, limit: int = 8) -> list[str]:
    st = stats(db)
    if not st:
        return []
    lines = ["Референс (авто-колер), исходы его колов на реальных свечах — что работало:"]
    for k, v in list(st.items())[: limit + 3]:
        lines.append(f"- {k}: {v['filled']} сделок, TP1 у {v['win_rate']}%, средний {v['avg_r']}R, "
                     f"не дошли до входа {v['not_filled_pct']}%")
    return lines


def lookup(db, symbol: str = "", tag: str = "", limit: int = 8) -> dict:
    """Tool: similar reference calls (by coin or idea type) with their real outcome."""
    ensure_table(db)
    q, args = "SELECT created, kind, symbol, direction, entry_low, entry_high, stop, targets, rr_tp1, tags, outcome, r, " \
              "mfe_pct, reported, substr(text, 1, 600) FROM reference_calls WHERE stop IS NOT NULL", []
    if symbol:
        q += " AND symbol = ?"
        args.append(symbol.upper().lstrip("$"))
    if tag:
        q += " AND tags LIKE ?"
        args.append(f'%"{tag}"%')
    rows = db.execute(q + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()
    cols = ("created", "kind", "symbol", "direction", "entry_low", "entry_high", "stop", "targets", "rr_tp1", "tags",
            "outcome", "r", "mfe_pct", "reported", "text")
    items = []
    for r in rows:
        x = dict(zip(cols, r))
        x["days_ago"] = round((time.time() - x.pop("created")) / 86400, 1)
        x["targets"], x["tags"], x["reported"] = json.loads(x["targets"]), json.loads(x["tags"]), json.loads(x["reported"])
        items.append(x)
    return {"symbol": symbol or None, "tag": tag or None, "calls": items, "stats": stats(db),
            "note": "референс — учебный материал: его уровни не копировать, логику проверять своими данными"}


def main() -> None:
    import asyncio
    import sqlite3

    from ..config import load_settings

    logging.basicConfig(level=logging.INFO)
    s = load_settings()
    db = sqlite3.connect(s.db_path)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "import":
        recs, st = parse_export(sys.argv[2])
        n = store(db, recs, source_of(sys.argv[2]))
        print(f"разобрано: {st}; новых записей: {n}")
    elif cmd == "bundle":  # maintainer: merge an export into the calls shipped with Bottom
        added, total, st = merge_bundle(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
        print(f"в {DATA}: новых {added}, всего {total}; {st}")
        return
    elif cmd == "evaluate":
        from ..tools.market.exchanges import ExchangePool

        async def run():
            pool = ExchangePool(s.exchanges)
            try:
                return await evaluate(db, pool, int(sys.argv[2]) if len(sys.argv) > 2 else 500)
            finally:
                await pool.close()

        print(f"закрыто: {asyncio.run(run())}")
    print(json.dumps(stats(db, min_n=3), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
