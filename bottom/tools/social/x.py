"""X (Twitter): posts of accounts and search, through the first source that answers.

    1. twitterapi.io — paid, only with TWITTERAPI_IO_KEY; search and timelines, the most reliable.
    2. Nitter mirrors — free RSS of a Nitter instance: /<account>/rss and /search/rss?f=tweets&q=...
       Public mirrors come and go, so the list is X_NITTER_URLS; a mirror that fails is skipped for 10 minutes.
    3. X's embed timeline (syndication.twitter.com, what "embed a timeline" widgets load) — free, accounts only,
       can lag behind or show popular posts instead of the newest.
    4. Web search "site:x.com …" — only titles and snippets, the last resort.

Every result says where it came from ("via"). X is a source of news and opinions only — never of market numbers.
"""

from __future__ import annotations

import asyncio
import base64
import email.utils
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote, unquote

from .. import http
from ..symbols import normalize_base

TWITTERAPI = "https://api.twitterapi.io/twitter/tweet/advanced_search"
SYNDICATION = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{user}"
NITTER_DEFAULT = ("https://nitter.net", "https://xcancel.com", "https://nitter.poast.org",
                  "https://nitter.privacydev.net")
DEAD_FOR = 600  # a mirror that failed is skipped this long
CACHE_SEC = 180
REQUEST_TIMEOUT = 8
FREE_ACCOUNTS_MAX = 12  # free sources answer per account: read at most this many of the watched ones
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
RULE = "это мнения и новости, не данные — цифры рынка бери с бирж"
_TW_FMT = "%a %b %d %H:%M:%S %z %Y"

_dead: dict[str, float] = {}
_cache: dict[tuple, tuple[float, dict]] = {}


