import time
from types import SimpleNamespace

from bottom.config import LLMConfig, Settings
from bottom.tg.client import has_image, to_row
from bottom.tg.sources import Source
from bottom.tg.store import TgStore
from bottom.tools import crosscheck as cc
from bottom.tools.news import _tweet
from bottom.tools.registry import Toolbox
from bottom.vision import Vision

NOW = 1_800_000_000.0


def test_build_timeline_first_and_waves():
    ev = [
        {"ts": NOW - 3600, "type": "telegram_channel", "source": "@a", "text": "Binance листит XYZ!! https://t.me/x"},
        {"ts": NOW - 3500, "type": "telegram_channel", "source": "@b", "text": "binance листит xyz"},
        {"ts": NOW - 7200, "type": "telegram_group", "source": "чат", "text": "слышал про листинг XYZ"},
        {"ts": NOW - 600, "type": "media", "source": "CoinDesk", "text": "Binance to list XYZ"},
    ]
    t = cc.build_timeline(ev, NOW)
    assert t["first_overall"]["source"] == "чат" and t["first_overall"]["age_hours"] == 2.0
    assert t["independent_sources"] == 4
    assert t["repost_waves"][0]["copies"] == 2 and t["repost_waves"][0]["first_source"] == "@a"
    assert t["counts_by_type"] == {"telegram_group": 1, "telegram_channel": 2, "media": 1}


def test_price_reaction():
    t0 = NOW - 10 * 3600
    candles = [{"t": (t0 + i * 3600) * 1000, "o": 100 + i, "h": 101 + i, "l": 99 + i, "c": 100 + i + 0.5}
               for i in range(10)]
    r = cc.price_reaction(candles, t0 + 7 * 3600 + 60)
    assert r["price_at_first_mention"] == 107
    assert r["change_6h_before_first_mention_pct"] == round((107 - 101) / 101 * 100, 2)
    assert r["change_since_first_mention_pct"] == round((109.5 - 107) / 107 * 100, 2)
    assert cc.price_reaction(candles, None) is None


def test_tweet_ts_and_images():
    assert cc._tweet_ts("Tue Sep 23 10:00:00 +0000 2026") is not None
    assert cc._tweet_ts("garbage") is None
    tw = _tweet({"text": "chart", "extendedEntities": {"media": [
        {"type": "photo", "media_url_https": "https://pbs.twimg.com/a.jpg"},
        {"type": "video", "media_url_https": "https://pbs.twimg.com/v.jpg"}]}})
    assert tw["images"] == ["https://pbs.twimg.com/a.jpg"]


def test_has_image_and_image_only_rows():
    photo = SimpleNamespace(id=1, date=__import__("datetime").datetime.now(), message="", photo=object(),
                            document=None, sticker=None, views=1, reply_to=None, post_author=None, grouped_id=77)
    assert has_image(photo)
    row = to_row(photo)
    assert row and row["has_image"] and row["grouped_id"] == 77 and row["text"] == ""
    sticker = SimpleNamespace(photo=None, document=SimpleNamespace(mime_type="image/webp"), sticker=object())
    assert not has_image(sticker)


def test_store_images_albums_source_filter_and_migration(tmp_path):
    path = str(tmp_path / "s.db")
    import sqlite3

    old = sqlite3.connect(path)  # a DB from the previous version, without image columns
    old.execute("CREATE TABLE tg_messages (chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, ts REAL NOT NULL, "
                "text TEXT NOT NULL, views INTEGER, author TEXT, topic_id INTEGER, PRIMARY KEY (chat_id, msg_id))")
    old.commit()
    old.close()
    st = TgStore(path)
    st.sync_sources([Source(id=-1001, username="chart_guy", title="Chart Guy", public=True)])
    now = time.time()
    st.add_messages(-1001, [
        {"id": 1, "ts": now - 60, "text": "мой взгляд на $SOL", "has_image": True, "grouped_id": 5},
        {"id": 2, "ts": now - 60, "text": "", "has_image": True, "grouped_id": 5},
        {"id": 3, "ts": now - 30, "text": "просто текст"},
    ])
    assert sorted(p[1] for p in st.pending_images(now - 3600)) == [1, 2]
    st.set_image_text(-1001, 2, "график SOL 4ч, уровень 148")
    st.set_image_text(-1001, 1, None)  # failed attempt
    post = st.search("SOL", hide_private=False)["posts"][0]
    assert "уровень 148" in post["image"]  # album description attached to the captioned post
    r = st.search("", hours=1, source="@chart_guy", hide_private=False)
    assert len(r["posts"]) == 3 and "top_cashtags_by_sources" not in r
    assert st.stats()["images"] == 2 and st.stats()["images_described"] == 1


async def test_vision_limits_and_fallback(monkeypatch):
    calls = []

    async def fake_call(cfg, data, mime, prompt):
        calls.append(cfg.model)
        if cfg.model == "bad":
            raise RuntimeError("no vision")
        return " описание "

    monkeypatch.setattr("bottom.vision._call", fake_call)
    v = Vision(LLMConfig(model="bad"), LLMConfig(model="good"), daily_limit=1)
    assert await v.describe(b"img") == "описание" and calls == ["bad", "good"]
    try:
        await v.describe(b"img")
        raise AssertionError("limit not enforced")
    except RuntimeError as e:
        assert "лимит" in str(e)
    assert not Vision(None).enabled


def test_toolbox_new_tools():
    s = Settings(exchanges=())
    tb = Toolbox(s)
    assert "cross_check" in tb.tools and "analyze_image" not in tb.tools and "track_record" not in tb.tools
    tb2 = Toolbox(s, vision=Vision(LLMConfig(model="m")))
    assert "analyze_image" in tb2.tools
