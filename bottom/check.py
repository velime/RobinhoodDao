"""Check the setup: python -m bottom check

Verifies .env, then asks every data source for a small real answer: exchanges, Binance Alpha, awtrader, X
(each Nitter mirror, the embed timeline), web search and the page reader, CoinGecko, DexScreener, DefiLlama,
news feeds, the models and the Telegram bot. Each line says what works and, if not, what to do.
"""

from __future__ import annotations

import asyncio
import os
import sys

from .config import LLMConfig, Settings, load_settings

OK, FAIL, WARN = "✅", "❌", "⚠️"


def static_checks(s: Settings) -> list[tuple[str, str, str]]:
    """Checks that need no network. Returns (status, what, hint)."""
    out = []
    if not os.path.exists(".env"):
        out.append((FAIL, "Файл .env", "не найден — запусти install.bat или скопируй .env.example в .env"))
    if not s.telegram_token:
        out.append((WARN, "TELEGRAM_BOT_TOKEN", "пусто — Telegram-бот не запустится (Claude Code в claude_chat.bat "
                                                "работает и без него); токен — у @BotFather"))
    elif ":" not in s.telegram_token:
        out.append((FAIL, "TELEGRAM_BOT_TOKEN", "неверный формат — токен у @BotFather вида 123456:ABC..."))
    if s.llm.provider == "claude_code":
        from .llm.claude_code import find_claude

        exe = find_claude()
        out.append((OK, "Claude Code", exe) if exe else
                   (FAIL, "Claude Code", "не найден — запусти claude_setup.bat (или укажи CLAUDE_BIN)"))
    elif s.llm.provider == "agy":
        from .llm.agy import find_agy

        exe = find_agy()
        out.append((OK, "Antigravity CLI (agy)", exe) if exe else
                   (FAIL, "Antigravity CLI (agy)", "не найден — установи с antigravity.google, войди (agy) или укажи AGY_PATH"))
    else:
        if not s.llm.model:
            out.append((FAIL, "LLM_MODEL", "не задана модель — впиши имя модели Gemini из AI Studio"))
        if s.llm.provider != "anthropic" and not s.llm.api_key:
            out.append((FAIL, "LLM_API_KEY", "пусто — ключ Gemini: https://aistudio.google.com/apikey"))
    if s.llm_fallback is None:
        out.append((WARN, "Запасная модель", "не задана (FALLBACK_LLM_MODEL) — бот будет работать только на основной"))
    elif s.llm_fallback.provider != "anthropic" and not s.llm_fallback.api_key:
        out.append((FAIL, "FALLBACK_LLM_API_KEY", "пусто — ключ OpenRouter: https://openrouter.ai/settings/keys"))
    if s.tg_api_id or s.tg_api_hash:
        if not (s.tg_api_id and s.tg_api_hash):
            out.append((FAIL, "TG_API_ID / TG_API_HASH", "заполнены не оба — возьми на https://my.telegram.org"))
        elif not os.path.exists(s.tg_session + ".session"):
            out.append((WARN, "Telegram-сессия", "нет входа — запусти tg_login.bat"))
        elif not os.path.exists(s.tg_sources_file):
            out.append((WARN, "Источники Telegram", f"нет {s.tg_sources_file}"))
        else:
            from .tg.sources import load_all

            src = load_all(s.tg_sources_file)
            priv = sum(1 for x in src if x.origin != s.tg_sources_file)
            on = sum(1 for x in src if x.enabled)
            out.append((OK, "Telegram-источники", f"сессия есть; читается {on} из {len(src)} (из них приватных "
                                                  f"{priv} в channels/private.json)"))
    else:
        out.append((WARN, "Telegram-каналы", "не настроены (TG_API_ID/TG_API_HASH) — бот работает без них"))
    return out


async def _check_bot(token: str) -> tuple[str, str, str]:
    from aiogram import Bot

    bot = Bot(token)
    try:
        me = await bot.get_me()
        return OK, "Telegram-бот", f"@{me.username}"
    except Exception as e:  # noqa: BLE001
        return FAIL, "Telegram-бот", f"токен не принят: {e}"
    finally:
        await bot.session.close()


async def _check_llm(label: str, cfg: LLMConfig) -> tuple[str, str, str]:
    from .llm import single

    try:
        sess = single(cfg).new_session("Отвечай одним словом.", [], "Скажи: OK")
        res = await asyncio.wait_for(sess.step([], allow_tools=False), 400 if cfg.provider == "agy" else 60)
        return OK, label, f"{cfg.model or cfg.provider} отвечает: {res.text.strip()[:30]!r}"
    except Exception as e:  # noqa: BLE001
        return FAIL, label, f"{cfg.model or cfg.provider}: {type(e).__name__}: {str(e)[:200]}"


PING = {  # light public endpoints: does the exchange answer from this network at all
    "binance": "https://api.binance.com/api/v3/ping", "binance futures": "https://fapi.binance.com/fapi/v1/ping",
    "bybit": "https://api.bybit.com/v5/market/time", "okx": "https://www.okx.com/api/v5/public/time",
    "bitget": "https://api.bitget.com/api/v2/public/time", "gate": "https://api.gateio.ws/api/v4/spot/time",
    "mexc": "https://api.mexc.com/api/v3/ping", "kucoin": "https://api.kucoin.com/api/v1/timestamp",
    "bingx": "https://open-api.bingx.com/openApi/swap/v2/server/time", "aster": "https://fapi.asterdex.com/fapi/v1/ping",
}


