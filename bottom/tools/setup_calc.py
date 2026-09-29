"""Setup validator and risk calculator.

The LLM must pass every trade setup through this before showing it:
the numbers (stop %, R:R, leverage loss) come from here, not from the model.
"""

from __future__ import annotations

from ..config import RiskRules

LEVERAGES = (3, 5, 10, 20)


def _r(x: float, n: int = 2) -> float:
    return round(x, n)


def calc_setup(
    rules: RiskRules,
    symbol: str,
    direction: str,
    entry_low: float,
    stop: float,
    targets: list[float],
    entry_high: float | None = None,
    atr_pct: float | None = None,
) -> dict:
    """Validate a setup. Entry may be a zone [entry_low, entry_high].

    The zone is checked at its worst edge for R:R (highest entry for a long,
    lowest for a short) and at its best edge for stop distance, so a setup
    only passes if the whole zone is tradable.
    """
    direction = direction.lower().strip()
    if direction not in ("long", "short"):
        return {"verdict": "ERROR", "problems": ["direction должен быть long или short"]}
    if not targets:
        return {"verdict": "ERROR", "problems": ["нужна хотя бы одна цель"]}
    entry_high = entry_high if entry_high is not None else entry_low
    lo, hi = min(entry_low, entry_high), max(entry_low, entry_high)
    is_long = direction == "long"
    targets = sorted(targets, reverse=not is_long)

    problems: list[str] = []
    suggestions: list[str] = []

    # Geometry
    if is_long and not stop < lo:
        problems.append(f"для лонга стоп ({stop}) должен быть ниже входа ({lo})")
    if not is_long and not stop > hi:
        problems.append(f"для шорта стоп ({stop}) должен быть выше входа ({hi})")
    bad_t = [t for t in targets if (t <= hi if is_long else t >= lo)]
    if bad_t:
        problems.append(f"цели {bad_t} по неверную сторону от входа")
    if problems:
        return {"verdict": "ERROR", "problems": problems}

    worst_entry = hi if is_long else lo  # worst fill for R:R
    best_entry = lo if is_long else hi  # tightest stop distance

    def stop_pct(e: float) -> float:
        return abs(e - stop) / e * 100

    def rr(e: float, t: float) -> float:
        return abs(t - e) / abs(e - stop)

    base = symbol.upper().replace("$", "").split("/")[0].replace("USDT", "")
    floor = rules.min_stop_pct_major if base in rules.majors else rules.min_stop_pct_alt
    atr_floor = (atr_pct or 0) * rules.min_stop_atr_mult
    min_stop = max(floor, atr_floor)

    sp_tight = stop_pct(best_entry)
    rr1 = rr(worst_entry, targets[0])

    if sp_tight < min_stop:
        problems.append(
            f"стоп слишком близко: {_r(sp_tight)}% от входа, нужно ≥{_r(min_stop)}% "
            f"(минимум {floor}%"
            + (f", {rules.min_stop_atr_mult}×ATR1h = {_r(atr_floor)}%" if atr_floor else "")
            + ") — выбьет шумом"
        )
        need_stop = best_entry * (1 - min_stop / 100) if is_long else best_entry * (1 + min_stop / 100)
        suggestions.append(f"стоп не ближе {need_stop:.6g} (или вход дальше от стопа)")
    if rr1 < rules.min_rr:
        problems.append(f"R:R к первой цели {_r(rr1)}:1 ниже минимума {rules.min_rr}:1")
        # entry that gives min R:R with the same stop and TP1
        R = rules.min_rr
        need_entry = (targets[0] + R * stop) / (1 + R)
        side = "не выше" if is_long else "не ниже"
        suggestions.append(f"вход {side} {need_entry:.6g} при том же стопе и TP1 даст R:R {R}:1")

    verdict = "OK" if not problems else "REJECT"

    per_target = [
        {
            "target": t,
            "move_pct": _r(abs(t - worst_entry) / worst_entry * 100),
            "rr": _r(rr(worst_entry, t)),
        }
        for t in targets
    ]
    sp_worst = stop_pct(worst_entry)
    lev = []
    for L in LEVERAGES:
        loss = sp_worst * L
        lev.append(
            {
                "leverage": L,
                "loss_at_stop_pct_of_margin": _r(loss, 1),
                "gain_at_tp1_pct_of_margin": _r(per_target[0]["move_pct"] * L, 1),
                "liquidation_before_stop": loss >= 90,  # isolated, approx incl. maintenance margin
            }
        )

    return {
        "verdict": verdict,
        "symbol": symbol,
        "direction": direction,
        "entry_zone": [lo, hi],
        "stop": stop,
        "stop_pct_from_worst_entry": _r(sp_worst),
        "stop_pct_from_best_entry": _r(sp_tight),
        "min_stop_pct_required": _r(min_stop),
        "min_rr_required": rules.min_rr,
        "targets": per_target,
        "rr_tp1": _r(rr1),
        "leverage_table": lev,
        "problems": problems,
        "suggestions": suggestions,
        "note": "R:R и % считаются от худшей точки зоны входа; используй эти числа как есть",
    }
