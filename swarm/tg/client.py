"""Telegram user-account client (Telethon): login, one-off channel scan, post collector.

Reads public channels through YOUR account session. The session file gives
full access to the account — keep it private, never commit it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from telethon import TelegramClient, errors
from telethon.tl.functions.channels import GetFullChannelRequest

from .classify import CRYPTO, NOT_CRYPTO, REVIEW, UNAVAILABLE, Verdict, classify
from .store import TgStore

log = logging.getLogger(__name__)


def make_client(api_id: int, api_hash: str, session: str) -> TelegramClient:
    os.makedirs(os.path.dirname(session) or ".", exist_ok=True)
    # flood_sleep_threshold: sleep automatically on short FloodWait instead of failing
    return TelegramClient(session, api_id, api_hash, flood_sleep_threshold=120, receive_updates=False)


def _post(m) -> dict | None:
    text = (m.message or "").strip()
    if not text:
        return None
    return {
        "id": m.id,
        "ts": m.date.replace(tzinfo=m.date.tzinfo or timezone.utc).timestamp(),
        "text": text,
        "views": getattr(m, "views", None),
        "forwards": getattr(m, "forwards", None),
    }


async def scan_channel(client: TelegramClient, username: str, n_posts: int = 50) -> dict:
    """Title, bio, last posts and the crypto verdict for one channel."""
    try:
        entity = await client.get_entity(username)
    except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError, ValueError) as e:
        return {"username": username, "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason=f"не найден: {type(e).__name__}")}
    except errors.ChannelPrivateError:
        return {"username": username, "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason="приватный канал")}
    kind = "channel" if getattr(entity, "broadcast", False) else ("group" if getattr(entity, "megagroup", False) else "user")
    if kind == "user":
        return {"username": username, "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason="это аккаунт, не канал")}
    about, subscribers = "", None
    try:
        full = await client(GetFullChannelRequest(entity))
        about = full.full_chat.about or ""
        subscribers = getattr(full.full_chat, "participants_count", None)
    except Exception:  # noqa: BLE001
        pass
    msgs = await client.get_messages(entity, limit=n_posts)
    posts = [p for p in (_post(m) for m in msgs) if p]
    last = max((m.date for m in msgs), default=None)
    title = getattr(entity, "title", "") or ""
    return {
        "username": getattr(entity, "username", None) or username,
        "title": title,
        "about": about,
        "kind": kind,
        "subscribers": subscribers,
        "last_post": last.astimezone(timezone.utc).strftime("%Y-%m-%d") if last else None,
        "days_since_last_post": (datetime.now(timezone.utc) - last).days if last else None,
        "posts": posts,
        "verdict": classify(title, about, [p["text"] for p in posts]),
    }


async def subscribed_channels(client: TelegramClient) -> list[str]:
    """Public broadcast channels the account is subscribed to."""
    out = []
    async for d in client.iter_dialogs():
        e = d.entity
        if getattr(e, "broadcast", False) and getattr(e, "username", None):
            out.append(e.username)
    return out


class Collector:
    """Polls the kept channels for new posts and stores them for the bot tool.

    Polling (not live updates) — works without joining the channels.
    """

    def __init__(self, client: TelegramClient, store: TgStore, channels: list[str],
                 poll_minutes: int = 10, backfill_hours: int = 48):
        self.client = client
        self.store = store
        self.channels = channels
        self.poll_minutes = poll_minutes
        self.backfill_hours = backfill_hours
        self._task: asyncio.Task | None = None

    async def _poll_one(self, ch: str) -> int:
        last = self.store.last_id(ch)
        posts = []
        if last:
            async for m in self.client.iter_messages(ch, min_id=last, limit=200):
                p = _post(m)
                if p:
                    posts.append(p)
        else:  # first run: recent history
            cutoff = datetime.now(timezone.utc).timestamp() - self.backfill_hours * 3600
            async for m in self.client.iter_messages(ch, limit=100):
                if m.date.timestamp() < cutoff:
                    break
                p = _post(m)
                if p:
                    posts.append(p)
        return self.store.add_posts(ch, posts)

    async def run_once(self) -> int:
        total = 0
        for ch in self.channels:
            try:
                total += await self._poll_one(ch)
            except errors.FloodWaitError as e:
                log.warning("Telegram FloodWait %ss — пауза", e.seconds)
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:  # noqa: BLE001
                log.info("канал %s: %s", ch, e)
            await asyncio.sleep(1.5)  # be gentle with the account
        self.store.prune()
        return total

    async def loop(self) -> None:
        while True:
            try:
                n = await self.run_once()
                log.info("Telegram: собрано %d новых постов из %d каналов", n, len(self.channels))
            except Exception:  # noqa: BLE001
                log.exception("Telegram collector error")
            await asyncio.sleep(self.poll_minutes * 60)

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram-сессия не авторизована: выполни `python -m swarm.tg login`")
        self._task = asyncio.create_task(self.loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.client.disconnect()


LABELS = {CRYPTO: "✅ крипта", NOT_CRYPTO: "❌ не крипта", REVIEW: "❓ проверить", UNAVAILABLE: "⚠️ недоступен"}


def verdict_label(v: Verdict) -> str:
    return LABELS[v.label]
