"""Failover: run on the primary model, continue the same question on the fallback when it errors."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from .base import Session, StepResult, ToolCall

if TYPE_CHECKING:
    from ..tools.registry import Tool

log = logging.getLogger(__name__)


def cooldown_for(err: Exception, default: int) -> int:
    """How long to keep the primary model off after an error, in seconds."""
    text = str(err).lower()
    status = getattr(err, "status_code", None)
    if "per day" in text or "daily" in text or "quota" in text:
        return max(default, 3600)  # daily quota: wait longer
    if status == 429 or "rate limit" in text:
        return min(default, 120)  # per-minute limit: retry soon
    return default


class FailoverSession(Session):
    """Runs on the primary model; on any error continues on the fallback.

    Between two OpenAI-compatible backends (e.g. Gemini ↔ OpenRouter) the
    message list is shared, so the tool results already collected are kept.
    Between different formats (Claude ↔ OpenAI-compatible) the question is
    restarted on the fallback.
    """

    def __init__(self, fb: "FailoverBackend", system: str, history: list[tuple[str, str]], user: str):
        self.fb = fb
        self.args = (system, history, user)
        self.backend = fb.fallback if fb.primary_is_down() else fb.primary
        self.sess = self.backend.new_session(*self.args)

    async def step(self, tools: list[Tool], allow_tools: bool) -> StepResult:
        try:
            return await self.sess.step(tools, allow_tools)
        except Exception as e:  # noqa: BLE001
            if self.backend is self.fb.fallback:
                raise
            log.warning("LLM %s failed (%s: %s) → fallback %s", self.backend.name, type(e).__name__,
                        str(e)[:200], self.fb.fallback.name)
            self.fb.mark_primary_down(e)
            self._switch()
            return await self.sess.step(tools, allow_tools)

    def _switch(self) -> None:
        old = self.sess
        self.backend = self.fb.fallback
        self.sess = self.backend.new_session(*self.args)
        if type(old) is type(self.sess) and hasattr(old, "messages"):
            self.sess.messages = old.messages

    def add_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.sess.add_results(results)

    def add_user_note(self, text: str) -> None:
        self.sess.add_user_note(text)


class FailoverBackend:
    def __init__(self, primary, fallback, cooldown_sec: int = 600):
        self.primary = primary
        self.fallback = fallback
        self.cooldown_sec = cooldown_sec
        self.down_until = 0.0
        self.name = f"{primary.name} → {fallback.name}"

    def primary_is_down(self) -> bool:
        return time.monotonic() < self.down_until

    def mark_primary_down(self, err: Exception) -> None:
        self.down_until = time.monotonic() + cooldown_for(err, self.cooldown_sec)

    def new_session(self, system: str, history: list[tuple[str, str]], user: str) -> Session:
        return FailoverSession(self, system, history, user)
