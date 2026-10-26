"""Deterministic check that the numbers in an answer come from the data.

The model's own claim that its numbers are right is not evidence (agent-harness
"verification discipline"): this check is code, outside the model. Numbers in the
final answer are compared with every number in the tool outputs, the question and
the rules; the ones found nowhere are returned so the agent can fix them once.
"""

from __future__ import annotations

import re

# 1 234,5 · 68.1K · $0.01033 · -3.7% · 1.2e-05 (tool JSON)
_NUM = re.compile(
    r"(?<![\w.])(?P<sign>[-+−]?)(?P<cur>\$?)"
    r"(?P<int>\d{1,3}(?:[  ,]\d{3})+|\d+)(?P<frac>\.\d+)?(?P<exp>[eE][-+]?\d+)?"
    r"\s?(?P<suf>[KkMmBb](?![a-zA-Z])|тыс|млн|млрд)?(?P<pct>\s?%)?"
)
_SKIP_CONTEXT = re.compile(r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{2,4}")
_SCALE = {"k": 1e3, "m": 1e6, "b": 1e9, "тыс": 1e3, "млн": 1e6, "млрд": 1e9}


def extract(text: str) -> list[dict]:
    """Numbers with value and flags. Times and dates are masked out first."""
    masked = _SKIP_CONTEXT.sub(lambda m: " " * len(m.group(0)), text)
    out = []
    for m in _NUM.finditer(masked):
        digits = re.sub(r"[  ,]", "", m.group("int"))
        raw = digits + (m.group("frac") or "") + (m.group("exp") or "")
        try:
            val = float(raw)
        except ValueError:
            continue
        suf = (m.group("suf") or "").lower()
        val *= _SCALE.get(suf, 1)
        if m.group("sign") in ("-", "−"):
            val = -val
        end = m.end()
        tail = masked[end:end + 1]
        out.append({
            "raw": m.group(0).strip(),
            "value": val,
            "pct": bool(m.group("pct")),
            "money": bool(m.group("cur")) or bool(suf),
            "decimal": bool(m.group("frac")),
            "leverage": tail.lower() in ("x", "х"),
        })
    return out


def _worth_checking(n: dict) -> bool:
    """Skip small plain integers (TP1, 5x, 24ч, list numbers) and leverage."""
    if n["leverage"]:
        return False
    if n["pct"] or n["money"] or n["decimal"]:
        return True
    return abs(n["value"]) >= 100


def _match(v: float, known: list[float], pct: bool) -> bool:
    av = abs(v)
    for t in known:
        at = abs(t)
        if at == 0 and av == 0:
            return True
        tol = max(at * 0.006, 0.15 if pct else 0.0)  # rounding to ~3 significant digits
        if abs(av - at) <= tol:
            return True
    return False


def ungrounded(answer: str, sources: list[str]) -> tuple[list[str], int]:
    """Numbers from the answer not found in any source. Returns (raw numbers, how many were checked)."""
    known = [abs(n["value"]) for src in sources for n in extract(src)]
    checked, missing = 0, []
    for n in extract(answer):
        if not _worth_checking(n):
            continue
        checked += 1
        if not _match(n["value"], known, n["pct"]):
            missing.append(n["raw"])
    return list(dict.fromkeys(missing)), checked


def needs_fix(missing: list[str], checked: int) -> bool:
    """A few derived numbers are fine; many unexplained ones mean invented data."""
    return len(missing) >= 3 and len(missing) >= 0.25 * max(checked, 1)


def fix_note(missing: list[str]) -> str:
    return (
        "Проверка ответа: эти числа не найдены ни в данных инструментов, ни в вопросе: "
        + ", ".join(missing[:12])
        + ". Перепиши ответ: каждое число — только из данных инструментов (если бюджет позволяет, вызови "
        "нужный инструмент); если данных нет — убери число и прямо скажи, что это не проверено. "
        "Не выдумывай и не бери цифры из примеров."
    )
