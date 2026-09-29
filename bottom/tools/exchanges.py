"""CEX market data via ccxt: prices across exchanges, order books, candles."""

from __future__ import annotations

import asyncio
import logging
import time
from statistics import median

import ccxt.async_support as ccxt

from .symbols import MULTIPLIER_PREFIXES, normalize_base

log = logging.getLogger(__name__)

MARKETS_TTL = 6 * 3600
RETRY_FAILED_AFTER = 600
PERP_QUOTES = ("USDT", "USDC")
# Load only spot + linear perps (skip options/inverse/futures: faster start, less memory)
MARKET_TYPES = {
    "binance": ["spot", "linear"],
    "bybit": ["spot", "linear"],
    "okx": ["spot", "swap"],
    "gate": ["spot", "swap"],
    "kucoin": ["spot", "swap"],
}


class ExchangePool:
    """Lazily created ccxt exchanges with cached markets."""

    def __init__(self, ids: tuple[str, ...]):
        self.ids = [i for i in ids if hasattr(ccxt, i)]
        self._ex: dict[str, ccxt.Exchange] = {}
        self._loaded_at: dict[str, float] = {}
        self._failed_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {i: asyncio.Lock() for i in self.ids}

    async def get(self, ex_id: str) -> ccxt.Exchange | None:
        if ex_id not in self._locks:
            return None
        async with self._locks[ex_id]:
            now = time.time()
            if now - self._failed_at.get(ex_id, 0) < RETRY_FAILED_AFTER:
                return None
            ex = self._ex.get(ex_id)
            if ex is None:
                ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 10000})
                fm = ex.options.get("fetchMarkets")
                if ex_id in MARKET_TYPES and isinstance(fm, dict):
                    fm["types"] = MARKET_TYPES[ex_id]
                self._ex[ex_id] = ex
            if now - self._loaded_at.get(ex_id, 0) > MARKETS_TTL:
                try:
                    await asyncio.wait_for(ex.load_markets(reload=True), 40)
                    self._loaded_at[ex_id] = now
                except Exception as e:  # noqa: BLE001
                    log.warning("load_markets %s failed: %s", ex_id, e)
                    self._failed_at[ex_id] = now
                    return None
            return ex

    async def all(self) -> list[ccxt.Exchange]:
        res = await asyncio.gather(*(self.get(i) for i in self.ids))
        return [e for e in res if e is not None]

    async def close(self) -> None:
        for ex in self._ex.values():
            try:
                await ex.close()
            except Exception:  # noqa: BLE001
                pass


def find_market(ex: ccxt.Exchange, base: str, kind: str) -> tuple[dict, int] | None:
    """Find a spot or linear perp market for base. Returns (market, multiplier)."""
    candidates = [(base, 1)] + [(p + base, m) for p, m in MULTIPLIER_PREFIXES.items()]
    for b, mult in candidates:
        for quote in PERP_QUOTES:
            for m in ex.markets.values():
                if m.get("base") != b or m.get("quote") != quote or m.get("active") is False:
                    continue
                if kind == "spot" and m.get("spot"):
                    return m, mult
                if kind == "perp" and m.get("swap") and m.get("linear"):
                    return m, mult
    return None


async def _ticker(ex: ccxt.Exchange, base: str, kind: str) -> dict | None:
    found = find_market(ex, base, kind)
    if not found:
        return None
    m, mult = found
    t = await asyncio.wait_for(ex.fetch_ticker(m["symbol"]), 10)
    last = t.get("last") or t.get("close")
    if not last:
        return None
    return {
        "exchange": ex.id,
        "kind": kind,
        "symbol": m["symbol"],
        "price": last / mult,
        "price_as_quoted": last if mult != 1 else None,
        "per_tokens": mult if mult != 1 else None,
        "change_24h_pct": t.get("percentage"),
        "quote_volume_24h": t.get("quoteVolume"),
        "bid": (t.get("bid") or 0) / mult or None,
        "ask": (t.get("ask") or 0) / mult or None,
    }


async def market_overview(pool: ExchangePool, raw_symbol: str) -> dict:
    base = normalize_base(raw_symbol)
    exs = await pool.all()
    unavailable = [i for i in pool.ids if i not in {e.id for e in exs}]
    if not exs:
        return {"base": base, "error": "биржи сейчас недоступны", "unavailable_exchanges": unavailable}
    tasks = [_ticker(ex, base, k) for ex in exs for k in ("spot", "perp")]
    res = await asyncio.gather(*tasks, return_exceptions=True)
    rows = [r for r in res if isinstance(r, dict)]
    if not rows:
        return {
            "base": base,
            "error": f"{base} не найден на биржах: {[e.id for e in exs]}",
            "unavailable_exchanges": unavailable,
        }

    prices = [r["price"] for r in rows]
    med = median(prices)
    for r in rows:
        r["dev_from_median_pct"] = round((r["price"] - med) / med * 100, 3)
    outliers = [r for r in rows if abs(r["dev_from_median_pct"]) > 3]

    spot = {r["exchange"]: r for r in rows if r["kind"] == "spot"}
    perp = {r["exchange"]: r for r in rows if r["kind"] == "perp"}
    basis = {
        ex: round((perp[ex]["price"] - spot[ex]["price"]) / spot[ex]["price"] * 100, 3)
        for ex in perp
        if ex in spot
    }
    vol_spot = sum(r["quote_volume_24h"] or 0 for r in spot.values())
    vol_perp = sum(r["quote_volume_24h"] or 0 for r in perp.values())
    perp_prices = [r["price"] for r in perp.values()]
    spot_prices = [r["price"] for r in spot.values()]
    return {
        "base": base,
        "median_price": med,
        "perp_spread_across_exchanges_pct": round((max(perp_prices) - min(perp_prices)) / med * 100, 3)
        if len(perp_prices) > 1
        else None,
        "spot_spread_across_exchanges_pct": round((max(spot_prices) - min(spot_prices)) / med * 100, 3)
        if len(spot_prices) > 1
        else None,
        "basis_perp_vs_spot_pct_by_exchange": basis,
        "spot_volume_24h_usd_sum": round(vol_spot),
        "perp_volume_24h_usd_sum": round(vol_perp),
        "possible_different_token_or_thin_market": [
            f"{r['exchange']} {r['kind']} {r['symbol']}: {r['dev_from_median_pct']}% от медианы" for r in outliers
        ],
        "rows": rows,
        "exchanges_checked": [e.id for e in exs],
        "unavailable_exchanges": unavailable,
    }


