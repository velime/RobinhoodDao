import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from swarm.config import Settings
from swarm.tg.classify import CRYPTO, NOT_CRYPTO, REVIEW, classify, extract_cashtags, parse_channel_list
from swarm.tg.client import Collector
from swarm.tg.store import TgStore
from swarm.tools.registry import Toolbox

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


def test_classify_crypto_and_not():
    assert classify("Flip calls", "", CRYPTO_POSTS).label == CRYPTO
    v = classify("Мой блог", "про жизнь", LIFE_POSTS)
    assert v.label == NOT_CRYPTO and v.post_ratio == 0


def test_classify_borderline_goes_to_review():
    posts = LIFE_POSTS + ["купил немного битка на просадке"]  # 1 of 6 ≈ 17%
    assert classify("Блог Артёма", "", posts).label == REVIEW
    # 1 of 11 ≈ 9% and nothing in the header → not crypto
    assert classify("Блог Артёма", "", LIFE_POSTS * 2 + ["купил битка"]).label == NOT_CRYPTO


def test_classify_no_text_uses_header():
    assert classify("Crypto calls", "Трейдинг и сигналы по крипте", []).label == CRYPTO
    assert classify("Котики", "", []).label == REVIEW


def test_parse_channel_list():
    text = """# comment
https://t.me/tradebyryndyk
@ghosteer_flip
t.me/crypto_patrik28/
https://t.me/Crypto_Patrik28
https://t.me/+AbCdEfGhIj
rugs29"""
    assert parse_channel_list(text) == ["tradebyryndyk", "ghosteer_flip", "crypto_patrik28", "rugs29"]


def test_repo_channel_list_parses():
    names = parse_channel_list(open("channels/all.txt", encoding="utf-8").read())
    assert len(names) == 105 and "Jacobs_pro" in names


def test_cashtags():
    assert extract_cashtags("берём $pepe и $WIF, а $5 не тикер") == ["PEPE", "WIF"]


def _store(tmp_path):
    st = TgStore(str(tmp_path / "tg.db"))
    now = time.time()
    st.add_posts("chan_a", [
        {"id": 1, "ts": now - 3600, "text": "Лонг $HYPE от 40", "views": 100},
        {"id": 2, "ts": now - 7200, "text": "шорт $PEPE, стоп выше хая"},
        {"id": 3, "ts": now - 90000, "text": "старый пост про $HYPE"},
    ])
    st.add_posts("chan_b", [{"id": 10, "ts": now - 600, "text": "HYPE летит, кто в позиции?"}])
    return st


def test_store_search_ticker(tmp_path):
    r = _store(tmp_path).search("hype", hours=24)
    assert r["mentions"] == 2 and r["channels_mentioning"] == 2
    assert r["posts"][0]["channel"] == "@chan_b"
    assert r["posts"][0]["link"] == "https://t.me/chan_b/10"


def test_store_search_trending(tmp_path):
    r = _store(tmp_path).search("", hours=24)
    tickers = {x["ticker"]: x["channels"] for x in r["top_cashtags_by_channels"]}
    assert tickers == {"HYPE": 1, "PEPE": 1}
    assert r["posts_in_window"] == 3


def test_store_last_id_and_empty(tmp_path):
    st = _store(tmp_path)
    assert st.last_id("chan_a") == 3 and st.last_id("nope") == 0
    empty = TgStore(str(tmp_path / "e.db")).search("BTC")
    assert "error" in empty


class FakeTgClient:
    def __init__(self, msgs):
        self.msgs = msgs  # newest first
        self.calls = []

    async def iter_messages(self, ch, min_id=0, limit=None):
        self.calls.append((ch, min_id))
        for m in self.msgs:
            if m.id > min_id:
                yield m


def _m(i, hours_ago, text):
    return SimpleNamespace(id=i, date=datetime.now(timezone.utc) - timedelta(hours=hours_ago), message=text, views=5, forwards=0)


async def test_collector_backfill_then_incremental(tmp_path):
    st = TgStore(str(tmp_path / "c.db"))
    client = FakeTgClient([_m(3, 1, "пост 3"), _m(2, 5, ""), _m(1, 100, "очень старый")])
    col = Collector(client, st, ["chan"], backfill_hours=48)
    assert await col._poll_one("chan") == 1  # only fresh text post; old one is past backfill window
    assert st.last_id("chan") == 3
    client.msgs.insert(0, _m(4, 0, "пост 4"))
    assert await col._poll_one("chan") == 1
    assert client.calls[-1] == ("chan", 3)


def test_toolbox_registers_telegram_tool(tmp_path):
    assert "telegram_channels" not in Toolbox(Settings(exchanges=())).tools
    tb = Toolbox(Settings(exchanges=()), tg_store=_store(tmp_path))
    assert "telegram_channels" in tb.tools


def test_save_env_updates_and_appends(tmp_path):
    from swarm.tg.__main__ import save_env

    p = tmp_path / ".env"
    p.write_text("A=1\nTG_API_ID=\n# comment\n", encoding="utf-8")
    save_env(str(p), {"TG_API_ID": "42", "TG_API_HASH": "abc"})
    assert p.read_text(encoding="utf-8") == "A=1\nTG_API_ID=42\n# comment\nTG_API_HASH=abc\n"
    new = tmp_path / "new.env"
    save_env(str(new), {"X": "1"})
    assert new.read_text(encoding="utf-8") == "X=1\n"
