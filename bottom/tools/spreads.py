"""Real spreads for a coin: every CEX (spot and perp) and the DEX pools of its real contract.

For the best pair it answers the questions a trader asks before entering:
- is it the same token (contract on the exchange's network = the DEX pool's token)?
- can it actually move: withdraw open where we buy, deposit open where we sell, on a common network?
- how long has the spread held (history of 5m closes on both venues), and is it closing?
- how much size fits (order books on both ends, DEX liquidity) after fees?
A long-living wide spread is usually not free money: closed transfers, a different token or no depth.
"""

from __future__ import annotations

import asyncio
import logging
from statistics import median

from . import http, onchain, transfers
from .exchanges import ExchangePool, find_market, market_overview

log = logging.getLogger(__name__)

TAKER_FEE_PCT = 0.1
MIN_VOL_USD = 10_000


def pairs(venues: list[dict], min_vol: float = MIN_VOL_USD) -> dict:
    """Best transfer pair (spot/DEX → spot/DEX) and best perp-perp pair by gross spread."""
    live = [v for v in venues if (v.get("vol24_usd") or v.get("liquidity_usd") or 0) >= min_vol and v.get("price")]
    out = {}
    spot_like = [v for v in live if v["kind"] in ("spot", "dex")]
    if len(spot_like) >= 2:
        buy = min(spot_like, key=lambda v: v.get("ask") or v["price"])
        sell = max((v for v in spot_like if v["venue"] != buy["venue"]), key=lambda v: v.get("bid") or v["price"])
        b, s = buy.get("ask") or buy["price"], sell.get("bid") or sell["price"]
        out["transfer"] = {"buy": buy, "sell": sell, "gross_pct": round((s - b) / b * 100, 2)}
    perps = [v for v in live if v["kind"] == "perp"]
    if len(perps) >= 2:
        lo, hi = min(perps, key=lambda v: v["price"]), max(perps, key=lambda v: v["price"])
        out["perp"] = {"long": lo, "short": hi, "gross_pct": round((hi["price"] - lo["price"]) / lo["price"] * 100, 2)}
    return out


def persistence(a: list[tuple[int, float]], b: list[tuple[int, float]], current_pct: float) -> dict | None:
    """a, b — (ts_ms, close) of buy and sell venue. How long the spread has stayed open."""
    bm = dict(b)
    series = [(t, (bm[t] - p) / p * 100) for t, p in a if t in bm and p]
    if len(series) < 6:
        return None
    vals = [x[1] for x in series]
    thr = max(0.5, current_pct * 0.5)
    run = 0
    for _, v in reversed(series):
        if v < thr:
            break
        run += 1
    step_min = (series[1][0] - series[0][0]) / 60000 if len(series) > 1 else 5
    return {
        "window_hours": round(len(series) * step_min / 60, 1),
        "median_pct": round(median(vals), 2),
        "max_pct": round(max(vals), 2),
        "held_minutes": round(run * step_min),
        "held_whole_window": run == len(series),
        "trend": "сужается" if len(vals) >= 6 and vals[-1] < mean3(vals[-6:-3]) * 0.8 else
                 "расширяется" if len(vals) >= 6 and vals[-1] > mean3(vals[-6:-3]) * 1.2 else "стабилен",
    }


def mean3(x: list[float]) -> float:
    return sum(x) / len(x) if x else 0


def capacity(asks: list, bids: list, fee_pct: float) -> dict:
    """Walk buy-side asks against sell-side bids while the trade still clears fees. Levels (price, amount)."""
    i = j = 0
    asks, bids = [list(x[:2]) for x in asks], [list(x[:2]) for x in bids]
    usd = profit = 0.0
    while i < len(asks) and j < len(bids):
        pa, qa = asks[i]
        pb, qb = bids[j]
        if pb <= pa * (1 + 2 * fee_pct / 100):
            break
        q = min(qa, qb)
        usd += q * pa
        profit += q * (pb - pa) - q * (pa + pb) * fee_pct / 100
        asks[i][1] -= q
        bids[j][1] -= q
        if asks[i][1] <= 1e-12:
            i += 1
        if bids[j][1] <= 1e-12:
            j += 1
    return {"fits_usd": round(usd), "profit_usd": round(profit)}


