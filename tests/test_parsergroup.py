"""Parser-group alerts: the plain text Telegram gives for each parser format becomes structured alerts.

SAMPLES are what the standalone parsers post (C:/parser/scanner/formats.py), with the HTML removed."""

import time

from bottom.tg.store import TgStore
from bottom.tools import parsergroup as pg

SAMPLES = {
    'oi': '$MUU | Bybit\nOI: +43.51% за 4ч | +27.18% за 15м\n28.34K → 40.94K (+$459.00K)\n\n💲 Цена: $37.1200 (-0.08%)\n⛽ Funding: +0.0000%\n⚖️ L/S: 53.1% 🟢 / 46.9% 🔴\n🏷 Mark: $36.9800 (short)\n🏛 OI/MCap: n/a\n📗 Book Delta: +$297.60K\n🌊 A/D спот 4ч: спот n/a  (перп -$2.52M ↓)\n\n🧱 Стакан Bybit:\n↑ $37.2499 (+0.4%) — $56.60K (77x)\n\nШорт-сквиз: шорты закрываются\n№4 OI+цена\n⚠️ перегрев\n\n12:00 UTC | #13 сигнал\n#OI #MUU',
    'funding': '🚨 GAIB/USDT\n\n▼ Bybit       -1.5945%/4ч  $0.038650  9м\n──────────────────\n  MEXC        -0.1597%  $0.039310\n  Gate        +0.0050%  $0.039600\n\n↔️ Разница фандинга: лонг на Bybit (-1.5945%), шорт на Gate (+0.0050%) = 1.5995% за выплату; цены расходятся на +2.46%\n\n12:00 UTC\n#FUNDING #GAIB',
    'settle': '⏰ Расчёт через ~10 мин (12:00 UTC)\n\n▼ GAIB/USDT\n  Bybit  -1.5945% ($0.038650)\n  MEXC  -0.1597% ($0.039310)\n\n#FUNDING #SETTLEMENT',
    'spread_pp': '🔀 WAL · фьюч↔фьюч 3.21%\n\nлонг Gate фьюч $0.091200 → шорт MEXC фьюч $0.094100\nфандинг: Gate фьюч -0.2000%/8ч · MEXC фьюч +0.0500%/8ч\nперенос: +0.2500% за 8ч в твою пользу\nиндекс: Gate фьюч $0.091500 · MEXC фьюч $0.093800\n⚠️ низкая ликвидность\n⌛️ Держится: 14 мин · макс 3.40%\n📦 Помещается: ~$12.00K, прибыль ~$380\n\n12:00 UTC\n#SPREAD #WAL',
    'spread_sp': '🔀 WAL · спот↔фьюч -9.09%\n\nспот MEXC спот $0.066000 (ликв $50.00K) · фьюч Gate фьюч $0.060000\nсхема: лонг фьюч + продать спот\nфандинг Gate фьюч: +0.1000%/8ч → платишь 0.1000% за 8ч\nиндекс Gate фьюч: $0.060000\nпул\n⌛️ Держится: 3 мин · макс 9.50%\n\n12:00 UTC\n#SPREAD #WAL',
    'index': '🧭 WAL · проверка индексов\n\n• индекс Gate отклоняется на 4%\n\nмедиана индексов: $0.060000\nмедиана спота: $0.066000\nGate: last $0.060000 · mark $0.060000 · index $0.061500\n\n12:00 UTC\n#INDEX #WAL',
    'transfer': '🏦 Bybit · $WAL\n\n🟢 открыт депозит · ERC20\n🔴 закрыт вывод · BEP20\n\n12:00 UTC\n#TRANSFERS #WAL #bybit',
    'dex': '🔴 $FET · слив · $240.00K\nEthereum · UniswapX\n\nПродал: $240.00K = 11.5% ликвидности ($2.09M) · 2 сделок за 10 мин\n0x53bafdecbb326b928316646216444582f82f538d\nпул\n\n12:00 UTC\n#DEX #FET',
    'with': '🟢 Withdrawal | $CBXRP\n\n🪙 Объём: 100.00K ($114.00K)\n\n⬇️ From: Binance · Hot Wallet (0x40E)\n⏺️ To: 0xaF8B...Ae14\n🔗 Network: Base\n\n📉 Вес:\n21.08% от суточного объёма\n0.11% от капы ($100.00M)\n\n💸 Этот получатель: 0xaF8B…Ae14\n   выводов: 1 · вывел всего: $114.00K\n   на кошельке: 118.94K CBXRP (~$135.60K)\n   ⛽ native: 56.5500 ETH (~$94.61K)\n\n🕒 12:00 UTC · tx\n#WITHDRAWAL #CBXRP #Coinbase',
    'dep': '🔴 Deposit 🐋 | $SYRUP\n\n🪙 Объём: 500.00K ($2.40M)\n👛 Остаток: 0 · завёл весь запас\n\n⬇️ From: 0x1111...1111\n⏺️ To: Binance · Hot Wallet (0x40E)\n🔗 Network: Ethereum\n\n📉 Вес:\n21.08% от суточного объёма\n0.11% от капы ($100.00M)\n\n🔁 Серия заводов с 14.10: $3.50M (3 транз)\n0x1111…1111 $3.50M\n\n🕒 12:00 UTC · tx\n#DEPOSIT #SYRUP #Binance',
}


