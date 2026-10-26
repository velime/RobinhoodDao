"""Binance Alpha: tokens Binance lists on-chain before (or instead of) spot and futures.

Public endpoints of binance.com (no key), as documented in Binance's Alpha market-data API:
    token list   GET /bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list
                 data: [{"symbol", "name", "alphaId": "ALPHA_123", "chainId", "chainName", "contractAddress",
                         "price", "percentChange24h", "volume24h", "liquidity", "marketCap", "fdv", "holders",
                         "listingTime", "listingCex", ...}]
    klines       GET /bapi/defi/v1/public/alpha-trade/klines?symbol=ALPHA_123USDT&interval=1h&limit=200
                 data: [[open_ms, o, h, l, c, volume, close_ms, quote_volume, trades, taker_buy_base, ...], ...]
    24h ticker   GET /bapi/defi/v1/public/alpha-trade/ticker?symbol=ALPHA_123USDT
                 data: {"lastPrice", "priceChangePercent", "highPrice", "lowPrice", "volume", "quoteVolume", "count"}

Alpha has no public order book: trades are routed to on-chain pools, so the depth of an Alpha token is the
liquidity of its DEX pools (DexScreener by contract).
"""

from __future__ import annotations

import asyncio
import time

from .. import http
from ..symbols import normalize_base
from .transfers import chain_id

BASE = "https://www.binance.com/bapi/defi/v1/public"
TOKEN_LIST = f"{BASE}/wallet-direct/buw/wallet/cex/alpha/all/token/list"
KLINES = f"{BASE}/alpha-trade/klines"
TICKER = f"{BASE}/alpha-trade/ticker"
LIST_TTL = 600
# Alpha's chainId → canonical chain used by the rest of Bottom (DexScreener mapping lives in transfers)
CHAINS = {"1": "ETH", "56": "BSC", "8453": "BASE", "42161": "ARB", "137": "POLYGON", "10": "OP", "43114": "AVAXC",
          "CT_501": "SOL", "CT_195": "TRX", "CT_784": "SUI", "784": "SUI"}
_list_cache: tuple[float, list[dict]] = (0.0, [])


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _data(payload):
    """Binance bapi wraps everything: {"code": "000000", "data": ..., "success": true}."""
    if isinstance(payload, dict) and "data" in payload:
        if payload.get("success") is False or payload.get("code") not in (None, "000000", 0, "0"):
            raise RuntimeError(f"Binance Alpha: {payload.get('message') or payload.get('code')}")
        return payload["data"]
    return payload


async def token_list(force: bool = False) -> list[dict]:
    global _list_cache
    if not force and _list_cache[1] and time.time() - _list_cache[0] < LIST_TTL:
        return _list_cache[1]
    data = _data(await http.get_json(TOKEN_LIST)) or []
    _list_cache = (time.time(), [t for t in data if isinstance(t, dict) and t.get("symbol")])
    return _list_cache[1]


def chain_of(t: dict) -> str:
    return CHAINS.get(str(t.get("chainId")), chain_id(t.get("chainName")))


def card(t: dict) -> dict:
    """One Alpha token in Bottom's terms."""
    listed = _f(t.get("listingTime"))
    return {
        "symbol": t.get("symbol"), "name": t.get("name"), "alpha_id": t.get("alphaId"),
        "chain": chain_of(t), "contract": t.get("contractAddress"),
        "price": _f(t.get("price")), "change_24h_pct": _f(t.get("percentChange24h")),
        "volume_24h_usd": _f(t.get("volume24h")), "liquidity_usd": _f(t.get("liquidity")),
        "market_cap": _f(t.get("marketCap")), "fdv": _f(t.get("fdv")), "holders": _f(t.get("holders")),
        "listed": time.strftime("%Y-%m-%d", time.gmtime(listed / 1000)) if listed else None,
        "also_on_binance_spot_or_futures": bool(t.get("listingCex")),
    }


def find(tokens: list[dict], query: str) -> list[dict]:
    """By contract address (exact) or by ticker; several tokens can share a ticker — biggest volume first."""
    q = query.strip()
    if len(q) > 25:  # a contract address
        return [t for t in tokens if (t.get("contractAddress") or "").lower() == q.lower()]
    base = normalize_base(q)
    hits = [t for t in tokens if (t.get("symbol") or "").upper() == base]
    return sorted(hits, key=lambda t: -(_f(t.get("volume24h")) or 0))


