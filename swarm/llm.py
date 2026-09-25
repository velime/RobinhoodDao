"""LLM backends with tool calling.

- OpenAICompatBackend: any OpenAI-compatible Chat Completions API
  (OpenRouter free models, Groq, Gemini's OpenAI endpoint, local Ollama/vLLM).
- AnthropicBackend: Claude via the official Anthropic SDK.

Each backend creates a per-question Session that holds the message list in
the provider's own format.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .config import LLMConfig, Settings
from .tools.registry import Tool

log = logging.getLogger(__name__)


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


# --------------------------------------------------------------------------- OpenAI-compatible


class OpenAICompatSession(Session):
    def __init__(self, backend: "OpenAICompatBackend", system: str, history: list[tuple[str, str]], user: str):
        self.b = backend
        self.messages: list[dict] = [{"role": "system", "content": system}]
        self.messages += [{"role": r, "content": t} for r, t in history]
        self.messages.append({"role": "user", "content": user})

    async def step(self, tools: list[Tool], allow_tools: bool) -> StepResult:
        kwargs: dict = {"model": self.b.model, "messages": self.messages, "temperature": 0.3, "max_tokens": 4000}
        if allow_tools and tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            kwargs["tool_choice"] = "auto"
        resp = await self.b.client.chat.completions.create(**kwargs, extra_headers=self.b.extra_headers)
        if not resp.choices:
            # OpenRouter reports upstream errors as a 200 with no choices
            raise LLMError(f"пустой ответ от {self.b.name}: {getattr(resp, 'error', None)}")
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw) if raw.strip() else {}
                calls.append(ToolCall(tc.id, tc.function.name, args if isinstance(args, dict) else {}))
            except json.JSONDecodeError:
                calls.append(ToolCall(tc.id, tc.function.name, {}, bad_args=raw))
        entry: dict = {"role": "assistant", "content": msg.content or (None if msg.tool_calls else "")}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                for tc in msg.tool_calls
            ]
        self.messages.append(entry)
        return StepResult(text=msg.content or "", calls=calls)

    def add_results(self, results: list[tuple[ToolCall, str]]) -> None:
        for call, content in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    def add_user_note(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})


class OpenAICompatBackend:
    def __init__(self, cfg: LLMConfig):
        from openai import AsyncOpenAI

        if not cfg.model:
            raise ValueError("LLM_MODEL не задан")
        self.name = cfg.name
        self.model = cfg.model
        # few retries: on a rate limit it is better to switch to the fallback quickly
        self.client = AsyncOpenAI(api_key=cfg.api_key or "none", base_url=cfg.base_url, max_retries=1)
        self.extra_headers = (
            {"HTTP-Referer": "https://github.com/velime/RobinhoodDao", "X-Title": "Swarm research bot"}
            if "openrouter" in cfg.base_url
            else {}
        )

    def new_session(self, system: str, history: list[tuple[str, str]], user: str) -> Session:
        return OpenAICompatSession(self, system, history, user)


# --------------------------------------------------------------------------- Anthropic (Claude)

# Models that support the server-side refusal fallback (see Claude API docs).
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")


class AnthropicSession(Session):
    def __init__(self, backend: "AnthropicBackend", system: str, history: list[tuple[str, str]], user: str):
        self.b = backend
        self.system = system
        self.messages: list = [{"role": r, "content": t} for r, t in history]
        self.messages.append({"role": "user", "content": user})

    async def step(self, tools: list[Tool], allow_tools: bool) -> StepResult:
        kwargs: dict = {
            "model": self.b.model,
            "max_tokens": 16000,
            "system": self.system,
            "messages": self.messages,
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
            kwargs["tool_choice"] = {"type": "auto"} if allow_tools else {"type": "none"}
        if self.b.model.startswith(_FALLBACK_MODELS):
            resp = await self.b.client.beta.messages.create(
                **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
        else:
            resp = await self.b.client.messages.create(**kwargs)

        if resp.stop_reason == "refusal":
            return StepResult(text="", refused=True)
        self.messages.append({"role": "assistant", "content": resp.content})
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"]
        return StepResult(text=text, calls=calls)

    def add_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": c.id,
                        "content": content,
                        **({"is_error": True} if content.startswith('{"error"') else {}),
                    }
                    for c, content in results
                ],
            }
        )

    def add_user_note(self, text: str) -> None:
        # Must follow tool results in the same user turn to keep roles alternating.
        last = self.messages[-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            last["content"].append({"type": "text", "text": text})
        else:
            self.messages.append({"role": "user", "content": text})


class AnthropicBackend:
    def __init__(self, cfg: LLMConfig):
        import anthropic

        self.name = cfg.name
        self.model = cfg.model or "claude-opus-5"
        self.client = (
            anthropic.AsyncAnthropic(api_key=cfg.api_key, max_retries=1)
            if cfg.api_key
            else anthropic.AsyncAnthropic(max_retries=1)
        )

    def new_session(self, system: str, history: list[tuple[str, str]], user: str) -> Session:
        return AnthropicSession(self, system, history, user)


# --------------------------------------------------------------------------- Failover


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


def _single(cfg: LLMConfig):
    return AnthropicBackend(cfg) if cfg.provider == "anthropic" else OpenAICompatBackend(cfg)


def make_backend(settings: Settings):
    primary = _single(settings.llm)
    if settings.llm_fallback is None:
        return primary
    return FailoverBackend(primary, _single(settings.llm_fallback), settings.llm_cooldown_sec)
