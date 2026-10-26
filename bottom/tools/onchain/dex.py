"""On-chain side: DEX prices by contract (batch) and large wallet sells in a pool ("слив").

- DexScreener tokens/v1: up to 30 contracts per request → best-liquidity pool price for each.
- GeckoTerminal pool trades: last 24h trades above a size → per-wallet sold/bought vs pool liquidity.
"""

from __future__ import annotations

import time
from datetime import datetime

from .. import http
from ..market.transfers import DEXSCREENER_CHAIN, GECKO_NETWORK

DEXS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2"
GT_HEADERS = {"Accept": "application/json;version=20230302"}
MIN_POOL_LIQ = 10_000


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower() if a.startswith("0x") else a == b


def best_pools(pairs: list[dict], min_liq: float = MIN_POOL_LIQ) -> dict[str, dict]:
    """DexScreener pairs → {token_address: best pool by liquidity} (token must be the base side)."""
    best: dict[str, dict] = {}
    for p in pairs:
        tok = (p.get("baseToken") or {}).get("address")
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if not tok or not p.get("priceUsd") or liq < min_liq:
            continue
        key = tok.lower() if tok.startswith("0x") else tok
        if key not in best or liq > best[key]["liquidity_usd"]:
            best[key] = {
                "chain": p.get("chainId"), "dex": p.get("dexId"), "pair_address": p.get("pairAddress"),
                "price_usd": float(p["priceUsd"]), "liquidity_usd": liq,
                "volume_24h_usd": (p.get("volume") or {}).get("h24"), "url": p.get("url"),
                "symbol": (p.get("baseToken") or {}).get("symbol"),
            }
    return best


async def dex_prices(chain: str, contracts: list[str]) -> dict[str, dict]:
    """chain — canonical (ETH, BSC, SOL...). Returns {contract(lower for EVM): pool}."""
    cid = DEXSCREENER_CHAIN.get(chain)
    if not cid or not contracts:
        return {}
    out: dict[str, dict] = {}
    for i in range(0, len(contracts), 30):
        batch = contracts[i:i + 30]
        data = await http.get_json(f"{DEXS}/tokens/v1/{cid}/{','.join(batch)}")
        out.update(best_pools(data if isinstance(data, list) else data.get("pairs") or []))
    return out


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def wallet_sells(trades: list[dict], liquidity_usd: float, now: float | None = None, window_min: int = 60) -> list[dict]:
    """GeckoTerminal trades → wallets ranked by net selling within the window."""
    now = now or time.time()
    w: dict[str, dict] = {}
    for t in trades:
        a = t.get("attributes") or t
        ts = _ts(a.get("block_timestamp"))
        if ts is None or now - ts > window_min * 60:
            continue
        who = a.get("tx_from_address") or "?"
        usd = float(a.get("volume_in_usd") or 0)
        d = w.setdefault(who, {"wallet": who, "sold_usd": 0.0, "bought_usd": 0.0, "sells": 0, "first": ts, "last": ts})
        if a.get("kind") == "sell":
            d["sold_usd"] += usd
            d["sells"] += 1
        else:
            d["bought_usd"] += usd
        d["first"], d["last"] = min(d["first"], ts), max(d["last"], ts)
    out = []
    for d in w.values():
        net = d["sold_usd"] - d["bought_usd"]
        if net <= 0:
            continue
        out.append({
            "wallet": d["wallet"], "net_sold_usd": round(net), "sells": d["sells"],
            "minutes": round((d["last"] - d["first"]) / 60),
            "pct_of_liquidity": round(net / liquidity_usd * 100, 2) if liquidity_usd else None,
        })
    out.sort(key=lambda x: -x["net_sold_usd"])
    return out


async def pool_trades(dexscreener_chain: str, pool: str, min_usd: float = 500) -> list[dict]:
    net = GECKO_NETWORK.get(dexscreener_chain, dexscreener_chain)
    data = await http.get_json(f"{GT}/networks/{net}/pools/{pool}/trades",
                               {"trade_volume_in_usd_greater_than": min_usd}, GT_HEADERS)
    return data.get("data") or []


async def dex_whales(query: str, window_min: int = 60) -> dict:
    """Tool: the coin's main pool, who is dumping into it now and how heavy relative to liquidity."""
    from .tokens import dex_pairs

    pairs = await dex_pairs(query, limit=3)
    if not pairs.get("pairs"):
        return {"query": query, "error": pairs.get("error") or "пул не найден"}
    p = pairs["pairs"][0]
    if not p.get("pair_address") or not p.get("chain"):
        return {"query": query, "error": "нет адреса пула"}
    trades = await pool_trades(p["chain"], p["pair_address"])
    sellers = wallet_sells(trades, p.get("liquidity_usd") or 0, window_min=window_min)
    heavy = [s for s in sellers if (s["pct_of_liquidity"] or 0) >= 3 or s["net_sold_usd"] >= 50_000]
    return {
        "pool": {k: p.get(k) for k in ("chain", "dex", "pair", "pair_address", "price_usd", "liquidity_usd",
                                       "volume_24h_usd", "change_1h_pct")},
        "window_min": window_min,
        "top_sellers": sellers[:8],
        "dumping": bool(heavy),
        "read": (f"{len(heavy)} кошел. сливают ≥3% ликвидности или ≥$50k — давление продаж в пул"
                 if heavy else "крупных сливов в пул за окно нет"),
        "source": "DexScreener + GeckoTerminal trades",
    }
