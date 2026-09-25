from swarm.config import RiskRules
from swarm.tools.setup_calc import calc_setup

RULES = RiskRules(min_rr=1.3, min_stop_pct_major=1.5, min_stop_pct_alt=3.0, min_stop_atr_mult=1.5)


def test_published_maga_calls_pass():
    # RAY long: entry 1.42, stop 1.373, TP1 1.484 → R:R ≈ 1.36
    r = calc_setup(RULES, "RAY", "long", 1.42, 1.373, [1.484, 1.52])
    assert r["verdict"] == "OK", r["problems"]
    assert r["rr_tp1"] == 1.36
    # PHA short: 0.034 / stop 0.0354 / TP1 0.0298 → R:R 3.0
    r = calc_setup(RULES, "PHA", "short", 0.034, 0.0354, [0.0298, 0.0281])
    assert r["verdict"] == "OK", r["problems"]
    assert r["rr_tp1"] == 3.0
    # SAGA short: 0.0182 / 0.01877 / 0.01625 → R:R ≈ 3.42
    r = calc_setup(RULES, "SAGA", "short", 0.0182, 0.01877, [0.01625, 0.0148])
    assert r["verdict"] == "OK", r["problems"]
    assert r["rr_tp1"] == 3.42


def test_pengu_chat_setup_rejected_for_rr():
    # Swarm in chat claimed "R:R ≈ 1:3–5", but TP1 +0.9% vs stop −3.7%
    r = calc_setup(RULES, "PENGU", "long", 0.00592, 0.00574, [0.006, 0.0066, 0.007], entry_high=0.00595)
    assert r["verdict"] == "REJECT"
    assert r["rr_tp1"] < 0.5
    assert any("R:R" in p for p in r["problems"])
    assert r["suggestions"]


def test_re_breakout_rejected():
    # Long on breakout 0.4750, stop 0.4621 (2.7%), target 0.4768 (+0.4%)
    r = calc_setup(RULES, "RE", "long", 0.4750, 0.4621, [0.4768])
    assert r["verdict"] == "REJECT"
    assert any("стоп слишком близко" in p for p in r["problems"])
    assert any("R:R" in p for p in r["problems"])


def test_atr_raises_min_stop():
    # stop 4% away passes the 3% alt floor, but fails 1.5×ATR when ATR1h = 3%
    ok = calc_setup(RULES, "EUL", "long", 1.0, 0.96, [1.2])
    assert ok["verdict"] == "OK"
    r = calc_setup(RULES, "EUL", "long", 1.0, 0.96, [1.2], atr_pct=3.0)
    assert r["verdict"] == "REJECT"
    assert r["min_stop_pct_required"] == 4.5


def test_major_floor_is_lower():
    r = calc_setup(RULES, "BTC", "long", 100_000, 98_000, [104_000])
    assert r["verdict"] == "OK"
    assert r["min_stop_pct_required"] == 1.5


def test_zone_uses_worst_edge_for_rr_and_best_for_stop():
    r = calc_setup(RULES, "X", "long", 10.0, 9.5, [11.0], entry_high=10.2)
    assert r["rr_tp1"] == round((11.0 - 10.2) / (10.2 - 9.5), 2)
    assert r["stop_pct_from_best_entry"] == 5.0


def test_geometry_errors():
    assert calc_setup(RULES, "X", "long", 10, 11, [12])["verdict"] == "ERROR"
    assert calc_setup(RULES, "X", "short", 10, 11, [12])["verdict"] == "ERROR"
    assert calc_setup(RULES, "X", "sideways", 10, 9, [12])["verdict"] == "ERROR"


def test_leverage_table():
    r = calc_setup(RULES, "X", "long", 100, 95, [110])
    row5 = next(x for x in r["leverage_table"] if x["leverage"] == 5)
    assert row5["loss_at_stop_pct_of_margin"] == 25.0
    row20 = next(x for x in r["leverage_table"] if x["leverage"] == 20)
    assert row20["liquidation_before_stop"] is True
