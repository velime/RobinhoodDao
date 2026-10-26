"""Learning from mistakes.

1. Journal: every setup Bottom publishes (validated by calc_setup) is recorded.
2. Evaluator: hourly candles decide what happened — not filled / stop / TP1 → BE / TP2 —
   with MFE/MAE and the result in R.
3. Feedback: users reply /wrong to a bot answer and say what was wrong.
4. Review: once a day the model reads closed setups and feedback and proposes short,
   checkable lessons, each citing the cases it rests on.
5. Promotion is earned, not asserted (agent-memory / memory-engineering methodology):
   - a proposed lesson starts as a *candidate* and is NOT injected into the prompt;
   - it becomes *active* only when supported by >= 2 distinct cases on >= 2 distinct
     review days, or when an admin adopts it (/adopt, /lesson);
   - evidence is checked: a lesson may only cite cases shown in that review;
   - contradicting evidence never deletes a lesson — it is marked *contested* and
     still shown with the tag until a human resolves it (/adopt or /forget);
   - forgetting policy: candidates expire after 30 days, active auto lessons expire
     after 60 days without re-confirmation, at most 30 active lessons (oldest-confirmed
     auto lessons evicted first; admin lessons are never evicted).
6. Use: active and contested lessons plus the track record go into the system prompt,
   and the track_record tool lets the model check its own history before a setup.
"""

from __future__ import annotations

import logging
import re
import time

from .evaluate import evaluate_setup
from .journal import Journal

log = logging.getLogger(__name__)

REVIEW_PROMPT = """\
Ты — наставник крипто-аналитика Bottom. Ниже его прошлые сетапы с фактическим исходом по свечам, \
жалобы пользователей на его ответы и его текущие уроки.
Задача: сформулировать короткие, конкретные, проверяемые уроки, которые улучшат будущие ответы.
Правила:
- Урок — одно правило-действие, до 200 символов: что делать иначе и в какой ситуации. Без воды.
- Опирайся только на факты ниже (исходы, MFE/MAE, текст жалоб). Один случай — слабое основание: \
пиши «наблюдение», если подтверждение одно.
- Ищи закономерности: где стопы выбивает шумом (MAE до стопа, потом цель), где вход не \
исполняется (слишком далёкий лимит), где цели недостижимы (MFE меньше TP1), какие монеты/сетапы \
работают, в чём пользователи правы.
- Каждый урок опирается на конкретные случаи: в квадратных скобках укажи их номера (S12 — сетап, \
F3 — жалоба). Ссылаться можно только на случаи из этого списка. Без ссылок урок не принимается.
- Не дублируй существующие уроки: если новый случай подтверждает существующий урок или кандидата — \
подтверди его. Если факты противоречат уроку — отметь это (урок не удаляется, решает человек).
Формат ответа — только строки:
+ текст нового урока [S12, F3]
= #id [S15]
! #id почему факты противоречат уроку [S16]
Если сказать нечего — ответь одним словом: НЕТ."""


REFERENCE_REVIEW_PROMPT = """\
Ты — наставник крипто-аналитика Bottom. Ниже колы сильного авто-колера (референс) с фактическим исходом \
на реальных 1ч-свечах: тот же строгий оценщик, что и для сетапов Bottom (вход по худшему краю зоны, стоп раньше \
цели в одной свече, после TP1 — безубыток). Рядом — результаты, которые писали люди, и текущие уроки Bottom.
Задача: вывести короткие проверяемые уроки для Bottom — что в идеях и сетапах референса работало, а что нет.
Правила:
- Урок — одно правило-действие, до 200 символов: в какой ситуации что делать (тип идеи, направление, \
расстояние до стопа, лимит или от текущих, R:R). Без воды.
- Опирайся только на исходы ниже. Закономерность — минимум два случая с разными датами; один случай не урок.
- Учись и на минусах: стопы, выбитые шумом (MAE до стопа, потом цель), лимиты, которые не исполнились, \
цели, до которых цена не дошла (MFE меньше TP1).
- Не копируй уровни и цифры конкретных монет — только логику.
- Каждый урок опирается на случаи из списка: номера в квадратных скобках (R12). Без ссылок урок не принимается.
- Если случаи подтверждают существующий урок или кандидата — подтверди его; противоречат — отметь.
Формат ответа — только строки:
+ текст нового урока [R12, R40]
= #id [R15]
! #id почему факты противоречат уроку [R16]
Если сказать нечего — ответь одним словом: НЕТ."""



_REFS = re.compile(r"\[([^\]]*)\]\s*$")


