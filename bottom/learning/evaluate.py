"""Outcome of a setup on real hourly candles (pure code, shared by own setups and the reference caller's calls).

Conservative on purpose: a limit fills at the worst edge of the zone, a stop and a target in the same candle count
as the stop, after TP1 the stop moves to breakeven.
"""

from __future__ import annotations

import time

FILL_WINDOW_H = 72  # a limit entry not reached in 3 days → not filled
MAX_OPEN_H = 7 * 24  # a filled trade still open after 7 days → closed at market

def evaluate_setup(setup: dict, candles: list[dict], now: float | None = None) -> dict:
    """Walk hourly candles after the setup was given. Returns status/outcome fields.

    Conservative rules: fill at the worst edge of the zone; if stop and target are
    touched in the same candle, the stop counts first; after TP1 half is closed and the
    stop moves to breakeven.
    """
    now = now or time.time()
    long = setup["direction"] == "long"
    lo, hi = setup["entry_low"], setup["entry_high"] or setup["entry_low"]
    entry = hi if long else lo
    stop = setup["stop"]
    targets = setup["targets"]
    risk = abs(entry - stop)
    created_ms = setup["created"] * 1000
    after = [c for c in candles if c["t"] + 3_600_000 > created_ms]

    fill_i = None
    for i, c in enumerate(after):
        if (c["t"] - created_ms) / 3_600_000 > FILL_WINDOW_H:
            break
        if (long and c["l"] <= hi) or (not long and c["h"] >= lo):
            fill_i = i
            break
    age_h = (now - setup["created"]) / 3600
    if fill_i is None:
        if age_h > FILL_WINDOW_H:
            return {"status": "closed", "outcome": "not_filled", "r": 0.0}
        return {"status": "open"}

    mfe = mae = 0.0
    tp1_hit = False
    stop_now = stop
    for c in after[fill_i:]:
        fav = (c["h"] - entry) if long else (entry - c["l"])
        adv = (entry - c["l"]) if long else (c["h"] - entry)
        mfe, mae = max(mfe, fav), max(mae, adv)
        hit_stop = (c["l"] <= stop_now) if long else (c["h"] >= stop_now)
        hit_tp1 = (c["h"] >= targets[0]) if long else (c["l"] <= targets[0])
        hit_tp2 = len(targets) > 1 and ((c["h"] >= targets[1]) if long else (c["l"] <= targets[1]))
        base = {"mfe_pct": round(mfe / entry * 100, 2), "mae_pct": round(mae / entry * 100, 2)}
        if not tp1_hit:
            if hit_stop:
                return {"status": "closed", "outcome": "stop", "r": -1.0, **base}
            if hit_tp1:
                tp1_hit = True
                stop_now = entry
                r1 = abs(targets[0] - entry) / risk
                if hit_tp2:
                    r2 = abs(targets[1] - entry) / risk
                    return {"status": "closed", "outcome": "tp2", "r": round(0.5 * r1 + 0.5 * r2, 2), **base}
                continue
        else:
            r1 = abs(targets[0] - entry) / risk
            if hit_stop:
                return {"status": "closed", "outcome": "tp1_then_be", "r": round(0.5 * r1, 2), **base}
            if hit_tp2:
                r2 = abs(targets[1] - entry) / risk
                return {"status": "closed", "outcome": "tp2", "r": round(0.5 * r1 + 0.5 * r2, 2), **base}
    last = after[-1]["c"]
    move_r = ((last - entry) if long else (entry - last)) / risk
    r_now = round(0.5 * abs(targets[0] - entry) / risk + 0.5 * move_r if tp1_hit else move_r, 2)
    res = {"mfe_pct": round(mfe / entry * 100, 2), "mae_pct": round(mae / entry * 100, 2)}
    if age_h > MAX_OPEN_H:
        return {"status": "closed", "outcome": "tp1_expired" if tp1_hit else "expired", "r": r_now, **res}
    return {"status": "open", "outcome": "tp1_open" if tp1_hit else "in_trade", "r": r_now, **res}


def published_setups(calc_results: list[dict], answer: str) -> list[dict]:
    """calc_setup OK results whose stop price actually appears in the final answer."""
    out, seen = [], set()
    for r in reversed(calc_results):  # the last validated version wins
        key = (r.get("symbol", "").upper(), r.get("direction"))
        if r.get("verdict") != "OK" or key in seen:
            continue
        stop = r.get("stop")
        variants = {f"{stop:g}", f"{stop:.10f}".rstrip("0").rstrip("."), str(stop)}
        if any(v in answer for v in variants):
            seen.add(key)
            out.append(r)
    return out


# --------------------------------------------------------------------------- storage
