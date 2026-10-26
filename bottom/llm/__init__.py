"""Model backends. LLM_PROVIDER picks one: openai (any OpenAI-compatible API), anthropic, agy or claude_code.

    single(cfg)          one backend for one config
    make_backend(s)      the main model, wrapped with the fallback when FALLBACK_LLM_* is set
"""

from __future__ import annotations

from ..config import LLMConfig, Settings
from .base import LLMError, Session, StepResult, ToolCall
from .failover import FailoverBackend, FailoverSession, cooldown_for

__all__ = ["LLMError", "Session", "StepResult", "ToolCall", "FailoverBackend", "FailoverSession", "cooldown_for",
           "single", "make_backend"]


def single(cfg: LLMConfig):
    if cfg.provider == "claude_code":
        from .claude_code import ClaudeCodeBackend

        return ClaudeCodeBackend(cfg.model)
    if cfg.provider == "agy":
        from .agy import AgyBackend

        return AgyBackend(cfg.model)
    from .api import AnthropicBackend, OpenAICompatBackend

    return AnthropicBackend(cfg) if cfg.provider == "anthropic" else OpenAICompatBackend(cfg)


def make_backend(settings: Settings):
    primary = single(settings.llm)
    if settings.llm_fallback is None:
        return primary
    return FailoverBackend(primary, single(settings.llm_fallback), settings.llm_cooldown_sec)
