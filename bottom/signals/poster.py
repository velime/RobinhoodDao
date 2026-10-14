"""Post into a forum group's topics through the Bot API (topics are created on first use)."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import TOPICS
from .store import SignalStore

log = logging.getLogger(__name__)

MIN_GAP_SEC = 3.1  # Telegram: about 20 messages a minute into one group


class Poster:
    def __init__(self, token: str, chat_id: int | None, store: SignalStore, dry: bool = False):
        self.token, self.chat_id, self.store = token, chat_id, store
        self.dry = dry or not token or chat_id is None
        self._last = 0.0
        self._http = httpx.AsyncClient(timeout=20)

    async def _api(self, method: str, **params):
        r = await self._http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            if data.get("error_code") == 429:
                await asyncio.sleep(float((data.get("parameters") or {}).get("retry_after", 5)) + 1)
                return await self._api(method, **params)
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    async def topic(self, feed: str) -> int | None:
        """message_thread_id of the feed's topic; created once and remembered in the signals db."""
        key = f"topic:{self.chat_id}:{feed}"
        tid = self.store.get(key)
        if tid:
            return tid
        res = await self._api("createForumTopic", chat_id=self.chat_id, name=TOPICS.get(feed, feed))
        self.store.put(key, res["message_thread_id"])
        return res["message_thread_id"]

    async def send(self, feed: str, html: str) -> int | None:
        if self.dry:
            print(f"\n[{TOPICS.get(feed, feed)}]\n{html}\n", flush=True)
            return None
        wait = MIN_GAP_SEC - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            thread = await self.topic(feed)
            res = await self._api("sendMessage", chat_id=self.chat_id, message_thread_id=thread, text=html,
                                  parse_mode="HTML", link_preview_options={"is_disabled": True})
        except Exception as e:  # noqa: BLE001
            if "thread not found" in str(e).lower():  # topic deleted by an admin — make a new one
                self.store.put(f"topic:{self.chat_id}:{feed}", None)
            log.warning("post to %s failed: %s", feed, e)
            return None
        finally:
            self._last = time.monotonic()
        return res["message_id"]

    async def close(self) -> None:
        await self._http.aclose()
