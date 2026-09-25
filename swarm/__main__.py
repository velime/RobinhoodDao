"""Entry point: python -m swarm"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .agent import Agent
from .bot import SwarmBot
from .config import Settings, load_settings
from .llm import make_backend
from .storage import Storage
from .tools import http
from .tools.registry import Toolbox

log = logging.getLogger("swarm")


async def start_telegram_collector(s: Settings):
    """Start the channel collector if Telegram is configured. Returns (store, collector) or (None, None)."""
    if not (s.tg_api_id and s.tg_api_hash):
        return None, None
    if not os.path.exists(s.tg_channels_file):
        log.warning("Telegram: нет %s — сначала `python -m swarm.tg scan`", s.tg_channels_file)
        return None, None
    from .tg.classify import parse_channel_list
    from .tg.client import Collector, make_client
    from .tg.store import TgStore

    channels = parse_channel_list(open(s.tg_channels_file, encoding="utf-8").read())
    store = TgStore(s.db_path)
    collector = Collector(make_client(s.tg_api_id, s.tg_api_hash, s.tg_session), store, channels, s.tg_poll_minutes)
    try:
        await collector.start()
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram-сборщик не запущен: %s", e)
        return None, None
    log.info("Telegram: слежу за %d каналами (опрос раз в %d мин)", len(channels), s.tg_poll_minutes)
    return store, collector


async def main() -> None:
    s = load_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not s.telegram_token:
        sys.exit("TELEGRAM_BOT_TOKEN не задан (см. .env.example)")
    tg_store, collector = await start_telegram_collector(s)
    toolbox = Toolbox(s, tg_store)
    backend = make_backend(s)
    agent = Agent(backend, toolbox, s.timezone, s.max_steps_per_question)
    bot = SwarmBot(s, agent, Storage(s.db_path))
    log.info("Swarm запущен: llm=%s tools=%s", backend.name, list(toolbox.tools))
    try:
        await bot.run()
    finally:
        if collector:
            await collector.stop()
        await toolbox.close()
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
