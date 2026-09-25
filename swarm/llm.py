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
from dataclasses import dataclass, field

from .config import Settings
from .tools.registry import Tool

log = logging.getLogger(__name__)


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
            return StepResult(text="")
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
    def __init__(self, settings: Settings):
        from openai import AsyncOpenAI

        if not settings.llm_model:
            raise ValueError("LLM_MODEL не задан")
        self.model = settings.llm_model
        self.client = AsyncOpenAI(api_key=settings.llm_api_key or "none", base_url=settings.llm_base_url)
        self.extra_headers = (
            {"HTTP-Referer": "https://github.com/velime/RobinhoodDao", "X-Title": "Swarm research bot"}
            if "openrouter" in settings.llm_base_url
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
    def __init__(self, settings: Settings):
        import anthropic

        self.model = settings.llm_model or "claude-opus-5"
        self.client = (
            anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
            if settings.anthropic_api_key
            else anthropic.AsyncAnthropic()
        )

    def new_session(self, system: str, history: list[tuple[str, str]], user: str) -> Session:
        return AnthropicSession(self, system, history, user)


def make_backend(settings: Settings):
    if settings.llm_provider == "anthropic":
        return AnthropicBackend(settings)
    return OpenAICompatBackend(settings)
