"""Cross-check one topic across all sources: Telegram channels/chats, X, crypto media, and price.

Answers: who said it first, how many independent sources confirm it, is it a
repost wave or real news, and did the price move before or after the news.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

from . import exchanges as exch
from . import news
from .symbols import normalize_base

_TW_FMT = "%a %b %d %H:%M:%S %z %Y"


def _tweet_ts(created_at: str | None) -> float | None:
    if not created_at:
        return None
    for fmt in (_TW_FMT, "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(created_at.replace("Z", "+0000"), fmt).timestamp()
        except ValueError:
            continue
    return None


def _norm(text: str) -> str:
    t = re.sub(r"https?://\S+|[@#$]\w+|[^\w\s]", " ", text.lower())
    return " ".join(t.split())[:80]


def build_timeline(events: list[dict], now: float | None = None) -> dict:
    """events: {ts, type, source, text, private?} → timeline, first mentions, repost waves."""
    now = now or time.time()
    events = sorted((e for e in events if e.get("ts")), key=lambda e: e["ts"])
    counts: dict[str, int] = {}
    first: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
        first.setdefault(e["type"], {"age_hours": round((now - e["ts"]) / 3600, 1), "source": e["source"]})
        groups.setdefault(_norm(e["text"]), []).append(e)
    waves = [
        {"text": g[0]["text"][:160], "copies": len(g), "sources": len({x["source"] for x in g}),
         "first_source": g[0]["source"]}
        for g in groups.values() if len(g) >= 2
    ]
    waves.sort(key=lambda w: -w["copies"])
    return {
        "counts_by_type": counts,
        "independent_sources": len({e["source"] for e in events}),
        "first_seen_by_type": first,
        "first_overall": ({"type": events[0]["type"], "source": events[0]["source"],
                           "age_hours": round((now - events[0]["ts"]) / 3600, 1), "text": events[0]["text"][:200]}
                          if events else None),
        "repost_waves": waves[:5],
        "timeline": [
            {"age_hours": round((now - e["ts"]) / 3600, 1), "type": e["type"], "source": e["source"],
             "private": e.get("private", False), "text": e["text"][:220]}
            for e in events[:30]
        ],
    }


def price_reaction(candles: list[dict], first_ts: float | None) -> dict | None:
    """Price at the first mention vs now, and the move in the 6h before it (was it priced in?)."""
    if not candles or not first_ts:
        return None
    t_ms = first_ts * 1000
    idx = next((i for i, c in enumerate(candles) if c["t"] <= t_ms < c["t"] + 3_600_000), None)
    if idx is None:
        return {"note": "первое упоминание вне окна свечей"}
    at = candles[idx]["o"]
    now = candles[-1]["c"]
    before = candles[max(idx - 6, 0)]["o"]
    return {
        "price_at_first_mention": at,
        "price_now": now,
        "change_since_first_mention_pct": round((now - at) / at * 100, 2),
        "change_6h_before_first_mention_pct": round((at - before) / before * 100, 2),
        "max_after_pct": round((max(c["h"] for c in candles[idx:]) - at) / at * 100, 2),
        "min_after_pct": round((min(c["l"] for c in candles[idx:]) - at) / at * 100, 2),
    }


async def cross_check(topic: str, hours: int, *, tg_store=None, hide_private: bool = True, x_key: str = "",
                      cryptopanic_key: str = "", pool=None) -> dict:
    hours = max(1, min(int(hours), 24 * 30))
    now = time.time()
    is_ticker = len(topic.strip()) <= 12 and " " not in topic.strip()

    async def tg():
        return tg_store.search(topic, hours, limit=60, hide_private=hide_private) if tg_store else None

    async def x():
        return await news.x_discussion(x_key, topic) if x_key else None

    async def px():
        if not (pool and is_ticker):
            return None
        candles, _meta = await exch.ohlcv(pool, topic, "1h", min(hours + 8, 500))
        return candles

    tg_r, news_r, x_r, candles = await asyncio.gather(
        tg(), news.crypto_news(topic, hours, cryptopanic_key), x(), px(), return_exceptions=True
    )
    events: list[dict] = []
    errors = {}
    if isinstance(tg_r, dict):
        for p in tg_r.get("posts", []):
            text = p["text"] + (f" [картинка: {p['image']}]" if p.get("image") and "не разобрана" not in p["image"] else "")
            events.append({"ts": now - p["age_hours"] * 3600, "type": "telegram_group" if p["kind"] != "channel"
                           else "telegram_channel", "source": p["source"], "text": text, "private": p["private"]})
    elif isinstance(tg_r, Exception):
        errors["telegram"] = str(tg_r)
    if isinstance(news_r, dict):
        for h in news_r.get("headlines", []):
            if h.get("age_hours") is not None:
                events.append({"ts": now - h["age_hours"] * 3600, "type": "media", "source": h["source"],
                               "text": h["title"]})
    elif isinstance(news_r, Exception):
        errors["media"] = str(news_r)
    if isinstance(x_r, dict):
        for tw in (x_r.get("latest") if isinstance(x_r.get("latest"), list) else []) + \
                  (x_r.get("top") if isinstance(x_r.get("top"), list) else []):
            ts = _tweet_ts(tw.get("created_at"))
            if ts and ts >= now - hours * 3600:
                events.append({"ts": ts, "type": "x", "source": f"@{tw.get('author')}", "text": tw.get("text") or ""})
    elif isinstance(x_r, Exception):
        errors["x"] = str(x_r)

    # de-duplicate the same tweet coming from top+latest
    seen, uniq = set(), []
    for e in events:
        k = (e["source"], e["text"][:120])
        if k not in seen:
            seen.add(k)
            uniq.append(e)

    out = {"topic": normalize_base(topic) if is_ticker else topic, "window_hours": hours, **build_timeline(uniq, now)}
    first_ts = min((e["ts"] for e in uniq), default=None)
    if isinstance(candles, list):
        out["price_reaction"] = price_reaction(candles, first_ts)
    elif isinstance(candles, Exception):
        errors["price"] = str(candles)
    if errors:
        out["source_errors"] = errors
    out["how_to_read"] = (
        "first_overall — кто первым; independent_sources — сколько разных источников; repost_waves — одна "
        "новость, разнесённая копиями (это не подтверждения); media/официальные источники весомее каналов; "
        "change_6h_before_first_mention_pct большой — рынок знал раньше / уже в цене"
    )
    return out
