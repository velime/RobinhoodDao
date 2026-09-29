"""Derivatives: funding, open interest, long/short ratios, taker flow.

Uses official public endpoints of Binance / Bybit / OKX for history
(OI change, account L/S ratio), and ccxt for current funding/OI elsewhere.
"""

from __future__ import annotations

import asyncio
import logging

from . import http
from .exchanges import ExchangePool, find_market
from .indicators import pct
from .symbols import normalize_base

log = logging.getLogger(__name__)

BINANCE_F = "https://fapi.binance.com"
BYBIT = "https://api.bybit.com"
OKX = "https://www.okx.com"


def _changes(series: list[tuple[int, float]]) -> dict:
    """series: oldest→newest hourly points. Returns % change over 1h/4h/24h."""
    if not series:
        return {}
    now = series[-1][1]

    def back(n):
        return series[-1 - n][1] if len(series) > n else None

    return {
        "now": now,
        "chg_1h_pct": round(pct(now, back(1)) or 0, 2) if back(1) else None,
        "chg_4h_pct": round(pct(now, back(4)) or 0, 2) if back(4) else None,
        "chg_24h_pct": round(pct(now, back(24)) or 0, 2) if back(24) else None,
    }


async def _binance(market_id: str) -> dict:
    p = {"symbol": market_id, "period": "1h", "limit": 25}
    oi_h, ls, top, taker, prem, fund = await asyncio.gather(
        http.get_json(f"{BINANCE_F}/futures/data/openInterestHist", p),
        http.get_json(f"{BINANCE_F}/futures/data/globalLongShortAccountRatio", p),
        http.get_json(f"{BINANCE_F}/futures/data/topLongShortPositionRatio", p),
        http.get_json(f"{BINANCE_F}/futures/data/takerlongshortRatio", p),
        http.get_json(f"{BINANCE_F}/fapi/v1/premiumIndex", {"symbol": market_id}),
        http.get_json(f"{BINANCE_F}/fapi/v1/fundingRate", {"symbol": market_id, "limit": 6}),
        return_exceptions=True,
    )
    out: dict = {"exchange": "binance", "symbol": market_id}
    if isinstance(oi_h, list) and oi_h:
        out["oi_usd"] = _changes([(int(x["timestamp"]), float(x["sumOpenInterestValue"])) for x in oi_h])
    if isinstance(ls, list) and ls:
        out["accounts_long_pct"] = round(float(ls[-1]["longAccount"]) * 100, 1)
        out["accounts_long_pct_24h_ago"] = round(float(ls[0]["longAccount"]) * 100, 1)
    if isinstance(top, list) and top:
        out["top_traders_positions_long_pct"] = round(float(top[-1]["longAccount"]) * 100, 1)
    if isinstance(taker, list) and taker:
        last4 = taker[-4:]
        buy = sum(float(x["buyVol"]) for x in last4)
        sell = sum(float(x["sellVol"]) for x in last4)
        out["taker_buy_sell_ratio_last_4h"] = round(buy / sell, 3) if sell else None
    if isinstance(prem, dict) and prem.get("markPrice"):
        out["mark_price"] = float(prem["markPrice"])
        out["index_price"] = float(prem["indexPrice"])
        out["funding_now_pct"] = round(float(prem["lastFundingRate"]) * 100, 4)
        out["next_funding_time_ms"] = int(prem["nextFundingTime"])
    if isinstance(fund, list) and fund:
        out["funding_last_payments_pct"] = [round(float(x["fundingRate"]) * 100, 4) for x in fund]
        if len(fund) >= 2:
            out["funding_interval_hours"] = round((int(fund[-1]["fundingTime"]) - int(fund[-2]["fundingTime"])) / 3.6e6)
    errs = [str(x) for x in (oi_h, ls, top, taker, prem, fund) if isinstance(x, Exception)]
    if errs:
        out["partial_errors"] = errs[:3]
    return out


async def _bybit(market_id: str) -> dict:
    base_p = {"category": "linear", "symbol": market_id}
    tick, oi, ls = await asyncio.gather(
        http.get_json(f"{BYBIT}/v5/market/tickers", base_p),
        http.get_json(f"{BYBIT}/v5/market/open-interest", {**base_p, "intervalTime": "1h", "limit": 25}),
        http.get_json(f"{BYBIT}/v5/market/account-ratio", {**base_p, "period": "1h", "limit": 25}),
        return_exceptions=True,
    )
    out: dict = {"exchange": "bybit", "symbol": market_id}
    price = None
    if isinstance(tick, dict) and tick.get("result", {}).get("list"):
        t = tick["result"]["list"][0]
        price = float(t.get("lastPrice") or 0) or None
        if t.get("fundingRate"):
            out["funding_now_pct"] = round(float(t["fundingRate"]) * 100, 4)
        if t.get("nextFundingTime"):
            out["next_funding_time_ms"] = int(t["nextFundingTime"])
        if t.get("fundingIntervalHour"):
            out["funding_interval_hours"] = int(t["fundingIntervalHour"])
        if t.get("openInterestValue"):
            out["oi_usd_now"] = float(t["openInterestValue"])
    if isinstance(oi, dict) and oi.get("result", {}).get("list"):
        pts = sorted((int(x["timestamp"]), float(x["openInterest"])) for x in oi["result"]["list"])
        ch = _changes(pts)
        ch.pop("now", None)
        out["oi_contracts_changes"] = ch
    if isinstance(ls, dict) and ls.get("result", {}).get("list"):
        lst = sorted(ls["result"]["list"], key=lambda x: int(x["timestamp"]))
        out["accounts_long_pct"] = round(float(lst[-1]["buyRatio"]) * 100, 1)
        out["accounts_long_pct_24h_ago"] = round(float(lst[0]["buyRatio"]) * 100, 1)
    if price:
        out["last_price"] = price
    return out