def _split_refs(body: str) -> tuple[str, list[str]]:
    """'text [S12, F3, R7]' → ('text', ['S12', 'F3', 'R7'])."""
    m = _REFS.search(body)
    if not m:
        return body.strip(), []
    refs = re.findall(r"\b([SFR]\d+)\b", m.group(1).upper())
    return body[: m.start()].strip(), list(dict.fromkeys(refs))


def parse_review(text: str) -> dict:
    """Parse the reviewer's lines.

    '+ lesson [S1, F2]' → new candidate; '= #id [S3]' → confirmation;
    '! #id reason [S4]' → contradiction (the lesson gets contested, not deleted).
    """
    out: dict = {"new": [], "confirm": [], "contest": []}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("+"):
            body, refs = _split_refs(line[1:])
            if 10 <= len(body) <= 300:
                out["new"].append((body, refs))
            continue
        m = re.match(r"^([=!])\s*#?(\d+)\s*(.*)$", line)
        if m:
            body, refs = _split_refs(m.group(3))
            if m.group(1) == "=":
                out["confirm"].append((int(m.group(2)), refs))
            else:
                out["contest"].append((int(m.group(2)), body, refs))
    return out


def _idea(text: str, n: int = 400) -> str:
    """The idea section of a reference post (what the trade was about), in one line."""
    i = text.find("💡")
    part = text[i:] if i >= 0 else text
    j = part.find("🎯", 1)
    return " ".join((part[:j] if j > 0 else part).split())[:n]


