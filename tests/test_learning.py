import json

from bottom.agent import Agent
from bottom.config import Settings
from bottom.learning import Journal, Learner, evaluate_setup, parse_review, published_setups
from bottom.llm import Session, StepResult, ToolCall
from bottom.tools.registry import Toolbox

T0 = 1_800_000_000  # setup created at this unix time


def c(i, o, h, l, cl):
    return {"t": (T0 + i * 3600) * 1000, "o": o, "h": h, "l": l, "c": cl, "v": 1}


LONG = {"created": T0 + 1, "direction": "long", "entry_low": 99.0, "entry_high": 100.0, "stop": 95.0,
        "targets": [105.0, 110.0]}


def test_long_stop_first_when_same_candle():
    res = evaluate_setup(LONG, [c(0, 101, 101, 99.5, 100), c(1, 100, 106, 94, 100)], now=T0 + 10 * 3600)
    assert res["outcome"] == "stop" and res["r"] == -1.0


def test_long_tp1_then_breakeven():
    candles = [c(0, 101, 101, 99.8, 100), c(1, 100, 105.5, 99.5, 105), c(2, 105, 105, 99.9, 100)]
    res = evaluate_setup(LONG, candles, now=T0 + 5 * 3600)
    assert res["outcome"] == "tp1_then_be" and res["r"] == 0.5 and res["status"] == "closed"


def test_long_tp2():
    candles = [c(0, 101, 101, 99.9, 100), c(1, 100, 106, 99.9, 105), c(2, 105, 111, 104, 110)]
    res = evaluate_setup(LONG, candles, now=T0 + 5 * 3600)
    assert res["outcome"] == "tp2" and res["r"] == round(0.5 * 1 + 0.5 * 2, 2)


def test_not_filled_and_open():
    far = [c(i, 103, 104, 102, 103) for i in range(80)]
    assert evaluate_setup(LONG, far, now=T0 + 80 * 3600)["outcome"] == "not_filled"
    assert evaluate_setup(LONG, far[:5], now=T0 + 5 * 3600)["status"] == "open"


def test_short_stop():
    short = {"created": T0 + 1, "direction": "short", "entry_low": 50.0, "entry_high": 50.5, "stop": 52.0,
             "targets": [47.0]}
    res = evaluate_setup(short, [c(0, 49, 50.2, 48.9, 50), c(1, 50, 52.5, 49.8, 52)], now=T0 + 3 * 3600)
    assert res["outcome"] == "stop" and res["mae_pct"] > 0


def test_expired_in_trade():
    candles = [c(0, 101, 101, 99.9, 100)] + [c(i, 101, 102, 99, 101) for i in range(1, 200)]
    res = evaluate_setup(LONG, candles, now=T0 + 200 * 3600)
    assert res["outcome"] == "expired" and res["status"] == "closed"


def test_parse_review():
    new, retire = parse_review("+ На мемах стоп ставить за 4ч уровнем, 1ч выбивает шумом\n- #3 не подтвердилось\nмусор")
    assert new == ["На мемах стоп ставить за 4ч уровнем, 1ч выбивает шумом"] and retire == [(3, "не подтвердилось")]


def test_published_setups_only_those_in_answer():
    ok = {"verdict": "OK", "symbol": "RAY", "direction": "long", "stop": 1.373}
    old = {"verdict": "OK", "symbol": "RAY", "direction": "long", "stop": 1.30}
    rej = {"verdict": "REJECT", "symbol": "SOL", "direction": "long", "stop": 130.0}
    got = published_setups([old, ok, rej], "Стоп: $1.373, вход 1.42")
    assert got == [ok]


