"""What every model backend speaks: a per-question Session that steps the conversation and takes tool results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..tools.registry import Tool


class LLMError(Exception):
    """Provider returned an unusable response."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    bad_args: str | None = None  # raw text if arguments were not valid JSON


@dataclass
class StepResult:
    text: str
    calls: list[ToolCall] = field(default_factory=list)
    refused: bool = False


class Session:
    async def step(self, tools: list[Tool], allow_tools: bool) -> StepResult:  # pragma: no cover
        raise NotImplementedError

    def add_results(self, results: list[tuple[ToolCall, str]]) -> None:  # pragma: no cover
        raise NotImplementedError

    def add_user_note(self, text: str) -> None:  # pragma: no cover
        raise NotImplementedError