def test_every_format_is_recognised_by_its_feed():
    feeds = {k: pg.parse(v)["feed"] for k, v in SAMPLES.items()}
    assert feeds == {"oi": "oi", "funding": "funding", "settle": "funding", "spread_pp": "spreads",
                     "spread_sp": "spreads", "index": "spreads", "transfer": "status", "dex": "dex",
                     "with": "flows", "dep": "flows"}
    assert pg.parse("просто болтовня про биткоин") is None
    assert pg.parse("#OI без нормального заголовка") is None


def test_oi_and_funding_fields():
    oi = pg.parse(SAMPLES["oi"])
    assert oi["base"] == "MUU" and oi["ex"] == "Bybit"
    d = oi["data"]
    assert d["oi_chg_pct"] == 43.51 and d["oi_fast_chg_pct"] == 27.18 and d["signal_no"] == 13
    assert d["ad_perp_usd"] == -2_520_000 and d["pattern"]
    f = pg.parse(SAMPLES["funding"])
    assert f["base"] == "GAIB" and f["data"]["trigger"] == ["Bybit"]
    assert [r["ex"] for r in f["data"]["rows"]] == ["Bybit", "MEXC", "Gate"]
    assert f["data"]["rows"][0]["funding_pct"] == -1.5945 and f["data"]["rows"][0]["interval_h"] == 4
    s = pg.parse(SAMPLES["settle"])
    assert s["data"]["kind"] == "settlement" and s["data"]["bases"] == ["GAIB"] and s["base"] is None


def test_spreads_flows_dex_status_fields():
    pp = pg.parse(SAMPLES["spread_pp"])["data"]
    assert pp["type"] == "perp_perp" and pp["gross_pct"] == 3.21 and pp["cheap"] == "Gate фьюч"
    assert pp["carry_8h_pct"] == 0.25 and pp["held_min"] == 14 and pp["capacity"]["fits_usd"] == 12_000
    sp = pg.parse(SAMPLES["spread_sp"])["data"]
    assert sp["type"] == "spot_perp" and sp["gross_pct"] == -9.09 and sp["carry_8h_pct"] == -0.1
    dep = pg.parse(SAMPLES["dep"])
    assert dep["base"] == "SYRUP" and dep["ex"] == "Binance"
    assert dep["data"]["kind"] == "deposit" and dep["data"]["usd"] == 2_400_000 and dep["data"]["whale"]
    wd = pg.parse(SAMPLES["with"])["data"]
    assert wd["kind"] == "withdrawal" and wd["chain"] == "Base" and wd["pct_vol"] == 21.08
    dx = pg.parse(SAMPLES["dex"])
    assert dx["base"] == "FET" and dx["data"]["net_sold_usd"] == 240_000 and dx["data"]["pct_of_liquidity"] == 11.5
    assert dx["data"]["wallet"].startswith("0x53bafdec")
    st = pg.parse(SAMPLES["transfer"])
    assert st["base"] == "WAL" and st["ex"] == "bybit" and st["data"]["changes"][0].startswith("🟢 открыт депозит")


def _store(tmp_path, chat=-100500, other=-100999):
    st = TgStore(str(tmp_path / "tg.db"))
    now = time.time()
    msgs = [{"id": i, "ts": now - 600 * i, "text": t} for i, t in enumerate(SAMPLES.values(), start=1)]
    st.add_messages(chat, msgs)
    st.add_messages(other, [{"id": 1, "ts": now, "text": SAMPLES["dex"].replace("FET", "NOPE")}])
    return st


def test_group_only_and_tool_shapes(tmp_path):
    st = _store(tmp_path)
    rows = pg.load_alerts(st, [-100500], hours=24)
    assert len(rows) == 10 and all(r["base"] != "NOPE" for r in rows)

    top = pg.signals(st, [-100500])
    wal = next(c for c in top["top"] if c["base"] == "WAL")
    assert wal["signals"] == 4 and wal["feeds"] == ["spreads", "status"]

    one = pg.signals(st, [-100500], symbol="$wal", hours=24)
    assert one["alerts"] == 4 and {r["feed"] for r in one["recent"]} == {"spreads", "status"}
    assert pg.signals(st, [-100500], feed="dex", symbol="FET")["recent"][0]["net_sold_usd"] == 240_000
    fund = pg.signals(st, [-100500], feed="funding", hours=24)
    assert fund["alerts"] == 2 and fund["recent"][0]["rates"] or fund["recent"][1]["rates"]


