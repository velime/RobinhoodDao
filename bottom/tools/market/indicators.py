"""Pure-python technical indicators and level detection.

Candles are lists of dicts: {"t": ms, "o", "h", "l", "c", "v"} (+ optional
"taker_buy" base volume and "trades").
"""

from __future__ import annotations

from statistics import median


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI of the last close."""
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def atr(candles: list[dict], period: int = 14) -> float | None:
    """Wilder's ATR of the last candle."""
    if len(candles) <= period:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def stochastic(candles: list[dict], period: int = 14) -> float | None:
    if len(candles) < period:
        return None
    window = candles[-period:]
    hi = max(c["h"] for c in window)
    lo = min(c["l"] for c in window)
    if hi == lo:
        return 50.0
    return (candles[-1]["c"] - lo) / (hi - lo) * 100


def swing_levels(candles: list[dict], left: int = 3, right: int = 3) -> dict:
    """Fractal swing highs/lows — where stop/liquidity pools usually sit."""
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h = candles[i]["h"]
        l = candles[i]["l"]
        if all(h >= candles[j]["h"] for j in range(i - left, i + right + 1)):
            highs.append({"price": h, "t": candles[i]["t"]})
        if all(l <= candles[j]["l"] for j in range(i - left, i + right + 1)):
            lows.append({"price": l, "t": candles[i]["t"]})
    return {"swing_highs": highs, "swing_lows": lows}


def nearest_levels(price: float, levels: dict, n: int = 3) -> dict:
    above = sorted({x["price"] for x in levels["swing_highs"] + levels["swing_lows"] if x["price"] > price})
    below = sorted({x["price"] for x in levels["swing_highs"] + levels["swing_lows"] if x["price"] < price}, reverse=True)
    return {"resistance": above[:n], "support": below[:n]}


def range_position(price: float, lo: float, hi: float) -> float | None:
    if hi <= lo:
        return None
    return (price - lo) / (hi - lo) * 100


def pct(a: float | None, b: float | None) -> float | None:
    """Percent change from b to a."""
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(closes) < slow + signal:
        return None
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s for f, s in zip(ef, es) if f is not None and s is not None]
    sig = ema(line, signal)
    if sig[-1] is None:
        return None
    hist, prev = line[-1] - sig[-1], (line[-2] - sig[-2]) if sig[-2] is not None else None
    out = {"macd": line[-1], "signal": sig[-1], "hist": hist}
    if prev is not None:
        out["cross"] = "бычий кросс" if prev <= 0 < hist else "медвежий кросс" if prev >= 0 > hist else None
        out["hist_rising"] = hist > prev
    return out


def bollinger(closes: list[float], period: int = 20, k: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    sd = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
    up, lo = mid + k * sd, mid - k * sd
    return {
        "upper": up, "mid": mid, "lower": lo,
        "width_pct": round((up - lo) / mid * 100, 2) if mid else None,
        "position_pct": round((closes[-1] - lo) / (up - lo) * 100, 1) if up > lo else None,
    }


def fib_last_impulse(candles: list[dict], lookback: int = 100) -> dict | None:
    """Retracement levels of the last move between the extreme high and low of the window."""
    w = candles[-lookback:]
    if len(w) < 10:
        return None
    hi_i = max(range(len(w)), key=lambda i: w[i]["h"])
    lo_i = min(range(len(w)), key=lambda i: w[i]["l"])
    hi, lo = w[hi_i]["h"], w[lo_i]["l"]
    if hi <= lo:
        return None
    up = lo_i < hi_i  # impulse up: low came first
    rng = hi - lo
    levels = {str(r): (hi - rng * r) if up else (lo + rng * r) for r in (0.236, 0.382, 0.5, 0.618, 0.786)}
    return {"impulse": "вверх" if up else "вниз", "from": lo if up else hi, "to": hi if up else lo, "retracements": levels}


def level_touches(candles: list[dict], level: float, tol_pct: float = 0.3) -> int:
    """How many candles tested the level (wick within tolerance) — more touches, stronger/thinner level."""
    tol = level * tol_pct / 100
    return sum(1 for c in candles if c["l"] - tol <= level <= c["h"] + tol)


def summarize_candles(candles: list[dict], tail: int = 12) -> dict:
    """Compact summary the LLM can read: indicators, levels, volume, delta."""
    if not candles:
        return {"error": "нет свечей"}
    closes = [c["c"] for c in candles]
    price = closes[-1]
    e20, e50, e200 = ema(closes, 20)[-1], ema(closes, 50)[-1], ema(closes, 200)[-1]
    a = atr(candles)
    lv = swing_levels(candles)
    vols = [c["v"] for c in candles]
    vol_med = median(vols[-50:]) if vols else 0
    out: dict = {
        "last_close": price,
        "change_pct_over_period": round(pct(price, candles[0]["o"]) or 0, 2),
        "high": max(c["h"] for c in candles),
        "low": min(c["l"] for c in candles),
        "ema20": e20,
        "ema50": e50,
        "ema200": e200,
        "rsi14": round(rsi(closes) or 0, 1) if rsi(closes) is not None else None,
        "stoch14": round(stochastic(candles) or 0, 1) if stochastic(candles) is not None else None,
        "atr14": a,
        "atr14_pct": round(a / price * 100, 2) if a and price else None,
        "levels": nearest_levels(price, lv),
        "macd": macd(closes),
        "bollinger20": bollinger(closes),
        "fib_last_impulse": fib_last_impulse(candles),
        "last_volume_vs_median50": round(vols[-1] / vol_med, 2) if vol_med else None,
    }
    a_pct = (a / price * 100) if a and price else 0.3
    out["level_touches"] = {
        str(x): level_touches(candles, x, max(0.15, a_pct / 3))
        for x in out["levels"]["resistance"] + out["levels"]["support"]
    }
    if all("taker_buy" in c for c in candles):
        deltas = [2 * c["taker_buy"] - c["v"] for c in candles]
        cvd, series = 0.0, []
        for d in deltas:
            cvd += d
            series.append(cvd)
        out["taker_delta_last"] = deltas[-1]
        out["cvd_change_last_tail"] = series[-1] - series[-tail] if len(series) >= tail else None
        out["taker_buy_share_last_tail_pct"] = round(
            sum(c["taker_buy"] for c in candles[-tail:]) / max(sum(c["v"] for c in candles[-tail:]), 1e-12) * 100, 1
        )
    if all("trades" in c for c in candles):
        tr = [c["trades"] for c in candles]
        tr_med = median(tr[-50:])
        out["last_trades_vs_median50"] = round(tr[-1] / tr_med, 2) if tr_med else None
    out["last_candles"] = [
        {k: c[k] for k in ("t", "o", "h", "l", "c", "v")} for c in candles[-tail:]
    ]
    return out
