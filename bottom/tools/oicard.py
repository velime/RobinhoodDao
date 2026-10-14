"""Our own OI card for one coin — the same metrics the OI alerts carry, computed from exchange APIs.

Per exchange (Binance, Bybit): OI now and its change over 4h and 15m (in contracts, so price moves do
not fake OI growth), $ added, price change 4h, funding, L/S by accounts, mark side, Book Delta (bids − asks
within ±1%), walls (levels many times the average size within ±20%), A/D spot/perp 4h (taker net),
OI/MCap. Then the methodology classifier (pattern, scenario, combos 1–10, red flags).
"""

from __future__ import annotations

import asyncio
import logging
import time
from statistics import median

from . import derivatives as deriv
from . import feeds, http
from .ultra import classify

log = logging.getLogger(__name__)

BINANCE_F = feeds.BINANCE_F
BYBIT = feeds.BYBIT

_native: dict = {"ts": 0.0, "map": {}}


async def native_symbols() -> dict[tuple[str, str], tuple[str, int]]:
    """(exchange, base) → (native perp symbol, multiplier) for Binance and Bybit, cached 1h."""
    if time.time() - _native["ts"] < 3600 and _native["map"]:
        return _native["map"]
    res = await asyncio.gather(feeds.binance_perp(), feeds.bybit_perp(), return_exceptions=True)
    m: dict = {}
    for rows in res:
        if isinstance(rows, Exception):
            continue
        for r in rows:
            bm = feeds.base_of(r["symbol"])
            if bm:
                m.setdefault((r["ex"], r["base"]), (r["symbol"], bm[1]))
    if m:
        _native.update(ts=time.time(), map=m)
    return m


def _pct(a, b):
    return round((a - b) / b * 100, 2) if a is not None and b else None


def book_stats(bids: list, asks: list, mid: float, mult: int = 1) -> dict:
    """Book Delta (±1%) and walls (±20%, ≥8× the median level, ≥$5k). Levels as [price, size] per contract.
    Median, not mean: one huge wall must not raise the bar it is measured against."""
    lv = [(float(p) / mult, float(q) * float(p), "bid") for p, q, *_ in bids] + \
         [(float(p) / mult, float(q) * float(p), "ask") for p, q, *_ in asks]
    near = [x for x in lv if abs(x[0] - mid) / mid <= 0.01]
    delta = sum(x[1] for x in near if x[2] == "bid") - sum(x[1] for x in near if x[2] == "ask")
    zone = [x for x in lv if abs(x[0] - mid) / mid <= 0.20]
    avg = median(x[1] for x in zone) if zone else 0
    walls = sorted(
        ({"side": "below" if x[2] == "bid" else "above", "price": x[0], "dist_pct": round((x[0] - mid) / mid * 100, 1),
          "usd": round(x[1]), "x_avg": round(x[1] / avg)} for x in zone if avg and x[1] >= 8 * avg and x[1] >= 5000),
        key=lambda w: -w["usd"],
    )[:6]
    return {"book_delta_usd": round(delta), "walls": walls}


async def _binance(symbol: str, mult: int) -> dict:
    p5 = {"symbol": symbol, "period": "5m", "limit": 49}
    oi, kl, prem, ls, book = await asyncio.gather(
        http.get_json(f"{BINANCE_F}/futures/data/openInterestHist", p5),
        http.get_json(f"{BINANCE_F}/fapi/v1/klines", {"symbol": symbol, "interval": "5m", "limit": 49}),
        http.get_json(f"{BINANCE_F}/fapi/v1/premiumIndex", {"symbol": symbol}),
        http.get_json(f"{BINANCE_F}/futures/data/globalLongShortAccountRatio", {**p5, "limit": 1}),
        http.get_json(f"{BINANCE_F}/fapi/v1/depth", {"symbol": symbol, "limit": 500}),
    )
    coins = [float(x["sumOpenInterest"]) for x in oi]
    price = float(kl[-1][4]) / mult
    mark = float(prem["markPrice"]) / mult
    out = {
        "exchange": "Binance", "price": price,
        "oi_usd": round(float(oi[-1]["sumOpenInterestValue"])),
        "oi_chg_pct": _pct(coins[-1], coins[0]), "oi_window_h": 4,
        "oi_fast_chg_pct": _pct(coins[-1], coins[-4]) if len(coins) >= 4 else None, "oi_fast_window_min": 15,
        "oi_added_usd": round((coins[-1] - coins[0]) * price * mult),
        "price_chg_pct": _pct(float(kl[-1][4]), float(kl[0][1])),
        "funding_pct": round(float(prem["lastFundingRate"]) * 100, 4),
        "mark_side": "long" if mark >= price else "short",
    }
    if ls:
        out["long_pct"] = round(float(ls[-1]["longAccount"]) * 100, 1)
        out["short_pct"] = round(100 - out["long_pct"], 1)
    out.update(book_stats(book["bids"], book["asks"], price, mult))
    return out


