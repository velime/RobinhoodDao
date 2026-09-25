from types import SimpleNamespace

from swarm.config import Settings
from swarm.llm import AnthropicSession, OpenAICompatSession
from swarm.prompts import FAQ
from swarm.tools.exchanges import find_market
from swarm.tools.registry import Toolbox


class FakeCompletions:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def _oa_msg(content, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


async def test_openai_session_roundtrip():
    tc = SimpleNamespace(id="t1", function=SimpleNamespace(name="market_overview", arguments='{"symbol":"BTC"}'))
    bad = SimpleNamespace(id="t2", function=SimpleNamespace(name="candles", arguments="{oops"))
    comp = FakeCompletions([_oa_msg(None, [tc, bad]), _oa_msg("ответ")])
    backend = SimpleNamespace(model="m", client=SimpleNamespace(chat=SimpleNamespace(completions=comp)), extra_headers={})
    tools = Toolbox(Settings(exchanges=())).schemas()

    s = OpenAICompatSession(backend, "SYS", [("user", "q0"), ("assistant", "a0")], "q1")
    r = await s.step(tools, allow_tools=True)
    assert [c.name for c in r.calls] == ["market_overview", "candles"]
    assert r.calls[0].args == {"symbol": "BTC"} and r.calls[1].bad_args == "{oops"
    assert comp.calls[0]["tool_choice"] == "auto"
    assert comp.calls[0]["tools"][0]["type"] == "function"
    s.add_results([(r.calls[0], "{}"), (r.calls[1], "{}")])
    r2 = await s.step(tools, allow_tools=False)
    assert r2.text == "ответ" and "tools" not in comp.calls[1]
    roles = [m["role"] for m in s.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant", "tool", "tool", "assistant"]


class FakeAnthropicMessages:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


async def test_anthropic_session_roundtrip():
    tool_use = SimpleNamespace(type="tool_use", id="tu1", name="derivatives", input={"symbol": "ETH"})
    resp1 = SimpleNamespace(stop_reason="tool_use", content=[tool_use])
    resp2 = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="итог")])
    msgs = FakeAnthropicMessages([resp1, resp2])
    backend = SimpleNamespace(model="claude-sonnet-5", client=SimpleNamespace(messages=msgs))
    tools = Toolbox(Settings(exchanges=())).schemas()

    s = AnthropicSession(backend, "SYS", [], "q")
    r = await s.step(tools, allow_tools=True)
    assert r.calls[0].name == "derivatives" and r.calls[0].args == {"symbol": "ETH"}
    assert msgs.calls[0]["tools"][0]["input_schema"]["type"] == "object"
    s.add_results([(r.calls[0], '{"error":"x"}')])
    s.add_user_note("лимит")
    assert s.messages[-1]["content"][0]["is_error"] is True
    assert s.messages[-1]["content"][-1] == {"type": "text", "text": "лимит"}
    r2 = await s.step(tools, allow_tools=False)
    assert r2.text == "итог" and msgs.calls[1]["tool_choice"] == {"type": "none"}


async def test_anthropic_refusal():
    msgs = FakeAnthropicMessages([SimpleNamespace(stop_reason="refusal", content=[])])
    backend = SimpleNamespace(model="claude-sonnet-5", client=SimpleNamespace(messages=msgs))
    r = await AnthropicSession(backend, "S", [], "q").step([], True)
    assert r.refused


def test_find_market_handles_multiplier_and_types():
    ex = SimpleNamespace(markets={
        "PEPE/USDT": {"symbol": "PEPE/USDT", "base": "PEPE", "quote": "USDT", "spot": True, "active": True},
        "1000PEPE/USDT:USDT": {"symbol": "1000PEPE/USDT:USDT", "base": "1000PEPE", "quote": "USDT",
                               "swap": True, "linear": True, "active": True, "id": "1000PEPEUSDT"},
        "HYPE/USDC:USDC": {"symbol": "HYPE/USDC:USDC", "base": "HYPE", "quote": "USDC", "swap": True, "linear": True},
    })
    m, mult = find_market(ex, "PEPE", "perp")
    assert m["id"] == "1000PEPEUSDT" and mult == 1000
    m, mult = find_market(ex, "PEPE", "spot")
    assert m["symbol"] == "PEPE/USDT" and mult == 1
    m, _ = find_market(ex, "HYPE", "perp")
    assert m["quote"] == "USDC"
    assert find_market(ex, "BTC", "perp") is None


def test_faq_formats():
    assert "50 шагов" in FAQ.format(daily=50)
