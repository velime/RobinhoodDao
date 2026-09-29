"""Image understanding: charts, position screenshots, news screenshots from channels and X.

Uses a vision-capable model (Gemini Flash / Claude / any OpenAI-compatible
vision model). A daily cap protects free-tier limits.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone

from .config import LLMConfig, Settings
from .tools import http

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024

IMAGE_PROMPT = """\
Опиши картинку из крипто-канала или твита для трейдера-аналитика. По-русски, кратко, только то, что \
реально видно, без домыслов.
1) Тип: график / скрин позиции или PnL / стакан / скрин новости или твита / ончейн (кошелёк, \
транзакция, холдеры) / таблица / листинг-анонс / мем / другое.
2) График: площадка, тикер, таймфрейм, текущая цена; нарисованные уровни, линии, зоны, стрелки \
(с ценами по шкале); направление идеи автора (лонг/шорт/нейтрально); индикаторы.
3) Скрин позиции: биржа, тикер, лонг/шорт, вход, плечо, PnL, ликвидация.
4) Весь значимый текст с картинки дословно: тикеры, цифры, даты, адреса, названия.
Если цифру не разобрать — так и напиши. Мем без смысла для рынка — одним предложением. До 700 символов."""


class Vision:
    def __init__(self, cfg: LLMConfig | None, fallback: LLMConfig | None = None, daily_limit: int = 300):
        self.cfg = cfg
        self.fallback = fallback
        self.daily_limit = daily_limit
        self._day = ""
        self._used = 0

    @property
    def enabled(self) -> bool:
        return bool(self.cfg and self.cfg.model)

    def remaining(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day, self._used = day, 0
        return max(self.daily_limit - self._used, 0)

    async def describe(self, data: bytes, mime: str = "image/jpeg", context: str = "") -> str:
        if not self.enabled:
            raise RuntimeError("модель для картинок не настроена")
        if self.remaining() <= 0:
            raise RuntimeError("дневной лимит разбора картинок исчерпан")
        if len(data) > MAX_IMAGE_BYTES:
            raise RuntimeError("картинка слишком большая")
        prompt = IMAGE_PROMPT + (f"\n\nКонтекст (подпись к посту): {context[:500]}" if context else "")
        self._used += 1
        last_err: Exception | None = None
        for cfg in [c for c in (self.cfg, self.fallback) if c and c.model]:
            try:
                return (await _call(cfg, data, mime, prompt)).strip()
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.info("vision %s failed: %s", cfg.name, e)
        raise RuntimeError(f"не удалось разобрать картинку: {last_err}")

    async def describe_url(self, url: str, context: str = "") -> str:
        r = await http.client().get(url)
        r.raise_for_status()
        mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if not mime.startswith("image/"):
            raise RuntimeError(f"по ссылке не картинка ({mime})")
        return await self.describe(r.content, mime, context)


async def _call(cfg: LLMConfig, data: bytes, mime: str, prompt: str) -> str:
    b64 = base64.standard_b64encode(data).decode("ascii")
    if cfg.provider == "anthropic":
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=cfg.api_key) if cfg.api_key else anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=cfg.model,
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("модель отказалась")
        return "".join(b.text for b in resp.content if b.type == "text")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=cfg.api_key or "none", base_url=cfg.base_url, max_retries=1)
    resp = await client.chat.completions.create(
        model=cfg.model,
        max_tokens=900,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    )
    if not resp.choices:
        raise RuntimeError("пустой ответ")
    return resp.choices[0].message.content or ""


def make_vision(s: Settings) -> Vision:
    return Vision(s.vision_llm or s.llm, s.llm_fallback, s.vision_daily_limit)
