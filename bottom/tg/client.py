"""Telegram user-account client (Telethon): login, source scan, live collector.

All sources — public and private, channels and groups — are read through the
user's account, which is already a member of every one of them:
- new messages arrive as live updates (no polling, instant, minimal load);
- the first run reads TG_BACKFILL_DAYS of history (0 = the whole channel), then
  periodically catches up on anything missed while offline (from the last stored id);
- images (charts, position screenshots, news screenshots) are described by a
  vision model in the background and on demand.

The session file gives full access to the account — keep it private.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from telethon import TelegramClient, errors, events, utils
from telethon.tl.functions.channels import GetFullChannelRequest

from .classify import CRYPTO, NOT_CRYPTO, REVIEW, UNAVAILABLE, Verdict, classify
from .sources import Source
from .store import TgStore

log = logging.getLogger(__name__)

MIN_TEXT = 2  # skip empty/one-char messages ("+", stickers, media without caption)


def make_client(api_id: int, api_hash: str, session: str, receive_updates: bool = False) -> TelegramClient:
    os.makedirs(os.path.dirname(session) or ".", exist_ok=True)
    # flood_sleep_threshold: sleep automatically on short FloodWait instead of failing
    return TelegramClient(session, api_id, api_hash, flood_sleep_threshold=120, receive_updates=receive_updates)


def topic_of(m) -> int | None:
    r = getattr(m, "reply_to", None)
    if r is not None and getattr(r, "forum_topic", False):
        return getattr(r, "reply_to_top_id", None) or getattr(r, "reply_to_msg_id", None)
    return None


def has_image(m) -> bool:
    """Photo, or an image sent as a file (not stickers/GIFs)."""
    if getattr(m, "photo", None):
        return True
    doc = getattr(m, "document", None)
    mime = getattr(doc, "mime_type", "") or ""
    return mime in ("image/jpeg", "image/png", "image/webp") and not getattr(m, "sticker", None)


def to_row(m, author: str | None = None) -> dict | None:
    text = (getattr(m, "message", None) or "").strip()
    image = has_image(m)
    if len(text) < MIN_TEXT and not image:
        return None
    date = m.date if m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
    return {
        "id": m.id,
        "ts": date.timestamp(),
        "text": text,
        "views": getattr(m, "views", None),
        "author": author or getattr(m, "post_author", None),
        "topic_id": topic_of(m),
        "has_image": image,
        "grouped_id": getattr(m, "grouped_id", None),
    }


def kind_of(entity) -> str | None:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
        return "forum" if getattr(entity, "forum", False) else "group"
    if entity.__class__.__name__ == "Chat":  # legacy small group
        return "group"
    return None


async def load_dialogs(client: TelegramClient) -> dict[int, object]:
    """All chats the account is in: {marked chat id: entity}. Also fills the entity cache."""
    out = {}
    async for d in client.iter_dialogs():
        if kind_of(d.entity):
            out[d.id] = d.entity
    return out


async def resolve(client: TelegramClient, src: Source, dialogs: dict[int, object]):
    """Entity for a source: from the account's dialogs, else by username (public only)."""
    ent = dialogs.get(src.id)
    if ent is not None:
        return ent
    if src.username:
        try:
            return await client.get_entity(src.username)
        except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError, errors.ChannelPrivateError, ValueError):
            return None
    return None


def refresh_source(src: Source, entity) -> None:
    """Update title/username/kind/public from the live entity (usernames change)."""
    src.title = getattr(entity, "title", None) or src.title
    username = getattr(entity, "username", None)
    src.username = username
    src.public = bool(username)
    src.kind = kind_of(entity) or src.kind


