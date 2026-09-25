from swarm.tools.news import _parse_rss, _tweet

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin hits $70K</title><description>&lt;p&gt;BTC rallies&lt;/p&gt;</description>
<link>https://x.test/1</link><pubDate>Tue, 25 Aug 2026 10:00:00 GMT</pubDate></item>
<item><title>ETH upgrade</title><description>news</description><link>https://x.test/2</link></item>
</channel></rss>"""


def test_parse_rss():
    items = _parse_rss("Test", RSS)
    assert len(items) == 2
    assert items[0]["title"] == "Bitcoin hits $70K"
    assert items[0]["summary"] == "BTC rallies"
    assert items[0]["ts"] is not None and items[1]["ts"] is None


def test_parse_rss_garbage():
    assert _parse_rss("Bad", "not xml") == []


def test_tweet_mapping():
    t = _tweet({"text": "gm", "author": {"userName": "cobie", "followers": 10}, "likeCount": 5, "url": "u"})
    assert t["author"] == "cobie" and t["likes"] == 5 and t["text"] == "gm"