def tweet_ts(created_at: str | None) -> float | None:
    """'Sun Oct 26 10:00:00 +0000 2026' (X) or ISO-8601 → unix seconds."""
    if not created_at:
        return None
    for fmt in (_TW_FMT, "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(created_at.replace("Z", "+0000"), fmt).timestamp()
        except ValueError:
            continue
    return None


def _post(author, text, ts, url, via, **extra) -> dict:
    return {"author": (author or "").lstrip("@") or None, "text": (text or "").strip()[:500], "ts": ts,
            "age_hours": round((time.time() - ts) / 3600, 1) if ts else None, "url": url, "via": via,
            **{k: v for k, v in extra.items() if v is not None}}


def query_of(topic: str) -> str:
    """A ticker becomes a cashtag ($HYPE), a phrase is searched as is."""
    t = topic.strip()
    return f"${normalize_base(t)}" if t and len(t) <= 12 and " " not in t and not t.startswith(("@", "#")) else t


# ------------------------------------------------------------------ twitterapi.io (paid)


def from_twitterapi(t: dict) -> dict:
    a = t.get("author") or {}
    media = ((t.get("extendedEntities") or {}).get("media") or []) + ((t.get("entities") or {}).get("media") or [])
    images = list(dict.fromkeys(m.get("media_url_https") or m.get("url") for m in media
                                if m.get("type", "photo") == "photo" and (m.get("media_url_https") or m.get("url"))))
    return _post(a.get("userName"), t.get("text"), tweet_ts(t.get("createdAt")), t.get("url"), "twitterapi.io",
                 followers=a.get("followers"), likes=t.get("likeCount"), retweets=t.get("retweetCount"),
                 views=t.get("viewCount"), images=images[:4] or None)


async def _twitterapi(key: str, q: str, query_type: str = "Latest", n: int = 20) -> list[dict]:
    data = await http.get_json(TWITTERAPI, {"query": q, "queryType": query_type, "cursor": ""}, {"X-API-Key": key})
    return [from_twitterapi(t) for t in (data.get("tweets") or [])[:n]]


# ------------------------------------------------------------------ Nitter RSS (free)


def _image_url(src: str) -> str:
    """Nitter proxies pictures (/pic/media%2FXYZ.jpg, /pic/orig/…, /pic/enc/<base64>) — back to pbs.twimg.com."""
    m = re.search(r"/pic/(.+)$", src)
    if not m:
        return src
    path = unquote(m.group(1))
    if path.startswith("enc/"):
        try:
            path = base64.urlsafe_b64decode(path[4:] + "=" * (-len(path[4:]) % 4)).decode()
        except (ValueError, UnicodeDecodeError):
            return src
    path = path.removeprefix("orig/")
    return path if path.startswith("http") else "https://pbs.twimg.com/" + path.lstrip("/")


def parse_nitter_rss(xml_text: str, base: str) -> list[dict]:
    """Nitter RSS → posts with x.com links. Raises ValueError if this is not an RSS feed (blocked, captcha)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"не RSS: {xml_text[:60]!r}") from e
    if root.tag != "rss":
        raise ValueError("не RSS")
    out = []
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        author = it.findtext("{http://purl.org/dc/elements/1.1/}creator")
        desc = it.findtext("description") or ""
        text = html.unescape(re.sub(r"<[^>]+>", " ", desc)).strip() or (it.findtext("title") or "")
        text = re.sub(r"\s+", " ", text)
        pub = it.findtext("pubDate")
        try:
            ts = email.utils.parsedate_to_datetime(pub).timestamp() if pub else None
        except (TypeError, ValueError):
            ts = None
        images = [_image_url(src) for src in re.findall(r'<img[^>]+src="([^"]+)"', desc)]
        url = re.sub(r"#m$", "", link.replace(base, "https://x.com", 1)) if link.startswith(base) else link
        title = it.findtext("title") or ""
        out.append(_post(author, text, ts, url, "nitter", images=images[:4] or None,
                         repost=True if title.startswith("RT by") else None,
                         reply=True if title.startswith("R to") else None))
    return out


async def _nitter(mirrors: tuple[str, ...], path: str) -> list[dict]:
    """First live mirror that returns RSS. Raises with every mirror's error when none does."""
    errors = []
    now = time.time()
    for base in mirrors:
        base = base.rstrip("/")
        if now - _dead.get(base, 0) < DEAD_FOR:
            continue
        try:
            r = await http.client().get(base + path, headers={"User-Agent": BROWSER_UA}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return parse_nitter_rss(r.text, base)
        except Exception as e:  # noqa: BLE001
            _dead[base] = time.time()
            errors.append(f"{base}: {type(e).__name__}: {str(e)[:60]}")
    raise RuntimeError("; ".join(errors) or "все зеркала Nitter недавно не отвечали")


# ------------------------------------------------------------------ X embed timeline (free, accounts only)


def parse_syndication(page: str) -> list[dict]:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', page, re.S)
    if not m:
        raise ValueError("в ответе нет ленты (__NEXT_DATA__)")
    data = json.loads(m.group(1))
    entries = (((data.get("props") or {}).get("pageProps") or {}).get("timeline") or {}).get("entries") or []
    out = []
    for e in entries:
        t = (e.get("content") or {}).get("tweet") if e.get("type") == "tweet" else None
        if not t:
            continue
        user = t.get("user") or {}
        screen = user.get("screen_name")
        media = ((t.get("extended_entities") or {}).get("media") or []) + ((t.get("entities") or {}).get("media") or [])
        images = list(dict.fromkeys(x["media_url_https"] for x in media
                                    if x.get("type", "photo") == "photo" and x.get("media_url_https")))
        out.append(_post(screen, t.get("full_text") or t.get("text"), tweet_ts(t.get("created_at")),
                         f"https://x.com/{screen}/status/{t.get('id_str')}" if screen and t.get("id_str") else None,
                         "x-embed", followers=user.get("followers_count"), likes=t.get("favorite_count"),
                         retweets=t.get("retweet_count"), images=images[:4] or None))
    return sorted(out, key=lambda p: -(p["ts"] or 0))


async def _syndication(user: str) -> list[dict]:
    r = await http.client().get(SYNDICATION.format(user=quote(user)), headers={"User-Agent": BROWSER_UA},
                                timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return parse_syndication(r.text)


# ------------------------------------------------------------------ web search (snippets)


async def _web(query: str, hours: int, web_keys: dict | None) -> list[dict]:
    from ..web import web_search

    res = await web_search(f"site:x.com {query}", recent_days=max(1, (hours + 23) // 24), max_results=10,
                           **(web_keys or {}))
    out = []
    for w in res.get("results") or []:
        url = w.get("url") or ""
        if "x.com/" not in url and "twitter.com/" not in url:
            continue
        m = re.search(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status", url)
        age = w.get("age_hours")
        out.append(_post(m.group(1) if m else None, f"{w.get('title', '')} — {w.get('snippet', '')}",
                         time.time() - age * 3600 if age is not None else None, url, "web-search",
                         snippet_only=True))
    return out


# ------------------------------------------------------------------ reader


class XReader:
    def __init__(self, api_key: str = "", accounts: tuple[str, ...] = (), mirrors: tuple[str, ...] = NITTER_DEFAULT,
                 web_keys: dict | None = None):
        self.key, self.accounts, self.mirrors, self.web_keys = api_key, accounts, tuple(mirrors), web_keys or {}

    async def _cached(self, key: tuple, make) -> dict:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SEC:
            return hit[1]
        val = await make()
        if val.get("posts"):
            _cache[key] = (time.time(), val)
        return val

    async def search(self, query: str, hours: int = 24, limit: int = 20) -> dict:
        """Search all of X: $TICKER or a phrase."""
        q = query_of(query)

        async def make() -> dict:
            errors = {}
            if self.key:
                try:
                    top, latest = await asyncio.gather(
                        _twitterapi(self.key, f"{q} min_faves:20 -filter:replies -filter:retweets", "Top", 12),
                        _twitterapi(self.key, f"{q} -filter:replies -filter:retweets", "Latest", 12))
                    return {"posts": _merge(top + latest), "via": "twitterapi.io"}
                except Exception as e:  # noqa: BLE001
                    errors["twitterapi.io"] = f"{type(e).__name__}: {str(e)[:80]}"
            if self.mirrors:
                try:
                    return {"posts": await _nitter(self.mirrors, f"/search/rss?f=tweets&q={quote(q)}"), "via": "nitter",
                            "errors": errors or None}
                except Exception as e:  # noqa: BLE001
                    errors["nitter"] = str(e)[:300]
            try:
                return {"posts": await _web(q, hours, self.web_keys), "via": "web-search", "errors": errors or None}
            except Exception as e:  # noqa: BLE001
                errors["web-search"] = f"{type(e).__name__}: {str(e)[:80]}"
            return {"posts": [], "via": None, "errors": errors}

        return _window(await self._cached(("search", q), make), hours, limit)

    async def timeline(self, user: str, hours: int = 24, limit: int = 20) -> dict:
        user = user.strip().lstrip("@")

        async def make() -> dict:
            errors = {}
            if self.key:
                try:
                    return {"posts": await _twitterapi(self.key, f"from:{user}", "Latest", 20), "via": "twitterapi.io"}
                except Exception as e:  # noqa: BLE001
                    errors["twitterapi.io"] = f"{type(e).__name__}: {str(e)[:80]}"
            if self.mirrors:
                try:
                    return {"posts": await _nitter(self.mirrors, f"/{quote(user)}/rss"), "via": "nitter",
                            "errors": errors or None}
                except Exception as e:  # noqa: BLE001
                    errors["nitter"] = str(e)[:300]
            try:
                return {"posts": await _syndication(user), "via": "x-embed", "errors": errors or None,
                        "note": "лента для встраивания: может отставать или показывать популярное вместо свежего"}
            except Exception as e:  # noqa: BLE001
                errors["x-embed"] = f"{type(e).__name__}: {str(e)[:80]}"
            try:
                return {"posts": await _web(f"from:{user}", hours, self.web_keys) or await _web(user, hours,
                                                                                                 self.web_keys),
                        "via": "web-search", "errors": errors or None}
            except Exception as e:  # noqa: BLE001
                errors["web-search"] = f"{type(e).__name__}: {str(e)[:80]}"
            return {"posts": [], "via": None, "errors": errors}

        return _window(await self._cached(("timeline", user.lower()), make), hours, limit)

    async def accounts_feed(self, accounts: list[str], topic: str = "", hours: int = 24, limit: int = 30) -> dict:
        """Latest posts of several accounts, optionally only those about a topic."""
        accounts = [a.strip().lstrip("@") for a in accounts if a.strip()]
        if self.key:  # one combined query per 15 accounts
            since = time.strftime("%Y-%m-%d_%H:%M:%S_UTC", time.gmtime(time.time() - hours * 3600))
            extra = f" {query_of(topic)}" if topic else ""
            chunks = [accounts[i:i + 15] for i in range(0, len(accounts), 15)]
            res = await asyncio.gather(*(_twitterapi(self.key, "(" + " OR ".join(f"from:{a}" for a in ch) + ")" + extra
                                                     + f" since:{since} -filter:replies", "Latest", 20)
                                         for ch in chunks), return_exceptions=True)
            posts = [p for r in res if isinstance(r, list) for p in r]
            if posts:
                return _window({"posts": _merge(posts), "via": "twitterapi.io",
                                "errors": [str(r)[:100] for r in res if isinstance(r, Exception)] or None},
                               hours, limit)
        sem = asyncio.Semaphore(4)
        picked = accounts[:FREE_ACCOUNTS_MAX]

        async def one(a):
            async with sem:
                return await self.timeline(a, hours, 10)

        res = await asyncio.gather(*(one(a) for a in picked), return_exceptions=True)
        posts = [p for r in res if isinstance(r, dict) for p in r["posts"]]
        if topic:
            words = {topic.lower().strip(), normalize_base(topic).lower()}
            posts = [p for p in posts if any(re.search(rf"(?<!\w)\$?{re.escape(w)}\b", p["text"].lower()) for w in words)]
        vias = sorted({r["via"] for r in res if isinstance(r, dict) and r.get("via")})
        out = _window({"posts": _merge(posts), "via": ", ".join(vias) or None}, hours, limit)
        if len(accounts) > len(picked):
            out["note"] = f"без ключа читаю первые {len(picked)} из {len(accounts)} аккаунтов"
        return out


def _merge(posts: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in sorted(posts, key=lambda p: -(p["ts"] or 0)):
        k = p.get("url") or (p.get("author"), p["text"][:100])
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _window(res: dict, hours: int, limit: int) -> dict:
    cutoff = time.time() - hours * 3600
    posts = [p for p in res.get("posts") or [] if p["ts"] is None or p["ts"] >= cutoff]
    return {**res, "posts": posts[:max(1, int(limit))], "found": len(posts)}


async def x_posts(reader: XReader, query: str = "", account: str = "", hours: int = 24, limit: int = 20) -> dict:
    """Tool: posts of accounts (account), a search (query), or both — posts of these accounts about the topic."""
    hours = max(1, min(int(hours), 24 * 14))
    limit = max(1, min(int(limit), 40))
    if account.strip().lower() in ("watched", "отслеживаемые", "все") or (not account and not query):
        res = await reader.accounts_feed(list(reader.accounts), query, hours, limit)
        res["accounts"] = "watched"
    elif account:
        names = [a.lstrip("@") for a in re.split(r"[,\s]+", account) if a.lstrip("@")]
        res = await (reader.timeline(names[0], hours, limit) if len(names) == 1 and not query
                     else reader.accounts_feed(names, query, hours, limit))
        res["accounts"] = names
    else:
        res = await reader.search(query, hours, limit)
    res.update({"query": query or None, "hours": hours, "rule": RULE})
    if not res.get("posts"):
        res["note"] = (res.get("note") or "") + " Постов не нашёл: все источники X пусты или недоступны — скажи об этом."
    return res