def test_journal_flow(tmp_path):
    j = Journal(str(tmp_path / "j.db"))
    j.record_message(-5, 10, "u1", "дай сетап RAY", "ответ")
    calc = {"verdict": "OK", "symbol": "ray", "direction": "long", "entry_zone": [99, 100], "stop": 95,
            "targets": [{"target": 105}, {"target": 110}], "rr_tp1": 1.0}
    (sid,) = j.record_setups("u1", -5, 10, "дай сетап RAY", "ответ", [calc])
    assert j.open_setups()[0]["targets"] == [105, 110]
    j.update_setup(sid, {"status": "closed", "outcome": "stop", "r": -1.0, "mfe_pct": 1, "mae_pct": 5})
    tr = j.track_record("RAY")
    assert tr["closed"] == 1 and tr["win_rate_filled_pct"] == 0.0 and tr["avg_r_filled"] == -1.0
    assert j.add_feedback(-5, 10, 7, "стоп слишком близко") is True
    assert j.add_feedback(-5, 999, 7, "?") is False
    lid = j.add_lesson("Ставить стоп за 4ч уровнем на мемах", "manual")
    assert j.active_lessons()[0][0] == lid
    assert j.retire_lesson(lid, "тест") and not j.active_lessons()


class ReviewSession(Session):
    def __init__(self, text):
        self.text = text

    async def step(self, tools, allow_tools):
        return StepResult(self.text)


class ReviewBackend:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def new_session(self, system, history, user):
        self.prompts.append(user)
        return ReviewSession(self.text)


async def test_learner_review_and_prompt_block(tmp_path):
    j = Journal(str(tmp_path / "l.db"))
    old = j.add_lesson("Старый урок, который оказался неверным", "auto")
    j.record_message(1, 2, "u", "q", "a")
    (sid,) = j.record_setups("u", 1, 2, "q", "a", [{"verdict": "OK", "symbol": "PEPE", "direction": "long",
                                                    "entry_zone": [1, 1], "stop": 0.9, "targets": [{"target": 1.2}],
                                                    "rr_tp1": 2}])
    j.update_setup(sid, {"status": "closed", "outcome": "stop", "r": -1.0, "mfe_pct": 0.5, "mae_pct": 12})
    j.add_feedback(1, 2, 5, "вход был в самый хай")
    backend = ReviewBackend(f"+ Не давать вход в верхних 10% суточного диапазона без отката\n- #{old} опровергнут")
    learner = Learner(j, backend)
    res = await learner.review()
    assert res == {"new": 1, "retired": 1}
    assert "PEPE long" in backend.prompts[0] and "вход был в самый хай" in backend.prompts[0]
    block = learner.lessons_block()
    assert "Не давать вход в верхних 10%" in block and "Старый урок" not in block
    assert "винрейт" in block
    assert await learner.review() == {"new": 0, "retired": 0}  # nothing new to review


class SetupSession(Session):
    def __init__(self):
        self.n = 0

    async def step(self, tools, allow_tools):
        self.n += 1
        if self.n == 1:
            return StepResult("", [ToolCall("c1", "calc_setup", {"symbol": "RAY", "direction": "long",
                                                                  "entry_low": 1.42, "stop": 1.373,
                                                                  "targets": [1.484, 1.52]})])
        return StepResult("Лонг RAY: вход 1.42, стоп 1.373, TP1 1.484")

    def add_results(self, results):
        pass

    def add_user_note(self, text):
        pass


class SetupBackend:
    def new_session(self, system, history, user):
        assert "Опыт" in system  # experience block is appended
        return SetupSession()


async def test_agent_returns_published_setups_and_uses_lessons(tmp_path):
    s = Settings(exchanges=())
    j = Journal(str(tmp_path / "a.db"))
    j.add_lesson("Проверяй track_record перед сетапом", "manual")
    tb = Toolbox(s, journal=j)

    async def no_atr(symbol):
        return None

    tb._atr_pct_1h = no_atr
    agent = Agent(SetupBackend(), tb, "Europe/Kyiv", 12, learner=Learner(j))
    res = await agent.answer("дай сетап RAY", [])
    assert len(res.setups) == 1 and res.setups[0]["stop"] == 1.373
    assert "track_record" in tb.tools
    assert json.loads(await tb.execute("track_record", {}))["setups"] == 0
