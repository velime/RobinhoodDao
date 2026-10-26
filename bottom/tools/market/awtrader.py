"""awtrader.com: market-wide futures spreads and the funding table (public endpoints of a friend's service).

Main source for "which spreads exist right now" and "what is funding on every exchange": one request covers 12-13
exchanges, including Aster, Lighter, LBank and Phemex that Bottom's own exchange feeds do not have. Bottom's own tools
(spread_check, derivatives) remain for the deep look at one coin: order books, transfer status, indexes, history.

    GET /api/market/spreads?mode=ff&exchanges=...&min_vol=...&min_spread=...&limit=...
        {"rows": [{"b": coin, "sp": spread %, "n": exchanges, "odd": flag,
                   "buy": {"ex","sym","ask","bid","vol","f": {"rate","iv","next"},"liq"}, "sell": {...}}]}
        sp = (sell.bid - buy.ask) / buy.ask * 100, f.rate is % per f.iv hours (same unit as the exchanges' own feeds).
    GET /api/market/funding
        {"exchanges": [...], "ts": unix, "rows": [{"b","r": [...],"i": [...],"n": [...],"s": [...]}]}  (arrays by exchange)

Calls are cached (spreads 60 s, funding 120 s) so a chat of questions makes a few requests, not one per question.
"""

from __future__ import annotations

import os
import time

from .. import http

BASE = os.getenv("AWTRADER_URL", "https://awtrader.com/api/market")
EXCHANGES = "binance,bybit,okx,gate,bitget,aster,kucoin,hyperliquid,lighter,mexc,bingx,lbank"
TIMEOUT = 30  # the spreads endpoint takes several seconds
SPREADS_CACHE_SEC = 60
FUNDING_CACHE_SEC = 120
_cache: dict[tuple, tuple[float, object]] = {}


async def _get(path: str, params: dict | None = None) -> dict:
    r = await http.client().get(f"{BASE}/{path}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


async def _cached(key: tuple, ttl: float, path: str, params: dict | None) -> dict:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]  # type: ignore[return-value]
    d = await _get(path, params)
    _cache[key] = (time.time(), d)
    return d


def _side(s: dict, price_key: str) -> dict:
    f = s.get("f") or {}
    rate, iv = f.get("rate"), f.get("iv")
    return {"exchange": s.get("ex"), price_key: s.get(price_key), "funding_pct": rate, "interval_h": iv,
            "funding_8h_pct": round(rate * 8 / iv, 4) if rate is not None and iv else None,
            "vol24_usd": round(s["vol"]) if s.get("vol") else None, "liquidity_usd": s.get("liq")}


async def spreads(symbol: str = "", min_spread: float = 1.0, limit: int = 15, exchange: str = "",
                  min_vol: float = 100_000) -> dict:
    """Tool: open futures-futures spreads across all exchanges, biggest first (executable bid/ask prices)."""
    try:
        d = await _cached(("spreads", float(min_spread), int(min_vol)), SPREADS_CACHE_SEC, "spreads",
                          {"mode": "ff", "exchanges": EXCHANGES, "min_vol": int(min_vol), "min_spread": min_spread,
                           "limit": 400})
    except Exception as e:  # noqa: BLE001
        return {"error": f"awtrader недоступен ({type(e).__name__}: {str(e)[:80]}) — возьми spread_check и derivatives "
                         "по конкретным монетам"}
    base = symbol.upper().lstrip("$").replace("USDT", "") if symbol else ""
    ex = exchange.lower()
    out = []
    for row in d.get("rows") or []:
        buy, sell = row.get("buy") or {}, row.get("sell") or {}
        if base and (row.get("b") or "").upper() != base:
            continue
        if ex and ex not in (buy.get("ex"), sell.get("ex")):
            continue
        b, s = _side(buy, "ask"), _side(sell, "bid")
        carry = None
        if b["funding_8h_pct"] is not None and s["funding_8h_pct"] is not None:
            carry = round(s["funding_8h_pct"] - b["funding_8h_pct"], 4)  # short receives the sell side's, long pays buy's
        out.append({"coin": row.get("b"), "spread_pct": row.get("sp"), "buy_long": b, "sell_short": s,
                    "carry_8h_pct": carry, "exchanges_with_coin": row.get("n"), "odd_flag": row.get("odd")})
    out.sort(key=lambda r: -(r["spread_pct"] or 0))
    res = {"mode": "futures-futures", "min_spread_pct": min_spread, "found": len(out), "spreads": out[:max(1, int(limit))],
           "source": "awtrader.com (bid/ask, ~секунды)",
           "note": "buy_long — купить/лонг по ask, sell_short — продать/шорт по bid; carry_8h_pct > 0 — фандинг в твою "
                   "пользу. Стаканы, статусы сетей и индексы здесь не проверены: перед выводом spread_check по монете"}
    if base and not out:
        res["note"] = f"по {base} спреда выше {min_spread}% сейчас нет (или монеты нет на 2+ биржах из списка)"
    return res


