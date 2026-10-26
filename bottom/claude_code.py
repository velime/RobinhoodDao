"""Bottom on Claude Code — Claude with your subscription, no API key.

How it works:
- claude_workspace/ is Claude's folder: CLAUDE.md (Bottom's rules + how to work there), .mcp.json (one entry
  that connects every Bottom tool through bottom.mcp_server), .claude/settings.json (what it may do),
  live/ (files we keep fresh: parsers' signals, latest Telegram posts, experience and lessons) and
  inbox/ (pictures users send, for Claude to open).
- The Telegram bot runs `claude -p` in that folder for each question (LLM_PROVIDER=claude_code) and streams
  its tool calls into the status line; the answer goes back to the chat. Setups and number checks work as
  with the API models. If Claude Code fails or hits its usage limit, the bot falls back to the API model
  when one is configured (FALLBACK_LLM_* or a second LLM_*).
- claude_chat.bat opens the same folder interactively: talk to Bottom in the terminal with everything
  connected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass

from . import grounding
from .learning import published_setups
from .llm import StepResult

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(ROOT, "claude_workspace")
ALLOWED_TOOLS = ["mcp__bottom", "WebSearch", "WebFetch", "Read(./live/**)", "Read(./inbox/**)"]
WEB_LABELS = {"WebSearch": "🌐 Ищу в интернете", "WebFetch": "📄 Читаю страницу", "Read": "📂 Читаю сводку"}

WORKSPACE_GUIDE = """
# 11. Как работать в этой папке (Claude Code)
- Все данные — через MCP-сервер `bottom` (инструменты mcp__bottom__*): биржи, деривативы, OI, спреды, \
депозиты/выводы, ончейн-потоки, сигналы наших парсеров, Telegram-каналы, X, поиск. Цифры — только оттуда.
- Свежие сводки в `live/` (обновляются ботом): `signals.md` — алерты наших парсеров за последние часы; \
`telegram.md` — последние посты отслеживаемых каналов; `experience.md` — твоя статистика сетапов и уроки \
(обязательны к исполнению, как раздел «Опыт»). Открывай нужный файл, когда вопрос про «что сейчас \
происходит», сигналы, каналы — или перед сетапом (experience.md).
- Картинки от пользователя лежат в `inbox/` — открой файл, который указан в вопросе, и разбери его.
- Интернет (WebSearch, WebFetch) и X — по разделам 2.8 и 3.2: только когда просят или нужен первоисточник.
- Ты ресерчер: не меняй файлы, не запускай команды, не трогай ничего вне этой папки.
- Итог — только текст ответа для Telegram по разделу 6, без вступлений и без пересказа хода работы.
"""


def find_claude() -> str | None:
    if os.getenv("CLAUDE_BIN"):
        return os.getenv("CLAUDE_BIN")
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for p in (os.path.join(home, ".local", "bin", "claude.exe"), os.path.join(home, ".local", "bin", "claude"),
              os.path.join(os.getenv("APPDATA", ""), "npm", "claude.cmd"), os.path.join(home, ".claude", "local", "claude")):
        if p and os.path.exists(p):
            return p
    return None


def write_workspace(rules) -> str:
    """(Re)generate claude_workspace/ — CLAUDE.md, .mcp.json, settings, folders. Returns its path."""
    from .prompts import system_prompt

    os.makedirs(os.path.join(WORKSPACE, ".claude"), exist_ok=True)
    for d in ("live", "inbox"):
        os.makedirs(os.path.join(WORKSPACE, d), exist_ok=True)
    with open(os.path.join(WORKSPACE, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("<!-- Сгенерировано: python -m bottom.claude_code setup. Правки делай в bottom/prompts.py -->\n")
        fh.write(system_prompt(rules) + "\n" + WORKSPACE_GUIDE)
    mcp = {"mcpServers": {"bottom": {"command": sys.executable, "args": ["-m", "bottom.mcp_server"],
                                     "env": {"PYTHONPATH": ROOT, "PYTHONUTF8": "1"}}}}
    with open(os.path.join(WORKSPACE, ".mcp.json"), "w", encoding="utf-8") as fh:
        json.dump(mcp, fh, ensure_ascii=False, indent=2)
    settings = {
        "enableAllProjectMcpServers": True,
        "permissions": {
            "allow": ["mcp__bottom", "WebSearch", "WebFetch", "Read(./live/**)", "Read(./inbox/**)"],
            "deny": ["Bash", "Edit", "Write", "NotebookEdit"],
        },
    }
    with open(os.path.join(WORKSPACE, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
        json.dump(settings, fh, ensure_ascii=False, indent=2)
    return WORKSPACE


# ----------------------------------------------------------------------------- live files


def write_live(settings, learner=None, hours: float = 6) -> None:
    """Fresh summaries Claude reads from live/: signals, Telegram, experience. Local reads only, no network."""
    live = os.path.join(WORKSPACE, "live")
    os.makedirs(live, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [f"# Алерты наших парсеров за {hours:g}ч (на {stamp})", ""]
    db = settings.db_path if os.path.isabs(settings.db_path) else os.path.join(ROOT, settings.db_path)
    if not settings.parser_chat_ids:
        lines.append("группа парсеров не задана (PARSER_CHAT_ID)")
    elif not os.path.exists(db):
        lines.append("Telegram-сборщик ещё не создал базу сообщений")
    else:
        from .tg.store import TgStore
        from .tools import parsergroup

        rows = parsergroup.load_alerts(TgStore(db), settings.parser_chat_ids, hours=hours, limit=300)
        if not rows:
            lines.append("из группы парсеров за период алертов нет")
        for feed in parsergroup.FEEDS:
            fr = [r for r in rows if r["feed"] == feed]
            if not fr:
                continue
            lines.append(f"## {feed} — {len(fr)}")
            for r in fr[:25]:
                age = round((time.time() - r["ts"]) / 60)
                d = r["data"]
                if d.get("board"):  # a row of the pinned spreads board, not a message of its own
                    lines.append(f"- закреп, {age} мин назад: {r['base']} {d.get('cheap', '')} → {d.get('dear', '')} "
                                 f"{d.get('gross_pct')}% (макс {d.get('max_pct')}%, {d.get('held_min')} мин)")
                    continue
                first = (r["text"] or "").split("\n")
                lines.append(f"- {age} мин назад: " + " | ".join(x.strip() for x in first[:4] if x.strip())[:300])
            lines.append("")
    _write(os.path.join(live, "signals.md"), lines)

    tg = [f"# Последние посты Telegram-источников (на {stamp})", ""]
    try:
        from .tg.store import TgStore

        if os.path.exists(os.path.join(ROOT, settings.db_path)):
            res = TgStore(os.path.join(ROOT, settings.db_path)).search("", 6, limit=40, hide_private=not settings.tg_show_private)
            for m in res.get("posts", [])[:40]:
                tg.append(f"- [{m.get('source')}] {m.get('age_hours')}ч: {(m.get('text') or '')[:280]}")
    except Exception as e:  # noqa: BLE001
        tg.append(f"(лента недоступна: {e})")
    _write(os.path.join(live, "telegram.md"), tg)

    exp = [f"# Опыт (на {stamp})", ""]
    if learner is not None:
        exp.append(learner.lessons_block() or "уроков пока нет")
    _write(os.path.join(live, "experience.md"), exp)


def _write(path: str, lines: list[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- running claude


class ClaudeCodeError(RuntimeError):
    pass


async def run_claude(prompt: str, *, model: str = "", on_event=None, timeout: float = 280,
                     resume: str | None = None, allowed: list[str] | None = None, system: str | None = None) -> dict:
    """`claude -p` in the workspace with stream-json; returns {text, session_id, tools:[(name, input, output)]}."""
    exe = find_claude()
    if not exe:
        raise ClaudeCodeError("Claude Code не найден: установи (claude_setup.bat) или укажи CLAUDE_BIN")
    allowed = ALLOWED_TOOLS if allowed is None else allowed
    cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]  # a .py stand-in runs the same on every OS
    cmd += ["-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config"]
    if allowed:  # built-ins limited to web + reading; Bash/Edit/Write are not even available
        cmd += ["--tools", "WebSearch,WebFetch,Read", "--mcp-config", os.path.join(WORKSPACE, ".mcp.json"),
                "--allowedTools", *allowed]
    else:  # plain completion: no tools, no MCP server
        cmd += ["--tools", ""]
    if model:
        cmd += ["--model", model]
    if resume:
        cmd += ["--resume", resume]
    if system:
        cmd += ["--system-prompt", system]
    os.makedirs(WORKSPACE, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=WORKSPACE, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()
    tools: dict[str, list] = {}
    order: list[str] = []
    result: dict = {}

    async def read():
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        tools[c["id"]] = [c.get("name"), c.get("input") or {}, None]
                        order.append(c["id"])
                        if on_event:
                            await on_event(c.get("name"), c.get("input") or {})
            elif t == "user":
                for c in (ev.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in tools:
                        content = c.get("content")
                        if isinstance(content, list):
                            content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                        tools[c["tool_use_id"]][2] = content or ""
            elif t == "result":
                result.update(ev)

    try:
        await asyncio.wait_for(read(), timeout)
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        proc.kill()
        raise ClaudeCodeError("Claude Code не уложился во время")
    if not result:
        err = (await proc.stderr.read()).decode("utf-8", "replace")[-400:]
        raise ClaudeCodeError(f"Claude Code завершился без ответа: {err.strip() or proc.returncode}")
    if result.get("is_error") or result.get("subtype", "success") != "success":
        raise ClaudeCodeError(f"Claude Code: {result.get('subtype')} {str(result.get('result') or '')[:200]}")
    return {"text": (result.get("result") or "").strip(), "session_id": result.get("session_id"),
            "turns": result.get("num_turns"), "tools": [tools[i] for i in order]}


@dataclass
class _Result:
    text: str
    steps: int
    setups: list


class ClaudeCodeAgent:
    """Same interface as Agent.answer; the agent loop runs inside Claude Code."""

    def __init__(self, settings, toolbox, learner=None, fallback=None, model: str = ""):
        self.s, self.toolbox, self.learner, self.fallback = settings, toolbox, learner, fallback
        self.model = model
        self.rules_text = ""
        self._live_at = 0.0
        write_workspace(settings.risk)
        from .prompts import system_prompt

        self.rules_text = system_prompt(settings.risk).split("# 9. Примеры")[0]

    def _label(self, name: str) -> str:
        if name.startswith("mcp__bottom__"):
            return self.toolbox.label(name[len("mcp__bottom__"):])
        return WEB_LABELS.get(name, "🧠 Думаю")

    @staticmethod
    def _prompt(question: str, history: list[tuple[str, str]], stamp: str, budget: int = 12) -> str:
        parts = [stamp]
        if history:
            parts.append("Предыдущий разговор (для контекста):")
            parts += [f"{'Пользователь' if r == 'user' else 'Bottom'}: {t[:1500]}" for r, t in history[-8:]]
        parts += ["", "Вопрос пользователя:", question, "",
                  f"Ответь по правилам из CLAUDE.md, не больше {budget} вызовов инструментов. "
                  "Выведи только текст ответа для Telegram."]
        return "\n".join(parts)

    async def answer(self, question, history, on_status=None, step_budget=None):
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        if time.time() - self._live_at > 60:
            try:
                write_live(self.s, self.learner)
            except Exception as e:  # noqa: BLE001
                log.info("live files: %s", e)
            self._live_at = time.time()
        tz = ZoneInfo(self.s.timezone)
        now = datetime.now(timezone.utc)
        stamp = f"[Сейчас {now.astimezone(tz):%Y-%m-%d %H:%M} ({self.s.timezone}), {now:%H:%M} UTC]"
        budget = min(self.s.max_steps_per_question, step_budget or self.s.max_steps_per_question)

        async def on_event(name, args):
            if on_status:
                await on_status(self._label(name))

        try:
            r = await run_claude(self._prompt(question, history, stamp, budget), model=self.model, on_event=on_event)
            outputs = [o for _, _, o in r["tools"] if o]
            missing, checked = grounding.ungrounded(r["text"], outputs + [question, self.rules_text])
            if grounding.needs_fix(missing, checked) and r.get("session_id"):
                if on_status:
                    await on_status("🔎 Перепроверяю цифры")
                r2 = await run_claude(grounding.fix_note(missing), model=self.model, on_event=on_event,
                                      resume=r["session_id"])
                r = {**r2, "tools": r["tools"] + r2["tools"]}
        except ClaudeCodeError as e:
            log.warning("claude code failed: %s", e)
            if self.fallback is not None:
                if on_status:
                    await on_status("🔁 Переключаюсь на запасную модель")
                return await self.fallback.answer(question, history, on_status=on_status, step_budget=step_budget)
            return _Result(f"⚠️ {e}", 0, [])
        calcs = []
        for name, _, out in r["tools"]:
            if name.endswith("calc_setup") and out:
                try:
                    calcs.append(json.loads(out))
                except json.JSONDecodeError:
                    pass
        steps = sum(1 for name, _, _ in r["tools"] if not name.startswith("Read"))
        return _Result(r["text"] or "Не получилось сформулировать ответ — попробуй переформулировать.", steps,
                       published_setups(calcs, r["text"]))


class _CCSession:
    """Plain completion through Claude Code (used by the daily lessons review). No tools."""

    def __init__(self, system: str, history, user: str, model: str):
        self.system, self.user, self.model = system, user, model
        self.notes: list[str] = []

    async def step(self, tools, allow_tools: bool) -> StepResult:
        prompt = "\n\n".join([self.user, *self.notes])
        r = await run_claude(prompt, model=self.model, allowed=[], system=self.system)
        return StepResult(r["text"])

    def add_results(self, results) -> None:
        pass

    def add_user_note(self, text: str) -> None:
        self.notes.append(text)


class ClaudeCodeBackend:
    def __init__(self, model: str = ""):
        self.model = model
        self.name = f"claude-code{':' + model if model else ''}"

    def new_session(self, system: str, history, user: str) -> _CCSession:
        return _CCSession(system, history, user, self.model)


class FileVision:
    """Claude Code reads images itself: save the picture into inbox/ and tell it where it is."""

    enabled = True

    def remaining(self) -> int:
        return 10**6

    async def describe(self, data: bytes, mime: str = "image/jpeg", context: str = "") -> str:
        os.makedirs(os.path.join(WORKSPACE, "inbox"), exist_ok=True)
        ext = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
        name = f"img_{int(time.time() * 1000)}.{ext}"
        with open(os.path.join(WORKSPACE, "inbox", name), "wb") as fh:
            fh.write(data)
        self._prune()
        return f"картинка сохранена в inbox/{name} — открой её через Read и разбери" + (f" (подпись: {context})" if context else "")

    async def describe_url(self, url: str, context: str = "") -> str:
        from .tools import http

        r = await http.client().get(url)
        r.raise_for_status()
        return await self.describe(r.content, r.headers.get("content-type", "image/jpeg"), context)

    @staticmethod
    def _prune(keep_hours: float = 48) -> None:
        d = os.path.join(WORKSPACE, "inbox")
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if f.startswith("img_") and time.time() - os.path.getmtime(p) > keep_hours * 3600:
                os.remove(p)


def main() -> None:
    """`python -m bottom.claude_code setup` — write the workspace; `live` — refresh live files."""
    from .config import load_settings

    os.chdir(ROOT)
    s = load_settings()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "setup"
    if cmd in ("setup", "live"):
        write_workspace(s.risk)
        write_live(s)
        exe = find_claude()
        print(f"Папка Claude Code: {WORKSPACE}")
        print("Claude Code: " + (exe or "НЕ НАЙДЕН — запусти claude_setup.bat"))


if __name__ == "__main__":
    main()