async def _bybit(symbol: str, mult: int) -> dict:
    base = {"category": "linear", "symbol": symbol}
    oi, kl, tick, ls, book = await asyncio.gather(
        http.get_json(f"{BYBIT}/v5/market/open-interest", {**base, "intervalTime": "5min", "limit": 49}),
        http.get_json(f"{BYBIT}/v5/market/kline", {**base, "interval": "5", "limit": 49}),
        http.get_json(f"{BYBIT}/v5/market/tickers", base),
        http.get_json(f"{BYBIT}/v5/market/account-ratio", {**base, "period": "5min", "limit": 1}),
        http.get_json(f"{BYBIT}/v5/market/orderbook", {**base, "limit": 200}),
    )
    coins = [float(x["openInterest"]) for x in reversed(oi["result"]["list"])]  # newest first → oldest first
    kls = list(reversed(kl["result"]["list"]))
    t = tick["result"]["list"][0]
    price = float(t["lastPrice"]) / mult
    out = {
        "exchange": "Bybit", "price": price,
        "oi_usd": round(float(t.get("openInterestValue") or 0)),
        "oi_chg_pct": _pct(coins[-1], coins[0]), "oi_window_h": 4,
        "oi_fast_chg_pct": _pct(coins[-1], coins[-4]) if len(coins) >= 4 else None, "oi_fast_window_min": 15,
        "oi_added_usd": round((coins[-1] - coins[0]) * price * mult),
        "price_chg_pct": _pct(float(kls[-1][4]), float(kls[0][1])),
        "funding_pct": round(float(t.get("fundingRate") or 0) * 100, 4),
        "mark_side": "long" if float(t.get("markPrice") or 0) / mult >= price else "short",
    }
    rl = (ls.get("result") or {}).get("list") or []
    if rl:
        out["long_pct"] = round(float(rl[0]["buyRatio"]) * 100, 1)
        out["short_pct"] = round(100 - out["long_pct"], 1)
    b = book["result"]
    out.update(book_stats(b["b"], b["a"], price, mult))
    return out


async def market_cap(base: str, coingecko_key: str = "") -> float | None:
    headers = {"x-cg-demo-api-key": coingecko_key} if coingecko_key else None
    try:
        data = await http.get_json("https://api.coingecko.com/api/v3/coins/markets",
                                   {"vs_currency": "usd", "symbols": base.lower()}, headers)
    except Exception:  # noqa: BLE001
        return None
    caps = [c.get("market_cap") or 0 for c in data or []]
    return max(caps) if caps and max(caps) > 0 else None


async def oi_card(base: str, coingecko_key: str = "") -> dict:
    """Tool: OI card per exchange + combined reading."""
    base = base.upper()
    nat = await native_symbols()
    jobs, names = [], []
    for ex, fn in (("binance", _binance), ("bybit", _bybit)):
        if (ex, base) in nat:
            sym, mult = nat[(ex, base)]
            jobs.append(fn(sym, mult))
            names.append(ex)
    if not jobs:
        return {"base": base, "error": f"перпа {base} нет на Binance и Bybit (или биржи не ответили)"}
    res = await asyncio.gather(*jobs, asyncio.wait_for(deriv.spot_perp_flow(base), 20),
                               market_cap(base, coingecko_key), return_exceptions=True)
    flow, mcap = res[-2], res[-1]
    per, errors = [], {}
    for n, r in zip(names, res[:-2]):
        if isinstance(r, Exception):
            errors[n] = f"{type(r).__name__}: {str(r)[:100]}"
        else:
            per.append(r)
    if not per:
        return {"base": base, "error": "биржи не ответили", "errors": errors}
    ad_spot = ad_perp = None
    if isinstance(flow, dict):
        ad_spot = ((flow.get("spot") or {}).get("4h") or {}).get("net_usd")
        ad_perp = ((flow.get("perp") or {}).get("4h") or {}).get("net_usd")
    for e in per:
        if isinstance(mcap, (int, float)) and mcap:
            e["oi_mcap_pct"] = round(e["oi_usd"] / mcap * 100, 2)
        e["ad_spot_usd"], e["ad_perp_usd"] = ad_spot, ad_perp
        e.update(classify(e))
    main = max(per, key=lambda e: e["oi_usd"])
    combined = {k: v for k, v in main.items() if k not in ("pattern", "flags", "scenario", "combos")}
    combined["per_exchange"] = per
    reading = classify(combined)
    return {
        "base": base,
        "market_cap_usd": mcap if isinstance(mcap, (int, float)) else None,
        "per_exchange": per,
        "reading": reading,
        "a_d_note": "A/D спот/перп — чистые агрессивные покупки на Binance за 4ч (taker buy − sell)",
        "errors": errors or None,
        "checklist": "куда смотрит OI, куда цена, цвет Delta, куда толпа (L/S), масштаб (OI/MCap), ближайшие стенки; "
                     "OI — не главный индикатор: сверяй с объёмом, уровнями (POI/FVG/OB) и пластами ликвидаций",
    }
