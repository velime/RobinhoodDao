"""Last-price vs index-price spreads from the public channel @spread_last_index (read through the Telegram store).

A message of that channel looks like:

    🔴 RTXUSDT · Aster
    Spread: +5.35% (last above index)
    Holding: 11s
    Last:  0.5772
    Index: 0.5479

The channel is an ordinary Telegram source (channels/sources.json); the collector stores its posts and this
module turns them into rows. Index divergence is not computed anywhere else.
"""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict

from .symbols import normalize_base

SOURCE = os.getenv("INDEX_SPREAD_SOURCE", "spread_last_index")

_MSG = re.compile(
    r"([🔴🟢])\s*(?P<sym>[A-Z0-9_\-$]+)\s*·\s*(?P<ex>[^\n]+)\n"
    r"\s*Spread:\s*(?P<pct>[+\-−]?\d+(?:\.\d+)?)%\s*\((?P<side>last (?:above|below) index)\)\s*\n"
    r"\s*Holding:\s*(?P<hold>\d+)\s*s\s*\n"
    r"\s*Last:\s*(?P<last>[\d.eE+\-]+)\s*\n\s*Index:\s*(?P<index>[\d.eE+\-]+)")
_SUFFIX = re.compile(r"[_\-]?(USDTM|USDT|USDC|PERP|USD)$")


def parse(text: str) -> dict | None:
    m = _MSG.search(text)
    if not m:
        return None
    sym = m.group("sym").lstrip("$")
    base = normalize_base(_SUFFIX.sub("", sym)) or sym
    return {"base": base, "symbol": sym, "exchange": m.group("ex").strip(),
            "spread_pct": float(m.group("pct").replace("−", "-")),
            "side": "last выше индекса" if "above" in m.group("side") else "last ниже индекса",
            "holding_s": int(m.group("hold")), "last": float(m.group("last")), "index": float(m.group("index"))}


def _rows(store, hours: float, source: str) -> list[dict]:
    since = time.time() - hours * 3600
    rows = store.db.execute(
        "SELECT m.ts, m.text FROM tg_messages m JOIN tg_sources s ON s.chat_id = m.chat_id "
        "WHERE LOWER(s.username) = ? AND m.ts >= ? ORDER BY m.ts DESC LIMIT 4000", (source.lower().lstrip("@"), since)
    ).fetchall()
    out = []
    for ts, text in rows:
        p = parse(text)
        if p:
            out.append(p | {"ts": ts})
    return out


def index_spreads(store, symbol: str = "", hours: float = 24, source: str = "") -> dict:
    """Tool: coins whose last price diverges from the index by 5%+ (per the channel), or one coin's series."""
    source = source or SOURCE
    if store is None:
        return {"error": "Telegram-сборщик не подключён (TG_API_ID/TG_API_HASH)"}
    rows = _rows(store, hours, source)
    if not rows:
        have = store.db.execute("SELECT COUNT(*) FROM tg_sources WHERE LOWER(username) = ?",
                                (source.lower().lstrip("@"),)).fetchone()[0]
        return {"hours": hours, "source": f"@{source}", "events": 0,
                "note": "за период событий нет" if have else
                        f"канал @{source} не в источниках — добавь его в channels/sources.json и запусти сборщик"}
    now = time.time()
    base = normalize_base(symbol.lstrip("$")) if symbol else ""

    def brief(r: dict) -> dict:
        return {"exchange": r["exchange"], "spread_pct": r["spread_pct"], "side": r["side"],
                "holding_s": r["holding_s"], "last": r["last"], "index": r["index"],
                "hours_ago": round((now - r["ts"]) / 3600, 2)}

    if base:
        sel = [r for r in rows if r["base"] == base]
        if not sel:
            return {"hours": hours, "symbol": base, "events": 0, "note": f"по {base} отклонений last от индекса нет",
                    "source": f"@{source}"}
        by_ex: dict[str, list] = defaultdict(list)
        for r in sel:
            by_ex[r["exchange"]].append(r)
        return {"hours": hours, "symbol": base, "events": len(sel), "source": f"@{source}",
                "by_exchange": {ex: {"events": len(v), "max_abs_pct": max(abs(x["spread_pct"]) for x in v),
                                     "longest_holding_s": max(x["holding_s"] for x in v),
                                     "last_hours_ago": round((now - v[0]["ts"]) / 3600, 2)}
                                for ex, v in by_ex.items()},
                "recent": [brief(r) for r in sel[:15]]}
    agg: dict[str, dict] = {}
    for r in rows:  # newest first
        a = agg.setdefault(r["base"], {"base": r["base"], "events": 0, "exchanges": set(), "max_abs_pct": 0.0,
                                       "last": brief(r)})
        a["events"] += 1
        a["exchanges"].add(r["exchange"])
        a["max_abs_pct"] = max(a["max_abs_pct"], abs(r["spread_pct"]))
    top = sorted(agg.values(), key=lambda a: (-a["max_abs_pct"], -a["events"]))[:25]
    for a in top:
        a["exchanges"] = sorted(a["exchanges"])
    return {"hours": hours, "events": len(rows), "coins": len(agg), "source": f"@{source}", "top": top,
            "note": "отклонение last-цены от индексной цены биржи 5% и больше; это либо манипуляция/тонкий рынок, "
                    "либо риск ликвидации по марку — проверь стакан и индекс перед выводом"}
