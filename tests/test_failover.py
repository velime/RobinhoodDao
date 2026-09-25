from types import SimpleNamespace

import pytest

from swarm.config import LLMConfig, Settings, load_settings
from swarm.llm import (
    FailoverBackend,
    OpenAICompatBackend,
    OpenAICompatSession,
    cooldown_for,
    make_backend,
)


class RateLimited(Exception):
    status_code = 429


class FlakyCompletions:
    """Fails `fail_times` times, then answers."""

    def __init__(self, fail_times, answer="ok", exc=None):
        self.fail_times = fail_times
        self.answer = answer
        self.exc = exc or RateLimited("rate limit exceeded")
        self.calls = 0

    async def create(self, **kw):
        self.calls += 1
        self.last_messages = list(kw["messages"])
        if self.calls <= self.fail_times:
            raise self.exc
        msg = SimpleNamespace(content=self.answer, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _backend(name, completions):
    b = OpenAICompatBackend.__new__(OpenAICompatBackend)
    b.name, b.model, b.extra_headers = name, name, {}
    b.client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return b


async def test_switches_to_fallback_and_keeps_messages():
    prim = FlakyCompletions(fail_times=99)
    fall = FlakyCompletions(fail_times=0, answer="из запасной")
    fb = FailoverBackend(_backend("gemini", prim), _backend("openrouter", fall), cooldown_sec=600)

    s = fb.new_session("SYS", [("user", "q0"), ("assistant", "a0")], "q1")
    s.sess.messages.append({"role": "tool", "tool_call_id": "t1", "content": "{}"})  # collected data
    r = await s.step([], allow_tools=True)
    assert r.text == "из запасной"
    assert fall.last_messages[-1]["role"] == "tool"  # tool results carried over
    assert isinstance(s.sess, OpenAICompatSession) and s.backend.name == "openrouter"

    # primary stays in cooldown: next question starts on the fallback directly
    s2 = fb.new_session("SYS", [], "q2")
    assert s2.backend.name == "openrouter"
    await s2.step([], allow_tools=True)
    assert prim.calls == 1


async def test_primary_recovers_after_cooldown():
    prim = FlakyCompletions(fail_times=1, answer="основная")
    fall = FlakyCompletions(fail_times=0, answer="запасная")
    fb = FailoverBackend(_backend("p", prim), _backend("f", fall))
    assert (await fb.new_session("S", [], "q").step([], True)).text == "запасная"
    fb.down_until = 0  # cooldown over
    assert (await fb.new_session("S", [], "q").step([], True)).text == "основная"


async def test_fallback_error_propagates():
    fb = FailoverBackend(_backend("p", FlakyCompletions(99)), _backend("f", FlakyCompletions(99)))
    with pytest.raises(RateLimited):
        await fb.new_session("S", [], "q").step([], True)


async def test_empty_choices_triggers_fallback():
    class Empty:
        async def create(self, **kw):
            return SimpleNamespace(choices=[], error={"message": "upstream"})

    fb = FailoverBackend(_backend("p", Empty()), _backend("f", FlakyCompletions(0, "ок")))
    assert (await fb.new_session("S", [], "q").step([], True)).text == "ок"


def test_cooldown_classification():
    assert cooldown_for(RateLimited("Too many requests per minute"), 600) == 120
    assert cooldown_for(Exception("You exceeded your current quota"), 600) == 3600
    assert cooldown_for(Exception("503 upstream"), 600) == 600


def test_env_config(monkeypatch):
    for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY", "FALLBACK_LLM_MODEL",
              "FALLBACK_LLM_BASE_URL", "FALLBACK_LLM_API_KEY", "FALLBACK_LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
    monkeypatch.setenv("LLM_MODEL", "gemini-flash")
    monkeypatch.setenv("LLM_API_KEY", "g")
    monkeypatch.setenv("FALLBACK_LLM_MODEL", "z-ai/glm-5.2:free")
    monkeypatch.setenv("FALLBACK_LLM_API_KEY", "o")
    s = load_settings()
    assert s.llm.model == "gemini-flash" and s.llm.base_url.startswith("https://generativelanguage")
    assert s.llm_fallback.base_url == "https://openrouter.ai/api/v1"
    b = make_backend(s)
    assert isinstance(b, FailoverBackend) and b.name == "openai:gemini-flash → openai:z-ai/glm-5.2:free"


def test_no_fallback_configured():
    b = make_backend(Settings(llm=LLMConfig(model="m", base_url="https://x")))
    assert isinstance(b, OpenAICompatBackend)
