"""Tool registry: what the model can call, with JSON Schemas, progress labels and safe dispatch.

The tools themselves are registered by their domains, next to the code that does the work:
    tools/market   — exchanges (spot, perps, Binance Alpha), order books, candles, derivatives, spreads and funding
    tools/onchain  — token identity and fundamentals, DEX pairs, contract security, DeFi, DEX whales
    tools/social   — Telegram channels, X, crypto media, Ultra OI alerts, cross-checking a story, pictures
    tools/web      — web search and page reading
    tools/setups   — the setup validator, own track record, the reference caller's calls
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..config import Settings
from .schema import obj

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 60  # seconds per tool call
MAX_RESULT_CHARS = 12000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    label: str  # progress status shown to the user
    run: Callable[..., Awaitable[Any]]


class Toolbox:
    """Every tool available to the model, plus the shared resources they need."""

    def __init__(self, settings: Settings, tg_store=None, collector=None, vision=None, journal=None):
        from . import market, onchain, setups, social, web
        from .market.exchanges import ExchangePool

        self.s = settings
        self.tg_store = tg_store
        self.collector = collector
        self.vision = vision
        self.journal = journal
        self.pool = ExchangePool(settings.exchanges)
        self.tools: dict[str, Tool] = {}
        for domain in (market, onchain, social, web, setups):
            domain.register(self)

    def add(self, name: str, description: str, props: dict, required: list[str], label: str, run) -> None:
        self.tools[name] = Tool(name, description, obj(props, required), label, run)

    def schemas(self) -> list[Tool]:
        return list(self.tools.values())

    def label(self, name: str) -> str:
        t = self.tools.get(name)
        return t.label if t else "🔧 Работаю с данными"

    async def execute(self, name: str, args: dict) -> str:
        """Run a tool and return a compact JSON string for the model."""
        tool = self.tools.get(name)
        if tool is None:
            return json.dumps({"error": f"нет инструмента {name}"}, ensure_ascii=False)
        try:
            result = await asyncio.wait_for(tool.run(**(args or {})), TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"error": f"источник не ответил за {TOOL_TIMEOUT} с"}
        except TypeError as e:
            result = {"error": f"неверные аргументы: {e}"}
        except Exception as e:  # noqa: BLE001
            log.warning("tool %s failed: %s", name, e)
            result = {"error": f"источник недоступен: {type(e).__name__}: {str(e)[:200]}"}
        text = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '..."[обрезано]"'
        return text

    async def close(self) -> None:
        await self.pool.close()