async def funding(symbol: str = "", top: int = 15, min_exchanges: int = 3) -> dict:
    """Tool: funding of a coin on every exchange, or the market-wide extremes (rates normalised to 8 hours)."""
    try:
        d = await _cached(("funding",), FUNDING_CACHE_SEC, "funding", None)
    except Exception as e:  # noqa: BLE001
        return {"error": f"awtrader недоступен ({type(e).__name__}: {str(e)[:80]}) — возьми derivatives по монете"}
    exs = d.get("exchanges") or []
    now = d.get("ts") or time.time()
    base = symbol.upper().lstrip("$").replace("USDT", "") if symbol else ""

    def table(row: dict) -> list[dict]:
        out = []
        for i, ex in enumerate(exs):
            r = (row.get("r") or [None] * len(exs))[i]
            if r is None:
                continue
            iv = (row.get("i") or [None] * len(exs))[i]
            nxt = (row.get("n") or [None] * len(exs))[i]
            out.append({"exchange": ex, "symbol": (row.get("s") or [None] * len(exs))[i], "funding_pct": r,
                        "interval_h": iv, "funding_8h_pct": round(r * 8 / iv, 4) if iv else None,
                        "next_in_min": round((nxt - now) / 60) if nxt else None})
        return out

    rows = d.get("rows") or []
    meta = {"snapshot_age_s": round(time.time() - d["ts"]) if d.get("ts") else None, "source": "awtrader.com",
            "unit": "funding_pct — % за интервал выплаты; funding_8h_pct — пересчёт на 8 часов для сравнения бирж"}
    if base:
        row = next((r for r in rows if (r.get("b") or "").upper() == base), None)
        if not row:
            return {"coin": base, "error": "монеты нет в таблице фандинга", **meta}
        t = sorted((x for x in table(row) if x["funding_8h_pct"] is not None), key=lambda x: -x["funding_8h_pct"])
        res = {"coin": base, "exchanges": t, **meta}
        if len(t) >= 2:
            res["highest"], res["lowest"] = t[0]["exchange"], t[-1]["exchange"]
            res["gap_8h_pct"] = round(t[0]["funding_8h_pct"] - t[-1]["funding_8h_pct"], 4)
            res["idea"] = (f"шорт на {t[0]['exchange']} (получаешь {t[0]['funding_8h_pct']}% за 8ч) + лонг на "
                           f"{t[-1]['exchange']} (платишь {t[-1]['funding_8h_pct']}%) — дельта-нейтрально, если цены сошлись")
        return res
    per_coin = []
    for row in rows:
        t = [x for x in table(row) if x["funding_8h_pct"] is not None]
        if len(t) >= max(2, int(min_exchanges)):
            hi, lo = max(t, key=lambda x: x["funding_8h_pct"]), min(t, key=lambda x: x["funding_8h_pct"])
            ext = max(t, key=lambda x: abs(x["funding_8h_pct"]))
            per_coin.append({"coin": row.get("b"), "exchanges": len(t), "extreme": ext,
                             "gap_8h_pct": round(hi["funding_8h_pct"] - lo["funding_8h_pct"], 4),
                             "highest": hi["exchange"], "lowest": lo["exchange"]})
    n = max(1, int(top))
    return {"coins_in_table": len(rows), "min_exchanges": min_exchanges, **meta,
            "most_extreme": sorted(per_coin, key=lambda c: -abs(c["extreme"]["funding_8h_pct"]))[:n],
            "widest_gap_between_exchanges": sorted(per_coin, key=lambda c: -c["gap_8h_pct"])[:n]}