def verdict(pair: dict, route: dict | None, persist: dict | None, cap: dict | None) -> dict:
    gross = pair["gross_pct"]
    reasons, ok = [], True
    if gross < 2 * TAKER_FEE_PCT + 0.3:
        return {"enter": False, "reasons": [f"спред {gross}% не покрывает комиссии и риск"]}
    if gross > 40:
        reasons.append("спред >40% — почти всегда другой токен, делистинг или замороженные переводы")
        ok = False
    if route:
        if route["status"] == "closed":
            reasons.append(f"перевод закрыт ({route.get('why')}) — поэтому спред и держится, взять его нельзя")
            ok = False
        elif route["status"] == "unknown":
            reasons.append(f"статус переводов неизвестен ({route.get('why')}) — проверь вручную на бирже")
        if route.get("contract_mismatch"):
            reasons.append(f"разные контракты на сетях {route['contract_mismatch']} — возможно другой токен")
            ok = False
        if route["status"] == "open":
            reasons.append(f"маршрут открыт: {route['fastest']}, перевод ~{route['transfer_minutes']} мин")
    if persist:
        if persist["held_whole_window"]:
            reasons.append(f"спред держится всё окно ({persist['window_hours']}ч) — его никто не закрывает: ищи причину "
                           "(переводы, ликвидность, другой токен)")
        elif persist["held_minutes"] < 15:
            reasons.append(f"спред свежий ({persist['held_minutes']} мин) — может закрыться раньше, чем дойдёт перевод")
        else:
            reasons.append(f"держится {persist['held_minutes']} мин, медиана окна {persist['median_pct']}%, {persist['trend']}")
        if route and route.get("transfer_minutes") and persist["held_minutes"] and \
                persist["trend"] == "сужается" and persist["held_minutes"] < 3 * route["transfer_minutes"]:
            reasons.append("сужается быстрее, чем идёт перевод — риск не успеть")
    if cap:
        if cap.get("fits_usd", 0) < 300:
            reasons.append(f"в стаканах помещается только ~${cap.get('fits_usd', 0)} — объёма нет")
            ok = False
        else:
            reasons.append(f"помещается ~${cap['fits_usd']} с прибылью ~${cap['profit_usd']} после комиссий биржи")
    return {"enter": ok and bool(route and route["status"] == "open"), "reasons": reasons}


