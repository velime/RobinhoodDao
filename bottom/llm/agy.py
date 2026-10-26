"""Bottom on Gemini through Antigravity CLI (`agy`) — Google AI Pro subscription, no API key.

The same idea as github.com/velime/agy-gemini-mcp, but here Bottom is the boss: agy is used only as
the model, and Bottom's own agent loop runs the tools, checks the numbers and validates setups.

How a question goes:
- Each question gets its own folder under agy_workspace/ (the only place agy works in). The first
  turn writes TASK.md there: Bottom's rules, the tool list with JSON schemas, the reply protocol,
  the conversation and the question.
- Gemini replies with one JSON object: {"calls": [...]} to ask for tools or {"answer": "..."}.
  Bottom runs the tools and sends the results back into the same agy conversation
  (--conversation <id>), in a file when they are long.
- agy's output goes to a file, not a pipe: agy print mode on Windows can return nothing on a pipe.
  "Exit 0 but no JSON" is treated as an error (quota, sign-in or region), so the fallback model
  (FALLBACK_LLM_*) takes over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid

from ..config import ROOT
from .base import LLMError, Session, StepResult, ToolCall

log = logging.getLogger(__name__)

WORK_ROOT = os.path.join(ROOT, "agy_workspace")
MAX_ARG = 5000  # longer prompts go through a file (Windows command line limit)
KEEP_HOURS = 24  # question folders older than this are deleted

PROTOCOL = """
# Протокол ответа (обязателен)
Ты работаешь как модель внутри бота Bottom. Свои инструменты (терминал, правка файлов, поиск, браузер) \
не используй: файлы в этой папке только читай. Данные даёт только Bottom через инструменты из списка ниже.
Каждый твой ответ — ровно один JSON-объект, без текста вокруг:
- нужны данные: {"calls": [{"name": "<инструмент>", "args": {...}}, ...]} — можно несколько вызовов сразу, \
они выполнятся параллельно; запрашивай всё нужное одним списком, каждый раунд стоит времени;
- данные собраны: {"answer": "<готовый текст ответа для Telegram по разделу 6>"}.
Результаты инструментов придут следующим сообщением (или в файле, который в нём указан).
"""


def find_agy() -> str | None:
    if os.getenv("AGY_PATH"):
        return os.getenv("AGY_PATH")
    found = shutil.which("agy")
    if found:
        return found
    p = os.path.join(os.path.expanduser("~"), "AppData", "Local", "agy", "bin", "agy.exe")
    return p if os.path.exists(p) else None


class AgyError(LLMError):
    pass


async def run_agy(prompt: str, cwd: str, *, conversation: str | None = None, model: str = "", effort: str = "",
                  timeout_min: float = 5) -> dict:
    """One `agy -p` turn in `cwd`; returns {"response", "conversation_id", "usage"}."""
    exe = find_agy()
    if not exe:
        raise AgyError("agy не найден: установи Antigravity CLI (antigravity.google) или укажи AGY_PATH")
    os.makedirs(cwd, exist_ok=True)
    if len(prompt) > MAX_ARG:
        name = f"MSG_{int(time.time() * 1000)}.md"
        with open(os.path.join(cwd, name), "w", encoding="utf-8") as fh:
            fh.write(prompt)
        prompt = f"Прочитай файл {name} в текущей папке и ответь строго по протоколу из TASK.md."
    cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]
    # agy reads the task files with its own tools; without auto-approve a headless run would wait forever
    cmd += ["--output-format", "json", "--dangerously-skip-permissions"]
    if conversation:
        cmd += ["--conversation", conversation]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--print-timeout", f"{max(int(timeout_min), 1)}m", "-p", prompt]  # -p must be last
    out_path, err_path = os.path.join(cwd, ".agy.out"), os.path.join(cwd, ".agy.err")
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=out, stderr=err)
        try:
            await asyncio.wait_for(proc.wait(), timeout_min * 60 + 30)
        except asyncio.TimeoutError:
            proc.kill()
            raise AgyError("agy не уложился во время")
    raw = _read(out_path)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        tail = (_read(err_path)[-300:] or raw[-300:]).strip()
        raise AgyError(f"agy вернул не JSON (квота, вход в аккаунт или регион?): {tail or proc.returncode}")
    text = (parsed.get("response") or "").strip()
    if not text:  # status ERROR with a complete answer happens after API stalls — only an empty one fails
        raise AgyError(f"agy: {parsed.get('status')} {str(parsed.get('error') or '')[:200]}")
    return {"response": text, "conversation_id": parsed.get("conversation_id"), "usage": parsed.get("usage")}


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def parse_reply(text: str) -> tuple[list[ToolCall], str]:
    """Gemini's reply → (tool calls, answer). Text that is not our JSON counts as the answer."""
    obj = None
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    for chunk in ([fenced.group(1)] if fenced else []) + [text[text.find("{"): text.rfind("}") + 1]]:
        try:
            obj = json.loads(chunk)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not isinstance(obj, dict) or not ("calls" in obj or "answer" in obj):
        return [], text.strip()
    calls = []
    for i, c in enumerate(obj.get("calls") or []):
        if isinstance(c, dict) and c.get("name"):
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            calls.append(ToolCall(f"c{uuid.uuid4().hex[:8]}{i}", str(c["name"]), args))
    return calls, str(obj.get("answer") or "").strip()


