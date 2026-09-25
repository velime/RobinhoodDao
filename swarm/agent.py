"""The agent loop: model ↔ tools, with a step budget and progress callbacks."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

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


StatusCallback = Callable[[str], Awaitable[None]]


class Agent:
    def __init__(self, backend, toolbox: Toolbox, tz: str, max_steps: int):
        self.backend = backend
        self.toolbox = toolbox
        self.tz = ZoneInfo(tz)
        self.max_steps = max_steps
        self.system = system_prompt(toolbox.s.risk)

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
        session: Session = self.backend.new_session(self.system, history, f"{self._stamp()}\n{question}")
        tools = self.toolbox.schemas()
        steps = 0
        limit_noted = False

        for _ in range(self.max_steps + 4):
            allow = steps < budget
            if not allow and not limit_noted:
                session.add_user_note(LIMIT_NOTE)
                limit_noted = True
            res = await session.step(tools, allow_tools=allow)
            if res.refused:
                return AgentResult(REFUSED_ANSWER, steps)
            if not res.calls:
                return AgentResult(res.text.strip() or EMPTY_ANSWER, steps)

            runnable = res.calls[: max(budget - steps, 0)]
            skipped = res.calls[len(runnable):]
            if on_status and runnable:
                labels = list(dict.fromkeys(self.toolbox.label(c.name) for c in runnable))
                await on_status(" · ".join(labels))
            outputs = await asyncio.gather(*(self._run(c) for c in runnable))
            results = list(zip(runnable, outputs)) + [
                (c, '{"error":"бюджет шагов исчерпан, инструмент не вызван"}') for c in skipped
            ]
            session.add_results(results)
            steps += len(runnable)
        return AgentResult(EMPTY_ANSWER, steps)

    async def _run(self, call: ToolCall) -> str:
        if call.bad_args is not None:
            return '{"error":"аргументы не являются валидным JSON — повтори вызов"}'
        log.info("tool %s %s", call.name, call.args)
        return await self.toolbox.execute(call.name, call.args)
