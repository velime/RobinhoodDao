import time

from bottom.tg.store import TgStore
from bottom.tools import derivatives as deriv
from bottom.tools import fundamentals as fund
from bottom.tools.indicators import bollinger, fib_last_impulse, level_touches, macd, summarize_candles
from bottom.tools.ultra import classify, parse_alert, score, summarize, ultra_alerts

VIC = """$VIC | Binance
OI: +116.12% за 4ч | +40.12% за 37м
37.89M → 53.22M (+$1.81M)

💲 Цена: $0.062410 (+31.83%)
⛽ Funding: -1.5635%
⚖️ L/S: 44.8% 🟢 / 55.2% 🔴
🏷 Mark: $0.063332 (long)
🏛 OI/MCap: 52.46%
📗 Book Delta: +$84.9K
🌊 A/D спот 4ч: +$233.5K ↑ (спот берут)  (перп +$31.9K ↑)

🧱 Стакан Binance:
↑ $0.056263 (-9.8%) — $40.7K (24x)
↓ $0.070000 (+12.2%) — $33.0K (19x)

07:20 UTC | #8 сигнал"""

TRAP = """$ABC | Bybit
OI: +42.10% за 4ч
10.0M → 14.2M (+$1.2M)
💲 Цена: $1.2300 (-6.40%)
⛽️ Funding: +0.0400%
⚖️ L/S: 71.0% 🟢 / 29.0% 🔴
🏛 OI/MCap: 1450.00%
📕 Book Delta: -$120.5K
🌊 A/D спот 4ч: спот n/a  (перп -$2.92M ↓)
11:02 UTC | #3 сигнал"""


def test_parse_full_alert():
    a = parse_alert(VIC)
    assert a["ticker"] == "VIC" and a["exchange"] == "Binance"
    assert a["oi_chg_pct"] == 116.12 and a["oi_fast_chg_pct"] == 40.12 and a["oi_fast_window_min"] == 37
    assert a["oi_added_usd"] == 1.81e6 and a["price"] == 0.06241 and a["price_chg_pct"] == 31.83
    assert a["funding_pct"] == -1.5635 and a["long_pct"] == 44.8 and a["mark_side"] == "long"
    assert a["oi_mcap_pct"] == 52.46 and a["book_delta_usd"] == 84900 and a["ad_spot_usd"] == 233500
    assert a["ad_perp_usd"] == 31900 and a["signal_no"] == 8 and a["utc_time"] == "07:20"
    assert [w["side"] for w in a["walls"]] == ["below", "above"] and a["walls"][0]["x_avg"] == 24


def test_parse_negative_and_na():
    a = parse_alert(TRAP)
    assert a["exchange"] == "Bybit" and "oi_fast_chg_pct" not in a
    assert a["book_delta_usd"] == -120500 and a["ad_spot_usd"] is None and a["ad_perp_usd"] == -2.92e6
    assert parse_alert("$BTC летит, всем лонг") is None


def test_classify_patterns():
    c = classify(parse_alert(VIC))
    assert c["pattern"].startswith("шорт-сквиз")
    assert any("прошло до алерта" in f for f in c["flags"]) and any("органический" in f for f in c["flags"])
    t = classify(parse_alert(TRAP))
    assert t["pattern"].startswith("ловушка лонгистов")
    assert any("не торговать" in f for f in t["flags"]) and any("дистрибуции" in f for f in t["flags"])
    noise = classify({"oi_chg_pct": 24, "price_chg_pct": 0.4, "funding_pct": 0.005, "long_pct": 51})
    assert noise["pattern"].startswith("шум")
    hot = classify({"oi_chg_pct": 30, "price_chg_pct": 6, "funding_pct": 0.12, "long_pct": 70})
    assert hot["pattern"].startswith("перегрев") and any("перегрев" in f for f in hot["flags"])


def test_series_dual_exchange_and_score():
    now = time.time()
    a1 = parse_alert(VIC) | {"ts": now - 3 * 3600}
    a2 = parse_alert(VIC.replace("Binance", "Bybit").replace("#8", "#2")) | {"ts": now - 1800, "price": 0.0650}
    s = summarize([a2, a1], now)
    assert s["signals"] == 2 and s["both_exchanges_together"] is False  # 2.5h apart — not together
    a2["ts"] = now - 2 * 3600
    s = summarize([a1, a2], now)
    assert s["both_exchanges_together"] and s["price_move_since_first_alert_pct"] == round((0.065 - 0.06241) / 0.06241 * 100, 2)
    trap = summarize([parse_alert(TRAP) | {"ts": now}], now)
    assert score(s) > score(trap)