async def _closes(pool: ExchangePool, v: dict, hours: int) -> list[tuple[int, float]]:
    limit = min(hours * 12, 288)
    if v["kind"] == "dex":
        net = transfers.GECKO_NETWORK.get(v["chain"], v["chain"])
        data = await http.get_json(f"{onchain.GT}/networks/{net}/pools/{v['pool']}/ohlcv/minute",
                                   {"aggregate": 5, "limit": limit}, onchain.GT_HEADERS)
        rows = (((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        return sorted((int(r[0]) * 1000 // 300000 * 300000, float(r[4])) for r in rows)
    ex = await pool.get(v["ex"])
    found = find_market(ex, v["base"], v["kind"]) if ex else None
    if not found:
        return []
    m, mult = found
    raw = await asyncio.wait_for(ex.fetch_ohlcv(m["symbol"], "5m", limit=limit), 15)
    return [(int(r[0]) // 300000 * 300000, r[4] / mult) for r in raw]


async def _book(pool: ExchangePool, v: dict):
    ex = await pool.get(v["ex"])
    found = find_market(ex, v["base"], v["kind"]) if ex else None
    if not found:
        return None
    m, mult = found
    b = await asyncio.wait_for(ex.fetch_order_book(m["symbol"], 50), 10)
    conv = lambda lv: [(p / mult, a * mult) for p, a, *_ in lv]  # noqa: E731
    return conv(b["asks"]), conv(b["bids"])


async def venues_for(pool: ExchangePool, base: str) -> tuple[list[dict], dict]:
    """All CEX spot/perp tickers + DEX pools of the contracts the exchanges list for this coin."""
    ov, (trows, terr) = await asyncio.gather(market_overview(pool, base), transfers.all_statuses())
    venues = [
        {"venue": f"{r['exchange']} {r['kind']}", "ex": r["exchange"], "kind": r["kind"], "base": base,
         "price": r["price"], "bid": r.get("bid"), "ask": r.get("ask"), "vol24_usd": r.get("quote_volume_24h")}
        for r in ov.get("rows", [])
    ]
    contracts: dict[str, set] = {}
    for r in trows:
        if r["base"] == base and r["contract"] and r["chain"] in transfers.DEXSCREENER_CHAIN:
            contracts.setdefault(r["chain"], set()).add(r["contract"])
    dex_err = {}
    for chain, addrs in contracts.items():
        try:
            pools = await onchain.dex_prices(chain, sorted(addrs)[:30])
        except Exception as e:  # noqa: BLE001
            dex_err[chain] = type(e).__name__
            continue
        for addr, p in pools.items():
            venues.append({"venue": f"dex {chain} {p['dex']}", "ex": "dex", "kind": "dex", "base": base,
                           "chain": p["chain"], "canon_chain": chain, "pool": p["pair_address"], "contract": addr,
                           "price": p["price_usd"], "liquidity_usd": p["liquidity_usd"],
                           "vol24_usd": p.get("volume_24h_usd"), "url": p.get("url")})
    return venues, {"transfers_without_data": terr, "dex_errors": dex_err or None,
                    "cex_unavailable": ov.get("unavailable_exchanges"), "transfer_rows": trows}


def dex_route(trows: list[dict], base: str, dex: dict, cex: str, direction: str) -> dict:
    """DEX→CEX needs CEX deposit on the pool's chain; CEX→DEX needs CEX withdraw. Contract must match."""
    r = next((x for x in trows if x["base"] == base and x["ex"] == cex and x["chain"] == dex["canon_chain"]), None)
    if not r:
        return {"status": "unknown", "why": f"{cex}: нет данных по сети {dex['canon_chain']}"}
    if transfers.same_contract(r["contract"], dex["contract"]) is False:
        return {"status": "closed", "why": "контракт на бирже другой", "contract_mismatch": [dex["canon_chain"]]}
    need = "deposit" if direction == "to_cex" else "withdraw"
    if not r[need]:
        return {"status": "closed", "why": f"{cex} {dex['canon_chain']}: {'депозит' if need == 'deposit' else 'вывод'} закрыт"}
    return {"status": "open", "networks": [dex["canon_chain"]], "fastest": dex["canon_chain"],
            "transfer_minutes": transfers.TRANSFER_MINUTES.get(dex["canon_chain"], 10), "withdraw_fee": r["fee"]}


async def spread_check(pool: ExchangePool, base: str, hours: int = 6) -> dict:
    """Tool entry point."""
    from .symbols import normalize_base

    base = normalize_base(base)
    venues, meta = await venues_for(pool, base)
    trows = meta.pop("transfer_rows")
    if len(venues) < 2:
        return {"base": base, "error": "меньше двух площадок с ценой — спреда считать не с чем", **meta}
    best = pairs(venues)
    out: dict = {"base": base, "venues": sorted(
        ({k: v.get(k) for k in ("venue", "price", "vol24_usd", "liquidity_usd") if v.get(k) is not None} for v in venues),
        key=lambda x: x["price"]), **meta}
    tp = best.get("transfer")
    if tp:
        buy, sell = tp["buy"], tp["sell"]
        if buy["kind"] == "dex" and sell["kind"] == "dex":
            route = {"status": "open", "fastest": "on-chain", "transfer_minutes": 1,
                     "why": "обе площадки DEX — нужен мост, если сети разные"} if buy["chain"] == sell["chain"] else \
                {"status": "unknown", "why": f"разные сети {buy['chain']}→{sell['chain']}: нужен мост"}
        elif buy["kind"] == "dex":
            route = dex_route(trows, base, buy, sell["ex"], "to_cex")
        elif sell["kind"] == "dex":
            route = dex_route(trows, base, sell, buy["ex"], "from_cex")
        else:
            route = transfers.route(trows, base, buy["ex"], sell["ex"])
        persist = cap = None
        try:
            a, b = await asyncio.gather(_closes(pool, buy, hours), _closes(pool, sell, hours))
            persist = persistence(a, b, tp["gross_pct"])
        except Exception as e:  # noqa: BLE001
            log.info("spread history failed: %s", e)
        try:
            if buy["kind"] != "dex" and sell["kind"] != "dex":
                (asks, _), (_, bids) = await asyncio.gather(_book(pool, buy), _book(pool, sell))
                cap = capacity(asks, bids, TAKER_FEE_PCT)
            else:
                dex = buy if buy["kind"] == "dex" else sell
                cap = {"fits_usd": round((dex.get("liquidity_usd") or 0) * 0.005), "profit_usd": None,
                       "note": "оценка DEX: размер сделки до ~1% проскальзывания"}
        except Exception as e:  # noqa: BLE001
            log.info("spread capacity failed: %s", e)
        out["transfer_spread"] = {
            "buy": buy["venue"], "buy_price": buy.get("ask") or buy["price"],
            "sell": sell["venue"], "sell_price": sell.get("bid") or sell["price"],
            "gross_pct": tp["gross_pct"], "route": route, "history": persist, "capacity": cap,
            "verdict": verdict(tp, route, persist, cap),
        }
    pp = best.get("perp")
    if pp:
        out["perp_spread"] = {
            "long": pp["long"]["venue"], "short": pp["short"]["venue"], "gross_pct": pp["gross_pct"],
            "note": "перп-перп без перевода: лонг дешевле + шорт дороже, заработок на схождении цен; "
                    "учти фандинг обеих сторон (funding в derivatives) и ликвидацию каждой ноги",
        }
    return out
