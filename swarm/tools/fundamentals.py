"""Token fundamentals, DEX data, contract security, DeFi TVL, market sentiment."""

from __future__ import annotations

import asyncio
import re
import time

from . import http
from .symbols import normalize_base

CG = "https://api.coingecko.com/api/v3"
DEXS = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1"
LLAMA = "https://api.llama.fi"

# GoPlus chain ids
GOPLUS_CHAINS = {
    "ethereum": "1", "eth": "1", "bsc": "56", "bnb": "56", "base": "8453", "arbitrum": "42161",
    "arb": "42161", "polygon": "137", "optimism": "10", "avalanche": "43114", "avax": "43114",
    "linea": "59144", "blast": "81457", "sonic": "146", "tron": "tron",
}

_ADDR_EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
_ADDR_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _cg_headers(api_key: str) -> dict:
    return {"x-cg-demo-api-key": api_key} if api_key else {}


def _short(text: str | None, n: int = 400) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


async def resolve_token(query: str, cg_key: str = "") -> dict:
    """Ticker/name/contract → candidate tokens. Guards against namesakes."""
    q = query.strip()
    if _ADDR_EVM.match(q) or _ADDR_SOL.match(q):
        pairs = await dex_pairs(q)
        return {"query": q, "type": "contract", "dex": pairs}
    base = normalize_base(q)
    data = await http.get_json(f"{CG}/search", {"query": base}, _cg_headers(cg_key))
    coins = data.get("coins", [])[:8]
    exact = [c for c in coins if c.get("symbol", "").upper() == base]
    return {
        "query": q,
        "base": base,
        "candidates": [
            {
                "coingecko_id": c["id"],
                "name": c.get("name"),
                "symbol": c.get("symbol"),
                "market_cap_rank": c.get("market_cap_rank"),
            }
            for c in (exact or coins)
        ],
        "hint": "если кандидатов несколько с одним тикером — сверяй цену с биржами и капитализацию, "
        "уточни у пользователя при сомнении",
    }