async def lookup(query: str) -> dict | None:
    """The Alpha token behind a ticker or contract (biggest volume), or None. Never raises."""
    try:
        hits = find(await token_list(), query)
    except Exception:  # noqa: BLE001
        return None
    return card(hits[0]) if hits else None


async def ticker(alpha_id: str) -> dict:
    d = _data(await http.get_json(TICKER, {"symbol": f"{alpha_id}USDT"})) or {}
    return {"last": _f(d.get("lastPrice")), "change_24h_pct": _f(d.get("priceChangePercent")),
            "high_24h": _f(d.get("highPrice")), "low_24h": _f(d.get("lowPrice")),
            "quote_volume_24h": _f(d.get("quoteVolume")), "trades_24h": d.get("count")}


async def klines(alpha_id: str, interval: str, limit: int) -> list[dict]:
    """Candles in Bottom's format; taker_buy (base volume) is there for delta/CVD."""
    raw = _data(await http.get_json(KLINES, {"symbol": f"{alpha_id}USDT", "interval": interval, "limit": limit})) or []
    out = []
    for r in raw:
        if len(r) < 6:
            continue
        c = {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
        if len(r) > 9:
            c["trades"], c["taker_buy"] = int(float(r[8])), float(r[9])
        out.append(c)
    return out


async def binance_alpha(query: str = "", top: int = 15) -> dict:
    """Tool: one Alpha token (ticker or contract) with its 24h ticker, or the market: top by volume and movers."""
    try:
        tokens = await token_list()
    except Exception as e:  # noqa: BLE001
        return {"error": f"Binance Alpha недоступен: {type(e).__name__}: {str(e)[:120]}"}
    if query:
        hits = find(tokens, query)
        if not hits:
            return {"query": query, "found": False,
                    "note": "на Binance Alpha такого токена нет — смотри market_overview (спот/фьючерсы) и dex_pairs"}
        main = card(hits[0])
        try:
            main["ticker_24h"] = await ticker(hits[0]["alphaId"])
        except Exception as e:  # noqa: BLE001
            main["ticker_24h"] = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
        out = {"query": query, "found": True, "token": main, "source": "binance.com Alpha"}
        if len(hits) > 1:
            out["same_ticker_other_tokens"] = [card(t) for t in hits[1:5]]
            out["note"] = "несколько токенов с этим тикером — сверь контракт (resolve_token, dex_pairs)"
        return out
    rows = [card(t) for t in tokens]
    n = max(1, min(int(top), 40))
    by_vol = sorted(rows, key=lambda r: -(r["volume_24h_usd"] or 0))[:n]
    movers = sorted((r for r in rows if (r["volume_24h_usd"] or 0) >= 100_000),
                    key=lambda r: -abs(r["change_24h_pct"] or 0))[:n]
    fresh = sorted((r for r in rows if r["listed"]), key=lambda r: r["listed"], reverse=True)[:min(n, 10)]
    return {"tokens_on_alpha": len(rows), "top_volume_24h": by_vol, "biggest_moves_24h": movers,
            "newest_listings": fresh, "source": "binance.com Alpha"}


async def alpha_book(query: str) -> dict:
    """Depth of an Alpha token: there is no Alpha order book, so the liquidity of its DEX pools stands in."""
    from ..onchain.dex import dex_prices

    tok = await lookup(query)
    if tok is None:
        return {"error": f"{query} нет на Binance Alpha"}
    pools = {}
    if tok["contract"]:
        try:
            pools = await asyncio.wait_for(dex_prices(tok["chain"], [tok["contract"]]), 15)
        except Exception as e:  # noqa: BLE001
            pools = {"error": f"{type(e).__name__}: {str(e)[:80]}"}
    return {"market": "alpha", "token": tok, "dex_pools": pools,
            "note": "у Binance Alpha нет публичного стакана: сделки идут через DEX-пулы, глубина = ликвидность пула; "
                    "при ликвидности до ~$500K сделка в $5K уже двигает цену"}
