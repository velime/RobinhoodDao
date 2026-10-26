import json
import os
import stat
import sys
from types import SimpleNamespace

from bottom import claude_code as cc
from bottom.config import RiskRules

FAKE = r'''#!{py}
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
open({log!r}, "a").write(json.dumps({{"args": args, "prompt": prompt}}) + "\n")
def out(x): print(json.dumps(x), flush=True)
out({{"type": "system", "subtype": "init"}})
if "--resume" in args:
    out({{"type": "result", "subtype": "success", "result": "исправлено", "session_id": "s1"}})
    sys.exit(0)
calc = {{"verdict": "OK", "symbol": "BTC", "direction": "long", "entry_zone": [100000, 100500], "stop": 97000,
        "targets": [106000], "rr_to_tp1": 2.0}}
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "mcp__bottom__calc_setup",
     "input": {{"symbol": "BTC"}}}}]}}}})
out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "t1",
     "content": [{{"type": "text", "text": json.dumps(calc)}}]}}]}}}})
out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t2", "name": "WebSearch", "input": {{}}}}]}}}})
out({{"type": "result", "subtype": "success", "session_id": "s1",
     "result": "**🎯 Сетап** лонг BTC: вход 100000–100500, стоп 97000, TP1 106000, R:R 2.0"}})
'''


def _fake(tmp_path):
    log = tmp_path / "calls.jsonl"
    p = tmp_path / "claude.py"
    p.write_text(FAKE.format(py=sys.executable, log=str(log)), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), log


def _settings():
    return SimpleNamespace(risk=RiskRules(), timezone="Europe/Kyiv", max_steps_per_question=12, db_path="none.db",
                           tg_show_private=False)


class _Toolbox:
    def label(self, name):
        return {"calc_setup": "🧮 Считаю сетап"}.get(name, name)


async def test_claude_code_agent(tmp_path, monkeypatch):
    exe, log = _fake(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", exe)
    monkeypatch.setattr(cc, "WORKSPACE", str(tmp_path / "ws"))
    agent = cc.ClaudeCodeAgent(_settings(), _Toolbox())
    ws = tmp_path / "ws"
    assert (ws / "CLAUDE.md").read_text(encoding="utf-8").count("# 11. Как работать в этой папке") == 1
    mcp = json.loads((ws / ".mcp.json").read_text())
    assert mcp["mcpServers"]["bottom"]["args"] == ["-m", "bottom.mcp_server"]
    st = json.loads((ws / ".claude" / "settings.json").read_text())
    assert "Bash" in st["permissions"]["deny"] and st["enableAllProjectMcpServers"]
    statuses = []

    async def on_status(s):
        statuses.append(s)

    r = await agent.answer("дай сетап по BTC", [("user", "привет"), ("assistant", "привет!")], on_status=on_status)
    assert r.text.startswith("**🎯 Сетап**") and r.steps == 2
    assert statuses[:2] == ["🧮 Считаю сетап", "🌐 Ищу в интернете"]
    call = json.loads(log.read_text().splitlines()[0])
    assert "--strict-mcp-config" in call["args"] and "mcp__bottom" in call["args"] and "--max-turns" not in call["args"]
    assert call["args"][call["args"].index("--tools") + 1] == "WebSearch,WebFetch,Read"
    assert "дай сетап по BTC" in call["prompt"] and "Пользователь: привет" in call["prompt"]
    assert len(r.setups) <= 1  # published_setups decides; the run itself must not crash


async def test_claude_code_fallback_and_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(cc, "find_claude", lambda: None)
    monkeypatch.setattr(cc, "WORKSPACE", str(tmp_path / "ws"))

    class FB:
        async def answer(self, q, h, on_status=None, step_budget=None):
            return SimpleNamespace(text="from api", steps=3, setups=[])

    r = await cc.ClaudeCodeAgent(_settings(), _Toolbox(), fallback=FB()).answer("q", [])
    assert r.text == "from api"
    r = await cc.ClaudeCodeAgent(_settings(), _Toolbox()).answer("q", [])
    assert r.text.startswith("⚠️ Claude Code не найден")


async def test_plain_completion_has_no_tools(tmp_path, monkeypatch):
    exe, log = _fake(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", exe)
    monkeypatch.setattr(cc, "WORKSPACE", str(tmp_path / "ws"))
    sess = cc.ClaudeCodeBackend("sonnet").new_session("ты аналитик", [], "разбери ошибки")
    res = await sess.step([], allow_tools=False)
    assert res.text
    args = json.loads(log.read_text().splitlines()[-1])["args"]
    assert args[args.index("--tools") + 1] == "" and "--mcp-config" not in args and args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--system-prompt") + 1] == "ты аналитик"


async def test_file_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "WORKSPACE", str(tmp_path / "ws"))
    note = await cc.FileVision().describe(b"\x89PNG..", "image/png", "график")
    name = note.split("inbox/")[1].split()[0]
    assert os.path.exists(tmp_path / "ws" / "inbox" / name) and "Read" in note and "график" in note


def test_make_vision_and_backend_for_claude_code():
    from bottom.config import LLMConfig
    from bottom.llm import _single
    from bottom.vision import make_vision

    s = SimpleNamespace(vision_llm=None, llm=LLMConfig(provider="claude_code"), llm_fallback=None, vision_daily_limit=5)
    assert isinstance(make_vision(s), cc.FileVision)
    assert _single(LLMConfig(provider="claude_code", model="opus")).name == "claude-code:opus"
