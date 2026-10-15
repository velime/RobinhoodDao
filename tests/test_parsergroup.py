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
