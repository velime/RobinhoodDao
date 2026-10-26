"""Own market feeds: every pair of an exchange in ONE public request.

Each fetcher returns rows:
    {"ex": "bybit", "kind": "perp"|"spot", "base": "PEPE", "symbol": "1000PEPEUSDT",
     "price": <per 1 token>, "oi_usd": float|None, "funding_pct": float|None,
     "interval_h": float|None, "next_funding_ms": int|None, "vol24_usd": float|None}

These feed the market watcher (OI movers, funding, cross-venue spreads) and spread checks.
A fetcher that fails just yields no rows — one dead exchange never breaks the scan.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import http
from ..symbols import strip_multiplier

log = logging.getLogger(__name__)

BINANCE_F = "https://fapi.binance.com"
BINANCE_S = "https://api.binance.com"
BYBIT = "https://api.bybit.com"
OKX = "https://www.okx.com"
BITGET = "https://api.bitget.com"
GATE = "https://api.gateio.ws/api/v4"
MEXC_F = "https://contract.mexc.com"
MEXC_S = "https://api.mexc.com"
KUCOIN_F = "https://api-futures.kucoin.com"
KUCOIN_S = "https://api.kucoin.com"
BINGX = "https://open-api.bingx.com"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"

ALIASES = {"XBT": "BTC"}


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def base_of(symbol: str, quote: str = "USDT") -> tuple[str, int] | None:
    """'1000PEPEUSDT' / 'PEPE-USDT-SWAP' / 'PEPE_USDT' / 'XBTUSDTM' → ('PEPE', 1000). None if not USDT-quoted."""
    s = symbol.upper().replace("-SWAP", "").replace("_UMCBL", "")
    for sep in ("-", "_", "/"):
        if sep in s:
            b, q = s.split(sep, 1)
            if q.rstrip("M") != quote:
                return None
            s = b
            break
    else:
        if s.endswith(quote + "M"):  # KuCoin futures
            s = s[: -len(quote) - 1]
        elif s.endswith(quote):
            s = s[: -len(quote)]
        else:
            return None
    if not s:
        return None
    b, mult = strip_multiplier(s)
    return ALIASES.get(b, b), mult


def _row(ex, kind, symbol, price, *, oi_usd=None, funding=None, interval_h=None, next_ms=None, vol=None,
         mark=None, index=None, quote="USDT"):
    bm = base_of(symbol, quote)
    p = _f(price)
    if not bm or not p:
        return None
    base, mult = bm
    fr = _f(funding)
    return {
        "ex": ex, "kind": kind, "base": base, "symbol": symbol, "price": p / mult,
        "oi_usd": _f(oi_usd), "funding_pct": round(fr * 100, 5) if fr is not None else None,
        "interval_h": interval_h, "next_funding_ms": int(next_ms) if next_ms else None, "vol24_usd": _f(vol),
        "mark": _f(mark) / mult if _f(mark) else None, "index": _f(index) / mult if _f(index) else None,
    }


async def binance_perp() -> list[dict]:
    prem, tick, info = await asyncio.gather(
        http.get_json(f"{BINANCE_F}/fapi/v1/premiumIndex"),
        http.get_json(f"{BINANCE_F}/fapi/v1/ticker/24hr"),
        http.get_json(f"{BINANCE_F}/fapi/v1/fundingInfo"),
        return_exceptions=True,
    )
    if isinstance(prem, Exception):
        raise prem
    vol = {t["symbol"]: t.get("quoteVolume") for t in tick} if isinstance(tick, list) else {}
    last = {t["symbol"]: t.get("lastPrice") for t in tick} if isinstance(tick, list) else {}
    ivl = {i["symbol"]: i.get("fundingIntervalHours") for i in info} if isinstance(info, list) else {}
    out = []
    for p in prem:
        s = p["symbol"]
        r = _row("binance", "perp", s, last.get(s) or p.get("markPrice"), funding=p.get("lastFundingRate"),
                 interval_h=ivl.get(s, 8), next_ms=p.get("nextFundingTime"), vol=vol.get(s),
                 mark=p.get("markPrice"), index=p.get("indexPrice"))
        if r:
            out.append(r)
    return out  # Binance has no bulk OI; per-coin OI comes from oi_card


async def binance_spot() -> list[dict]:
    data = await http.get_json(f"{BINANCE_S}/api/v3/ticker/24hr", {"type": "MINI"})
    return [r for t in data if (r := _row("binance", "spot", t["symbol"], t.get("lastPrice"), vol=t.get("quoteVolume")))]


async def bybit_perp() -> list[dict]:
    data = await http.get_json(f"{BYBIT}/v5/market/tickers", {"category": "linear"})
    out = []
    for t in (data.get("result") or {}).get("list") or []:
        r = _row("bybit", "perp", t["symbol"], t.get("lastPrice"), oi_usd=t.get("openInterestValue"),
                 funding=t.get("fundingRate") or None, interval_h=_f(t.get("fundingIntervalHour")) or 8,
                 next_ms=t.get("nextFundingTime") or None, vol=t.get("turnover24h"),
                 mark=t.get("markPrice"), index=t.get("indexPrice"))
        if r:
            out.append(r)
    return out


async def bybit_spot() -> list[dict]:
    data = await http.get_json(f"{BYBIT}/v5/market/tickers", {"category": "spot"})
    return [r for t in (data.get("result") or {}).get("list") or []
            if (r := _row("bybit", "spot", t["symbol"], t.get("lastPrice"), vol=t.get("turnover24h")))]


async def okx_perp() -> list[dict]:
    tick, oi, fr, mk, ix = await asyncio.gather(
        http.get_json(f"{OKX}/api/v5/market/tickers", {"instType": "SWAP"}),
        http.get_json(f"{OKX}/api/v5/public/open-interest", {"instType": "SWAP"}),
        http.get_json(f"{OKX}/api/v5/public/funding-rate", {"instId": "ANY"}),
        http.get_json(f"{OKX}/api/v5/public/mark-price", {"instType": "SWAP"}),
        http.get_json(f"{OKX}/api/v5/market/index-tickers", {"quoteCcy": "USDT"}),
        return_exceptions=True,
    )
    marks = {m["instId"]: m.get("markPx") for m in mk.get("data", [])} if isinstance(mk, dict) else {}
    idx = {i["instId"]: i.get("idxPx") for i in ix.get("data", [])} if isinstance(ix, dict) else {}
    if isinstance(tick, Exception):
        raise tick
    oi_usd = {o["instId"]: o.get("oiUsd") for o in oi.get("data", [])} if isinstance(oi, dict) else {}
    fund = {f["instId"]: f for f in fr.get("data", [])} if isinstance(fr, dict) else {}
    out = []
    for t in tick.get("data", []):
        s = t["instId"]
        f = fund.get(s) or {}
        nxt, cur = _f(f.get("nextFundingTime")), _f(f.get("fundingTime"))
        last = _f(t.get("last"))
        vol = (_f(t.get("volCcy24h")) or 0) * (last or 0) or None
        r = _row("okx", "perp", s, t.get("last"), oi_usd=oi_usd.get(s), funding=f.get("fundingRate"),
                 interval_h=round((nxt - cur) / 3.6e6, 2) if nxt and cur else None, next_ms=f.get("fundingTime"),
                 vol=vol, mark=marks.get(s), index=idx.get(s.replace("-SWAP", "")))
        if r:
            out.append(r)
    return out


async def okx_spot() -> list[dict]:
    data = await http.get_json(f"{OKX}/api/v5/market/tickers", {"instType": "SPOT"})
    return [r for t in data.get("data", []) if (r := _row("okx", "spot", t["instId"], t.get("last"), vol=t.get("volCcyQuote") or t.get("volCcy24h")))]


async def bitget_perp() -> list[dict]:
    data = await http.get_json(f"{BITGET}/api/v2/mix/market/tickers", {"productType": "USDT-FUTURES"})
    out = []
    for t in data.get("data") or []:
        price = _f(t.get("lastPr"))
        hold = _f(t.get("holdingAmount"))
        r = _row("bitget", "perp", t["symbol"], price, oi_usd=hold * price if hold and price else None,
                 funding=t.get("fundingRate"), interval_h=8, vol=t.get("usdtVolume") or t.get("quoteVolume"),
                 mark=t.get("markPrice"), index=t.get("indexPrice"))
        if r:
            out.append(r)
    return out


async def bitget_spot() -> list[dict]:
    data = await http.get_json(f"{BITGET}/api/v2/spot/market/tickers")
    return [r for t in data.get("data") or [] if (r := _row("bitget", "spot", t["symbol"], t.get("lastPr"), vol=t.get("usdtVolume") or t.get("quoteVolume")))]


async def gate_perp() -> list[dict]:
    contracts, tick = await asyncio.gather(
        http.get_json(f"{GATE}/futures/usdt/contracts"),
        http.get_json(f"{GATE}/futures/usdt/tickers"),
        return_exceptions=True,
    )
    if isinstance(contracts, Exception):
        raise contracts
    vol = {t["contract"]: t.get("volume_24h_quote") for t in tick} if isinstance(tick, list) else {}
    out = []
    for c in contracts:
        if c.get("in_delisting"):
            continue
        price, size, mult = _f(c.get("last_price")), _f(c.get("position_size")), _f(c.get("quanto_multiplier"))
        nxt = _f(c.get("funding_next_apply"))
        r = _row("gate", "perp", c["name"], price, oi_usd=size * mult * price if size and mult and price else None,
                 funding=c.get("funding_rate"), interval_h=(_f(c.get("funding_interval")) or 28800) / 3600,
                 next_ms=nxt * 1000 if nxt else None, vol=vol.get(c["name"]),
                 mark=c.get("mark_price"), index=c.get("index_price"))
        if r:
            out.append(r)
    return out


async def gate_spot() -> list[dict]:
    data = await http.get_json(f"{GATE}/spot/tickers")
    return [r for t in data if (r := _row("gate", "spot", t["currency_pair"], t.get("last"), vol=t.get("quote_volume")))]


async def mexc_perp() -> list[dict]:
    tick, detail = await asyncio.gather(
        http.get_json(f"{MEXC_F}/api/v1/contract/ticker"),
        http.get_json(f"{MEXC_F}/api/v1/contract/detail"),
        return_exceptions=True,
    )
    if isinstance(tick, Exception):
        raise tick
    size = {d["symbol"]: _f(d.get("contractSize")) for d in detail.get("data", [])} if isinstance(detail, dict) else {}
    out = []
    for t in tick.get("data") or []:
        price, hold, cs = _f(t.get("lastPrice")), _f(t.get("holdVol")), size.get(t["symbol"])
        r = _row("mexc", "perp", t["symbol"], price, oi_usd=hold * cs * price if hold and cs and price else None,
                 funding=t.get("fundingRate"), vol=t.get("amount24"), mark=t.get("fairPrice"), index=t.get("indexPrice"))
        if r:
            out.append(r)
    return out


async def mexc_spot() -> list[dict]:
    data = await http.get_json(f"{MEXC_S}/api/v3/ticker/24hr")
    return [r for t in data if (r := _row("mexc", "spot", t["symbol"], t.get("lastPrice"), vol=t.get("quoteVolume")))]


async def kucoin_perp() -> list[dict]:
    data = await http.get_json(f"{KUCOIN_F}/api/v1/contracts/active")
    out = []
    now = time.time() * 1000
    for c in data.get("data") or []:
        price, oi, mult = _f(c.get("lastTradePrice")), _f(c.get("openInterest")), _f(c.get("multiplier"))
        gran, till = _f(c.get("fundingRateGranularity")), _f(c.get("nextFundingRateTime"))
        r = _row("kucoin", "perp", c["symbol"], price, oi_usd=oi * mult * price if oi and mult and price else None,
                 funding=c.get("fundingFeeRate"), interval_h=gran / 3.6e6 if gran else None,
                 next_ms=now + till if till else None, vol=c.get("turnoverOf24h"),
                 mark=c.get("markPrice"), index=c.get("indexPrice"))
        if r:
            out.append(r)
    return out


async def kucoin_spot() -> list[dict]:
    data = await http.get_json(f"{KUCOIN_S}/api/v1/market/allTickers")
    return [r for t in ((data.get("data") or {}).get("ticker") or [])
            if (r := _row("kucoin", "spot", t["symbol"], t.get("last"), vol=t.get("volValue")))]


async def bingx_perp() -> list[dict]:
    prem, tick = await asyncio.gather(
        http.get_json(f"{BINGX}/openApi/swap/v2/quote/premiumIndex"),
        http.get_json(f"{BINGX}/openApi/swap/v2/quote/ticker"),
        return_exceptions=True,
    )
    if isinstance(prem, Exception):
        raise prem
    tk = {t["symbol"]: t for t in tick.get("data", [])} if isinstance(tick, dict) else {}
    out = []
    for p in prem.get("data") or []:
        t = tk.get(p["symbol"], {})
        r = _row("bingx", "perp", p["symbol"], t.get("lastPrice") or p.get("markPrice"),
                 funding=p.get("lastFundingRate"), next_ms=p.get("nextFundingTime"), vol=t.get("quoteVolume"),
                 mark=p.get("markPrice"), index=p.get("indexPrice"))
        if r:
            out.append(r)
    return out


async def hyperliquid_perp() -> list[dict]:
    meta, ctxs = await http.post_json(HYPERLIQUID, {"type": "metaAndAssetCtxs"})
    out = []
    for u, c in zip(meta.get("universe", []), ctxs):
        name = u["name"]
        mult = 1000 if name.startswith("k") and name[1:].isupper() else 1
        base = name[1:] if mult == 1000 else name
        price, oi = _f(c.get("markPx")), _f(c.get("openInterest"))
        if not price or u.get("isDelisted"):
            continue
        out.append({
            "ex": "hyperliquid", "kind": "perp", "base": base, "symbol": name, "price": price / mult,
            "oi_usd": oi * price if oi else None,
            "funding_pct": round(_f(c.get("funding")) * 100, 5) if c.get("funding") is not None else None,
            "interval_h": 1, "next_funding_ms": None, "vol24_usd": _f(c.get("dayNtlVlm")),
            "mark": price / mult, "index": (_f(c.get("oraclePx")) or 0) / mult or None,
        })
    return out


ASTER_F = "https://fapi.asterdex.com"
ASTER_S = "https://sapi.asterdex.com"


async def aster_perp() -> list[dict]:
    """Aster futures (Binance-compatible API): mark/index/funding bulk + 24h volume + funding intervals."""
    prem, tick, info = await asyncio.gather(
        http.get_json(f"{ASTER_F}/fapi/v1/premiumIndex"),
        http.get_json(f"{ASTER_F}/fapi/v1/ticker/24hr"),
        http.get_json(f"{ASTER_F}/fapi/v1/fundingInfo"),
        return_exceptions=True,
    )
    if isinstance(prem, Exception):
        raise prem
    last = {t["symbol"]: t for t in tick} if isinstance(tick, list) else {}
    ivl = {i["symbol"]: i.get("fundingIntervalHours") for i in info} if isinstance(info, list) else {}
    out = []
    for p in prem:
        s = p["symbol"]
        t = last.get(s) or {}
        r = _row("aster", "perp", s, t.get("lastPrice") or p.get("markPrice"), funding=p.get("lastFundingRate"),
                 interval_h=ivl.get(s, 8), next_ms=p.get("nextFundingTime"), vol=t.get("quoteVolume"),
                 mark=p.get("markPrice"), index=p.get("indexPrice"))
        if r:
            out.append(r)
    return out  # Aster has no bulk OI; per-coin OI comes from derivatives


async def aster_spot() -> list[dict]:
    data = await http.get_json(f"{ASTER_S}/api/v1/ticker/24hr")
    return [r for t in data if t.get("quoteAsset") == "USDT"
            and (r := _row("aster", "spot", t["symbol"], t.get("lastPrice"), vol=t.get("quoteVolume")))]


FETCHERS = {
    ("binance", "perp"): binance_perp, ("binance", "spot"): binance_spot,
    ("bybit", "perp"): bybit_perp, ("bybit", "spot"): bybit_spot,
    ("okx", "perp"): okx_perp, ("okx", "spot"): okx_spot,
    ("bitget", "perp"): bitget_perp, ("bitget", "spot"): bitget_spot,
    ("gate", "perp"): gate_perp, ("gate", "spot"): gate_spot,
    ("mexc", "perp"): mexc_perp, ("mexc", "spot"): mexc_spot,
    ("kucoin", "perp"): kucoin_perp, ("kucoin", "spot"): kucoin_spot,
    ("bingx", "perp"): bingx_perp,
    ("hyperliquid", "perp"): hyperliquid_perp,
    ("aster", "perp"): aster_perp, ("aster", "spot"): aster_spot,
}


async def snapshot(exchanges: list[str] | None = None, kinds: tuple[str, ...] = ("perp", "spot")) -> tuple[list[dict], dict]:
    """All rows from all feeds + {feed: error} for the ones that failed."""
    jobs = {k: f for k, f in FETCHERS.items() if k[1] in kinds and (not exchanges or k[0] in exchanges)}
    res = await asyncio.gather(*(asyncio.wait_for(f(), 25) for f in jobs.values()), return_exceptions=True)
    rows, errors = [], {}
    for (ex, kind), r in zip(jobs, res):
        if isinstance(r, Exception):
            errors[f"{ex} {kind}"] = f"{type(r).__name__}: {str(r)[:100]}"
            log.info("feed %s %s failed: %s", ex, kind, r)
        else:
            rows.extend(r)
    return rows, errors


_snap_cache: dict = {}


async def cached_snapshot(kinds: tuple[str, ...] = ("perp", "spot"), ttl: float = 60) -> tuple[list[dict], dict]:
    """Snapshot shared by on-demand tools (one bulk pull per minute, not per question)."""
    hit = _snap_cache.get(kinds)
    if hit and time.time() - hit[0] < ttl:
        return hit[1], hit[2]
    rows, errors = await snapshot(None, kinds)
    if rows:
        _snap_cache[kinds] = (time.time(), rows, errors)
    return rows, errors
