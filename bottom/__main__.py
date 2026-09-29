"""Entry point: python -m bottom"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

from .agent import Agent
from .bot import BottomBot
from .config import Settings, load_settings
from .learning import Journal, Learner
from .llm import make_backend
from .storage import Storage
from .tools import http
from .tools.registry import Toolbox
from .vision import make_vision

log = logging.getLogger("bottom")


async def start_telegram_collector(s: Settings, vision):
    """Start the channel collector if Telegram is configured. Returns (store, collector) or (None, None)."""
    if not (s.tg_api_id and s.tg_api_hash):
        return None, None
    from .tg.client import Collector, make_client
    from .tg.sources import load_sources
    from .tg.store import TgStore

    sources = load_sources(s.tg_sources_file)
    if not sources:
        log.warning("Telegram: нет источников в %s", s.tg_sources_file)
        return None, None
    store = TgStore(s.db_path)
    client = make_client(s.tg_api_id, s.tg_api_hash, s.tg_session, receive_updates=True)
    collector = Collector(client, store, sources, backfill_days=s.tg_backfill_days,
                          resync_minutes=s.tg_resync_minutes, keep_days=s.tg_keep_days, vision=vision)
    try:
        await collector.start()
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram-сборщик не запущен: %s", e)
        return None, None
    return store, collector


async def learning_loop(s: Settings, learner) -> None:
    """Every 30 min: evaluate open setups. Once a day at LEARN_HOUR_UTC: review → lessons."""
    last_review_day = ""
    while True:
        try:
            n = await learner.evaluate_open()
            if n:
                log.info("Обучение: закрыто сетапов %d", n)
            now = datetime.now(timezone.utc)
            day = now.strftime("%Y-%m-%d")
            if now.hour == s.learn_hour_utc and day != last_review_day:
                last_review_day = day
                await learner.review()
        except Exception:  # noqa: BLE001
            log.exception("learning loop error")
        await asyncio.sleep(1800)


async def main() -> None:
    s = load_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if not s.telegram_token:
        sys.exit("TELEGRAM_BOT_TOKEN не задан (см. .env.example)")
    vision = make_vision(s)
    tg_store, collector = await start_telegram_collector(s, vision if vision.enabled else None)
    backend = make_backend(s)
    journal = Journal(s.db_path)
    toolbox = Toolbox(s, tg_store, collector=collector, vision=vision, journal=journal)
    learner = Learner(journal, backend, toolbox.pool)
    agent = Agent(backend, toolbox, s.timezone, s.max_steps_per_question, learner=learner)
    bot = BottomBot(s, agent, Storage(s.db_path), journal=journal, learner=learner, vision=vision)
    log.info("Bottom запущен: llm=%s tools=%s", backend.name, list(toolbox.tools))
    learn_task = asyncio.create_task(learning_loop(s, learner))
    try:
        await bot.run()
    finally:
        learn_task.cancel()
        if collector:
            await collector.stop()
        await toolbox.close()
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
