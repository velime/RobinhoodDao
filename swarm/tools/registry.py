"""Tool definitions (JSON Schema), progress labels and dispatch."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..config import Settings
from . import derivatives as deriv
from . import exchanges as exch
from . import fundamentals as fund
from . import news
from .indicators import summarize_candles
from .setup_calc import calc_setup

log = logging.getLogger(__name__)

TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    label: str  # progress status shown to the user
    run: Callable[..., Awaitable[Any]]


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SYM = {"type": "string", "description": "Тикер без пары: BTC, HYPE, PEPE (можно с $)"}


class Toolbox:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pool = exch.ExchangePool(settings.exchanges)
        self.tools: dict[str, Tool] = {}
        self._register()

    # ---- tool implementations -------------------------------------------------

    async def _candles(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> dict:
        limit = max(30, min(int(limit), 500))
        if timeframe not in TIMEFRAMES:
            return {"error": f"timeframe должен быть одним из {TIMEFRAMES}"}
        bn = await self.pool.get("binance")
        found = exch.find_market(bn, exch.normalize_base(symbol), "perp") if bn else None
        meta: dict
        candles: list[dict] = []
        if found:
            m, mult = found
            try:
                candles = await deriv.binance_klines(m["id"], timeframe, limit)
                for c in candles:
                    for k in ("o", "h", "l", "c"):
                        c[k] /= mult
                    c["v"] *= mult
                    c["taker_buy"] *= mult
                meta = {"exchange": "binance", "market": "perp", "symbol": m["symbol"]}
            except Exception as e:  # noqa: BLE001
                log.info("binance klines failed: %s", e)
                candles = []
        if not candles:
            candles, meta = await exch.ohlcv(self.pool, symbol, timeframe, limit)
        if not candles:
            return meta
        s = summarize_candles(candles)
        # 24h range position from the same series
        per_day = {"5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}[timeframe]
        day = candles[-per_day:]
        hi, lo = max(c["h"] for c in day), min(c["l"] for c in day)
        s["range_24h"] = {
            "high": hi,
            "low": lo,
            "position_pct": round((candles[-1]["c"] - lo) / (hi - lo) * 100, 1) if hi > lo else None,
        }
        return {"source": meta, "timeframe": timeframe, **s}

    async def _atr_pct_1h(self, symbol: str) -> float | None:
        try:
            r = await self._candles(symbol, "1h", 100)
            return r.get("atr14_pct")
        except Exception:  # noqa: BLE001
            return None

    async def _calc_setup(self, symbol: str, direction: str, entry_low: float, stop: float,
                          targets: list[float], entry_high: float | None = None) -> dict:
        atr_pct = await self._atr_pct_1h(symbol)
        return calc_setup(self.s.risk, symbol, direction, float(entry_low), float(stop),
                          [float(t) for t in targets], float(entry_high) if entry_high else None, atr_pct)

    # ---- registration ---------------------------------------------------------

    def _add(self, name, description, props, required, label, run):
        self.tools[name] = Tool(name, description, _obj(props, required), label, run)

    def _register(self) -> None:
        s = self.s
        self._add(
            "resolve_token",
            "Определить, какой именно токен имеет в виду пользователь: тикер/название/контракт → кандидаты "
            "(CoinGecko id, ранг). Вызывай первым, если тикер редкий, неоднозначный или это адрес контракта.",
            {"query": {"type": "string", "description": "Тикер, название или адрес контракта"}},
            ["query"], "🔎 Определяю токен",
            lambda query: fund.resolve_token(query, s.coingecko_api_key),
        )
        self._add(
            "market_overview",
            "Цены спот и перп на всех подключённых CEX: медиана, разброс между биржами, базис перп/спот по "
            "каждой бирже, суммарные объёмы 24ч, подозрительные расхождения (другой токен или тонкий рынок).",
            {"symbol": SYM}, ["symbol"], "📊 Сверяю цены на биржах",
            lambda symbol: exch.market_overview(self.pool, symbol),
        )
        self._add(
            "derivatives",
            "Деривативы: фандинг по биржам (текущий и последние выплаты), OI и его изменение за 1ч/4ч/24ч, "
            "доля лонгов по аккаунтам (Binance/Bybit/OKX) и сводно, топ-трейдеры Binance, taker buy/sell.",
            {"symbol": SYM}, ["symbol"], "📈 Смотрю фандинг и OI",
            lambda symbol: deriv.derivatives(self.pool, symbol),
        )
        self._add(
            "candles",
            "Свечи и расчёты по ним: EMA20/50/200, RSI14, Stoch, ATR14 (и в %), ближайшие уровни поддержки/"
            "сопротивления (свинг-хаи/лоу = пулы ликвидности), позиция в 24ч диапазоне, всплеск объёма и "
            "числа сделок, дельта покупок/продаж и CVD (если Binance-перп).",
            {
                "symbol": SYM,
                "timeframe": {"type": "string", "enum": TIMEFRAMES},
                "limit": {"type": "integer", "description": "Число свечей, 30–500 (по умолчанию 200)"},
            },
            ["symbol", "timeframe"], "🕯 Смотрю свечи и уровни",
            self._candles,
        )
        self._add(
            "orderbook",
            "Стакан: глубина бидов/асков в ±1/2/5% от цены, перекос, спред, крупные стенки с дистанцией до цены.",
            {
                "symbol": SYM,
                "market": {"type": "string", "enum": ["perp", "spot"]},
                "exchange": {"type": "string", "description": "id биржи (binance, bybit, okx, mexc...). Пусто — первая, где есть рынок"},
            },
            ["symbol", "market"], "📚 Смотрю стакан",
            lambda symbol, market="perp", exchange=None: exch.orderbook(self.pool, symbol, market, exchange or None),
        )
        self._add(
            "token_fundamentals",
            "Фундаментал с CoinGecko: описание, категории, MCap, FDV, FDV/MCap, оборот/MCap, эмиссия, ATH, "
            "изменения 1ч/24ч/7д/30д, контракты по сетям, сайт и X проекта. Нужен coingecko_id из resolve_token.",
            {"coingecko_id": {"type": "string"}}, ["coingecko_id"], "🧾 Смотрю фундаментал",
            lambda coingecko_id: fund.token_fundamentals(coingecko_id, s.coingecko_api_key),
        )
        self._add(
            "dex_pairs",
            "DEX-пары с DexScreener: сеть, DEX, цена, ликвидность, объём 1ч/24ч, покупки/продажи, FDV, возраст пары. "
            "Для мемов, свежих запусков и токенов без CEX.",
            {"query": {"type": "string", "description": "Тикер или адрес контракта"}},
            ["query"], "🧪 Смотрю DEX-пары",
            lambda query: fund.dex_pairs(query),
        )
        self._add(
            "token_security",
            "Безопасность контракта (GoPlus): honeypot, налоги, mint, владелец, прокси, число холдеров, "
            "топ-10 холдеров и их доля, залоченность LP.",
            {
                "chain": {"type": "string", "description": "ethereum, bsc, base, arbitrum, solana, ..."},
                "address": {"type": "string"},
            },
            ["chain", "address"], "🛡 Проверяю контракт",
            lambda chain, address: fund.token_security(chain, address),
        )
        self._add(
            "defi_protocol",
            "DeFi-протокол по DefiLlama: TVL, изменение 1д/7д, сети, категория, MCap/TVL.",
            {"query": {"type": "string", "description": "Название или тикер протокола"}},
            ["query"], "🏦 Смотрю DefiLlama",
            lambda query: fund.defi_protocol(query),
        )
        self._add(
            "market_sentiment",
            "Фон рынка: Fear & Greed, общая капитализация и её изменение, доминация BTC/ETH, тренды CoinGecko.",
            {}, [], "🌡 Смотрю фон рынка",
            lambda: fund.market_sentiment(s.coingecko_api_key),
        )
        self._add(
            "crypto_news",
            "Свежие заголовки крипто-СМИ (CoinDesk, Cointelegraph, Decrypt, The Block и др.), с фильтром по "
            "тикеру/слову. Для поиска катализатора.",
            {
                "query": {"type": "string", "description": "Тикер или ключевое слово; пусто — все новости"},
                "hours": {"type": "integer", "description": "Глубина в часах (по умолчанию 48)"},
            },
            [], "📰 Читаю новости",
            lambda query="", hours=48: news.crypto_news(query, int(hours), s.cryptopanic_api_key),
        )
        if s.twitterapi_io_key:
            self._add(
                "x_discussion",
                "X (Twitter): что пишут о токене/теме — топовые и свежие посты без реплаев. Только новости и "
                "мнения, НЕ источник цифр рынка.",
                {"topic": {"type": "string", "description": "Тикер или тема"}},
                ["topic"], "🐦 Читаю X",
                lambda topic: news.x_discussion(s.twitterapi_io_key, topic),
            )
            self._add(
                "x_influencers",
                "X (Twitter): свежие посты отслеживаемых инфлюенсеров и новостных аккаунтов, опционально по теме. "
                "Для нарратива и настроений. НЕ источник цифр рынка.",
                {
                    "topic": {"type": "string", "description": "Тикер/тема; пусто — все свежие посты"},
                    "hours": {"type": "integer", "description": "За сколько часов (по умолчанию 24)"},
                },
                [], "🗣 Смотрю инфлюенсеров",
                lambda topic="", hours=24: news.x_influencers(s.twitterapi_io_key, s.x_accounts, topic, int(hours)),
            )
        if s.tavily_api_key:
            self._add(
                "web_search",
                "Веб-поиск: статьи, документация, анонсы, контекст события.",
                {"query": {"type": "string"}}, ["query"], "🌐 Ищу в вебе",
                lambda query: news.web_search(s.tavily_api_key, query),
            )
        self._add(
            "fetch_page",
            "Прочитать текст страницы по URL (из новостей или поиска).",
            {"url": {"type": "string"}}, ["url"], "📄 Читаю страницу",
            lambda url: news.fetch_page(url),
        )
        self._add(
            "calc_setup",
            "ОБЯЗАТЕЛЬНО перед любым сетапом. Проверяет вход/стоп/цели: геометрию, минимальную дистанцию стопа "
            "(с учётом ATR), минимальный R:R; считает R:R к каждой цели, % до стопа и потери/прибыль для плеч "
            "3/5/10/20x. Вердикт OK/REJECT/ERROR с причинами и подсказками.",
            {
                "symbol": SYM,
                "direction": {"type": "string", "enum": ["long", "short"]},
                "entry_low": {"type": "number", "description": "Вход или нижняя граница зоны входа"},
                "entry_high": {"type": "number", "description": "Верхняя граница зоны входа (если зона)"},
                "stop": {"type": "number"},
                "targets": {"type": "array", "items": {"type": "number"}, "description": "Цели по порядку"},
            },
            ["symbol", "direction", "entry_low", "stop", "targets"], "🧮 Считаю сетап",
            self._calc_setup,
        )

    # ---- API ------------------------------------------------------------------

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
            result = await tool.run(**(args or {}))
        except TypeError as e:
            result = {"error": f"неверные аргументы: {e}"}
        except Exception as e:  # noqa: BLE001
            log.warning("tool %s failed: %s", name, e)
            result = {"error": f"источник недоступен: {type(e).__name__}: {str(e)[:200]}"}
        text = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(text) > 12000:
            text = text[:12000] + '..."[обрезано]"'
        return text

    async def close(self) -> None:
        await self.pool.close()
