"""Token identity and fundamentals (CoinGecko), DEX pairs, contract security (GoPlus), DeFi (DefiLlama), DEX whales."""

from __future__ import annotations

from . import dex, tokens


def register(tb) -> None:
    s = tb.s
    tb.add(
        "resolve_token",
        "Определить, какой именно токен имеет в виду пользователь: тикер/название/контракт → кандидаты "
        "(CoinGecko id, ранг). Вызывай первым, если тикер редкий, неоднозначный или это адрес контракта.",
        {"query": {"type": "string", "description": "Тикер, название или адрес контракта"}},
        ["query"], "🔎 Определяю токен",
        lambda query: tokens.resolve_token(query, s.coingecko_api_key),
    )

    tb.add(
        "token_fundamentals",
        "Фундаментал с CoinGecko: описание, категории, MCap, FDV, FDV/MCap, оборот/MCap, эмиссия, ATH, "
        "изменения 1ч/24ч/7д/30д, контракты по сетям, сайт и X проекта. Нужен coingecko_id из resolve_token.",
        {"coingecko_id": {"type": "string"}}, ["coingecko_id"], "🧾 Смотрю фундаментал",
        lambda coingecko_id: tokens.token_fundamentals(coingecko_id, s.coingecko_api_key),
    )

    tb.add(
        "dex_pairs",
        "DEX-пары (DexScreener, если пусто — GeckoTerminal): сеть, DEX, цена, ликвидность, объём 1ч/24ч, "
        "покупки/продажи, FDV, возраст пары. Для мемов, свежих запусков, токенов без CEX и новых сетей.",
        {"query": {"type": "string", "description": "Тикер или адрес контракта"}},
        ["query"], "🧪 Смотрю DEX-пары",
        lambda query: tokens.dex_pairs(query),
    )

    tb.add(
        "token_security",
        "Безопасность контракта (GoPlus): honeypot, налоги, mint, владелец, прокси, число холдеров, "
        "топ-10 холдеров и их доля, залоченность LP.",
        {
            "chain": {"type": "string", "description": "ethereum, bsc, base, arbitrum, solana, ..."},
            "address": {"type": "string"},
        },
        ["chain", "address"], "🛡 Проверяю контракт",
        lambda chain, address: tokens.token_security(chain, address),
    )

    tb.add(
        "defi_protocol",
        "DeFi-протокол по DefiLlama: TVL, изменение 1д/7д, сети, категория, MCap/TVL.",
        {"query": {"type": "string", "description": "Название или тикер протокола"}},
        ["query"], "🏦 Смотрю DefiLlama",
        lambda query: tokens.defi_protocol(query),
    )

    tb.add(
        "market_sentiment",
        "Фон рынка: Fear & Greed, общая капитализация и её изменение, доминация BTC/ETH, тренды CoinGecko.",
        {}, [], "🌡 Смотрю фон рынка",
        lambda: tokens.market_sentiment(s.coingecko_api_key),
    )

    tb.add(
        "dex_whales",
        "Кто сливает в главный DEX-пул монеты: кошельки с чистыми продажами за окно, доля от ликвидности пула.",
        {"query": {"type": "string", "description": "Тикер или адрес контракта"},
         "window_min": {"type": "integer", "description": "Окно, минут (по умолчанию 60)"}},
        ["query"], "🐋 Смотрю сливы на DEX",
        lambda query, window_min=60: dex.dex_whales(query, max(5, min(int(window_min), 1440))),
    )
