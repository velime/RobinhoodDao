"""Spreads that can be traded without moving coins between exchanges, plus index sanity checks.

- perp ↔ perp across all exchanges: long the cheaper perp, short the dearer; funding of both legs gives
  the carry (what you earn or pay per 8h while waiting for convergence);
- spot ↔ perp: every CEX spot and the DEX pools of the coin's real contract against every perp
  (premium → buy spot + short perp; discount → long perp + sell spot you hold);
- index checks: perp vs its own index, mark vs index, each exchange's index vs the others and vs real spot.

For a pair it also answers: how long has it held (5m history), is it closing, how much size fits in the books,
and why the spread may exist (deposits/withdrawals closed on the spot side, a different token).
"""

from __future__ import annotations

import asyncio
import logging
from statistics import median

from . import feeds, http, onchain, transfers
from .exchanges import ExchangePool, find_market

log = logging.getLogger(__name__)

TAKER_FEE_PCT = 0.06  # per leg, perp taker on major venues
MIN_VOL_USD = 50_000


def per8h(f: float | None, interval_h: float | None) -> float | None:
    if f is None:
        return None
    return round(f * 8 / (interval_h or 8), 4)


def venues_from_rows(rows: list[dict], base: str) -> list[dict]:
    out = []
    for r in rows:
        if r["base"] != base or not r.get("price"):
            continue
        out.append({
            "venue": f"{feeds_name(r['ex'])} {'фьюч' if r['kind'] == 'perp' else 'спот'}", "ex": r["ex"],
            "kind": r["kind"], "base": base, "price": r["price"], "vol24_usd": r.get("vol24_usd"),
            "funding_pct": r.get("funding_pct"), "interval_h": r.get("interval_h"),
            "funding_8h": per8h(r.get("funding_pct"), r.get("interval_h")) if r["kind"] == "perp" else None,
            "mark": r.get("mark"), "index": r.get("index"), "next_funding_ms": r.get("next_funding_ms"),
        })
    return out


def feeds_name(ex: str) -> str:
    return {"okx": "OKX", "mexc": "MEXC", "bingx": "BingX", "kucoin": "KuCoin", "dex": "DEX"}.get(ex, ex.capitalize())


def _live(vs: list[dict], min_vol: float) -> list[dict]:
    return [v for v in vs if (v.get("vol24_usd") or v.get("liquidity_usd") or 0) >= min_vol and v.get("price")]


def perp_pair(vs: list[dict], min_vol: float = MIN_VOL_USD) -> dict | None:
    perps = _live([v for v in vs if v["kind"] == "perp"], min_vol)
    if len(perps) < 2:
        return None
    lo, hi = min(perps, key=lambda v: v["price"]), max(perps, key=lambda v: v["price"])
    gross = round((hi["price"] - lo["price"]) / lo["price"] * 100, 2)
    carry = None
    if hi.get("funding_8h") is not None and lo.get("funding_8h") is not None:
        carry = round(hi["funding_8h"] - lo["funding_8h"], 4)  # short receives hi's, long pays lo's
    return {"type": "perp_perp", "long": lo, "short": hi, "gross_pct": gross, "carry_8h_pct": carry,
            "net_pct": round(gross - 4 * TAKER_FEE_PCT, 2)}


def spot_perp_pair(vs: list[dict], min_vol: float = MIN_VOL_USD) -> dict | None:
    spots = _live([v for v in vs if v["kind"] in ("spot", "dex")], min_vol)
    perps = _live([v for v in vs if v["kind"] == "perp"], min_vol)
    if not spots or not perps:
        return None
    best = None
    for s in spots:
        for p in perps:
            prem = (p["price"] - s["price"]) / s["price"] * 100
            if best is None or abs(prem) > abs(best[2]):
                best = (s, p, prem)
    s, p, prem = best
    carry = p.get("funding_8h")
    if carry is not None and prem < 0:
        carry = -carry  # long perp: you receive when funding is negative
    return {"type": "spot_perp", "spot": s, "perp": p, "gross_pct": round(prem, 2),
            "scheme": "купить спот + шорт фьюч" if prem > 0 else "лонг фьюч + продать спот (нужен свой спот или займ)",
            "carry_8h_pct": carry, "net_pct": round(abs(prem) - 4 * TAKER_FEE_PCT, 2)}


