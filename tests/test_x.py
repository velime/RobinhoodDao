import json
import time

import pytest

from bottom.tools.social import x

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
<channel><title>WatcherGuru / X</title>
<item>
  <title>JUST IN: Binance to list $XYZ</title>
  <dc:creator>@WatcherGuru</dc:creator>
  <description><![CDATA[<p>JUST IN: Binance to list <a href="/search?q=%24XYZ">$XYZ</a></p>
  <img src="https://nitter.net/pic/media%2FGabc123.jpg" style="max-width:250px;" />]]></description>
  <pubDate>{now}</pubDate>
  <guid>https://nitter.net/WatcherGuru/status/1900000000000000001#m</guid>
  <link>https://nitter.net/WatcherGuru/status/1900000000000000001#m</link>
</item>
<item>
  <title>RT by @WatcherGuru: old news</title>
  <dc:creator>@someone</dc:creator>
  <description><![CDATA[<p>old news</p>]]></description>
  <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
  <link>https://nitter.net/someone/status/1#m</link>
</item>
</channel></rss>"""


def rss():
    import email.utils

    return RSS.replace("{now}", email.utils.formatdate(time.time() - 600, usegmt=True))


SYND = '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"timeline": {
    "entries": [{"type": "tweet", "content": {"tweet": {
        "id_str": "42", "full_text": "gm $BTC", "created_at": time.strftime("%a %b %d %H:%M:%S +0000 %Y", time.gmtime()),
        "favorite_count": 7, "retweet_count": 1, "user": {"screen_name": "cobie", "followers_count": 900},
        "extended_entities": {"media": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/A.jpg"}]}}}}]}}}}) \
    + "</script></html>"


def test_parse_nitter_rss_and_images():
    posts = x.parse_nitter_rss(rss(), "https://nitter.net")
    p = posts[0]
    assert p["author"] == "WatcherGuru" and "Binance to list $XYZ" in p["text"] and p["via"] == "nitter"
    assert p["url"] == "https://x.com/WatcherGuru/status/1900000000000000001"
    assert p["images"] == ["https://pbs.twimg.com/media/Gabc123.jpg"] and p["age_hours"] == pytest.approx(0.2, abs=0.05)
    assert posts[1]["repost"] is True
    assert x._image_url("https://n.net/pic/orig/media%2FB.png") == "https://pbs.twimg.com/media/B.png"
    import base64

    enc = base64.urlsafe_b64encode(b"media/C.jpg").decode().rstrip("=")
    assert x._image_url(f"https://n.net/pic/enc/{enc}") == "https://pbs.twimg.com/media/C.jpg"
    with pytest.raises(ValueError):
        x.parse_nitter_rss("<html>RSS reader not yet whitelisted!</html>", "https://xcancel.com")


def test_parse_syndication():
    p = x.parse_syndication(SYND)[0]
    assert p["author"] == "cobie" and p["url"] == "https://x.com/cobie/status/42" and p["likes"] == 7
    assert p["images"] == ["https://pbs.twimg.com/media/A.jpg"] and p["via"] == "x-embed"
    with pytest.raises(ValueError):
        x.parse_syndication("<html>login</html>")


def test_query_of():
    assert x.query_of("hype") == "$HYPE" and x.query_of("$btc") == "$BTC"
    assert x.query_of("Binance listing") == "Binance listing" and x.query_of("@cobie") == "@cobie"


class Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def client(self):
        return self

    async def get(self, url, headers=None, timeout=None, params=None):
        self.calls.append(url)
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp() if callable(resp) else resp
        return Resp(403, "blocked")

    async def get_json(self, url, params=None, headers=None):
        raise RuntimeError("no key path in this test")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(x, "_dead", {})
    monkeypatch.setattr(x, "_cache", {})


async def test_search_skips_dead_mirror(monkeypatch):
    fake = FakeHttp({"https://m2.example/search/rss": lambda: Resp(200, rss())})
    monkeypatch.setattr(x, "http", fake)
    r = x.XReader(mirrors=("https://m1.example", "https://m2.example"))
    res = await r.search("xyz", hours=24)
    assert res["via"] == "nitter" and res["found"] == 1 and res["posts"][0]["author"] == "WatcherGuru"
    assert fake.calls[0].startswith("https://m1.example/search/rss?f=tweets&q=%24XYZ")
    x._cache.clear()
    await r.search("abc", hours=24)
    assert not any(c.startswith("https://m1.example") for c in fake.calls[2:])  # m1 is skipped for a while


async def test_timeline_falls_back_to_embed_then_web(monkeypatch):
    fake = FakeHttp({x.SYNDICATION.format(user="cobie"): Resp(200, SYND)})
    monkeypatch.setattr(x, "http", fake)
    r = x.XReader(mirrors=("https://dead.example",))
    res = await r.timeline("@cobie")
    assert res["via"] == "x-embed" and res["posts"][0]["text"] == "gm $BTC" and "nitter" in res["errors"]

    async def web_search(q, recent_days=0, max_results=8, **kw):
        assert q.startswith("site:x.com")
        return {"results": [{"title": "Hayes on $BTC", "snippet": "…", "url": "https://x.com/CryptoHayes/status/9",
                             "age_hours": 2.0}, {"title": "not x", "url": "https://news.example/a"}]}

    monkeypatch.setattr("bottom.tools.web.web_search", web_search)
    res = await r.timeline("CryptoHayes")
    assert res["via"] == "web-search" and res["posts"][0]["author"] == "CryptoHayes" and res["found"] == 1


async def test_x_posts_routing_and_topic_filter(monkeypatch):
    seen = []

    async def timeline(self, user, hours=24, limit=20):
        seen.append(user)
        text = {"a": "$HYPE pumps", "b": "gm"}.get(user, "nothing")
        return {"posts": [x._post(user, text, time.time() - 60, f"https://x.com/{user}/status/1", "nitter")],
                "via": "nitter"}

    monkeypatch.setattr(x.XReader, "timeline", timeline)
    r = x.XReader(accounts=("a", "b", "c"))
    feed = await x.x_posts(r, query="hype", account="watched")
    assert [p["author"] for p in feed["posts"]] == ["a"] and set(seen) == {"a", "b", "c"}
    one = await x.x_posts(r, account="@b")
    assert one["posts"][0]["text"] == "gm" and one["accounts"] == ["b"]
    async def no_results(q, recent_days=0, max_results=8, **kw):
        return {"results": []}

    monkeypatch.setattr("bottom.tools.web.web_search", no_results)
    empty = await x.x_posts(x.XReader(mirrors=()), query="nothing-here")
    assert empty["posts"] == [] and "Постов не нашёл" in empty["note"]


async def test_empty_mirror_moves_on(monkeypatch):
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    monkeypatch.setattr(x, "http", FakeHttp({"https://m.example/search/rss": Resp(200, empty)}))

    async def web_search(q, recent_days=0, max_results=8, **kw):
        return {"results": [{"title": "$ABC listed", "snippet": "", "url": "https://x.com/a/status/1", "age_hours": 1}]}

    monkeypatch.setattr("bottom.tools.web.web_search", web_search)
    res = await x.XReader(mirrors=("https://m.example",)).search("abc")
    assert res["via"] == "web-search" and res["errors"] == {"nitter": "пусто"} and res["found"] == 1
