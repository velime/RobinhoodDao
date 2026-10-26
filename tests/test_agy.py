import json
from types import SimpleNamespace

import pytest

from bottom import agy
from bottom.agent import Agent
from bottom.config import LLMConfig, RiskRules
from bottom.llm import _single
from bottom.tools.registry import Tool

# A stand-in for `agy -p`: logs each run, answers by turn. A .py path runs through the current Python,
# so the same fake works on Windows and Linux.
FAKE = r'''
import json, os, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
conv = args[args.index("--conversation") + 1] if "--conversation" in args else None
files = {f: open(f, encoding="utf-8").read() for f in os.listdir(".") if f.endswith(".md")}
with open(__LOG__, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": args, "prompt": prompt, "files": files}, ensure_ascii=False) + "\n")
mode = os.environ.get("FAKE_AGY", "ok")
if mode == "silent":
    sys.exit(0)
if conv is None and "TASK.md" in files and "calc_setup" in files["TASK.md"]:
    resp = '```json\n{"calls": [{"name": "calc_setup", "args": {"symbol": "BTC"}}]}\n```'
elif conv is None:
    resp = "OK"
else:
    resp = json.dumps({"answer": "BTC: цена 100500, сетапа нет"}, ensure_ascii=False)
print(json.dumps({"status": "SUCCESS", "conversation_id": conv or "conv-1", "response": resp}))
'''


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    p = tmp_path / "agy_fake.py"
    p.write_text(FAKE.replace("__LOG__", repr(str(log))), encoding="utf-8")
    monkeypatch.setenv("AGY_PATH", str(p))
    monkeypatch.setattr(agy, "WORK_ROOT", str(tmp_path / "ws"))
    return log


class _Toolbox:
    s = SimpleNamespace(risk=RiskRules())

    def __init__(self):
        self.calls = []

    def schemas(self):
        return [Tool("calc_setup", "проверка сетапа", {"type": "object", "properties": {"symbol": {"type": "string"}}},
                     "🧮", None)]

    def label(self, name):
        return name

    async def execute(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"symbol": "BTC", "price": 100500})


def test_parse_reply():
    calls, text = agy.parse_reply('```json\n{"calls": [{"name": "candles", "args": {"symbol": "ETH"}}, {"x": 1}]}\n```')
    assert [(c.name, c.args) for c in calls] == [("candles", {"symbol": "ETH"})] and text == ""
    calls, text = agy.parse_reply('Вот: {"answer": "готово"}')
    assert not calls and text == "готово"
    calls, text = agy.parse_reply("просто текст без JSON")
    assert not calls and text == "просто текст без JSON"


async def test_agy_agent_loop(fake_agy):
    toolbox = _Toolbox()
    backend = _single(LLMConfig(provider="agy"))
    assert backend.name == "agy"
    agent = Agent(backend, toolbox, "Europe/Kyiv", 6)
    r = await agent.answer("что по BTC?", [("user", "привет")])
    assert r.text == "BTC: цена 100500, сетапа нет" and r.steps == 1
    assert toolbox.calls == [("calc_setup", {"symbol": "BTC"})]
    runs = [json.loads(x) for x in fake_agy.read_text(encoding="utf-8").splitlines()]
    assert len(runs) == 2
    first, second = runs
    assert "--conversation" not in first["args"] and first["args"][-2] == "-p"
    assert "--dangerously-skip-permissions" in first["args"] and first["args"][first["args"].index("--effort") + 1] == "low"
    task = first["files"]["TASK.md"]
    assert "# Протокол ответа" in task and "## calc_setup" in task and "что по BTC?" in task and "Пользователь: привет" in task
    assert second["args"][second["args"].index("--conversation") + 1] == "conv-1"
    assert "Результаты инструментов" in second["prompt"] and "100500" in second["prompt"]


async def test_agy_long_prompt_goes_to_file(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(agy, "MAX_ARG", 50)
    r = await agy.run_agy("x" * 200, str(tmp_path / "ws" / "q"), conversation="conv-1")
    assert r["response"]
    run = json.loads(fake_agy.read_text(encoding="utf-8").splitlines()[0])
    assert "MSG_" in run["prompt"] and any(v == "x" * 200 for v in run["files"].values())


async def test_agy_silent_failure_is_an_error(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_AGY", "silent")
    with pytest.raises(agy.AgyError, match="не JSON"):
        await agy.run_agy("hi", str(tmp_path / "ws" / "q"))


async def test_agy_missing_and_vision(fake_agy, tmp_path, monkeypatch):
    v = agy.AgyVision(agy.AgyBackend(), daily_limit=1)
    assert await v.describe(b"\x89PNG....", "image/png") == "OK"
    run = json.loads(fake_agy.read_text(encoding="utf-8").splitlines()[0])
    assert "image.png" in run["prompt"]
    with pytest.raises(RuntimeError, match="лимит"):
        await v.describe(b"x")
    monkeypatch.setenv("AGY_PATH", "")
    monkeypatch.setattr(agy.shutil, "which", lambda _: None)
    monkeypatch.setattr(agy.os.path, "expanduser", lambda _: str(tmp_path / "nohome"))
    with pytest.raises(agy.AgyError, match="не найден"):
        await agy.run_agy("hi", str(tmp_path / "ws" / "q"))