def index_checks(vs: list[dict], perp_dev: float = 1.5, mark_dev: float = 1.0, idx_dev: float = 1.0,
                 spot_dev: float = 1.5, min_vol: float = MIN_VOL_USD) -> dict:
    perps = [v for v in vs if v["kind"] == "perp" and v.get("price")]
    spots = _live([v for v in vs if v["kind"] in ("spot", "dex")], min_vol)
    idx = [v["index"] for v in perps if v.get("index")]
    med_idx = median(idx) if idx else None
    med_spot = median(v["price"] for v in spots) if spots else None
    flags, rows = [], []
    for v in perps:
        r = {"venue": v["venue"], "last": v["price"], "mark": v.get("mark"), "index": v.get("index")}
        if v.get("index"):
            r["last_vs_index_pct"] = round((v["price"] - v["index"]) / v["index"] * 100, 2)
            if abs(r["last_vs_index_pct"]) >= perp_dev and (v.get("vol24_usd") or 0) >= min_vol:
                flags.append(f"{v['venue']}: перп {r['last_vs_index_pct']:+.2f}% к своему индексу")
            if v.get("mark"):
                r["mark_vs_index_pct"] = round((v["mark"] - v["index"]) / v["index"] * 100, 2)
                if abs(r["mark_vs_index_pct"]) >= mark_dev:
                    flags.append(f"{v['venue']}: марк {r['mark_vs_index_pct']:+.2f}% к индексу — ликвидации считаются по марку")
            if med_idx and len(idx) >= 3:
                r["index_vs_median_pct"] = round((v["index"] - med_idx) / med_idx * 100, 2)
                if abs(r["index_vs_median_pct"]) >= idx_dev:
                    flags.append(f"{v['venue']}: индекс {r['index_vs_median_pct']:+.2f}% к индексам других бирж — "
                                 "другой состав или сломанный источник")
        rows.append(r)
    out = {"median_index": med_idx, "median_spot": med_spot, "per_exchange": rows, "flags": flags}
    if med_idx and med_spot:
        out["index_vs_spot_pct"] = round((med_idx - med_spot) / med_spot * 100, 2)
        if abs(out["index_vs_spot_pct"]) >= spot_dev:
            flags.append(f"медиана индексов {out['index_vs_spot_pct']:+.2f}% к реальному споту ({len(spots)} площадок)")
    return out


def persistence(a: list[tuple[int, float]], b: list[tuple[int, float]], current_pct: float) -> dict | None:
    """a, b — (ts_ms, close) of the cheap and the dear leg. How long the spread has stayed open."""
    bm = dict(b)
    series = [(t, (bm[t] - p) / p * 100) for t, p in a if t in bm and p]
    if len(series) < 6:
        return None
    sign = 1 if current_pct >= 0 else -1
    vals = [x[1] * sign for x in series]
    cur = abs(current_pct)
    thr = max(0.3, cur * 0.5)
    run = 0
    for v in reversed(vals):
        if v < thr:
            break
        run += 1
    step_min = (series[1][0] - series[0][0]) / 60000 if len(series) > 1 else 5
    prev = sum(vals[-6:-3]) / 3
    return {
        "window_hours": round(len(series) * step_min / 60, 1),
        "median_pct": round(median(vals), 2),
        "max_pct": round(max(vals), 2),
        "held_minutes": round(run * step_min),
        "held_whole_window": run == len(series),
        "trend": "сужается" if vals[-1] < prev * 0.8 else "расширяется" if vals[-1] > prev * 1.2 else "стабилен",
    }


def capacity(asks: list, bids: list, fee_pct: float) -> dict:
    """Walk the cheap leg's asks against the dear leg's bids while the trade still clears fees."""
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


def spot_context(trows: list[dict], base: str, spot: dict) -> str | None:
    """Why a spot venue may sit away from the rest: its deposits/withdrawals are closed."""
    if spot["kind"] == "dex":
        return None
    mine = [r for r in trows if r["base"] == base and r["ex"] == spot["ex"]]
    if not mine:
        return None
    if all(r["deposit"] is False for r in mine):
        return f"на {feeds_name(spot['ex'])} закрыты все депозиты — спот изолирован, цену не выровняют завозом"
    if all(r["withdraw"] is False for r in mine):
        return f"на {feeds_name(spot['ex'])} закрыты все выводы — спот изолирован, цену не выровняют вывозом"
    return None


def verdict(pair: dict, persist: dict | None, cap: dict | None, context: str | None = None,
            index_flags: list[str] | None = None) -> dict:
    gross = abs(pair["gross_pct"])
    reasons, ok = [], True
    if pair["net_pct"] <= 0.2:
        return {"enter": False, "reasons": [f"спред {gross}% почти не покрывает комиссии четырёх сделок"]}
    if gross > 30:
        reasons.append("спред >30% — почти всегда другой токен, делистинг или заморозка торгов")
        ok = False
    c = pair.get("carry_8h_pct")
    if c is not None:
        if c < 0 and abs(c) * 3 >= pair["net_pct"]:
            reasons.append(f"фандинг против позиции {c:+.3f}% за 8ч — съест спред примерно за сутки")
            ok = False
        else:
            reasons.append(f"фандинг {'в твою пользу' if c >= 0 else 'против'}: {c:+.3f}% за 8ч")
    if context:
        reasons.append(context)
    if index_flags:
        reasons.append("индексы: " + "; ".join(index_flags[:2]))
    if persist:
        if persist["held_whole_window"]:
            reasons.append(f"держится всё окно ({persist['window_hours']}ч), медиана {persist['median_pct']}% — "
                           "рынок его не закрывает: ищи причину (ликвидность, переводы, другой токен)")
        elif persist["held_minutes"] < 15:
            reasons.append(f"свежий: {persist['held_minutes']} мин — может закрыться быстрее, чем войдёшь")
        else:
            reasons.append(f"держится {persist['held_minutes']} мин, медиана окна {persist['median_pct']}%, {persist['trend']}")
    if cap:
        if cap.get("fits_usd", 0) < 500:
            reasons.append(f"в стаканах помещается только ~${cap.get('fits_usd', 0)} — объёма нет")
            ok = False
        elif cap.get("profit_usd") is not None:
            reasons.append(f"помещается ~${cap['fits_usd']}, прибыль ~${cap['profit_usd']} после комиссий")
    return {"enter": ok, "reasons": reasons}


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


