import json
from types import SimpleNamespace

from aiogram.enums import ChatType

from swarm.agent import LIMIT_NOTE, Agent
from swarm.bot import extract_question
from swarm.config import Settings
from swarm.llm import Session, StepResult, ToolCall
from swarm.storage import Storage
from swarm.tools.registry import Toolbox


class ScriptedSession(Session):
    """Calls market_overview on every step until tools are disallowed."""

    def __init__(self, log):
        self.log = log
        self.i = 0

    async def step(self, tools, allow_tools):
        self.log.append(("step", allow_tools))
        if allow_tools:
            self.i += 1
            return StepResult("", [ToolCall(f"c{self.i}a", "market_overview", {"symbol": "BTC"}),
                                   ToolCall(f"c{self.i}b", "calc_setup", {"bad": 1})])
        return StepResult("готово")

    def add_results(self, results):
        self.log.append(("results", [json.loads(r)["error"][:10] if "error" in r else "ok" for _, r in results]))

    def add_user_note(self, text):
        self.log.append(("note", text))


class FakeBackend:
    def __init__(self):
        self.log = []

    def new_session(self, system, history, user):
        assert "Swarm" in system and "[Сейчас" in user
        return ScriptedSession(self.log)


async def test_agent_respects_budget():
    tb = Toolbox(Settings(exchanges=()))

    async def fake_overview(symbol):
        return {"base": symbol}

    tb.tools["market_overview"].run = fake_overview
    backend = FakeBackend()
    agent = Agent(backend, tb, "Europe/Kyiv", max_steps=12)
    statuses = []

    async def on_status(s):
        statuses.append(s)

    res = await agent.answer("разбери BTC", [], on_status, step_budget=3)
    assert res.text == "готово"
    assert res.steps == 3
    assert ("note", LIMIT_NOTE) in backend.log
    assert statuses and "Сверяю цены" in statuses[0]


async def test_toolbox_bad_args_and_schemas():
    tb = Toolbox(Settings(exchanges=()))
    out = json.loads(await tb.execute("calc_setup", {"nope": 1}))
    assert "неверные аргументы" in out["error"]
    out = json.loads(await tb.execute("no_such_tool", {}))
    assert "нет инструмента" in out["error"]
    for t in tb.schemas():
        assert set(t.parameters["required"]) <= set(t.parameters["properties"]), t.name
    # optional tools are hidden without keys
    assert "x_discussion" not in tb.tools and "web_search" not in tb.tools
    tb2 = Toolbox(Settings(exchanges=(), twitterapi_io_key="k", tavily_api_key="k"))
    assert {"x_discussion", "x_influencers", "web_search"} <= set(tb2.tools)


def _msg(text, chat_type, reply_from_id=None):
    reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_from_id)) if reply_from_id else None
    return SimpleNamespace(text=text, caption=None, chat=SimpleNamespace(type=chat_type), reply_to_message=reply)


def test_extract_question():
    assert extract_question(_msg("что с BTC", ChatType.PRIVATE), "swarm_bot", 1) == "что с BTC"
    assert extract_question(_msg("что с BTC", ChatType.SUPERGROUP), "swarm_bot", 1) is None
    assert extract_question(_msg("@Swarm_Bot что с BTC", ChatType.SUPERGROUP), "swarm_bot", 1) == "что с BTC"
    assert extract_question(_msg("а лонг?", ChatType.GROUP, reply_from_id=1), "swarm_bot", 1) == "а лонг?"
    assert extract_question(_msg("а лонг?", ChatType.GROUP, reply_from_id=2), "swarm_bot", 1) is None


def test_storage(tmp_path):
    st = Storage(str(tmp_path / "t.db"))
    for i in range(10):
        st.add_turn("u1", f"q{i}", f"a{i}")
    h = st.history("u1", turns=3)
    assert h[0] == ("user", "q7") and h[-1] == ("assistant", "a9") and len(h) == 6
    st.add_steps(5, 4)
    st.add_steps(5, 3)
    assert st.steps_used(5) == 7
