"""Entry point: python -m swarm"""

from __future__ import annotations

import asyncio
import logging
import sys

from .agent import Agent
from .bot import SwarmBot
from .config import load_settings
from .llm import make_backend
from .storage import Storage
from .tools import http
from .tools.registry import Toolbox


async def main() -> None:
    s = load_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not s.telegram_token:
        sys.exit("TELEGRAM_BOT_TOKEN не задан (см. .env.example)")
    toolbox = Toolbox(s)
    agent = Agent(make_backend(s), toolbox, s.timezone, s.max_steps_per_question)
    bot = SwarmBot(s, agent, Storage(s.db_path))
    logging.getLogger("swarm").info(
        "Swarm запущен: provider=%s model=%s tools=%s", s.llm_provider, s.llm_model, list(toolbox.tools)
    )
    try:
        await bot.run()
    finally:
        await toolbox.close()
        await http.close()


if __name__ == "__main__":
    asyncio.run(main())