def test_ultra_alerts_from_store(tmp_path):
    st = TgStore(str(tmp_path / "u.db"))
    now = time.time()
    st.add_messages(-100, [{"id": 1, "ts": now - 600, "text": VIC}, {"id": 2, "ts": now - 300, "text": TRAP},
                           {"id": 3, "ts": now - 200, "text": "OI: растёт, что думаете?"}])
    st.add_messages(-200, [{"id": 5, "ts": now - 590, "text": VIC}])  # the same alert forwarded elsewhere
    r = ultra_alerts(st, hours=24)
    assert r["alerts_total"] == 2 and r["ranked"][0]["ticker"] == "VIC"
    one = ultra_alerts(st, "$vic", 24)
    assert one["signals"] == 1 and one["recent_alerts"][0]["signal_no"] == 8 and "ts" not in one["recent_alerts"][0]
    assert "error" in ultra_alerts(st, "ZZZ", 24)
    assert "error" in ultra_alerts(TgStore(str(tmp_path / "e.db")), hours=24)


def _candles(closes):
    return [{"t": i, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 100.0} for i, c in enumerate(closes)]


def test_new_indicators():
    up = [100 + i for i in range(60)]
    m = macd(up)
    assert m and m["macd"] > 0
    flat = [100.0] * 30
    b = bollinger(flat)
    assert b["width_pct"] == 0 and b["position_pct"] is None
    f = fib_last_impulse(_candles(up))
    assert f["impulse"] == "вверх" and f["retracements"]["0.5"] < up[-1] * 1.01
    down = fib_last_impulse(_candles(list(reversed(up))))
    assert down["impulse"] == "вниз"
    assert level_touches(_candles([100, 101, 100, 99, 120]), 100.0) == 4
    s = summarize_candles(_candles(up + list(reversed(up))[:20]))
    assert "macd" in s and "bollinger20" in s and "level_touches" in s


async def test_spot_perp_flow(monkeypatch):
    def k(vol, buy):
        return [0, 0, 0, 0, 0, 0, 0, str(vol), 0, 0, str(buy)]

    async def fake(url, params=None, headers=None):
        if "fapi" in url:
            return [k(100, 70)] * 48  # perp buys
        return [k(100, 40)] * 48  # spot sells

    monkeypatch.setattr(deriv.http, "get_json", fake)
    r = await deriv.spot_perp_flow("abc")
    assert r["pair"] == "ABCUSDT" and r["perp"]["4h"]["net_usd"] == 1920 and r["spot"]["1h"]["net_usd"] == -240
    assert "плечах" in r["read"] and r["perp_to_spot_volume"] == 1.0


async def test_dex_pairs_falls_back_to_geckoterminal(monkeypatch):
    async def fake(url, params=None, headers=None):
        if "dexscreener" in url:
            return {"pairs": []}
        return {"data": [{"attributes": {"name": "HOOD / ETH", "address": "0xabc", "base_token_price_usd": "1.5",
                                         "reserve_in_usd": "250000", "volume_usd": {"h24": "90000"},
                                         "transactions": {"h24": {"buys": 10, "sells": 4}},
                                         "pool_created_at": "2026-10-01T00:00:00Z"},
                          "relationships": {"network": {"data": {"id": "robinhood"}}, "dex": {"data": {"id": "uni"}}}}]}

    monkeypatch.setattr(fund.http, "get_json", fake)
    r = await fund.dex_pairs("HOOD")
    assert r["source"] == "GeckoTerminal" and r["pairs"][0]["chain"] == "robinhood"
    assert r["pairs"][0]["liquidity_usd"] == 250000 and r["pairs"][0]["txns_24h_buys_sells"] == [10, 4]


def test_classify_without_price_change_and_averaging_longs():
    assert classify({"oi_chg_pct": 20.8, "funding_pct": 0.005, "long_pct": 29.5})["pattern"].startswith("шорты набирают")
    assert classify({"oi_chg_pct": 26.6, "funding_pct": 0.01, "long_pct": 69.4})["pattern"].startswith("лонги перегружены")
    sign = {"oi_chg_pct": 25.79, "price_chg_pct": -4.57, "funding_pct": 0.005, "long_pct": 64.1}
    assert classify(sign)["pattern"].startswith("лонги усредняются")
