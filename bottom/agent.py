"""The agent loop: model ↔ tools, bounded.

Bounds (loop-library / agent-harness): a step budget per question, a hard cap on
model turns, identical tool calls answered from cache instead of re-run, per-tool
timeouts (in Toolbox), a wall-clock limit per question (in the bot), and one
verification pass: numbers in the answer that are not found in the data trigger a
single rewrite.
"""

from __future__ import annotations

import asyncio
import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from . import grounding
from .learning import published_setups
from .llm import Session, ToolCall
from .prompts import system_prompt
from .tools.registry import Toolbox

log = logging.getLogger(__name__)

LIMIT_NOTE = (
    "Бюджет шагов исчерпан — больше инструменты вызывать нельзя. Дай лучший ответ по уже собранным "
    "данным и прямо скажи, что не успел проверить."
)
EMPTY_ANSWER = "Не получилось собрать ответ. Попробуй переформулировать вопрос."
REFUSED_ANSWER = "С этим запросом помочь не могу."


@dataclass
class AgentResult:
    text: str
    steps: int
    setups: list = field(default_factory=list)  # calc_setup OK results that made it into the answer


StatusCallback = Callable[[str], Awaitable[None]]


class Agent:
    def __init__(self, backend, toolbox: Toolbox, tz: str, max_steps: int, learner=None):
        self.backend = backend
        self.toolbox = toolbox
        self.tz = ZoneInfo(tz)
        self.max_steps = max_steps
        self.learner = learner
        self.system = system_prompt(toolbox.s.risk)
        # rules only (not the illustrative examples) count as a legitimate source of numbers
        self.rules_text = self.system.split("# 9. Примеры")[0]

    def system_text(self) -> str:
        """Static rules + (changes about daily) experience block: track record and lessons."""
        return self.system + (self.learner.lessons_block() if self.learner else "")

    def _stamp(self) -> str:
        now = datetime.now(timezone.utc)
        local = now.astimezone(self.tz)
        return f"[Сейчас {local:%Y-%m-%d %H:%M} ({self.tz.key}), {now:%H:%M} UTC]"

    async def answer(
        self,
        question: str,
        history: list[tuple[str, str]],
        on_status: StatusCallback | None = None,
        step_budget: int | None = None,
    ) -> AgentResult:
        budget = min(self.max_steps, step_budget if step_budget is not None else self.max_steps)
        session: Session = self.backend.new_session(self.system_text(), history, f"{self._stamp()}\n{question}")
        tools = self.toolbox.schemas()
        steps = 0
        limit_noted = False
        calcs: list[dict] = []
        outputs_seen: list[str] = []
        cache: dict[str, str] = {}
        verified = False

        for _ in range(self.max_steps + 6):
            allow = steps < budget
            if not allow and not limit_noted:
                session.add_user_note(LIMIT_NOTE)
                limit_noted = True
            res = await session.step(tools, allow_tools=allow)
            if res.refused:
                return AgentResult(REFUSED_ANSWER, steps)
            if not res.calls:
                text = res.text.strip() or EMPTY_ANSWER
                if not verified and text != EMPTY_ANSWER:
                    verified = True  # one verification pass only — bounded
                    missing, checked = grounding.ungrounded(text, outputs_seen + [question, self.rules_text])
                    if grounding.needs_fix(missing, checked):
                        log.info("grounding: %d of %d numbers not in data: %s", len(missing), checked, missing[:8])
                        if on_status:
                            await on_status("🔎 Перепроверяю цифры")
                        session.add_user_note(grounding.fix_note(missing))
                        continue
                return AgentResult(text, steps, published_setups(calcs, text))

            # identical calls are answered from cache and do not spend the budget
            fresh = list({self._key(c): c for c in res.calls if self._key(c) not in cache}.values())
            runnable = fresh[: max(budget - steps, 0)]
            if on_status and runnable:
                labels = list(dict.fromkeys(self.toolbox.label(c.name) for c in runnable))
                await on_status(" · ".join(labels))
            outputs = await asyncio.gather(*(self._run(c) for c in runnable))
            for c, out in zip(runnable, outputs):
                cache[self._key(c)] = out
                outputs_seen.append(out)
                if c.name == "calc_setup":
                    try:
                        calcs.append(json.loads(out))
                    except json.JSONDecodeError:
                        pass
            results = []
            for c in res.calls:
                if self._key(c) in cache:
                    results.append((c, cache[self._key(c)]))
                else:
                    results.append((c, '{"error":"бюджет шагов исчерпан, инструмент не вызван"}'))
            session.add_results(results)
            # a turn with only repeated calls made no progress — it still costs one step
            steps += len(runnable) if runnable else 1
        return AgentResult(EMPTY_ANSWER, steps)

    @staticmethod
    def _key(call: ToolCall) -> str:
        return call.name + json.dumps(call.args, sort_keys=True, ensure_ascii=False, default=str)

    async def _run(self, call: ToolCall) -> str:
        if call.bad_args is not None:
            return '{"error":"аргументы не являются валидным JSON — повтори вызов"}'
        log.info("tool %s %s", call.name, call.args)
        return await self.toolbox.execute(call.name, call.args)
