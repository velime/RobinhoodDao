"""News and social: crypto media RSS, CryptoPanic, X (via twitterapi.io), web search.

X is used ONLY for crypto news and opinions of influencers — never as a
source of market numbers (price, OI, funding come from exchanges).
"""

from __future__ import annotations

import asyncio
import email.utils
import html
import re
import time
import xml.etree.ElementTree as ET

from . import http
from .symbols import normalize_base

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
    "CryptoSlate": "https://cryptoslate.com/feed/",
}

TWITTERAPI = "https://api.twitterapi.io/twitter/tweet/advanced_search"

_rss_cache: tuple[float, list[dict]] | None = None


def _parse_rss(source: str, xml_text: str) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", html.unescape(it.findtext("description") or "")).strip()
        pub = it.findtext("pubDate")
        ts = None
        if pub:
            try:
                ts = email.utils.parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                ts = None
        items.append({"source": source, "title": title, "summary": desc[:300], "link": it.findtext("link"), "ts": ts})
    return items


async def _all_rss() -> list[dict]:
    global _rss_cache
    if _rss_cache and time.time() - _rss_cache[0] < 300:
        return _rss_cache[1]

    async def one(name, url):
        r = await http.client().get(url)
        r.raise_for_status()
        return _parse_rss(name, r.text)

    res = await asyncio.gather(*(one(n, u) for n, u in RSS_FEEDS.items()), return_exceptions=True)
    items = [x for r in res if isinstance(r, list) for x in r]
    _rss_cache = (time.time(), items)
    return items


def _age_h(ts: float | None) -> float | None:
    return round((time.time() - ts) / 3600, 1) if ts else None


async def crypto_news(query: str = "", hours: int = 48, cryptopanic_key: str = "") -> dict:
    """Latest crypto headlines, optionally filtered by ticker/keyword."""
    items = await _all_rss()
    cutoff = time.time() - hours * 3600
    terms = []
    if query:
        base = normalize_base(query)
        terms = list({query.lower().strip(), base.lower()})
    sel = []
    for it in items:
        if it["ts"] and it["ts"] < cutoff:
            continue
        text = f"{it['title']} {it['summary']}".lower()
        if terms and not any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms):
            continue
        sel.append(it)
    sel.sort(key=lambda x: -(x["ts"] or 0))
    out = {
        "query": query or None,
        "headlines": [
            {"source": x["source"], "title": x["title"], "summary": x["summary"], "age_hours": _age_h(x["ts"]), "link": x["link"]}
            for x in sel[:12]
        ],
        "sources_checked": list(RSS_FEEDS),
    }
    if cryptopanic_key and query:
        try:
            cp = await http.get_json(
                "https://cryptopanic.com/api/developer/v2/posts/",
                {"auth_token": cryptopanic_key, "currencies": normalize_base(query), "public": "true"},
            )
            out["cryptopanic"] = [
                {"title": p.get("title"), "published_at": p.get("published_at"), "votes": p.get("votes")}
                for p in (cp.get("results") or [])[:10]
            ]
        except Exception as e:  # noqa: BLE001
            out["cryptopanic_error"] = str(e)
    return out


def _tweet(t: dict) -> dict:
    a = t.get("author") or {}
    return {
        "author": a.get("userName"),
        "followers": a.get("followers"),
        "verified": a.get("isBlueVerified"),
        "text": (t.get("text") or "")[:500],
        "created_at": t.get("createdAt"),
        "likes": t.get("likeCount"),
        "retweets": t.get("retweetCount"),
        "views": t.get("viewCount"),
        "url": t.get("url"),
    }


async def _x_search(api_key: str, query: str, query_type: str = "Latest", max_items: int = 20) -> list[dict]:
    data = await http.get_json(
        TWITTERAPI, {"query": query, "queryType": query_type, "cursor": ""}, {"X-API-Key": api_key}
    )
    return [_tweet(t) for t in (data.get("tweets") or [])[:max_items]]


async def x_discussion(api_key: str, topic: str, min_likes: int = 20) -> dict:
    """What crypto X says about a token/topic: top posts, no replies/retweets."""
    if not api_key:
        return {"error": "X не подключён (нет TWITTERAPI_IO_KEY)"}
    t = topic.strip()
    term = f"${normalize_base(t)}" if len(t) <= 12 and " " not in t else f'"{t}"'
    q = f"{term} min_faves:{min_likes} -filter:replies -filter:retweets"
    top, latest = await asyncio.gather(
        _x_search(api_key, q, "Top", 12),
        _x_search(api_key, f"{term} -filter:replies -filter:retweets", "Latest", 12),
        return_exceptions=True,
    )
    return {
        "query": q,
        "top": top if isinstance(top, list) else {"error": str(top)},
        "latest": latest if isinstance(latest, list) else {"error": str(latest)},
        "source": "X via twitterapi.io",
        "rule": "это мнения и новости, не данные — цифры рынка бери с бирж",
    }


async def x_influencers(api_key: str, accounts: tuple[str, ...], topic: str = "", hours: int = 24) -> dict:
    """Latest posts from the configured influencer/news accounts, optionally about a topic."""
    if not api_key:
        return {"error": "X не подключён (нет TWITTERAPI_IO_KEY)"}
    if not accounts:
        return {"error": "список аккаунтов X_ACCOUNTS пуст"}
    since = time.strftime("%Y-%m-%d_%H:%M:%S_UTC", time.gmtime(time.time() - hours * 3600))
    extra = ""
    if topic:
        t = topic.strip()
        extra = f" (${normalize_base(t)} OR {normalize_base(t)})" if " " not in t else f' "{t}"'
    # X search queries have a length limit — split accounts into chunks
    chunks = [accounts[i : i + 15] for i in range(0, len(accounts), 15)]
    queries = [
        "(" + " OR ".join(f"from:{a}" for a in ch) + ")" + extra + f" since:{since} -filter:replies"
        for ch in chunks
    ]
    res = await asyncio.gather(*(_x_search(api_key, q, "Latest", 20) for q in queries), return_exceptions=True)
    tweets = [t for r in res if isinstance(r, list) for t in r]
    errors = [str(r) for r in res if isinstance(r, Exception)]
    return {
        "topic": topic or None,
        "hours": hours,
        "posts": tweets[:30],
        "errors": errors or None,
        "accounts_watched": len(accounts),
        "source": "X via twitterapi.io",
        "rule": "это мнения и новости, не данные — цифры рынка бери с бирж",
    }


async def web_search(api_key: str, query: str) -> dict:
    if not api_key:
        return {"error": "веб-поиск не подключён (нет TAVILY_API_KEY)"}
    data = await http.post_json(
        "https://api.tavily.com/search",
        {"query": query, "max_results": 6, "search_depth": "basic", "include_answer": False},
        {"Authorization": f"Bearer {api_key}"},
    )
    return {
        "query": query,
        "results": [
            {"title": r.get("title"), "url": r.get("url"), "content": (r.get("content") or "")[:700]}
            for r in data.get("results", [])
        ],
        "source": "Tavily",
    }


async def fetch_page(url: str) -> dict:
    r = await http.client().get(url)
    r.raise_for_status()
    text = r.text
    text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return {"url": url, "text": text[:6000], "truncated": len(text) > 6000}
