"""What people say: Telegram channels, X, crypto media, Ultra OI alerts, cross-checking a story, pictures."""

from __future__ import annotations

from . import crosscheck as cc
from . import news
from .ultra import ultra_alerts


def register(tb) -> None:
    s = tb.s
    tb.add(
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
        tb.add(
            "x_discussion",
            "X (Twitter): что пишут о токене/теме — топовые и свежие посты без реплаев. Только новости и "
            "мнения, НЕ источник цифр рынка.",
            {"topic": {"type": "string", "description": "Тикер или тема"}},
            ["topic"], "🐦 Читаю X",
            lambda topic: news.x_discussion(s.twitterapi_io_key, topic),
        )
        tb.add(
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
    if tb.tg_store is not None:
        store = tb.tg_store

        async def tg_posts(query: str = "", hours: int = 24, source: str = "", limit: int = 25) -> dict:
            hours = max(1, min(int(hours), 24 * 180))
            return store.search(query, hours, limit=max(1, min(int(limit), 60)),
                                hide_private=not s.tg_show_private, source=source)

        tb.add(
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

        tb.add(
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

    async def cross_check(topic: str, hours: int = 24) -> dict:
        return await cc.cross_check(
            topic, hours, tg_store=tb.tg_store, hide_private=not s.tg_show_private,
            x_key=s.twitterapi_io_key, cryptopanic_key=s.cryptopanic_api_key, pool=tb.pool,
            web_keys={"tavily_key": s.tavily_api_key, "brave_key": s.brave_api_key},
        )

    tb.add(
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
    if tb.vision is not None and tb.vision.enabled:
        vision, collector = tb.vision, tb.collector

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

        tb.add(
            "analyze_image",
            "Разобрать картинку: график с уровнями, скрин позиции/PnL, скрин новости, ончейн-скрин. ref — "
            "'tg:<chat_id>:<msg_id>' из telegram_channels (поле image) или https-ссылка из твита (images). "
            "Вызывай, когда картинка важна для вывода.",
            {"ref": {"type": "string"}}, ["ref"], "🖼 Смотрю картинку",
            analyze_image,
        )
