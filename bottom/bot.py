"""Telegram bot: private chats, group mentions/replies, progress statuses."""

from __future__ import annotations

import asyncio
import logging
import re
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .agent import Agent
from .config import Settings
from .formatting import strip_markup, to_telegram_chunks
from .prompts import FAQ
from .storage import Storage

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.2  # Telegram rate-limits message edits
THINKING = ("💎 Думаю.", "💎 Думаю..", "💎 Думаю...")


def conv_key(message: Message) -> str:
    if message.chat.type == ChatType.PRIVATE:
        return f"u{message.from_user.id}"
    return f"c{message.chat.id}:t{message.message_thread_id or 0}"


IMAGE_ONLY_QUESTION = "Разбери картинку, которую я прислал."


def extract_question(message: Message, bot_username: str, bot_id: int) -> str | None:
    """Return the question if the bot should answer this message, else None."""
    text = message.text or message.caption or ""
    if message.chat.type == ChatType.PRIVATE:
        if not text.strip() and getattr(message, "photo", None):
            return IMAGE_ONLY_QUESTION
        return text.strip() or None
    mention = f"@{bot_username}".lower()
    mentioned = mention in text.lower()
    replied_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_id
    )
    if not (mentioned or replied_to_bot):
        return None
    q = re.sub(re.escape(mention), "", text, flags=re.I).strip()
    if not q and getattr(message, "photo", None):
        return IMAGE_ONLY_QUESTION
    return q or None


class StatusMessage:
    """One message that shows progress and is then replaced by the answer."""

    def __init__(self, msg: Message):
        self.msg = msg
        self.last_edit = 0.0
        self.n = 0

    async def update(self, label: str) -> None:
        if time.monotonic() - self.last_edit < EDIT_INTERVAL:
            return
        self.n += 1
        try:
            await self.msg.edit_text(f"{THINKING[self.n % 3]}\n{label}", parse_mode=None)
            self.last_edit = time.monotonic()
        except TelegramBadRequest:
            pass