def _book_stats(book: dict, mid: float, mult: int) -> dict:
    def side_depth(levels, lo, hi):
        return sum(p * a for p, a, *_ in levels if lo <= p <= hi)

    out: dict = {}
    for band in (1, 2, 5):
        lo, hi = mid * (1 - band / 100), mid * (1 + band / 100)
        b = side_depth(book["bids"], lo, mid)
        a = side_depth(book["asks"], mid, hi)
        out[f"within_{band}pct"] = {
            "bids_usd": round(b),
            "asks_usd": round(a),
            "bid_share_pct": round(b / (a + b) * 100, 1) if a + b else None,
        }
    lvls = [(p, p * a, "bid") for p, a, *_ in book["bids"] if p >= mid * 0.95] + [
        (p, p * a, "ask") for p, a, *_ in book["asks"] if p <= mid * 1.05
    ]
    if lvls:
        med = median(x[1] for x in lvls)
        walls = sorted((x for x in lvls if x[1] >= 3 * med), key=lambda x: -x[1])[:6]
        out["walls"] = [
            {
                "side": s,
                "price": p / mult,
                "usd": round(v),
                "distance_pct": round((p - mid) / mid * 100, 2),
            }
            for p, v, s in sorted(walls, key=lambda x: x[0])
        ]
    return out


async def orderbook(pool: ExchangePool, raw_symbol: str, market: str = "perp", exchange: str | None = None) -> dict:
    base = normalize_base(raw_symbol)
    order = [exchange] if exchange else ["binance", "bybit", "okx", "bitget", "gate", "mexc"]
    for ex_id in order:
        ex = await pool.get(ex_id)
        if not ex:
            continue
        found = find_market(ex, base, market)
        if not found:
            continue
        m, mult = found
        try:
            try:
                book = await asyncio.wait_for(ex.fetch_order_book(m["symbol"], 100), 10)
            except Exception:  # noqa: BLE001 — some venues reject limit=100
                book = await asyncio.wait_for(ex.fetch_order_book(m["symbol"]), 10)
        except Exception as e:  # noqa: BLE001
            if exchange:
                return {"error": f"{ex_id}: {e}"}
            continue
        if not book["bids"] or not book["asks"]:
            continue
        bb, ba = book["bids"][0][0], book["asks"][0][0]
        mid = (bb + ba) / 2
        return {
            "exchange": ex_id,
            "market": market,
            "symbol": m["symbol"],
            "mid": mid / mult,
            "spread_bps": round((ba - bb) / mid * 10_000, 2),
            "per_tokens": mult if mult != 1 else None,
            "depth_levels_fetched": {"bids": len(book["bids"]), "asks": len(book["asks"])},
            **_book_stats(book, mid, mult),
            "caveat": "стенки могут быть снятыми/спуфинговыми — смотри в динамике",
        }
    return {"error": f"стакан {base} ({market}) не найден на {order}"}


async def ohlcv(pool: ExchangePool, raw_symbol: str, timeframe: str, limit: int) -> tuple[list[dict], dict]:
    """Candles from the first exchange that has the market (perp first, then spot)."""
    base = normalize_base(raw_symbol)
    for kind in ("perp", "spot"):
        for ex_id in ["binance", "bybit", "okx", "bitget", "gate", "mexc", *pool.ids]:
            ex = await pool.get(ex_id)
            if not ex or not ex.has.get("fetchOHLCV"):
                continue
            found = find_market(ex, base, kind)
            if not found:
                continue
            m, mult = found
            try:
                raw = await asyncio.wait_for(ex.fetch_ohlcv(m["symbol"], timeframe, limit=limit), 12)
            except Exception:  # noqa: BLE001
                continue
            if not raw:
                continue
            candles = [
                {"t": r[0], "o": r[1] / mult, "h": r[2] / mult, "l": r[3] / mult, "c": r[4] / mult, "v": r[5] * mult}
                for r in raw
            ]
            return candles, {"exchange": ex_id, "market": kind, "symbol": m["symbol"]}
    return [], {"error": f"свечи {base} не найдены"}
