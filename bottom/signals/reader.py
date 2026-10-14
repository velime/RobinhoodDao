"""What Bottom reads: recent alerts of our own scanners, per feed and per coin."""

from __future__ import annotations

import os
import time

from .store import SignalStore

FEEDS = ("oi", "funding", "spreads", "transfers", "dex")


def _brief(feed: str, d: dict) -> dict:
    if feed == "oi":
        keys = ("ex", "oi_chg_pct", "oi_fast_chg_pct", "price_chg_pct", "funding_pct", "long_pct", "book_delta_usd",
                "oi_mcap_pct", "ad_spot_usd", "ad_perp_usd", "signal_no", "pattern", "scenario", "combos", "flags")
    elif feed == "funding":
        return {"kind": d.get("kind"), "trigger": d.get("trigger"), "bases": d.get("bases"),
                "rates": {r["ex"]: r["funding_pct"] for r in d.get("rows") or []}}
    elif feed == "spreads":
        keys = ("buy", "sell", "gross_pct", "route", "held_min", "max_pct", "capacity")
    elif feed == "transfers":
        keys = ("changes",)
    else:
        keys = ("net_sold_usd", "pct_of_liquidity", "sells", "minutes", "wallet")
    return {k: d.get(k) for k in keys if d.get(k) is not None}


def signals(path: str, feed: str = "", symbol: str = "", hours: float = 24) -> dict:
    if not os.path.exists(path):
        return {"error": "сканеры ещё не запускались (нет базы сигналов) — запусти signals.bat"}
    st = SignalStore(path)
    now = time.time()
    feed = feed if feed in FEEDS else ""
    base = symbol.upper().lstrip("$").replace("USDT", "") if symbol else ""
    rows = st.alerts(feed or None, base or None, hours, limit=300)
    if not rows:
        return {"feed": feed or "все", "symbol": base or None, "hours": hours, "alerts": 0,
                "note": "за период сигналов нет" + ("" if feed else " ни в одной ленте")}
    if base or feed == "funding" and not base:
        return {"feed": feed or "все", "symbol": base or None, "hours": hours, "alerts": len(rows), "recent": [
            {"feed": r["feed"], "hours_ago": round((now - r["ts"]) / 3600, 2), "ex": r["ex"], **_brief(r["feed"], r["data"])}
            for r in rows[:25]]}
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
            "note": "это наши сканеры (свои данные с бирж и DEX); перед выводом проверь свежие цифры"}
