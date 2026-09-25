from swarm.tools.indicators import atr, ema, nearest_levels, rsi, summarize_candles, swing_levels
from swarm.tools.symbols import normalize_base, strip_multiplier


def mk(closes, spread=1.0):
    return [
        {"t": i, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 100.0, "taker_buy": 60.0, "trades": 10}
        for i, c in enumerate(closes)
    ]


def test_rsi_extremes():
    assert rsi(list(range(1, 40))) == 100.0
    assert rsi(list(range(40, 1, -1))) < 1


def test_atr_constant_range():
    assert abs(atr(mk([100.0] * 30, spread=2.0)) - 4.0) < 1e-9


def test_ema_flat():
    assert ema([5.0] * 30, 20)[-1] == 5.0


def test_swing_levels_and_nearest():
    closes = [10, 11, 12, 15, 12, 11, 10, 9, 7, 9, 10, 11, 12, 13, 12]
    lv = swing_levels(mk(closes, spread=0.5), 2, 2)
    assert any(abs(h["price"] - 15.5) < 1e-9 for h in lv["swing_highs"])
    assert any(abs(l["price"] - 6.5) < 1e-9 for l in lv["swing_lows"])
    near = nearest_levels(11.0, lv)
    assert near["resistance"][0] > 11.0 and near["support"][0] < 11.0


def test_summary_has_delta():
    s = summarize_candles(mk([100 + i * 0.1 for i in range(60)]))
    assert s["taker_buy_share_last_tail_pct"] == 60.0
    assert s["atr14_pct"] is not None
    assert len(s["last_candles"]) == 12


def test_symbols():
    assert normalize_base("$hype") == "HYPE"
    assert normalize_base("HYPEUSDT") == "HYPE"
    assert normalize_base("hype/usdt") == "HYPE"
    assert normalize_base("BTC-USDT-SWAP") == "BTC"
    assert normalize_base("USDT") == "USDT"
    assert strip_multiplier("1000PEPE") == ("PEPE", 1000)
    assert strip_multiplier("1INCH") == ("1INCH", 1)
