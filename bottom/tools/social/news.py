"""Crypto media: RSS of the big outlets and CryptoPanic (with a key). X lives in x.py."""

from __future__ import annotations

import asyncio
import email.utils
import html
import re
import time
import xml.etree.ElementTree as ET

from .. import http
from ..symbols import normalize_base

RSS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    "Bitcoin Magazine": "https://bitcoinmagazine.com/.rss/full/",
    "CryptoSlate": "https://cryptoslate.com/feed/",
}

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
