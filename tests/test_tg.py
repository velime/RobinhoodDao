import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bottom.config import Settings
from bottom.tg.__main__ import apply_verdict, save_env, write_report
from bottom.tg.classify import CRYPTO, NOT_CRYPTO, REVIEW, UNAVAILABLE, Verdict, classify, extract_cashtags
from bottom.tg.client import Collector, kind_of, to_row, topic_of
from bottom.tg.sources import Source, internal_id, load_sources, message_link, save_sources
from bottom.tg.store import PRIVATE_LABEL, TgStore
from bottom.tools.registry import Toolbox

CRYPTO_POSTS = [
    "Зашёл в лонг $SOL от 142, стоп 136, тейк 155",
    "Фандинг на бинансе улетел, шорты платят",
    "Новый мемкоин на pump.fun, контракт в комментах, x10 изи",
    "Аирдроп от Hyperliquid — проверяйте кошельки",
    "Сегодня просто отдыхаю, всем хороших выходных",
]
LIFE_POSTS = [
    "Сходил в зал, сделал новый рекорд в жиме",
    "Рецепт пасты карбонара: яйца, гуанчиале, пекорино",
    "Смотрите мой новый влог из Тбилиси",
    "Какой фильм посмотреть вечером?",
    "Купил новые кроссовки",
]


# --- classifier ---

def test_classify_crypto_and_not():
    assert classify("Flip calls", "", CRYPTO_POSTS).label == CRYPTO
    v = classify("Мой блог", "про жизнь", LIFE_POSTS)
    assert v.label == NOT_CRYPTO and v.post_ratio == 0


def test_classify_borderline_goes_to_review():
    posts = LIFE_POSTS + ["купил немного битка на просадке"]  # 1 of 6 ≈ 17%
    assert classify("Блог Артёма", "", posts).label == REVIEW
    assert classify("Блог Артёма", "", LIFE_POSTS * 2 + ["купил битка"]).label == NOT_CRYPTO


def test_classify_no_text_uses_header():
    assert classify("Crypto calls", "Трейдинг и сигналы по крипте", []).label == CRYPTO
    assert classify("Котики", "", []).label == REVIEW


def test_cashtags():
    assert extract_cashtags("берём $pepe и $WIF, а $5 не тикер") == ["PEPE", "WIF"]


# --- sources registry ---

def test_repo_sources_file():
    src = load_sources("channels/sources.json")
    assert len(src) == 161 and len({s.id for s in src}) == 161
    # the repository holds public channels only (Durov's channel is listed by id); private chats live in private.json
    assert sum(not s.public for s in src) == 1 and {s.kind for s in src} == {"channel", "forum"}
    assert -1002797401414 not in {s.id for s in src}  # the former parsers' group
    news = [s for s in src if s.title in ("Павел Дуров", "Pavel Durov", "MarketTwits", "Walter Bloomberg")]
    assert len(news) == 4 and all(s.enabled and s.manual for s in news)  # scan must not switch them off


def test_sources_roundtrip_keeps_unknown_fields(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"sources": [
        {"id": "-1001", "title": "A", "username": "a", "public": True, "custom": 5},
        {"id": -1001, "title": "dup"},
    ]}), encoding="utf-8")
    src = load_sources(str(p))
    assert len(src) == 1 and src[0].id == -1001 and src[0].extra == {"custom": 5}
    save_sources(str(p), src)
    raw = json.loads(p.read_text(encoding="utf-8"))["sources"][0]
    assert raw["custom"] == 5 and raw["username"] == "a"


def test_public_and_private_files(tmp_path):
    from bottom.tg.sources import load_all, private_path, save_all

    pub, priv = tmp_path / "sources.json", tmp_path / "private.json"
    pub.write_text(json.dumps({"sources": [{"id": -1, "title": "pub", "username": "p", "public": True}]}), encoding="utf-8")
    priv.write_text(json.dumps({"sources": [{"id": -2, "title": "secret chat"}, {"id": -1, "title": "dup"}]}),
                    encoding="utf-8")
    src = load_all(str(pub))
    assert [s.title for s in src] == ["pub", "secret chat"] and private_path(str(pub)) == str(priv)
    src.append(Source(id=-3, title="found in the account", username="newpub", public=True, enabled=False))
    save_all(str(pub), src)
    assert [x["id"] for x in json.loads(pub.read_text(encoding="utf-8"))["sources"]] == [-1]
    assert [x["id"] for x in json.loads(priv.read_text(encoding="utf-8"))["sources"]] == [-2, -3]
    assert "origin" not in json.loads(priv.read_text(encoding="utf-8"))["sources"][0]