def test_exchange_flows_and_empty_states(tmp_path):
    st = _store(tmp_path)
    r = pg.exchange_flows(st, [-100500], "syrup", days=7)
    assert r["deposits_usd"] == 2_400_000 and r["by_exchange"]["Binance"]["n"] == 1 and "read" in r
    assert "note" in pg.exchange_flows(st, [-100500], "nothing", days=7)
    assert "error" in pg.signals(st, [])
    assert "error" in pg.signals(None, [-1])
    quiet = pg.signals(st, [-100777])
    assert quiet["alerts"] == 0 and "ещё нет сообщений" in quiet["note"]


BOARD = """🔀 Актуальные спреды · 17:11 UTC
Одно закреплённое сообщение, обновляется само

1. $WAL · фьюч↔фьюч · Gate фьюч → MEXC фьюч
сейчас 3.21% · макс 3.40% · держится 14 мин · влезает ~$12.00K

2. $XYZ · спот↔фьюч · MEXC спот → Gate фьюч
сейчас 9.09% · макс 9.50% · держится 3 мин

🧭 Индексы расходятся: FOO, BAR

#SPREADBOARD"""


def test_spread_board_rows_and_edits(tmp_path):
    assert pg.is_board(BOARD) and pg.parse(BOARD) is None
    rows = pg.parse_board(BOARD)
    d = {r["base"]: r["data"] for r in rows}
    assert set(d) == {"WAL", "XYZ", "FOO", "BAR"}
    assert d["WAL"]["gross_pct"] == 3.21 and d["WAL"]["max_pct"] == 3.4 and d["WAL"]["held_min"] == 14
    assert d["WAL"]["cheap"] == "Gate фьюч" and d["WAL"]["capacity"]["fits_usd"] == 12_000
    assert d["XYZ"]["type"] == "spot_perp" and d["FOO"]["type"] == "index"

    st = TgStore(str(tmp_path / "tg.db"))
    now = time.time()
    st.add_messages(-100500, [{"id": 1, "ts": now - 3600, "text": BOARD}])
    st.update_text(-100500, 1, BOARD.replace("3.21%", "4.50%"), now)  # the parser edited the pinned message
    got = pg.load_alerts(st, [-100500], hours=1)
    wal = next(r for r in got if r["base"] == "WAL")
    assert wal["data"]["gross_pct"] == 4.5 and wal["ts"] == now
    top = pg.signals(st, [-100500], feed="spreads", symbol="WAL", hours=1)
    assert top["recent"][0]["gross_pct"] == 4.5 and top["recent"][0]["max_pct"] == 3.4


def test_rich_spreads_board_flattened_from_telegram_blocks(tmp_path):
    """A bot's rich message arrives as page blocks, not as `message`: heading, paragraph, table, details."""
    from telethon.tl import types as T

    from bottom.tg.client import message_text, rich_plain

    def cell(s):
        return T.PageTableCell(text=T.TextBold(T.TextPlain(s)) if s.startswith("$") else T.TextPlain(s))

    def row(*cs):
        return T.PageTableRow([cell(c) for c in cs])

    rm = T.RichMessage(blocks=[
        T.PageBlockHeader(T.TextPlain("🔀 Актуальные спреды")),
        T.PageBlockParagraph(T.TextConcat([T.TextPlain("Обновлено "), T.TextPlain("17:11 UTC"),
                                           T.TextPlain(" · закреплено")])),
        T.PageBlockTable(T.TextEmpty(), [
            row("#", "Монета", "Тип", "Маршрут", "Сейчас", "Макс", "Держится, мин", "Влезает"),
            row("1", "$WAL", "фьюч↔фьюч", "Gate фьюч → MEXC фьюч", "3.21%", "3.40%", "14", "$12.00K"),
            row("2", "$XYZ", "спот↔фьюч", "MEXC спот → Gate фьюч", "9.09%", "9.50%", "3", "—")]),
        T.PageBlockDetails([T.PageBlockParagraph(T.TextPlain("FOO, BAR"))], T.TextPlain("🧭 Индексы расходятся: 2")),
        T.PageBlockParagraph(T.TextPlain("#SPREADBOARD")),
    ], photos=[], documents=[])

    class M:  # what Telethon hands the collector for a bot's rich message
        message = ""
        rich_message = rm

    text = rich_plain(rm)
    assert message_text(M()) == text
    assert "1 | $WAL | фьюч↔фьюч | Gate фьюч → MEXC фьюч | 3.21% | 3.40% | 14 | $12.00K" in text
    rows = {r["base"]: r["data"] for r in pg.parse_board(text)}
    assert rows["WAL"]["gross_pct"] == 3.21 and rows["WAL"]["max_pct"] == 3.4 and rows["WAL"]["held_min"] == 14
    assert rows["WAL"]["capacity"]["fits_usd"] == 12_000 and "capacity" not in rows["XYZ"]
    assert rows["XYZ"]["type"] == "spot_perp" and {"FOO", "BAR"} <= set(rows)