async def scan_source(client: TelegramClient, src: Source, entity, n_posts: int = 50) -> dict:
    """Title, bio, recent messages and the crypto verdict for one source."""
    if entity is None:
        return {"source": src, "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason="аккаунт не состоит / не найден")}
    refresh_source(src, entity)
    about, members = "", None
    try:
        full = await client(GetFullChannelRequest(entity))
        about = full.full_chat.about or ""
        members = getattr(full.full_chat, "participants_count", None)
    except Exception:  # noqa: BLE001 — legacy groups have no full channel
        pass
    # groups are chatty: look at more messages, ignore short ones
    limit = n_posts if src.kind == "channel" else n_posts * 2
    msgs = await client.get_messages(entity, limit=limit)
    texts = [m.message.strip() for m in msgs if getattr(m, "message", None)]
    if src.kind != "channel":
        texts = [t for t in texts if len(t) >= 15]
    last = max((m.date for m in msgs), default=None)
    return {
        "source": src,
        "about": about,
        "members": members,
        "texts": texts,
        "last_post": last.astimezone(timezone.utc).strftime("%Y-%m-%d") if last else None,
        "days_since_last_post": (datetime.now(timezone.utc) - last).days if last else None,
        "verdict": classify(src.title, about, texts),
    }


class Collector:
    """Live updates from enabled sources + catch-up + image descriptions, stored in SQLite."""

    def __init__(self, client: TelegramClient, store: TgStore, sources: list[Source],
                 backfill_days: int = 30, resync_minutes: int = 60, keep_days: int = 180, vision=None):
        self.client = client
        self.store = store
        self.sources = [s for s in sources if s.enabled]
        self.by_id: dict[int, Source] = {s.id: s for s in self.sources}
        self.entities: dict[int, object] = {}
        self.unavailable: list[Source] = []
        self.backfill_days = backfill_days  # 0 = whole history
        self.resync_minutes = resync_minutes
        self.keep_days = keep_days
        self.vision = vision
        self.live_count = 0
        self._tasks: list[asyncio.Task] = []

    async def _author(self, event) -> str | None:
        src = self.by_id.get(event.chat_id)
        if src is None or src.kind == "channel":
            return None
        try:
            return utils.get_display_name(await event.get_sender()) or None
        except Exception:  # noqa: BLE001
            return None

    async def on_message(self, event) -> None:
        row = to_row(event.message, await self._author(event))
        if row:
            self.live_count += self.store.add_messages(event.chat_id, [row])

    async def sync_one(self, src: Source) -> int:
        """Everything newer than the last stored message; first time — backfill_days of history."""
        entity = self.entities[src.id]
        last = self.store.last_id(src.id)
        cutoff = time.time() - self.backfill_days * 86400 if self.backfill_days > 0 else 0
        rows = []
        async for m in self.client.iter_messages(entity, min_id=last):
            if not last and m.date.timestamp() < cutoff:
                break
            author = None
            if src.kind != "channel" and m.sender is not None:
                author = utils.get_display_name(m.sender) or None
            row = to_row(m, author)
            if row:
                rows.append(row)
        return self.store.add_messages(src.id, rows)

    async def sync_all(self) -> int:
        total = 0
        for src in self.sources:
            if src.id not in self.entities:
                continue
            try:
                total += await self.sync_one(src)
            except errors.FloodWaitError as e:
                log.warning("Telegram FloodWait %ss — пауза", e.seconds)
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:  # noqa: BLE001
                log.info("источник %s: %s", src.label, e)
            await asyncio.sleep(1.0)  # be gentle with the account
        self.store.prune(self.keep_days)
        return total

    # --- images ---

    async def describe_message(self, chat_id: int, msg_id: int) -> str:
        """Download the image of one message and describe it (on demand or from the worker)."""
        if self.vision is None or not self.vision.enabled:
            raise RuntimeError("разбор картинок не настроен")
        entity = self.entities.get(chat_id)
        if entity is None:
            raise RuntimeError("источник не отслеживается")
        m = await self.client.get_messages(entity, ids=msg_id)
        if m is None or not has_image(m):
            raise RuntimeError("в сообщении нет картинки")
        data = await self.client.download_media(m, file=bytes)
        mime = getattr(getattr(m, "document", None), "mime_type", None) or "image/jpeg"
        try:
            text = await self.vision.describe(data, mime, context=(m.message or ""))
        except Exception:
            self.store.set_image_text(chat_id, msg_id, None)
            raise
        self.store.set_image_text(chat_id, msg_id, text)
        return text

    async def _vision_loop(self) -> None:
        """Describe fresh images in the background, channels first, within the daily limit."""
        while True:
            try:
                if self.vision is not None and self.vision.enabled and self.vision.remaining() > 0:
                    for chat_id, msg_id, _caption in self.store.pending_images(time.time() - 2 * 86400, limit=10):
                        if self.vision.remaining() <= 0:
                            break
                        try:
                            await self.describe_message(chat_id, msg_id)
                        except Exception as e:  # noqa: BLE001
                            log.info("картинка %s/%s: %s", chat_id, msg_id, e)
                        await asyncio.sleep(2)
            except Exception:  # noqa: BLE001
                log.exception("vision worker error")
            await asyncio.sleep(60)

    async def _loop(self) -> None:
        while True:
            try:
                n = await self.sync_all()
                log.info("Telegram: догружено %d сообщений; вживую с прошлого раза %d", n, self.live_count)
                self.live_count = 0
            except Exception:  # noqa: BLE001
                log.exception("Telegram catch-up error")
            await asyncio.sleep(self.resync_minutes * 60)

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована: запусти `python -m bottom.tg login`")
        dialogs = await load_dialogs(self.client)
        for src in self.sources:
            ent = await resolve(self.client, src, dialogs)
            if ent is None:
                self.unavailable.append(src)
            else:
                refresh_source(src, ent)
                self.entities[src.id] = ent
        if self.unavailable:
            log.warning(
                "Telegram: нет доступа к %d источникам (аккаунт не состоит или удалены): %s",
                len(self.unavailable), ", ".join(s.label for s in self.unavailable[:10]),
            )
        self.store.sync_sources(self.sources)
        if not self.entities:
            raise RuntimeError("ни один источник недоступен аккаунту — проверь channels/sources.json")
        self.client.add_event_handler(self.on_message, events.NewMessage(chats=list(self.entities)))
        self._tasks = [asyncio.create_task(self._loop()), asyncio.create_task(self._vision_loop())]
        log.info("Telegram: слушаю %d источников вживую, догрузка раз в %d мин", len(self.entities), self.resync_minutes)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.client.disconnect()


LABELS = {CRYPTO: "✅ крипта", NOT_CRYPTO: "❌ не крипта", REVIEW: "❓ проверить", UNAVAILABLE: "⚠️ недоступен"}


def verdict_label(v: Verdict) -> str:
    return LABELS[v.label]
