import asyncio
import json

from bottom import grounding
from bottom.agent import Agent
from bottom.config import Settings
from bottom.llm import Session, StepResult, ToolCall
from bottom.tools import registry
from bottom.tools.registry import Toolbox

TOOLS = ['{"price":68123.4,"oi_usd":{"now":7260000000},"funding_now_pct":0.009,"stop":64900,"target":69800}']


def test_extract_handles_suffixes_times_and_leverage():
    nums = {n["raw"]: n for n in grounding.extract("BTC $68.1K, OI $7.26B, 17:00–18:00, 5x, 2 380 BTC, −3.7%")}
    assert nums["$68.1K"]["value"] == 68100 and nums["$7.26B"]["value"] == 7.26e9
    assert "17" not in nums and nums["5"]["leverage"]
    assert nums["2 380"]["value"] == 2380 and nums["−3.7%"]["value"] == -3.7


def test_grounded_answer_passes():
    missing, checked = grounding.ungrounded("BTC $68.1K, OI $7.26B, фандинг +0.009%, стоп $64.9K, TP1 69.8K", TOOLS)
    assert missing == [] and checked == 5


def test_invented_numbers_flagged():
    missing, checked = grounding.ungrounded("цена $99.5K, OI $1.2B, фандинг 0.5%, стоп 12345, TP 68.1K", TOOLS)
    assert missing == ["$99.5K", "$1.2B", "0.5%", "12345"] and grounding.needs_fix(missing, checked)


def test_few_derived_numbers_tolerated():
    missing, checked = grounding.ungrounded("BTC $68.1K, OI $7.26B, стоп $64.9K, до хая 1.3%", TOOLS)
    assert missing == ["1.3%"] and not grounding.needs_fix(missing, checked)


class InventingSession(Session):
    """Calls one tool, answers with invented numbers, then fixes them after the note."""

    def __init__(self, log):
        self.log, self.n = log, 0

    async def step(self, tools, allow_tools):
        self.n += 1
        if self.n == 1:
            return StepResult("", [ToolCall("a", "market_overview", {"symbol": "BTC"}),
                                   ToolCall("b", "market_overview", {"symbol": "BTC"})])  # duplicate in one turn
        if self.n == 2:
            return StepResult("BTC $99.5K, OI $1.2B, фандинг 0.5%, объём $3.3B")
        return StepResult("BTC $68.1K по Binance")

    def add_results(self, results):
        self.log.append(("results", len(results)))

    def add_user_note(self, text):
        self.log.append(("note", text))


class Backend:
    def __init__(self):
        self.log = []

    def new_session(self, system, history, user):
        return InventingSession(self.log)


async def test_agent_rewrites_ungrounded_answer_once():
    tb = Toolbox(Settings(exchanges=()))
    calls = []

    async def fake_overview(symbol):
        calls.append(symbol)
        return {"median_price": 68123.4}

    tb.tools["market_overview"].run = fake_overview
    backend = Backend()
    statuses = []

    async def on_status(s):
        statuses.append(s)

    res = await Agent(backend, tb, "Europe/Kyiv", 12).answer("что с BTC", [], on_status)
    assert res.text == "BTC $68.1K по Binance"
    assert calls == ["BTC"] and res.steps == 1  # the duplicate call ran once
    notes = [x for x in backend.log if x[0] == "note"]
    assert len(notes) == 1 and "$99.5K" in notes[0][1]
    assert "🔎 Перепроверяю цифры" in statuses


async def test_tool_timeout(monkeypatch):
    monkeypatch.setattr(registry, "TOOL_TIMEOUT", 0.05)
    tb = Toolbox(Settings(exchanges=()))

    async def slow(symbol):
        await asyncio.sleep(1)

    tb.tools["market_overview"].run = slow
    out = json.loads(await tb.execute("market_overview", {"symbol": "BTC"}))
    assert "не ответил" in out["error"]