async def dex_venues(trows: list[dict], base: str) -> list[dict]:
    """DEX pools of the contracts the exchanges list for this coin (so it is the same token)."""
    contracts: dict[str, set] = {}
    for r in trows:
        if r["base"] == base and r["contract"] and r["chain"] in transfers.DEXSCREENER_CHAIN:
            contracts.setdefault(r["chain"], set()).add(r["contract"])
    out = []
    for chain, addrs in contracts.items():
        try:
            pools = await onchain.dex_prices(chain, sorted(addrs)[:30])
        except Exception as e:  # noqa: BLE001
            log.info("dex prices %s failed: %s", chain, e)
            continue
        for addr, p in pools.items():
            out.append({"venue": f"DEX {chain} {p['dex']}", "ex": "dex", "kind": "dex", "base": base,
                        "chain": p["chain"], "canon_chain": chain, "pool": p["pair_address"], "contract": addr,
                        "price": p["price_usd"], "liquidity_usd": p["liquidity_usd"],
                        "vol24_usd": p.get("volume_24h_usd"), "url": p.get("url")})
    return out


def _brief(v: dict) -> dict:
    return {k: v.get(k) for k in ("venue", "price", "vol24_usd", "liquidity_usd", "funding_8h", "mark", "index")
            if v.get(k) is not None}


async def _evaluate(pool: ExchangePool, pair: dict, cheap: dict, dear: dict, hours: int) -> tuple[dict | None, dict | None]:
    persist = cap = None
    try:
        a, b = await asyncio.gather(_closes(pool, cheap, hours), _closes(pool, dear, hours))
        persist = persistence(a, b, abs(pair["gross_pct"]))
    except Exception as e:  # noqa: BLE001
        log.info("spread history failed: %s", e)
    try:
        if "dex" not in (cheap["kind"], dear["kind"]):
            (asks, _), (_, bids) = await asyncio.gather(_book(pool, cheap), _book(pool, dear))
            cap = capacity(asks, bids, TAKER_FEE_PCT)
        else:
            dex = cheap if cheap["kind"] == "dex" else dear
            cap = {"fits_usd": round((dex.get("liquidity_usd") or 0) * 0.005), "profit_usd": None,
                   "note": "DEX: размер сделки до ~1% проскальзывания"}
    except Exception as e:  # noqa: BLE001
        log.info("spread capacity failed: %s", e)
    return persist, cap


async def spread_check(pool: ExchangePool, base: str, hours: int = 6) -> dict:
    """Tool entry point: perp↔perp, spot↔perp (CEX and DEX) and index checks for one coin."""
    from .symbols import normalize_base

    base = normalize_base(base)
    (rows, errors), (trows, terr) = await asyncio.gather(feeds.cached_snapshot(), transfers.all_statuses())
    vs = venues_from_rows(rows, base) + await dex_venues(trows, base)
    if not [v for v in vs if v["kind"] == "perp"]:
        return {"base": base, "error": "фьючерсов по монете не нашёл ни на одной бирже", "feeds_failed": errors or None}
    idx = index_checks(vs)
    out: dict = {"base": base, "venues": sorted((_brief(v) for v in vs), key=lambda x: x["price"]),
                 "index_check": idx, "feeds_failed": errors or None}
    pp = perp_pair(vs)
    if pp:
        persist, cap = await _evaluate(pool, pp, pp["long"], pp["short"], hours)
        out["perp_perp"] = {"long": pp["long"]["venue"], "short": pp["short"]["venue"], "gross_pct": pp["gross_pct"],
                            "carry_8h_pct": pp["carry_8h_pct"], "history": persist, "capacity": cap,
                            "verdict": verdict(pp, persist, cap, None, idx["flags"])}
    sp = spot_perp_pair(vs)
    if sp:
        cheap, dear = (sp["spot"], sp["perp"]) if sp["gross_pct"] > 0 else (sp["perp"], sp["spot"])
        persist, cap = await _evaluate(pool, sp, cheap, dear, hours)
        ctx = spot_context(trows, base, sp["spot"])
        out["spot_perp"] = {"spot": sp["spot"]["venue"], "perp": sp["perp"]["venue"], "gross_pct": sp["gross_pct"],
                            "scheme": sp["scheme"], "carry_8h_pct": sp["carry_8h_pct"], "history": persist,
                            "capacity": cap, "context": ctx, "verdict": verdict(sp, persist, cap, ctx, idx["flags"])}
    if terr:
        out["transfers_without_data"] = terr
    return out