async def token_fundamentals(coingecko_id: str, cg_key: str = "") -> dict:
    h = _cg_headers(cg_key)
    c = await http.get_json(
        f"{CG}/coins/{coingecko_id}",
        {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false"},
        h,
    )
    md = c.get("market_data", {}) or {}
    usd = lambda k: (md.get(k) or {}).get("usd")  # noqa: E731
    mcap, fdv, vol = usd("market_cap"), usd("fully_diluted_valuation"), usd("total_volume")
    return {
        "id": c.get("id"),
        "name": c.get("name"),
        "symbol": (c.get("symbol") or "").upper(),
        "categories": c.get("categories"),
        "description": _short((c.get("description") or {}).get("en")),
        "homepage": ((c.get("links") or {}).get("homepage") or [None])[0],
        "twitter": (c.get("links") or {}).get("twitter_screen_name"),
        "contracts": c.get("platforms"),
        "genesis_date": c.get("genesis_date"),
        "price_usd": usd("current_price"),
        "market_cap_usd": mcap,
        "market_cap_rank": c.get("market_cap_rank"),
        "fdv_usd": fdv,
        "fdv_to_mcap": round(fdv / mcap, 2) if fdv and mcap else None,
        "volume_24h_usd": vol,
        "volume_to_mcap": round(vol / mcap, 2) if vol and mcap else None,
        "circulating_supply": md.get("circulating_supply"),
        "total_supply": md.get("total_supply"),
        "max_supply": md.get("max_supply"),
        "ath_usd": usd("ath"),
        "ath_change_pct": usd("ath_change_percentage"),
        "ath_date": (md.get("ath_date") or {}).get("usd"),
        "change_1h_pct": (md.get("price_change_percentage_1h_in_currency") or {}).get("usd"),
        "change_24h_pct": md.get("price_change_percentage_24h"),
        "change_7d_pct": md.get("price_change_percentage_7d"),
        "change_30d_pct": md.get("price_change_percentage_30d"),
        "source": "CoinGecko",
    }


async def dex_pairs(query: str, limit: int = 6) -> dict:
    q = query.strip()
    if _ADDR_EVM.match(q) or _ADDR_SOL.match(q):
        data = await http.get_json(f"{DEXS}/latest/dex/tokens/{q}")
    else:
        data = await http.get_json(f"{DEXS}/latest/dex/search", {"q": normalize_base(q)})
    pairs = data.get("pairs") or []
    pairs.sort(key=lambda p: -((p.get("liquidity") or {}).get("usd") or 0))
    now_ms = time.time() * 1000
    out = []
    for p in pairs[:limit]:
        tx = (p.get("txns") or {}).get("h24") or {}
        tx1 = (p.get("txns") or {}).get("h1") or {}
        created = p.get("pairCreatedAt")
        out.append(
            {
                "chain": p.get("chainId"),
                "dex": p.get("dexId"),
                "pair": f"{(p.get('baseToken') or {}).get('symbol')}/{(p.get('quoteToken') or {}).get('symbol')}",
                "token_address": (p.get("baseToken") or {}).get("address"),
                "pair_address": p.get("pairAddress"),
                "price_usd": float(p["priceUsd"]) if p.get("priceUsd") else None,
                "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                "volume_24h_usd": (p.get("volume") or {}).get("h24"),
                "volume_1h_usd": (p.get("volume") or {}).get("h1"),
                "change_1h_pct": (p.get("priceChange") or {}).get("h1"),
                "change_24h_pct": (p.get("priceChange") or {}).get("h24"),
                "txns_24h_buys_sells": [tx.get("buys"), tx.get("sells")],
                "txns_1h_buys_sells": [tx1.get("buys"), tx1.get("sells")],
                "fdv_usd": p.get("fdv"),
                "market_cap_usd": p.get("marketCap"),
                "pair_age_days": round((now_ms - created) / 8.64e7, 1) if created else None,
                "url": p.get("url"),
            }
        )
    return {"query": q, "pairs": out, "source": "DexScreener"}


async def token_security(chain: str, address: str) -> dict:
    c = chain.lower().strip()
    if c in ("solana", "sol"):
        data = await http.get_json(f"{GOPLUS}/solana/token_security", {"contract_addresses": address})
    else:
        cid = GOPLUS_CHAINS.get(c, c)
        data = await http.get_json(f"{GOPLUS}/token_security/{cid}", {"contract_addresses": address})
    res = data.get("result") or {}
    info = res.get(address.lower()) or res.get(address) or (next(iter(res.values())) if res else None)
    if not info:
        return {"error": f"GoPlus не вернул данных для {address} на {chain}", "raw_code": data.get("code")}
    keys = [
        "token_name", "token_symbol", "is_honeypot", "buy_tax", "sell_tax", "cannot_sell_all",
        "is_mintable", "owner_address", "is_open_source", "is_proxy", "hidden_owner",
        "can_take_back_ownership", "transfer_pausable", "is_blacklisted", "holder_count",
        "lp_holder_count", "is_in_dex", "trust_list",
    ]
    out = {k: info.get(k) for k in keys if k in info}
    holders = info.get("holders") or []
    out["top10_holders_pct"] = round(sum(float(h.get("percent") or 0) for h in holders[:10]) * 100, 2) if holders else None
    out["top_holders"] = [
        {
            "address": h.get("address"),
            "pct": round(float(h.get("percent") or 0) * 100, 2),
            "tag": h.get("tag"),
            "is_contract": h.get("is_contract"),
            "is_locked": h.get("is_locked"),
        }
        for h in holders[:10]
    ]
    lp = info.get("lp_holders") or []
    out["lp_locked_pct"] = round(sum(float(h.get("percent") or 0) for h in lp if h.get("is_locked")) * 100, 2) if lp else None
    out["source"] = "GoPlus Security"
    out["note"] = "в топ-холдерах бывают биржи, бридж- и burn-адреса — смотри tag"
    return out


_llama_cache: tuple[float, list] | None = None


async def defi_protocol(query: str) -> dict:
    global _llama_cache
    if _llama_cache is None or time.time() - _llama_cache[0] > 1800:
        _llama_cache = (time.time(), await http.get_json(f"{LLAMA}/protocols"))
    q = query.lower().strip().lstrip("$")
    hits = [
        p for p in _llama_cache[1]
        if q in (p.get("name") or "").lower() or q == (p.get("symbol") or "").lower() or q == (p.get("slug") or "")
    ]
    hits.sort(key=lambda p: -(p.get("tvl") or 0))
    return {
        "query": query,
        "protocols": [
            {
                "name": p.get("name"),
                "slug": p.get("slug"),
                "symbol": p.get("symbol"),
                "category": p.get("category"),
                "chains": (p.get("chains") or [])[:8],
                "tvl_usd": round(p.get("tvl") or 0),
                "change_1d_pct": p.get("change_1d"),
                "change_7d_pct": p.get("change_7d"),
                "mcap_usd": p.get("mcap"),
                "mcap_to_tvl": round(p["mcap"] / p["tvl"], 2) if p.get("mcap") and p.get("tvl") else None,
            }
            for p in hits[:5]
        ],
        "source": "DefiLlama",
    }


async def market_sentiment(cg_key: str = "") -> dict:
    fng, glob, trend = await asyncio.gather(
        http.get_json("https://api.alternative.me/fng/", {"limit": 2}),
        http.get_json(f"{CG}/global", headers=_cg_headers(cg_key)),
        http.get_json(f"{CG}/search/trending", headers=_cg_headers(cg_key)),
        return_exceptions=True,
    )
    out: dict = {}
    if isinstance(fng, dict) and fng.get("data"):
        d = fng["data"]
        out["fear_greed"] = {"now": int(d[0]["value"]), "label": d[0]["value_classification"]}
        if len(d) > 1:
            out["fear_greed"]["yesterday"] = int(d[1]["value"])
    if isinstance(glob, dict) and glob.get("data"):
        g = glob["data"]
        out["total_mcap_usd"] = round((g.get("total_market_cap") or {}).get("usd") or 0)
        out["total_mcap_change_24h_pct"] = g.get("market_cap_change_percentage_24h_usd")
        out["btc_dominance_pct"] = round((g.get("market_cap_percentage") or {}).get("btc") or 0, 2)
        out["eth_dominance_pct"] = round((g.get("market_cap_percentage") or {}).get("eth") or 0, 2)
    if isinstance(trend, dict) and trend.get("coins"):
        out["coingecko_trending"] = [
            {
                "symbol": c["item"].get("symbol"),
                "name": c["item"].get("name"),
                "rank": c["item"].get("market_cap_rank"),
                "change_24h_pct": ((c["item"].get("data") or {}).get("price_change_percentage_24h") or {}).get("usd"),
            }
            for c in trend["coins"][:10]
        ]
    out["sources"] = "alternative.me (Fear&Greed), CoinGecko (global, trending)"
    return out
