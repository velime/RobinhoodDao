"""Tool definitions (JSON Schema), progress labels and dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..config import Settings
from . import crosscheck as cc
from . import derivatives as deriv
from . import exchanges as exch
from . import fundamentals as fund
from . import news
from . import websearch
from .indicators import summarize_candles
from .setup_calc import calc_setup, entry_status, stop_hunt_warnings
from .ultra import ultra_alerts
from . import indexfeed, oicard, onchain, parsergroup, spreads, transfers

log = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
TOOL_TIMEOUT = 60  # seconds per tool call


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
    def __init__(self, settings: Settings, tg_store=None, collector=None, vision=None, journal=None):
        self.s = settings
        self.tg_store = tg_store
        self.collector = collector
        self.vision = vision
        self.journal = journal
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
        per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}[timeframe]
        day = candles[-per_day:]
        hi, lo = max(c["h"] for c in day), min(c["l"] for c in day)
        s["range_24h"] = {
            "high": hi,
            "low": lo,
            "position_pct": round((candles[-1]["c"] - lo) / (hi - lo) * 100, 1) if hi > lo else None,
        }
        if len(candles) < per_day:  # 1m: 500 candles cover ~8h, not a day
            s["range_24h"]["covers_hours"] = round(len(candles) * 24 / per_day, 1)
        return {"source": meta, "timeframe": timeframe, **s}

    async def _atr_pct_1h(self, symbol: str) -> float | None:
        try:
            r = await self._candles(symbol, "1h", 100)
            return r.get("atr14_pct")
        except Exception:  # noqa: BLE001
            return None

    async def _calc_setup(self, symbol: str, direction: str, entry_low: float, stop: float,
                          targets: list[float], entry_high: float | None = None) -> dict:
        async def summary(tf):
            try:
                return await self._candles(symbol, tf, 100)
            except Exception:  # noqa: BLE001
                return {}

        h1, h4 = await asyncio.gather(summary("1h"), summary("4h"))
        res = calc_setup(self.s.risk, symbol, direction, float(entry_low), float(stop),
                         [float(t) for t in targets], float(entry_high) if entry_high else None, h1.get("atr14_pct"))
        if res.get("verdict") not in ("OK", "REJECT"):
            return res
        lo = float(entry_low)
        hi = float(entry_high) if entry_high else lo
        lo, hi = min(lo, hi), max(lo, hi)
        tp1 = sorted(float(t) for t in targets)[0] if direction == "long" else sorted(float(t) for t in targets)[-1]
        st = entry_status(direction, lo, hi, float(stop), tp1, h1.get("last_close"))
        res["entry_status"] = st
        if st["status"] == "invalid":
            res["verdict"] = "REJECT"
            res.setdefault("problems", []).append(st["note"])
        levels = []
        for tf, s in (("1ч", h1), ("4ч", h4)):
            lv = s.get("levels") or {}
            levels += [(p, f"свинг {tf}") for p in (lv.get("support") or []) + (lv.get("resistance") or [])]
            rg = s.get("range_24h") or {}
            if tf == "1ч" and rg.get("high"):
                levels += [(rg["high"], "хай 24ч"), (rg["low"], "лоу 24ч")]
        warn = stop_hunt_warnings(direction, lo, hi, float(stop), levels)
        if warn:
            res["warnings"] = res.get("warnings", []) + warn
        return res

    def _flows(self, base: str, days: int) -> dict:
        return parsergroup.exchange_flows(self.tg_store, self.s.parser_chat_ids, base, days)

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
            "сопротивления (свинг-хаи/лоу = пулы ликвидности) и сколько раз их тестировали (level_touches), "
            "MACD (кросс, рост гистограммы), Боллинджер (ширина, позиция), Фибоначчи последнего импульса, "
            "позиция в 24ч диапазоне, всплеск объёма и числа сделок, дельта покупок/продаж и CVD (если "
            "Binance-перп). 1m/5m/15m — для скальпа, 1h/4h/1d — для свинга.",
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
            "spot_perp_flow",
            "Кто ведёт движение: чистые агрессивные покупки (taker buy − sell, $) на споте и на перпе Binance "
            "за 1ч и 4ч, доля покупок, соотношение объёмов перп/спот и вывод. Аналог A/D спот/перп из Ultra.",
            {"symbol": SYM}, ["symbol"], "🌊 Смотрю поток спот/перп",
            lambda symbol: deriv.spot_perp_flow(exch.normalize_base(symbol)),
        )
        self._add(
            "oi_card",
            "Своя карточка OI по монете (Binance и Bybit): OI и его изменение за 4ч и 15м, $ притока, цена 4ч, "
            "фандинг, L/S, Mark, Book Delta, стенки стакана, A/D спот/перп, OI/MCap; и разбор по методичке: "
            "паттерн, сценарий, комбинации 1–10, красные флаги. Для вопросов про OI, сквиз, «кто заходит».",
            {"symbol": SYM}, ["symbol"], "📈 Считаю OI",
            lambda symbol: oicard.oi_card(exch.normalize_base(symbol), s.coingecko_api_key),
        )
        self._add(
            "spread_check",
            "Спреды монеты без переводов между биржами: фьюч↔фьюч по всем биржам (с переносом фандинга) и "
            "спот↔фьюч (спот CEX и DEX-пулы настоящего контракта против перпов), проверка индексов (перп к своему "
            "индексу, марк к индексу, индексы бирж между собой и к споту); сколько спред держится по истории 5м, "
            "сужается ли, сколько помещается в стаканы, вердикт. Только когда спрашивают про спред/арбитраж/индекс.",
            {"symbol": SYM, "hours": {"type": "integer", "description": "Окно истории спреда, ч (по умолчанию 6)"}},
            ["symbol"], "🔀 Проверяю спред",
            lambda symbol, hours=6: spreads.spread_check(self.pool, symbol, max(1, min(int(hours), 24))),
        )
        self._add(
            "transfer_status",
            "Депозиты и выводы монеты на биржах по всем сетям: открыто/закрыто, комиссия вывода, контракт; "
            "конфликты контрактов (другой токен с тем же тикером). Bybit/OKX/MEXC/BingX — только с API-ключом.",
            {"symbol": SYM}, ["symbol"], "🏦 Смотрю депы и выводы",
            lambda symbol: transfers.transfer_status(exch.normalize_base(symbol)),
        )
        self._add(
            "dex_whales",
            "Кто сливает в главный DEX-пул монеты: кошельки с чистыми продажами за окно, доля от ликвидности пула.",
            {"query": {"type": "string", "description": "Тикер или адрес контракта"},
             "window_min": {"type": "integer", "description": "Окно, минут (по умолчанию 60)"}},
            ["query"], "🐋 Смотрю сливы на DEX",
            lambda query, window_min=60: onchain.dex_whales(query, max(5, min(int(window_min), 1440))),
        )
        self._add(
            "signals",
            "Алерты НАШИХ парсеров (читаются из их Telegram-группы): oi — всплески OI, funding — экстремальный "
            "фандинг и скачки, spreads — спреды фьюч↔фьюч, спот↔фьюч и проверка индексов, flows — ончейн заводы "
            "на биржи и выводы с бирж (Dep / With), dex — сильные сливы в пулы (Whale perp), status — "
            "открытие/закрытие сетей. Без тикера — какие монеты чаще всего в сигналах; с тикером — история по "
            "монете. Основа для «что сейчас интересного / лучший сетап».",
            {"feed": {"type": "string", "enum": ["", "oi", "funding", "spreads", "flows", "dex", "status"]},
             "symbol": {"type": "string", "description": "Тикер; пусто — обзор"},
             "hours": {"type": "integer", "description": "За сколько часов (по умолчанию 24)"}},
            [], "📡 Смотрю наши сигналы",
            lambda feed="", symbol="", hours=24: asyncio.to_thread(
                parsergroup.signals, self.tg_store, s.parser_chat_ids, feed, symbol, max(1, min(int(hours), 720))),
        )
        self._add(
            "spread_board",
            "Закреплённое сообщение парсера «Актуальные спреды» из его Telegram-группы: все открытые сейчас спреды "
            "(фьюч↔фьюч, спот↔фьюч) со спредом сейчас, максимумом за время жизни, сколько минут держится и сколько "
            "помещается в стакан. Для «какие спреды сейчас», «что в закрепе», «лучший спред».",
            {}, [], "📌 Читаю закреп спредов",
            lambda: asyncio.to_thread(parsergroup.spread_board, self.tg_store, s.parser_chat_ids),
        )
        self._add(
            "index_spreads",
            "Отклонение last-цены от индексной цены на фьючерсах 5%+ (канал @spread_last_index из Telegram): "
            "без тикера — какие монеты чаще и сильнее отклонялись, с тикером — серия по биржам: сторона, процент, "
            "сколько секунд держится, last и index. Для «индекс», «last-index», «отклонение от индекса».",
            {"symbol": {"type": "string", "description": "Тикер; пусто — обзор"},
             "hours": {"type": "integer", "description": "За сколько часов (по умолчанию 24)"}},
            [], "🧭 Смотрю отклонение от индекса",
            lambda symbol="", hours=24: asyncio.to_thread(
                indexfeed.index_spreads, self.tg_store, symbol, max(1, min(int(hours), 720))),
        )
        self._add(
            "exchange_flows",
            "Ончейн-переводы монеты на биржи и с бирж (по кошелькам бирж): сколько завели/вывели и куда, нетто, "
            "кто больше всех заводит, крупнейшие переводы. Берётся из алертов парсера Dep / With в группе парсеров.",
            {"symbol": SYM, "days": {"type": "integer", "description": "За сколько дней (по умолчанию 30)"}},
            ["symbol"], "🏦 Смотрю заводы и выводы",
            lambda symbol, days=30: asyncio.to_thread(self._flows, exch.normalize_base(symbol), max(1, min(int(days), 90))),
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
            "DEX-пары (DexScreener, если пусто — GeckoTerminal): сеть, DEX, цена, ликвидность, объём 1ч/24ч, "
            "покупки/продажи, FDV, возраст пары. Для мемов, свежих запусков, токенов без CEX и новых сетей.",
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
        if self.tg_store is not None:
            store = self.tg_store

            async def tg_posts(query: str = "", hours: int = 24, source: str = "", limit: int = 25) -> dict:
                hours = max(1, min(int(hours), 24 * 180))
                return store.search(query, hours, limit=max(1, min(int(limit), 60)),
                                    hide_private=not s.tg_show_private, source=source)

            self._add(
                "telegram_channels",
                "Сообщения отслеживаемых крипто-Telegram-каналов и чатов (колы, мнения, алерты, скрины): "
                "по тикеру — сколько каналов/чатов упоминают, первое упоминание, динамика и сами сообщения "
                "(с описанием картинок); source — прочитать конкретный канал целиком за период; без тикера — "
                "свежие посты и самые обсуждаемые $тикеры. Это мнения, НЕ источник цифр рынка.",
                {
                    "query": {"type": "string", "description": "Тикер или слово; пусто — что обсуждают сейчас"},
                    "hours": {"type": "integer", "description": "За сколько часов (по умолчанию 24, до 4320)"},
                    "source": {"type": "string", "description": "Только этот канал/чат: @username или часть названия"},
                    "limit": {"type": "integer", "description": "Сколько сообщений вернуть (по умолчанию 25, до 60)"},
                },
                [], "💬 Читаю Telegram-каналы",
                tg_posts,
            )

            async def ultra(symbol: str = "", hours: int = 24) -> dict:
                return ultra_alerts(store, symbol, max(1, min(int(hours), 24 * 14)))

            self._add(
                "ultra_alerts",
                "Алерты Ultra (OVERTAKE OI-бот) из Telegram: всплески открытого интереса с ценой, фандингом, L/S, "
                "Book Delta, A/D спот/перп, OI/MCap и стенами. Без тикера — рейтинг монет за период (серии, обе "
                "биржи, паттерн и красные флаги по методичке); с тикером — вся серия алертов по монете. "
                "Это фильтр-кандидатов: перед выводом проверь текущие цифры другими инструментами.",
                {
                    "symbol": {"type": "string", "description": "Тикер; пусто — рейтинг всех монет с алертами"},
                    "hours": {"type": "integer", "description": "За сколько часов (по умолчанию 24, до 336)"},
                },
                [], "📡 Смотрю алерты Ultra",
                ultra,
            )
        if self.journal is not None:
            journal = self.journal

            async def reference_calls(symbol: str = "", tag: str = "") -> dict:
                from .. import reference

                return await asyncio.to_thread(reference.lookup, journal.db, symbol, tag)

            self._add(
                "reference_calls",
                "Колы референсного авто-колера (учебный материал) с их реальным исходом на свечах: по монете или "
                "по типу идеи, и статистика, какие идеи у него работали (винрейт до TP1, средний R, доля "
                "неисполненных). Смотри перед сетапом похожего типа. Его уровни не копировать.",
                {
                    "symbol": {"type": "string", "description": "Тикер; пусто — все"},
                    "tag": {"type": "string", "enum": ["", "exhausted_squeeze", "squeeze", "oi_flip", "oi_alert",
                                                       "onchain_distribution", "onchain_accumulation", "funding_payout",
                                                       "listing", "news", "fvg_limit", "range_extreme", "spot_led",
                                                       "tokenized_stock"]},
                },
                [], "📚 Смотрю опыт референса",
                reference_calls,
            )

            async def track_record(symbol: str = "", days: int = 90) -> dict:
                return journal.track_record(symbol, int(days))

            self._add(
                "track_record",
                "Твоя собственная история сетапов: сколько было, исходы (стоп/TP1/TP2/не исполнился), винрейт, "
                "средний R, MFE/MAE; можно по тикеру. Проверяй перед новым сетапом — особенно по той же монете.",
                {
                    "symbol": {"type": "string", "description": "Тикер; пусто — все сетапы"},
                    "days": {"type": "integer", "description": "За сколько дней (по умолчанию 90)"},
                },
                [], "📒 Смотрю свою статистику",
                track_record,
            )
        store_for_cc = self.tg_store

        async def cross_check(topic: str, hours: int = 24) -> dict:
            return await cc.cross_check(
                topic, hours, tg_store=store_for_cc, hide_private=not s.tg_show_private,
                x_key=s.twitterapi_io_key, cryptopanic_key=s.cryptopanic_api_key, pool=self.pool,
                web_keys={"tavily_key": s.tavily_api_key, "brave_key": s.brave_api_key},
            )

        self._add(
            "cross_check",
            "Сопоставить одну новость/тему/тикер по ВСЕМ источникам сразу: Telegram-каналы и чаты, X, "
            "крипто-СМИ, новости из интернета + цена. Даёт хронологию, кто написал первым, сколько независимых источников, волны "
            "репостов, реакцию цены с первого упоминания и за 6ч до него. Используй для любой новости, слуха, "
            "пампа или «почему растёт/падает».",
            {
                "topic": {"type": "string", "description": "Тикер (HYPE) или тема (\"Binance listing\", \"TON Durov\")"},
                "hours": {"type": "integer", "description": "Окно в часах (по умолчанию 24, до 720)"},
            },
            ["topic"], "🔀 Сопоставляю источники",
            cross_check,
        )
        if self.vision is not None and self.vision.enabled:
            vision, collector = self.vision, self.collector

            async def analyze_image(ref: str) -> dict:
                if ref.startswith("tg:"):
                    if collector is None:
                        return {"error": "Telegram-сборщик не запущен"}
                    _, chat_id, msg_id = ref.split(":")
                    cached = collector.store.get_message(int(chat_id), int(msg_id))
                    if cached and cached.get("image_text"):
                        return {"ref": ref, "description": cached["image_text"], "cached": True}
                    return {"ref": ref, "description": await collector.describe_message(int(chat_id), int(msg_id))}
                if ref.startswith("http"):
                    return {"ref": ref, "description": await vision.describe_url(ref)}
                return {"error": "ref должен быть tg:<chat_id>:<msg_id> или https-ссылкой на картинку"}

            self._add(
                "analyze_image",
                "Разобрать картинку: график с уровнями, скрин позиции/PnL, скрин новости, ончейн-скрин. ref — "
                "'tg:<chat_id>:<msg_id>' из telegram_channels (поле image) или https-ссылка из твита (images). "
                "Вызывай, когда картинка важна для вывода.",
                {"ref": {"type": "string"}}, ["ref"], "🖼 Смотрю картинку",
                analyze_image,
            )
        self._add(
            "web_search",
            "Поиск в интернете: анонсы бирж и проектов, документация, блоги, регуляторы, взломы, разлоки, "
            "команда, партнёрства — всё, чего нет в рыночных данных. recent_days > 0 — поиск свежих новостей "
            "за этот период (с датами). Возвращает заголовки, ссылки, выдержки; нужную страницу читай "
            "fetch_page.",
            {
                "query": {"type": "string", "description": "Запрос; для крипто-новостей лучше на английском"},
                "recent_days": {"type": "integer", "description": "0 — обычный поиск; 1/7/30 — новости за период"},
                "max_results": {"type": "integer", "description": "Сколько результатов (по умолчанию 8, до 15)"},
            },
            ["query"], "🌐 Ищу в интернете",
            lambda query, recent_days=0, max_results=8: websearch.web_search(
                query, recent_days, max_results, tavily_key=s.tavily_api_key, brave_key=s.brave_api_key),
        )
        self._add(
            "fetch_page",
            "Прочитать текст страницы по ссылке (из поиска, новостей, твита): статья, анонс, документация.",
            {"url": {"type": "string"}}, ["url"], "📄 Читаю страницу",
            lambda url: websearch.fetch_page(url, s.jina_api_key, s.web_reader),
        )
        self._add(
            "calc_setup",
            "ОБЯЗАТЕЛЬНО перед любым сетапом. Проверяет вход/стоп/цели: геометрию, минимальную дистанцию стопа "
            "(с учётом ATR), минимальный R:R; считает R:R к каждой цели, % до стопа и потери/прибыль для плеч "
            "3/5/10/20x. Вердикт OK/REJECT/ERROR с причинами и подсказками. entry_status — где живая цена "
            "относительно плана (in_zone / away — только лимитка / gone — вход ушёл / invalid — за стопом); "
            "warnings — ликвидность сразу за стопом (риск съёма стопа).",
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
            result = await asyncio.wait_for(tool.run(**(args or {})), TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"error": f"источник не ответил за {TOOL_TIMEOUT} с"}
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