async def _okx(base_ccy: str) -> dict:
    r = await http.get_json(
        f"{OKX}/api/v5/rubik/stat/contracts/long-short-account-ratio", {"ccy": base_ccy, "period": "1H"}
    )
    data = r.get("data") or []
    out: dict = {"exchange": "okx"}
    if data:
        ratio = float(data[0][1])  # newest first: [ts, longs/shorts]
        out["accounts_long_pct"] = round(ratio / (1 + ratio) * 100, 1)
    return out


async def _ccxt_funding_oi(ex, base: str) -> dict | None:
    found = find_market(ex, base, "perp")
    if not found:
        return None
    m, _mult = found
    out: dict = {"exchange": ex.id, "symbol": m["symbol"]}
    if ex.has.get("fetchFundingRate"):
        try:
            f = await asyncio.wait_for(ex.fetch_funding_rate(m["symbol"]), 10)
            if f.get("fundingRate") is not None:
                out["funding_now_pct"] = round(f["fundingRate"] * 100, 4)
            if f.get("interval"):
                out["funding_interval"] = f["interval"]
        except Exception:  # noqa: BLE001
            pass
    if ex.has.get("fetchOpenInterest"):
        try:
            oi = await asyncio.wait_for(ex.fetch_open_interest(m["symbol"]), 10)
            val = oi.get("openInterestValue")
            if val is None and oi.get("openInterestAmount") is not None:
                t = await asyncio.wait_for(ex.fetch_ticker(m["symbol"]), 10)
                val = oi["openInterestAmount"] * (m.get("contractSize") or 1) * (t.get("last") or 0)
            if val:
                out["oi_usd_now"] = round(val)
        except Exception:  # noqa: BLE001
            pass
    return out if len(out) > 2 else None


async def derivatives(pool: ExchangePool, raw_symbol: str) -> dict:
    base = normalize_base(raw_symbol)
    result: dict = {"base": base, "by_exchange": []}

    tasks = []
    bn = await pool.get("binance")
    bn_m = find_market(bn, base, "perp") if bn else None
    if bn_m:
        tasks.append(_binance(bn_m[0]["id"]))
    by = await pool.get("bybit")
    by_m = find_market(by, base, "perp") if by else None
    if by_m:
        tasks.append(_bybit(by_m[0]["id"]))
    ok = await pool.get("okx")
    ok_m = find_market(ok, base, "perp") if ok else None
    if ok_m:
        tasks.append(_okx(ok_m[0]["base"]))
    others = [ex for ex in await pool.all() if ex.id not in ("binance", "bybit")]
    tasks += [_ccxt_funding_oi(ex, base) for ex in others]

    res = await asyncio.gather(*tasks, return_exceptions=True)
    merged: dict[str, dict] = {}
    for r in res:
        if isinstance(r, dict):
            merged.setdefault(r["exchange"], {}).update(r)
        elif isinstance(r, Exception):
            log.debug("derivatives source failed: %s", r)
    result["by_exchange"] = list(merged.values())
    if not merged:
        result["error"] = f"перпов {base} не найдено или источники недоступны"
        return result

    longs = [v["accounts_long_pct"] for v in merged.values() if "accounts_long_pct" in v]
    if longs:
        result["crowd_accounts_long_pct_avg"] = round(sum(longs) / len(longs), 1)
        result["crowd_sources"] = [k for k, v in merged.items() if "accounts_long_pct" in v]
    fund = {k: v["funding_now_pct"] for k, v in merged.items() if "funding_now_pct" in v}
    if fund:
        result["funding_now_pct_by_exchange"] = fund
    oi_total = 0.0
    for k, v in merged.items():
        if k == "binance" and v.get("oi_usd", {}).get("now"):
            oi_total += v["oi_usd"]["now"]
        elif v.get("oi_usd_now"):
            oi_total += v["oi_usd_now"]
    if oi_total:
        result["oi_usd_total_known_exchanges"] = round(oi_total)
    result["notes"] = (
        "funding_now_pct — ставка за один интервал выплаты (см. funding_interval_hours); "
        "oi_usd.chg_* — изменение OI Binance в USD; oi_contracts_changes — изменение OI Bybit в контрактах"
    )
    return result


async def binance_klines(market_id: str, interval: str, limit: int) -> list[dict]:
    """Binance futures klines with taker-buy volume and trade count (for delta/CVD)."""
    raw = await http.get_json(
        f"{BINANCE_F}/fapi/v1/klines", {"symbol": market_id, "interval": interval, "limit": limit}
    )
    return [
        {
            "t": int(k[0]),
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "v": float(k[5]),
            "trades": int(k[8]),
            "taker_buy": float(k[9]),
        }
        for k in raw
    ]