FF = """📊 #BRIC | Spread: 6.00% | +3.00%
📌 BRIC_USDT (COPY: BRIC)

🔴 Short MEXC : $1.0600
🟢 Long  Gate : $1.0000

🌗 Funding MEXC : -0.02%
🌓 Funding Gate : -1.28%

🔍 Доп. информация
⏱ F/Interval MEXC : 4H
⏱ F/Interval Gate : 4H
💱 Перенос фандинга: +1.2600% за 8ч в твою пользу
📦 Помещается ~$479, прибыль ~$20
⛓ MEXC     BEP20 : Dep:🟢 | W/d:🟢
0xF39e
⛓ Gate     BSC : Dep:🟢 | W/d:🔴
0xF39e

📋 Цены на всех биржах:
🔵 MEXC     : $1.0600
⚪️ Gate     : $1.0000

18:15 UTC
#SPREAD #BRIC"""


def test_detailed_futures_spread_message():
    p = pg.parse(FF)
    assert p["feed"] == "spreads" and p["base"] == "BRIC"
    d = p["data"]
    assert d["type"] == "perp_perp" and d["gross_pct"] == 6.0 and d["delta_pct"] == 3.0
    assert d["dear"] == "MEXC" and d["cheap"] == "Gate" and d["short_price"] == 1.06 and d["long_price"] == 1.0
    assert d["funding_pct"] == {"MEXC": -0.02, "Gate": -1.28} and d["funding_interval_h"]["Gate"] == 4
    assert d["carry_8h_pct"] == 1.26 and d["capacity"]["fits_usd"] == 479
    assert d["networks"][1] == {"ex": "Gate", "chain": "BSC", "deposit": True, "withdraw": False}
    assert pg.parse(FF.replace("+3.00%", "NEW"))["data"].get("delta_pct") is None


def test_spread_board_tool_returns_the_latest_board(tmp_path):
    st = TgStore(str(tmp_path / "tg.db"))
    now = time.time()
    st.add_messages(-100500, [{"id": 1, "ts": now - 7200, "text": BOARD.replace("3.21%", "1.00%")},
                              {"id": 2, "ts": now - 120, "text": BOARD}])
    r = pg.spread_board(st, [-100500])
    assert r["spreads"] == 2 and 1.5 < r["updated_min_ago"] < 3
    first = r["rows"][0]
    assert first["base"] == "XYZ" and first["now_pct"] == 9.09 and first["max_pct"] == 9.5  # biggest first
    wal = next(x for x in r["rows"] if x["base"] == "WAL")
    assert wal["now_pct"] == 3.21 and wal["buy_at"] == "Gate фьюч" and wal["fits_usd"] == 12_000
    assert "rows" in pg.spread_board(st, [-100999]) and "error" in pg.spread_board(st, [])


def test_index_feed_channel_is_parsed(tmp_path):
    from bottom.tg.sources import Source
    from bottom.tools import indexfeed

    st = TgStore(str(tmp_path / "tg.db"))
    st.sync_sources([Source(id=-100700, title="spread last index 5%", username="spread_last_index", kind="forum")])
    now = time.time()
    msg = "🔴 RTXUSDT · Aster\nSpread: +5.35% (last above index)\nHolding: 11s\nLast:  0.5772\nIndex: 0.5479"
    msg2 = "🟢 ESIMUSDTM · KuCoin\nSpread: -10.34% (last below index)\nHolding: 17477s\nLast:  0.063284\nIndex: 0.070583"
    st.add_messages(-100700, [{"id": 1, "ts": now - 600, "text": msg}, {"id": 2, "ts": now - 60, "text": msg2},
                              {"id": 3, "ts": now - 30, "text": "просто текст"}])
    p = indexfeed.parse(msg2)
    assert p["base"] == "ESIM" and p["exchange"] == "KuCoin" and p["spread_pct"] == -10.34 and p["holding_s"] == 17477
    top = indexfeed.index_spreads(st, hours=1)
    assert top["events"] == 2 and top["top"][0]["base"] == "ESIM" and top["top"][0]["max_abs_pct"] == 10.34
    one = indexfeed.index_spreads(st, "$rtx", 1)
    assert one["events"] == 1 and one["by_exchange"]["Aster"]["max_abs_pct"] == 5.35
    assert "note" in indexfeed.index_spreads(st, "NOPE", 1)
    assert "не в источниках" in indexfeed.index_spreads(st, hours=1, source="missing")["note"]