def _tool_catalog(tools) -> str:
    lines = ["# Инструменты Bottom"]
    for t in tools:
        lines.append(f"## {t.name}\n{t.description}\nargs (JSON Schema): "
                     + json.dumps(t.parameters, ensure_ascii=False, separators=(",", ":")))
    return "\n\n".join(lines)


def _prune() -> None:
    if not os.path.isdir(WORK_ROOT):
        return
    for d in os.listdir(WORK_ROOT):
        p = os.path.join(WORK_ROOT, d)
        try:
            if os.path.isdir(p) and time.time() - os.path.getmtime(p) > KEEP_HOURS * 3600:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass


class AgySession(Session):
    def __init__(self, backend: "AgyBackend", system: str, history: list[tuple[str, str]], user: str):
        self.b = backend
        self.system, self.history, self.user = system, history, user
        self.cwd = os.path.join(WORK_ROOT, f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
        self.conversation: str | None = None
        self.pending: list[str] = []  # tool results and notes for the next turn

    def _task(self, tools) -> str:
        parts = [self.system, PROTOCOL if tools else "", _tool_catalog(tools) if tools else ""]
        if self.history:
            parts.append("# Предыдущий разговор\n" + "\n".join(
                f"{'Пользователь' if r == 'user' else 'Bottom'}: {t[:1500]}" for r, t in self.history[-8:]))
        parts.append("# Вопрос пользователя\n" + self.user)
        return "\n\n".join(p for p in parts if p)

    async def step(self, tools, allow_tools: bool) -> StepResult:
        if self.conversation is None:
            os.makedirs(self.cwd, exist_ok=True)
            with open(os.path.join(self.cwd, "TASK.md"), "w", encoding="utf-8") as fh:
                fh.write(self._task(tools))
            prompt = ("Прочитай TASK.md в текущей папке и выполни его. Ответ — строго один JSON-объект по протоколу."
                      if tools else "Прочитай TASK.md в текущей папке и ответь на вопрос из него.")
            _prune()
        else:
            if not allow_tools:
                self.pending.append('Инструменты больше вызывать нельзя — верни {"answer": "..."}.')
            prompt = "\n\n".join(self.pending)
        self.pending = []
        r = await run_agy(prompt, self.cwd, conversation=self.conversation, model=self.b.model,
                          effort=self.b.effort, timeout_min=self.b.timeout_min)
        self.conversation = r.get("conversation_id") or self.conversation
        if not tools:
            return StepResult(r["response"])
        calls, answer = parse_reply(r["response"])
        if not allow_tools:
            calls = []
        return StepResult(text=answer, calls=calls)

    def add_results(self, results: list[tuple[ToolCall, str]]) -> None:
        self.pending.append("Результаты инструментов:\n" + "\n\n".join(
            f"### {c.name} {json.dumps(c.args, ensure_ascii=False, default=str)}\n{out}" for c, out in results))

    def add_user_note(self, text: str) -> None:
        self.pending.append(text)


class AgyBackend:
    def __init__(self, model: str = "", effort: str | None = None, timeout_min: float | None = None):
        self.model = model
        self.effort = os.getenv("AGY_EFFORT", "low") if effort is None else effort
        self.timeout_min = float(os.getenv("AGY_TIMEOUT_MIN", "5")) if timeout_min is None else timeout_min
        self.name = f"agy{':' + model if model else ''}"

    def new_session(self, system: str, history, user: str) -> AgySession:
        return AgySession(self, system, history, user)


class AgyVision:
    """Gemini looks at the picture itself: save it next to the task and let agy open it."""

    enabled = True

    def __init__(self, backend: AgyBackend, daily_limit: int = 300):
        self.b, self.daily_limit = backend, daily_limit
        self._day, self._used = "", 0

    def remaining(self) -> int:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self._day:
            self._day, self._used = day, 0
        return max(self.daily_limit - self._used, 0)

    async def describe(self, data: bytes, mime: str = "image/jpeg", context: str = "") -> str:
        from ..vision import IMAGE_PROMPT, MAX_IMAGE_BYTES

        if self.remaining() <= 0:
            raise RuntimeError("дневной лимит разбора картинок исчерпан")
        if len(data) > MAX_IMAGE_BYTES:
            raise RuntimeError("картинка слишком большая")
        self._used += 1
        cwd = os.path.join(WORK_ROOT, f"img-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
        os.makedirs(cwd, exist_ok=True)
        name = "image." + {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
        with open(os.path.join(cwd, name), "wb") as fh:
            fh.write(data)
        prompt = (f"Открой файл {name} в текущей папке (только посмотри, ничего не меняй) и ответь текстом.\n\n"
                  + IMAGE_PROMPT + (f"\n\nКонтекст (подпись к посту): {context[:500]}" if context else ""))
        try:
            r = await run_agy(prompt, cwd, model=self.b.model, effort="low", timeout_min=self.b.timeout_min)
        except AgyError as e:
            raise RuntimeError(f"не удалось разобрать картинку: {e}") from e
        finally:
            _prune()
        return r["response"]

    async def describe_url(self, url: str, context: str = "") -> str:
        from ..tools import http

        r = await http.client().get(url)
        r.raise_for_status()
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if not mime.startswith("image/"):
            raise RuntimeError(f"по ссылке не картинка ({mime})")
        return await self.describe(r.content, mime, context)
