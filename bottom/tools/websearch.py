"""Web search and page reading.

Search providers, first available wins (a failed provider falls through to the next):
  1. Tavily  — if TAVILY_API_KEY (free tier with signup)
  2. Brave   — if BRAVE_API_KEY (free tier with signup)
  3. DuckDuckGo metasearch via `ddgs` — no key, always available

Page reading: Jina Reader (clean text, no key needed; JINA_API_KEY raises limits),
falling back to fetching the HTML directly.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone

from . import http

log = logging.getLogger(__name__)

MAX_PAGE_CHARS = 8000


def _timelimit(days: int) -> str | None:
    if days <= 0:
        return None
    return "d" if days <= 1 else "w" if days <= 7 else "m" if days <= 31 else "y"


def _age_hours(date: str | None) -> float | None:
    if not date:
        return None
    try:
        ts = datetime.fromisoformat(date.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1)
    except ValueError:
        return None


def _row(title, url, snippet, date=None, source=None) -> dict:
    return {
        "title": (title or "").strip(),
        "url": url,
        "snippet": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(snippet or ""))).strip()[:600],
        "date": date,
        "age_hours": _age_hours(date),
        "source": source,
    }


async def _tavily(key: str, query: str, days: int, n: int) -> list[dict]:
    body: dict = {"query": query, "max_results": n, "search_depth": "basic", "include_answer": False}
    if days > 0:
        body.update(topic="news", days=days)
    data = await http.post_json("https://api.tavily.com/search", body, {"Authorization": f"Bearer {key}"})
    return [_row(r.get("title"), r.get("url"), r.get("content"), r.get("published_date")) for r in data.get("results", [])]


async def _brave(key: str, query: str, days: int, n: int) -> list[dict]:
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    params: dict = {"q": query, "count": n}
    if days > 0:
        params["freshness"] = "pd" if days <= 1 else "pw" if days <= 7 else "pm" if days <= 31 else "py"
    data = await http.get_json("https://api.search.brave.com/res/v1/web/search", params, headers)
    items = ((data.get("news") or {}).get("results") or []) + ((data.get("web") or {}).get("results") or [])
    return [_row(r.get("title"), r.get("url"), r.get("description"), r.get("page_age"), (r.get("meta_url") or {}).get("hostname"))
            for r in items[:n]]


def _ddgs_sync(query: str, days: int, n: int) -> list[dict]:
    from ddgs import DDGS

    d = DDGS()
    if days > 0:
        rows = d.news(query, region="wt-wt", timelimit=_timelimit(days), max_results=n)
        return [_row(r.get("title"), r.get("url"), r.get("body"), r.get("date"), r.get("source")) for r in rows]
    rows = d.text(query, region="wt-wt", max_results=n)
    return [_row(r.get("title"), r.get("href"), r.get("body")) for r in rows]


async def _ddgs(query: str, days: int, n: int) -> list[dict]:
    return await asyncio.to_thread(_ddgs_sync, query, days, n)


async def web_search(query: str, recent_days: int = 0, max_results: int = 8, *,
                     tavily_key: str = "", brave_key: str = "") -> dict:
    """recent_days > 0 → news search limited to that period; 0 → general web search."""
    n = max(1, min(int(max_results), 15))
    days = max(0, int(recent_days))
    providers = []
    if tavily_key:
        providers.append(("Tavily", lambda: _tavily(tavily_key, query, days, n)))
    if brave_key:
        providers.append(("Brave", lambda: _brave(brave_key, query, days, n)))
    providers.append(("DuckDuckGo", lambda: _ddgs(query, days, n)))
    errors = {}
    for name, run in providers:
        try:
            rows = await asyncio.wait_for(run(), 25)
        except Exception as e:  # noqa: BLE001
            errors[name] = f"{type(e).__name__}: {str(e)[:150]}"
            log.info("web search %s failed: %s", name, e)
            continue
        if rows:
            out = {"query": query, "mode": "news" if days else "web", "provider": name, "results": rows}
            if errors:
                out["fallback_after"] = errors
            return out
        errors[name] = "пусто"
    return {"query": query, "error": "поиск ничего не дал или недоступен", "providers_tried": errors}


def _strip_html(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style|nav|footer|header|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


async def fetch_page(url: str, jina_key: str = "", use_reader: bool = True) -> dict:
    """Readable text of a page. Jina Reader first (handles JS sites), then direct fetch."""
    if not url.startswith(("http://", "https://")):
        return {"error": "нужна ссылка http(s)"}
    errors = {}
    if use_reader:
        try:
            headers = {"Accept": "text/plain", "X-Return-Format": "text"}
            if jina_key:
                headers["Authorization"] = f"Bearer {jina_key}"
            r = await http.client().get(f"https://r.jina.ai/{url}", headers=headers)
            r.raise_for_status()
            text = re.sub(r"\n{3,}", "\n\n", r.text).strip()
            if len(text) > 200:
                return {"url": url, "reader": "jina", "text": text[:MAX_PAGE_CHARS], "truncated": len(text) > MAX_PAGE_CHARS}
        except Exception as e:  # noqa: BLE001
            errors["jina"] = f"{type(e).__name__}: {str(e)[:120]}"
    r = await http.client().get(url)
    r.raise_for_status()
    text = _strip_html(r.text)
    out = {"url": url, "reader": "direct", "text": text[:MAX_PAGE_CHARS], "truncated": len(text) > MAX_PAGE_CHARS}
    if errors:
        out["reader_errors"] = errors
    return out