def test_restore_private(tmp_path, capsys):
    from types import SimpleNamespace

    from bottom.tg.__main__ import cmd_restore_private

    pub = tmp_path / "sources.json"
    pub.write_text(json.dumps({"sources": [{"id": -1, "title": "pub", "public": True}]}), encoding="utf-8")
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"sources": [{"id": -1, "title": "pub", "public": True},
                                           {"id": -2, "title": "arty private"},
                                           {"id": -1002797401414, "title": "PARSER"}]}), encoding="utf-8")
    cmd_restore_private(SimpleNamespace(tg_sources_file=str(pub)), SimpleNamespace(from_file=str(old)))
    priv = json.loads((tmp_path / "private.json").read_text(encoding="utf-8"))["sources"]
    assert [x["id"] for x in priv] == [-2]  # public ones and the old parsers' group stay out
    cmd_restore_private(SimpleNamespace(tg_sources_file=str(pub)), SimpleNamespace(from_file=str(old)))
    assert len(json.loads((tmp_path / "private.json").read_text(encoding="utf-8"))["sources"]) == 1
    assert "добавлено 0" in capsys.readouterr().out


def test_links():
    assert internal_id(-1002472080907) == 2472080907
    assert message_link("tradebyryndyk", -1002472080907, 5) == "https://t.me/tradebyryndyk/5"
    assert message_link(None, -1002797401414, 9, topic_id=3) == "https://t.me/c/2797401414/3/9"


def test_apply_verdict_respects_manual():
    s = Source(id=1, enabled=True)
    apply_verdict(s, NOT_CRYPTO)
    assert s.enabled is False and s.verdict == NOT_CRYPTO
    apply_verdict(s, REVIEW)
    assert s.enabled is True  # hand-picked list: doubtful ones stay on
    m = Source(id=2, enabled=True, manual=True)
    apply_verdict(m, NOT_CRYPTO)
    assert m.enabled is True and m.verdict == NOT_CRYPTO


def test_write_report(tmp_path):
    items = [
        {"source": Source(id=1, title="Flip", username="flip", public=True), "verdict": classify("Flip", "", CRYPTO_POSTS)},
        {"source": Source(id=2, title="Закрытый", kind="forum"), "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason="нет доступа")},
    ]
    p = tmp_path / "r.md"
    write_report(str(p), items)
    text = p.read_text(encoding="utf-8")
    assert "@flip — Flip | канал | публичный" in text and "чат с темами | приватный" in text


# --- store ---

def _store(tmp_path):
    st = TgStore(str(tmp_path / "tg.db"))
    st.sync_sources([
        Source(id=-1001, username="chan_a", title="A", kind="channel", public=True),
        Source(id=-1002, username=None, title="Private chat", kind="forum", public=False),
    ])
    now = time.time()
    st.add_messages(-1001, [
        {"id": 1, "ts": now - 3600, "text": "Лонг $HYPE от 40", "views": 100},
        {"id": 2, "ts": now - 7200, "text": "шорт $PEPE, стоп выше хая"},
        {"id": 3, "ts": now - 90000, "text": "старый пост про $HYPE"},
    ])
    st.add_messages(-1002, [{"id": 10, "ts": now - 600, "text": "HYPE летит, кто в позиции?", "author": "Вася", "topic_id": 7}])
    return st


def test_store_search_ticker_hides_private(tmp_path):
    r = _store(tmp_path).search("hype", hours=24)
    assert r["mentions"] == 2 and r["channels_mentioning"] == 1 and r["groups_mentioning"] == 1
    priv, pub = r["posts"]
    assert priv["private"] and priv["source"] == PRIVATE_LABEL and priv["link"] is None and priv["author"] is None
    assert pub["source"] == "@chan_a" and pub["link"] == "https://t.me/chan_a/1"


def test_store_search_can_show_private(tmp_path):
    r = _store(tmp_path).search("hype", hide_private=False)
    priv = r["posts"][0]
    assert priv["source"] == "Private chat" and priv["author"] == "Вася"
    assert priv["link"] == "https://t.me/c/1002/7/10" or priv["link"].startswith("https://t.me/c/")


def test_store_trending_and_last_id(tmp_path):
    st = _store(tmp_path)
    r = st.search("", hours=24)
    tickers = {x["ticker"]: x["sources"] for x in r["top_cashtags_by_sources"]}
    assert tickers == {"HYPE": 1, "PEPE": 1}
    assert all(p["kind"] == "channel" for p in r["posts"])  # channel posts first
    assert st.last_id(-1001) == 3 and st.last_id(-999) == 0
    assert "error" in TgStore(str(tmp_path / "e.db")).search("BTC")


# --- telethon helpers & collector ---

def _m(i, hours_ago, text, **kw):
    return SimpleNamespace(id=i, date=datetime.now(timezone.utc) - timedelta(hours=hours_ago), message=text,
                           views=5, sender=kw.get("sender"), reply_to=kw.get("reply_to"), post_author=None)


def test_to_row_and_topic():
    assert to_row(_m(1, 0, "+")) is None
    rt = SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=42)
    row = to_row(_m(2, 0, "текст", reply_to=rt), author="Петя")
    assert row["topic_id"] == 42 and row["author"] == "Петя"
    assert topic_of(_m(3, 0, "x", reply_to=SimpleNamespace(forum_topic=False))) is None


