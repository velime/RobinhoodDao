# Swarm — крипто-ресерчер в Telegram

ИИ-агент, которому можно задавать вопросы о крипте: разбор монеты, деривативы,
стакан, DEX и безопасность контракта, новости, X, сетапы с проверенным расчётом.
Сам сигналы не публикует — только отвечает. Спецификация всей системы:
[`docs/agent-spec.md`](docs/agent-spec.md).

## Как это работает

```
вопрос → модель решает, какие данные нужны → инструменты собирают данные
       → модель пишет разбор → сетап проходит валидатор (calc_setup) → ответ
```

- **Правила для модели** — `swarm/prompts.py`: цифры только из инструментов, порядок
  сбора, как читать OI/фандинг/стакан, жёсткие правила сетапов, формат ответа.
- **Валидатор сетапов** — `swarm/tools/setup_calc.py`: минимальный стоп (по классу актива
  и ATR), минимальный R:R, таблица потерь по плечам. Модель не имеет права показать сетап
  без вердикта OK.
- **Модель подключается конфигом**: любой OpenAI-совместимый API (бесплатные модели
  OpenRouter, Groq, Gemini, локальная Ollama) или Claude.

### Инструменты

| Инструмент | Что делает | Источник |
|---|---|---|
| `resolve_token` | какой именно токен (защита от однофамильцев) | CoinGecko |
| `market_overview` | цены спот/перп на 9 биржах, базис, разброс | ccxt |
| `derivatives` | фандинг, OI и его динамика, лонги/шорты, taker flow | Binance, Bybit, OKX + ccxt |
| `candles` | EMA, RSI, Stoch, ATR, уровни, дельта/CVD, объём | Binance / ccxt |
| `orderbook` | глубина, перекос, стенки | ccxt |
| `token_fundamentals` | MCap, FDV, эмиссия, категории, ATH | CoinGecko |
| `dex_pairs` | пары, ликвидность, объём, возраст | DexScreener |
| `token_security` | honeypot, налоги, холдеры, LP | GoPlus |
| `defi_protocol` | TVL и динамика | DefiLlama |
| `market_sentiment` | Fear & Greed, доминация, тренды | alternative.me, CoinGecko |
| `crypto_news` | заголовки крипто-СМИ | RSS, CryptoPanic (опц.) |
| `x_discussion`, `x_influencers` | новости и мнения в X (только с ключом) | twitterapi.io |
| `web_search`, `fetch_page` | веб-поиск и чтение страниц (поиск — с ключом) | Tavily |
| `calc_setup` | проверка и расчёт сетапа | свой код |

X используется **только для новостей и мнений инфлюенсеров**, не для цифр рынка.
Список аккаунтов — `X_ACCOUNTS` в `.env` (по умолчанию — в `swarm/config.py`).

## Запуск

1. **Бот в Telegram.** Напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получи токен.
2. **Модель.** Бесплатный вариант — [OpenRouter](https://openrouter.ai): зарегистрируйся,
   создай ключ и выбери бесплатную модель **с поддержкой инструментов**:
   <https://openrouter.ai/models?max_price=0&supported_parameters=tools>.
   Бесплатные модели бывают перегружены и с лимитами — если модель путает вызовы или
   отвечает пусто, попробуй другую.
3. **Настройки.**
   ```bash
   cp .env.example .env
   # заполни TELEGRAM_BOT_TOKEN, LLM_API_KEY, LLM_MODEL
   ```
4. **Установка и старт** (Python 3.11+):
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -e .
   python -m swarm
   ```
   Или в Docker:
   ```bash
   docker build -t swarm . && docker run --env-file .env -v $(pwd)/data:/data swarm
   ```
5. Напиши боту `/start`.

### Группы
Добавь бота в группу. Он отвечает, если его упомянули через `@имя_бота` или ответили
на его сообщение. Режим приватности BotFather можно не трогать — бот и так получает
такие сообщения. Если ответить боту реплаем на чужое сообщение с упоминанием, он учтёт
его текст как контекст.

### Команды
`/start`, `/help` — FAQ · `/limit` — остаток шагов на сегодня · `/reset` — очистить контекст.

## Настройки

| Переменная | Что это |
|---|---|
| `LLM_PROVIDER` | `openai` (любой OpenAI-совместимый API) или `anthropic` |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | провайдер и модель |
| `MAX_STEPS_PER_QUESTION` | максимум вызовов инструментов на один вопрос (12) |
| `DAILY_STEPS_PER_USER` | дневной лимит шагов на пользователя (50) |
| `ADMIN_IDS` | Telegram id без лимита |
| `EXCHANGES` | биржи ccxt |
| `MIN_RR`, `MIN_STOP_PCT_MAJOR`, `MIN_STOP_PCT_ALT`, `MIN_STOP_ATR_MULT`, `MAJORS` | правила валидатора |
| `TWITTERAPI_IO_KEY`, `X_ACCOUNTS` | X: ключ [twitterapi.io](https://twitterapi.io) и список аккаунтов |
| `TAVILY_API_KEY` | веб-поиск ([Tavily](https://tavily.com), есть бесплатный тариф) |
| `COINGECKO_API_KEY` | демо-ключ CoinGecko (выше лимиты) |
| `CRYPTOPANIC_API_KEY` | новости CryptoPanic |

### Переход на Claude
```env
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=...
LLM_MODEL=claude-opus-5      # или claude-sonnet-5 — дешевле
```
Для `claude-opus-5` включён серверный фолбэк при отказе модели (`fallbacks: "default"`).

## Разработка

```bash
pip install -e ".[dev]"
pytest
```
Тесты не ходят в сеть: валидатор проверяется на реальных примерах колов (опубликованные
колы проходят, ошибочные сетапы из чатов отклоняются), плюс индикаторы, форматирование,
цикл агента и формат сообщений обоих провайдеров.

## Дальше
- Чтение Telegram-каналов (юзербот) — после списка каналов.
- Ончейн: потоки на биржи и разметка адресов (Arkham/Nansen), Solana/TON/Tron.
- Индексатор Robinhood Chain.
- Caller (автоматические колы) — по спецификации.
