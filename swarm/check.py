"""Check the setup: python -m swarm.check

Verifies .env and that every service answers, with a hint for each problem.
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
    if not s.telegram_token or ":" not in s.telegram_token:
        out.append((FAIL, "TELEGRAM_BOT_TOKEN", "пусто или неверный формат — возьми токен у @BotFather (вида 123456:ABC...)"))
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
        elif not os.path.exists(s.tg_channels_file):
            out.append((WARN, "Список каналов", f"нет {s.tg_channels_file} — запусти tg_scan.bat"))
        else:
            out.append((OK, "Telegram-каналы", "сессия и список каналов на месте"))
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
    from .llm import _single

    try:
        sess = _single(cfg).new_session("Отвечай одним словом.", [], "Скажи: OK")
        res = await asyncio.wait_for(sess.step([], allow_tools=False), 60)
        return OK, label, f"{cfg.model} отвечает: {res.text.strip()[:30]!r}"
    except Exception as e:  # noqa: BLE001
        return FAIL, label, f"{cfg.model}: {type(e).__name__}: {str(e)[:200]}"


async def _check_exchange() -> tuple[str, str, str]:
    from .tools import http

    try:
        d = await http.get_json("https://fapi.binance.com/fapi/v1/ticker/price", {"symbol": "BTCUSDT"})
        return OK, "Биржи (Binance)", f"BTC = {float(d['price']):,.0f}"
    except Exception as e:  # noqa: BLE001
        return WARN, "Биржи (Binance)", f"недоступен ({type(e).__name__}) — возможно, блок по региону; остальные биржи могут работать"


async def run() -> int:
    s = load_settings()
    rows = static_checks(s)
    net = []
    if s.telegram_token:
        net.append(_check_bot(s.telegram_token))
    if s.llm.model:
        net.append(_check_llm("Основная модель", s.llm))
    if s.llm_fallback:
        net.append(_check_llm("Запасная модель", s.llm_fallback))
    net.append(_check_exchange())
    rows += await asyncio.gather(*net)
    from .tools import http

    await http.close()

    print("\nПроверка настроек Swarm\n")
    for status, what, hint in rows:
        print(f"{status} {what}: {hint}")
    fails = sum(1 for r in rows if r[0] == FAIL)
    print("\n" + ("Всё готово — запускай start.bat" if not fails else f"Исправь пункты с {FAIL} в файле .env и запусти проверку снова"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