def test_kind_of():
    assert kind_of(SimpleNamespace(broadcast=True)) == "channel"
    assert kind_of(SimpleNamespace(broadcast=False, megagroup=True, forum=True)) == "forum"
    assert kind_of(SimpleNamespace(broadcast=False, megagroup=True, forum=False)) == "group"
    assert kind_of(SimpleNamespace(broadcast=False, megagroup=False)) is None


class FakeTgClient:
    def __init__(self, msgs):
        self.msgs = msgs  # newest first
        self.calls = []

    async def iter_messages(self, entity, min_id=0, limit=None):
        self.calls.append((entity, min_id))
        for m in self.msgs:
            if m.id > min_id:
                yield m


async def test_collector_backfill_then_incremental(tmp_path):
    st = TgStore(str(tmp_path / "c.db"))
    client = FakeTgClient([_m(3, 1, "пост 3"), _m(2, 5, ""), _m(1, 100, "очень старый")])
    src = Source(id=-1005, title="chan", kind="channel")
    col = Collector(client, st, [src, Source(id=-1006, enabled=False)], backfill_days=2)
    assert list(col.by_id) == [-1005]  # disabled sources are skipped
    col.entities[-1005] = "ENTITY"
    assert await col.sync_one(src) == 1  # only fresh text message
    assert st.last_id(-1005) == 3
    client.msgs.insert(0, _m(4, 0, "пост 4"))
    assert await col.sync_one(src) == 1
    assert client.calls[-1] == ("ENTITY", 3)


async def test_collector_live_message(tmp_path):
    st = TgStore(str(tmp_path / "l.db"))
    col = Collector(FakeTgClient([]), st, [Source(id=-1007, kind="group")])

    async def get_sender():
        from telethon.tl.types import User

        return User(id=1, first_name="Иван")

    ev = SimpleNamespace(chat_id=-1007, message=_m(9, 0, "шорт $BTC"), get_sender=get_sender)
    await col.on_message(ev)
    r = st.search("BTC", hide_private=False)
    assert r["mentions"] == 1 and r["posts"][0]["author"] == "Иван"


def test_toolbox_registers_telegram_tool(tmp_path):
    assert "telegram_channels" not in Toolbox(Settings(exchanges=())).tools
    assert "telegram_channels" in Toolbox(Settings(exchanges=()), tg_store=_store(tmp_path)).tools


def test_save_env_updates_and_appends(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nTG_API_ID=\n# comment\n", encoding="utf-8")
    save_env(str(p), {"TG_API_ID": "42", "TG_API_HASH": "abc"})
    assert p.read_text(encoding="utf-8") == "A=1\nTG_API_ID=42\n# comment\nTG_API_HASH=abc\n"
