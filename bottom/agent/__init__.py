"""The brain: the bounded agent loop (model ↔ tools), the rules it follows and the number check on its answers."""

from .loop import EMPTY_ANSWER, LIMIT_NOTE, REFUSED_ANSWER, Agent, AgentResult

__all__ = ["Agent", "AgentResult", "EMPTY_ANSWER", "LIMIT_NOTE", "REFUSED_ANSWER"]