class BottomBot:
    def __init__(self, settings: Settings, agent: Agent, storage: Storage, journal=None, learner=None, vision=None):
        self.s = settings
        self.agent = agent
        self.storage = storage
        self.journal = journal
        self.learner = learner
        self.vision = vision
        self.bot = Bot(
            settings.telegram_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        self.dp = Dispatcher()
        self.router = Router()
        self.locks: dict[int, asyncio.Lock] = {}
        self.me = None
        self._routes()
        self.dp.include_router(self.router)

    def _routes(self) -> None:
        r = self.router
        r.message(CommandStart())(self.on_start)
        r.message(Command("help"))(self.on_start)
        r.message(Command("limit"))(self.on_limit)
        r.message(Command("reset"))(self.on_reset)
        r.message(Command("wrong"))(self.on_wrong)
        r.message(Command("stats"))(self.on_stats)
        r.message(Command("lessons"))(self.on_lessons)
        r.message(Command("lesson"))(self.on_lesson)
        r.message(Command("forget"))(self.on_forget)
        r.message(F.text | F.caption | F.photo)(self.on_text)

    # --- learning commands ---

    def _is_admin(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id in self.s.admin_ids)

    @staticmethod
    def _arg(message: Message) -> str:
        parts = (message.text or "").split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    async def on_wrong(self, message: Message) -> None:
        if self.journal is None:
            return
        rt = message.reply_to_message
        text = self._arg(message)
        if not rt or not text:
            await message.reply("Ответь командой на моё сообщение и напиши, что не так:\n"
                                "/wrong стоп слишком близко, выбило за час", parse_mode=None)
            return
        if self.journal.add_feedback(message.chat.id, rt.message_id, message.from_user.id, text):
            await message.reply("🙏 Принял. Разберу при ежедневном анализе ошибок и сделаю выводы.", parse_mode=None)
        else:
            await message.reply("Не нашёл это сообщение у себя — ответь на мой разбор.", parse_mode=None)

    async def on_stats(self, message: Message) -> None:
        if self.journal is None:
            return
        tr = self.journal.track_record(self._arg(message))
        if not tr["setups"]:
            await message.answer("📒 Пока нет сетапов в журнале.", parse_mode=None)
            return
        lines = [
            f"📒 Мои сетапы за {tr['days']} дн.{' по ' + tr['symbol'] if tr['symbol'] else ''}: {tr['setups']} "
            f"(открыто {tr['open']}, закрыто {tr['closed']}, не исполнилось {tr['not_filled']})",
            f"Винрейт исполненных: {tr['win_rate_filled_pct']}% · средний результат: {tr['avg_r_filled']}R",
            "",
        ] + [f"• {r['symbol']} {r['direction']}: {r['outcome']} {'' if r['r'] is None else str(r['r']) + 'R'} "
             f"({r['days_ago']} дн. назад)" for r in tr["recent"]]
        await message.answer("\n".join(lines), parse_mode=None)

    async def on_lessons(self, message: Message) -> None:
        if self.journal is None:
            return
        lessons = self.journal.active_lessons()
        text = "\n".join(f"#{i} {t}{' (админ)' if k == 'manual' else ''}" for i, t, k in lessons)
        await message.answer("🧠 Мои уроки:\n" + text if lessons else "🧠 Уроков пока нет.", parse_mode=None)

    async def on_lesson(self, message: Message) -> None:
        if self.journal is None or not self._is_admin(message):
            return
        text = self._arg(message)
        if len(text) < 10:
            await message.reply("Напиши урок после команды: /lesson на мемах стоп ставить за 4ч уровнем", parse_mode=None)
            return
        lid = self.journal.add_lesson(text, "manual")
        if self.learner:
            self.learner.invalidate()
        await message.reply(f"✅ Урок #{lid} добавлен.", parse_mode=None)

    async def on_forget(self, message: Message) -> None:
        if self.journal is None or not self._is_admin(message):
            return
        arg = self._arg(message).lstrip("#")
        if arg.isdigit() and self.journal.retire_lesson(int(arg), "снят админом"):
            if self.learner:
                self.learner.invalidate()
            await message.reply(f"🗑 Урок #{arg} снят.", parse_mode=None)
        else:
            await message.reply("Укажи номер урока: /forget 3 (список — /lessons)", parse_mode=None)

    def _daily_left(self, user_id: int) -> int:
        if user_id in self.s.admin_ids:
            return 10**6
        return max(self.s.daily_steps_per_user - self.storage.steps_used(user_id), 0)

    async def on_start(self, message: Message) -> None:
        name = message.from_user.first_name if message.from_user else ""
        head = f"👋 Привет{', ' + name if name else ''}!\n\n" if message.text and message.text.startswith("/start") else ""
        await message.answer(head + FAQ.format(daily=self.s.daily_steps_per_user))

    async def on_limit(self, message: Message) -> None:
        left = self._daily_left(message.from_user.id)
        used = self.storage.steps_used(message.from_user.id)
        await message.answer(f"⏳ Сегодня потрачено {used} шагов, осталось {min(left, self.s.daily_steps_per_user)}.")

    async def on_reset(self, message: Message) -> None:
        self.storage.clear(conv_key(message))
        await message.answer("🧹 Контекст диалога очищен.")

    async def on_text(self, message: Message) -> None:
        if not message.from_user or message.from_user.is_bot:
            return
        if self.me is None:
            self.me = await self.bot.me()
        question = extract_question(message, self.me.username, self.me.id)
        if not question:
            return

        uid = message.from_user.id
        left = self._daily_left(uid)
        if left <= 0:
            await message.reply(
                f"⏳ Потрачено {self.s.daily_steps_per_user} из {self.s.daily_steps_per_user} шагов анализа "
                "на сегодня. Продолжим завтра."
            )
            return
        lock = self.locks.setdefault(uid, asyncio.Lock())
        if lock.locked():
            await message.reply("⏳ Ещё разбираю твой прошлый вопрос — подожди пару секунд.")
            return

        async with lock:
            await self._answer(message, question, uid, left)

    async def _answer(self, message: Message, question: str, uid: int, left: int) -> None:
        # Context from a replied-to message that is not the bot's own
        rt = message.reply_to_message
        if rt and rt.from_user and self.me and rt.from_user.id != self.me.id and (rt.text or rt.caption):
            question = f"Контекст — сообщение, на которое ответили:\n«{(rt.text or rt.caption)[:1500]}»\n\nВопрос: {question}"

        status = StatusMessage(await message.reply(THINKING[0], parse_mode=None))
        conv = conv_key(message)
        if getattr(message, "photo", None):
            question = await self._with_image(message, question, status)
        try:
            result = await self.agent.answer(
                question, self.storage.history(conv), on_status=status.update, step_budget=left
            )
        except Exception:  # noqa: BLE001
            log.exception("agent failed")
            await status.msg.edit_text("⚠️ Что-то пошло не так при разборе. Попробуй ещё раз чуть позже.", parse_mode=None)
            return

        if uid not in self.s.admin_ids:
            self.storage.add_steps(uid, result.steps)
        self.storage.add_turn(conv, question, result.text)
        sent = await self._deliver(status.msg, message, result.text)
        if self.journal is not None:
            for mid in sent:
                self.journal.record_message(message.chat.id, mid, conv, question, result.text)
            if result.setups:
                self.journal.record_setups(conv, message.chat.id, sent[0], question, result.text, result.setups)

        if uid not in self.s.admin_ids and self._daily_left(uid) <= 0:
            await message.answer(
                f"⏳ Потрачено {self.s.daily_steps_per_user} из {self.s.daily_steps_per_user} шагов анализа "
                "на сегодня. Когда шаги закончатся, продолжим уже завтра."
            )

    async def _with_image(self, message: Message, question: str, status: StatusMessage) -> str:
        """User sent a picture (chart, screenshot): describe it and add to the question."""
        if self.vision is None or not self.vision.enabled:
            return question + "\n[Пользователь прислал картинку, но разбор картинок не настроен.]"
        await status.update("🖼 Смотрю картинку")
        try:
            buf = await self.bot.download(message.photo[-1])
            desc = await self.vision.describe(buf.read(), "image/jpeg", context=message.caption or "")
            return f"[Пользователь прислал картинку. Что на ней: {desc}]\n{question}"
        except Exception as e:  # noqa: BLE001
            log.info("user image failed: %s", e)
            return question + "\n[Картинку разобрать не удалось.]"

    async def _deliver(self, status_msg: Message, origin: Message, text: str) -> list[int]:
        """Send the answer (split if long). Returns ids of the messages that carry it."""
        chunks = to_telegram_chunks(text)
        sent = [status_msg.message_id]
        try:
            await status_msg.edit_text(chunks[0])
            for ch in chunks[1:]:
                sent.append((await origin.answer(ch)).message_id)
        except TelegramBadRequest as e:
            log.warning("HTML rejected (%s), sending plain text", e)
            plain = strip_markup(text)
            parts = [plain[i : i + 4000] for i in range(0, len(plain), 4000)] or [""]
            await status_msg.edit_text(parts[0], parse_mode=None)
            sent = [status_msg.message_id]
            for p in parts[1:]:
                sent.append((await origin.answer(p, parse_mode=None)).message_id)
        return sent

    async def run(self) -> None:
        await self.dp.start_polling(self.bot, allowed_updates=["message"])