class Learner:
    def __init__(self, journal: Journal, backend=None, pool=None):
        self.journal = journal
        self.backend = backend
        self.pool = pool
        self._block_cache: tuple[float, str] = (0.0, "")
        try:  # reference caller's calls shipped with Bottom → journal (first run only)
            from . import reference

            n = reference.load_bundled(journal.db)
            if n:
                log.info("reference calls loaded: %d", n)
        except Exception as e:  # noqa: BLE001
            log.info("reference load: %s", e)

    def lessons_block(self) -> str:
        """Text appended to the system prompt: track record + active lessons (cached 10 min)."""
        ts, text = self._block_cache
        if time.time() - ts < 600:
            return text
        tr = self.journal.track_record(days=60)
        lessons = self.journal.active_lessons()
        parts = []
        if tr["closed"]:
            parts.append(
                f"Твоя статистика за 60 дней: сетапов {tr['setups']}, закрыто {tr['closed']}, не исполнилось "
                f"{tr['not_filled']}, винрейт исполненных {tr['win_rate_filled_pct']}%, средний результат "
                f"{tr['avg_r_filled']}R. Исходы: {tr['by_outcome']}."
            )
        if lessons:
            parts.append("Уроки из твоих прошлых ошибок и отзывов (соблюдай; спорные — применяй осторожно):\n" + "\n".join(
                f"#{i} {t}" + (" (от админа)" if kind == "manual" else "")
                + (f" [СПОРНО: {reason}]" if status == "contested" else "")
                for i, t, kind, status, reason in lessons))
        try:
            from . import reference

            ref = reference.experience_lines(self.journal.db)
            if ref:
                parts.append("\n".join(ref))
        except Exception as e:  # noqa: BLE001
            log.info("reference stats: %s", e)
        text = ("\n\n# 10. Опыт\n" + "\n".join(parts)) if parts else ""
        self._block_cache = (time.time(), text)
        return text

    def invalidate(self) -> None:
        self._block_cache = (0.0, "")

    async def evaluate_open(self) -> int:
        from ..tools.market.exchanges import ohlcv

        closed = 0
        for st in self.journal.open_setups():
            hours = int((time.time() - st["created"]) / 3600) + 3
            try:
                candles, _meta = await ohlcv(self.pool, st["symbol"], "1h", min(max(hours, 5), 500))
            except Exception as e:  # noqa: BLE001
                log.info("evaluate %s: %s", st["symbol"], e)
                continue
            if not candles:
                continue
            res = evaluate_setup(st, candles)
            self.journal.update_setup(st["id"], res)
            closed += res.get("status") == "closed"
        try:  # the reference caller's calls, a few per run, on the same evaluator
            from . import reference

            if self.pool is not None:
                closed += await reference.evaluate(self.journal.db, self.pool, limit=20)
        except Exception as e:  # noqa: BLE001
            log.info("reference evaluate: %s", e)
        if closed:
            self.invalidate()
        return closed

    async def review_reference(self, batch: int = 40) -> dict | None:
        """Closed reference calls → lessons, the same way as own setups. None when nothing is left to review.

        A case counts on the day the call was made, so a pattern seen on calls of different days can be
        promoted in a single run (one-time self-training over the whole history)."""
        from . import reference

        calls = reference.unreviewed(self.journal.db, batch)
        if not calls:
            return None
        days = {f"R{c['id']}": time.strftime("%Y-%m-%d", time.gmtime(c["created"])) for c in calls}
        lines = ["## Колы референса с исходом"]
        for c in calls:
            day, rep = days[f"R{c['id']}"], c["reported"]
            people = (f" Люди: {rep['pct']}%" if rep.get("pct") else "") + (" Люди: стоп." if rep.get("stop") else "")
            lines.append(
                f"[R{c['id']}] {day} {c['kind']} {c['symbol']} {c['direction']} "
                f"{'лимит' if c['limit_entry'] else 'от текущих'} вход {c['entry_low']}–{c['entry_high']} стоп {c['stop']} "
                f"цели {c['targets']} план R:R {c['rr_tp1']} типы {c['tags']} → {c['outcome']}, {c['r']}R, "
                f"MFE {c['mfe_pct']}%, MAE {c['mae_pct']}%.{people}\n  Идея: {_idea(c['text'])}")
        lines.append("\n## Текущие уроки и кандидаты")
        lines += [f"#{x['id']} ({x['status']}, случаев: {x['cases']}) {x['text']}" for x in self.journal.all_lessons()] \
            or ["(нет)"]
        res = await self.backend.new_session(REFERENCE_REVIEW_PROMPT, [], "\n".join(lines)).step([], allow_tools=False)
        result = self._apply_review(res.text, set(days), days)
        reference.mark_reviewed(self.journal.db, [c["id"] for c in calls])
        self.invalidate()
        log.info("Обучение на референсе: %s", result)
        return result

    def _apply_review(self, text: str, shown: set[str], days: dict[str, str] | None = None) -> dict:
        parsed = {"new": [], "confirm": [], "contest": []}
        if not text.strip().upper().startswith("НЕТ"):
            parsed = parse_review(text)
        # cite, don't invent: only cases actually shown in this review count as evidence
        new = confirmed = promoted = contested = 0
        for body, refs in parsed["new"]:
            refs = [r for r in refs if r in shown]
            if refs:
                lid = self.journal.add_candidate(body, refs, days)  # a candidate still has to earn promotion
                if days and self.journal.promotable(self.journal._lesson(lid)["evidence"]):
                    self.journal.confirm(lid, refs, days)
                    promoted += 1
                new += 1
        for lid, refs in parsed["confirm"]:
            refs = [r for r in refs if r in shown]
            before = (self.journal._lesson(lid) or {}).get("status")
            after = self.journal.confirm(lid, refs, days)
            if after:
                confirmed += 1
                promoted += int(before == "candidate" and after == "active")
        for lid, reason, refs in parsed["contest"]:
            if [r for r in refs if r in shown] and self.journal.contest(lid, reason):
                contested += 1
        return {"new": new, "confirmed": confirmed, "promoted": promoted, "contested": contested}

    async def review(self) -> dict:
        """Daily: closed setups + feedback → candidates, confirmations, contests; then forgetting."""
        setups, fb = self.journal.unreviewed()
        forgotten = self.journal.apply_forgetting()
        if not setups and not fb:
            if forgotten:
                self.invalidate()
            return {"new": 0, "confirmed": 0, "promoted": 0, "contested": 0, "forgotten": forgotten}
        shown = {f"S{s[0]}" for s in setups} | {f"F{f[0]}" for f in fb}
        lines = ["## Сетапы с исходом"]
        for (i, sym, d, lo, hi, stop, tg, rr, outcome, r, mfe, mae, q, _a) in setups:
            lines.append(f"[S{i}] {sym} {d} вход {lo}–{hi} стоп {stop} цели {tg} план R:R {rr} → {outcome}, "
                         f"{r}R, MFE {mfe}%, MAE {mae}%. Вопрос: {q[:200]}")
        lines.append("\n## Жалобы пользователей")
        for (i, text, q, a) in fb:
            lines.append(f"[F{i}] Жалоба: {text}\nВопрос: {(q or '')[:300]}\nОтвет бота: {(a or '')[:700]}")
        lines.append("\n## Текущие уроки и кандидаты")
        lessons = self.journal.all_lessons()
        lines += [f"#{x['id']} ({x['status']}, случаев: {x['cases']}) {x['text']}" for x in lessons] or ["(нет)"]
        sess = self.backend.new_session(REVIEW_PROMPT, [], "\n".join(lines))
        res = await sess.step([], allow_tools=False)
        result = self._apply_review(res.text, shown)  # own cases count as seen today: never promoted on day one
        self.journal.mark_reviewed([s[0] for s in setups], [f[0] for f in fb])
        self.invalidate()
        result["forgotten"] = forgotten
        log.info("Обучение: %s", result)
        return result