async def _probe(what: str, make, miss: str = WARN) -> tuple[str, str, str]:
    try:
        return OK, what, str(await asyncio.wait_for(make(), 30))[:160]
    except Exception as e:  # noqa: BLE001
        return miss, what, f"не отвечает: {type(e).__name__}: {str(e)[:120]}"


def source_probes(s: Settings) -> list:
    from .tools import http
    from .tools.market import alpha, awtrader
    from .tools.social import news, x
    from .tools.web import fetch_page, web_search

    async def ping(url):
        r = await http.client().get(url, timeout=12)
        r.raise_for_status()
        return f"HTTP {r.status_code}"

    async def hyperliquid():
        d = await http.post_json("https://api.hyperliquid.xyz/info", {"type": "meta"})
        return f"{len(d.get('universe') or [])} перпов"

    async def alpha_list():
        return f"{len(await alpha.token_list(force=True))} токенов"

    async def aw_funding():
        d = await awtrader.funding(top=1)
        if d.get("error"):
            raise RuntimeError(d["error"])
        return f"{d['coins_in_table']} монет в таблице фандинга"

    async def mirror(base):
        r = await http.client().get(f"{base}/WatcherGuru/rss", headers={"User-Agent": x.BROWSER_UA}, timeout=12)
        r.raise_for_status()
        return f"{len(x.parse_nitter_rss(r.text, base))} постов"

    async def embed():
        r = await http.client().get(x.SYNDICATION.format(user="WatcherGuru"), headers={"User-Agent": x.BROWSER_UA},
                                    timeout=12)
        r.raise_for_status()
        return f"{len(x.parse_syndication(r.text))} постов"

    async def search():
        d = await web_search("bitcoin price", 0, 3, tavily_key=s.tavily_api_key, brave_key=s.brave_api_key)
        if d.get("error"):
            raise RuntimeError(d.get("providers_tried"))
        return f"{d['provider']}: {len(d['results'])} результата"

    async def reader():
        d = await fetch_page("https://example.com", s.jina_api_key, s.web_reader)
        if d.get("error"):
            raise RuntimeError(d["error"])
        return f"{d.get('reader') or 'ok'}: {len(d.get('text') or '')} символов"

    async def rss():
        items = await news._all_rss()
        if not items:
            raise RuntimeError("ни одна лента не ответила")
        return f"{len(items)} заголовков из {len({i['source'] for i in items})} лент"

    async def get(url):
        await http.get_json(url)
        return "ok"

    exchanges = [_probe(f"Биржа {name}", lambda u=url: ping(u)) for name, url in PING.items()]
    exchanges.append(_probe("Биржа hyperliquid", hyperliquid))
    mirrors = s.x_nitter_urls or x.NITTER_DEFAULT
    return [
        *exchanges,
        _probe("Binance Alpha", alpha_list),
        _probe("awtrader (спреды и фандинг)", aw_funding),
        *[_probe(f"X: зеркало {m}", lambda m=m: mirror(m.rstrip("/"))) for m in mirrors],
        _probe("X: лента для встраивания", embed),
        _probe("Поиск в интернете", search, FAIL),
        _probe("Чтение страниц", reader),
        _probe("Крипто-СМИ (RSS)", rss),
        _probe("CoinGecko", lambda: get("https://api.coingecko.com/api/v3/ping")),
        _probe("DexScreener", lambda: get("https://api.dexscreener.com/latest/dex/search?q=PEPE")),
        _probe("DefiLlama", lambda: get("https://api.llama.fi/tvl/uniswap")),
    ]


async def run() -> int:
    s = load_settings()
    rows = static_checks(s)
    net = []
    if s.telegram_token:
        net.append(_check_bot(s.telegram_token))
    if s.llm.model or s.llm.provider in ("claude_code", "agy"):
        net.append(_check_llm("Основная модель", s.llm))
    if s.llm_fallback:
        net.append(_check_llm("Запасная модель", s.llm_fallback))
    net += source_probes(s)
    rows += await asyncio.gather(*net)
    from .tools import http

    await http.close()

    print("\nПроверка настроек и источников Bottom\n")
    for status, what, hint in rows:
        print(f"{status} {what}: {hint}")
    fails = sum(1 for r in rows if r[0] == FAIL)
    xs = [r for r in rows if r[1].startswith("X:")]
    if xs and all(r[0] != OK for r in xs):
        print(f"\n{WARN} X: ни один бесплатный источник не ответил — бот будет искать по X через поиск в интернете "
              "(только выдержки). Добавь живые зеркала в X_NITTER_URLS или ключ TWITTERAPI_IO_KEY.")
    print("\n" + ("Всё готово — запускай start.bat" if not fails else f"Исправь пункты с {FAIL} и запусти проверку снова"))
    return 1 if fails else 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
