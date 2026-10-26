"""Setups: the validator every setup must pass, own track record, and the reference caller's calls with outcomes."""

from __future__ import annotations

import asyncio
from functools import partial

from ..market import candle_summary
from ..schema import SYM
from .calc import calc_setup, entry_status, stop_hunt_warnings


async def checked_setup(tb, symbol: str, direction: str, entry_low: float, stop: float, targets: list[float],
                        entry_high: float | None = None) -> dict:
    """calc_setup with the live market around it: ATR for the minimum stop, where the price is now relative to
    the plan (entry_status), and liquidity right behind the stop (stop-hunt warnings)."""
    async def summary(tf):
        try:
            return await candle_summary(tb.pool, symbol, tf, 100)
        except Exception:  # noqa: BLE001
            return {}

    h1, h4 = await asyncio.gather(summary("1h"), summary("4h"))
    res = calc_setup(tb.s.risk, symbol, direction, float(entry_low), float(stop),
                     [float(t) for t in targets], float(entry_high) if entry_high else None, h1.get("atr14_pct"))
    if res.get("verdict") not in ("OK", "REJECT"):
        return res
    lo = float(entry_low)
    hi = float(entry_high) if entry_high else lo
    lo, hi = min(lo, hi), max(lo, hi)
    tp1 = sorted(float(t) for t in targets)[0] if direction == "long" else sorted(float(t) for t in targets)[-1]
    status = entry_status(direction, lo, hi, float(stop), tp1, h1.get("last_close"))
    res["entry_status"] = status
    if status["status"] == "invalid":
        res["verdict"] = "REJECT"
        res.setdefault("problems", []).append(status["note"])
    levels = []
    for tf, sm in (("1ч", h1), ("4ч", h4)):
        lv = sm.get("levels") or {}
        levels += [(p, f"свинг {tf}") for p in (lv.get("support") or []) + (lv.get("resistance") or [])]
        rg = sm.get("range_24h") or {}
        if tf == "1ч" and rg.get("high"):
            levels += [(rg["high"], "хай 24ч"), (rg["low"], "лоу 24ч")]
    warn = stop_hunt_warnings(direction, lo, hi, float(stop), levels)
    if warn:
        res["warnings"] = res.get("warnings", []) + warn
    return res


def register(tb) -> None:
    if tb.journal is not None:
        journal = tb.journal

        async def reference_calls(symbol: str = "", tag: str = "") -> dict:
            from ...learning import reference

            return await asyncio.to_thread(reference.lookup, journal.db, symbol, tag)

        tb.add(
            "reference_calls",
            "Колы референсного авто-колера (учебный материал) с их реальным исходом на свечах: по монете или "
            "по типу идеи, и статистика, какие идеи у него работали (винрейт до TP1, средний R, доля "
            "неисполненных). Смотри перед сетапом похожего типа. Его уровни не копировать.",
            {
                "symbol": {"type": "string", "description": "Тикер; пусто — все"},
                "tag": {"type": "string", "enum": ["", "exhausted_squeeze", "squeeze", "oi_flip", "oi_alert",
                                                   "onchain_distribution", "onchain_accumulation", "funding_payout",
                                                   "listing", "news", "fvg_limit", "range_extreme", "spot_led",
                                                   "tokenized_stock"]},
            },
            [], "📚 Смотрю опыт референса",
            reference_calls,
        )

        async def track_record(symbol: str = "", days: int = 90) -> dict:
            return journal.track_record(symbol, int(days))

        tb.add(
            "track_record",
            "Твоя собственная история сетапов: сколько было, исходы (стоп/TP1/TP2/не исполнился), винрейт, "
            "средний R, MFE/MAE; можно по тикеру. Проверяй перед новым сетапом — особенно по той же монете.",
            {
                "symbol": {"type": "string", "description": "Тикер; пусто — все сетапы"},
                "days": {"type": "integer", "description": "За сколько дней (по умолчанию 90)"},
            },
            [], "📒 Смотрю свою статистику",
            track_record,
        )
    tb.add(
        "calc_setup",
        "ОБЯЗАТЕЛЬНО перед любым сетапом. Проверяет вход/стоп/цели: геометрию, минимальную дистанцию стопа "
        "(с учётом ATR), минимальный R:R; считает R:R к каждой цели, % до стопа и потери/прибыль для плеч "
        "3/5/10/20x. Вердикт OK/REJECT/ERROR с причинами и подсказками. entry_status — где живая цена "
        "относительно плана (in_zone / away — только лимитка / gone — вход ушёл / invalid — за стопом); "
        "warnings — ликвидность сразу за стопом (риск съёма стопа).",
        {
            "symbol": SYM,
            "direction": {"type": "string", "enum": ["long", "short"]},
            "entry_low": {"type": "number", "description": "Вход или нижняя граница зоны входа"},
            "entry_high": {"type": "number", "description": "Верхняя граница зоны входа (если зона)"},
            "stop": {"type": "number"},
            "targets": {"type": "array", "items": {"type": "number"}, "description": "Цели по порядку"},
        },
        ["symbol", "direction", "entry_low", "stop", "targets"], "🧮 Считаю сетап",
        partial(checked_setup, tb),
    )
