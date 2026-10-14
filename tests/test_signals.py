import time

import pytest

from bottom.signals import formats as fmt
from bottom.signals import scanners as sc
from bottom.signals.config import SignalsConfig
from bottom.signals.poster import Poster
from bottom.signals.reader import signals as read_signals
from bottom.signals.store import SignalStore
from bottom.tools import feeds, oicard, onchain, spreads, transfers
from bottom.tools.ultra import classify, combos, parse_alert


def test_base_of():
    assert feeds.base_of("1000PEPEUSDT") == ("PEPE", 1000)
    assert feeds.base_of("PEPE-USDT-SWAP") == ("PEPE", 1)
    assert feeds.base_of("XBTUSDTM") == ("BTC", 1)
    assert feeds.base_of("ETH_USDT") == ("ETH", 1)
    assert feeds.base_of("ETHBTC") is None and feeds.base_of("ETH-USDC") is None


async def test_bulk_feeds(monkeypatch):
    async def fake(url, params=None, headers=None):
        if "bybit" in url:
            return {"result": {"list": [{"symbol": "1000PEPEUSDT", "lastPrice": "0.012", "openInterestValue": "5000000",
                                         "fundingRate": "-0.0123", "nextFundingTime": "1760000000000",
                                         "turnover24h": "9000000", "fundingIntervalHour": "4"}]}}
        if "futures/usdt/contracts" in url:
            return [{"name": "WAL_USDT", "last_price": "0.06", "funding_rate": "0.001", "funding_interval": 14400,
                     "funding_next_apply": 1760000000, "position_size": 1000000, "quanto_multiplier": "10"}]
        if "futures/usdt/tickers" in url:
            return [{"contract": "WAL_USDT", "volume_24h_quote": "123456"}]
        raise AssertionError(url)

    async def fake_post(url, json, headers=None):
        return [{"universe": [{"name": "kPEPE"}, {"name": "HYPE"}]},
                [{"markPx": "12.0", "openInterest": "100", "funding": "0.0001", "dayNtlVlm": "5"},
                 {"markPx": "40", "openInterest": "1000", "funding": "-0.00005", "dayNtlVlm": "7"}]]

    monkeypatch.setattr(feeds.http, "get_json", fake)
    monkeypatch.setattr(feeds.http, "post_json", fake_post)
    b = (await feeds.bybit_perp())[0]
    assert b["base"] == "PEPE" and b["price"] == pytest.approx(0.000012) and b["funding_pct"] == -1.23
    assert b["interval_h"] == 4 and b["oi_usd"] == 5e6
    g = (await feeds.gate_perp())[0]
    assert g["oi_usd"] == pytest.approx(1e6 * 10 * 0.06) and g["interval_h"] == 4 and g["vol24_usd"] == 123456
    h = await feeds.hyperliquid_perp()
    assert h[0]["base"] == "PEPE" and h[0]["price"] == pytest.approx(0.012) and h[1]["oi_usd"] == 40000


def _tr(ex, base, chain, dep, wd, contract="0xabc", fee=1.0):
    return {"ex": ex, "base": base, "chain": transfers.chain_id(chain), "chain_raw": chain, "deposit": dep,
            "withdraw": wd, "fee": fee, "contract": contract}


def test_chain_ids_and_route():
    assert transfers.chain_id("BEP20") == "BSC" and transfers.chain_id("ERC20") == "ETH"
    assert transfers.chain_id("Arbitrum One") == "ARB"
    rows = [_tr("mexc", "XYZ", "BEP20", True, True), _tr("gate", "XYZ", "BSC", True, True),
            _tr("mexc", "XYZ", "ERC20", True, False), _tr("gate", "XYZ", "ETH", True, True)]
    r = transfers.route(rows, "XYZ", "mexc", "gate")
    assert r["status"] == "open" and r["networks"] == ["BSC"] and r["fastest"] == "BSC"
    rows[0]["withdraw"] = False
    r = transfers.route(rows, "XYZ", "mexc", "gate")
    assert r["status"] == "closed" and "вывод закрыт" in r["why"]
    assert transfers.route(rows, "XYZ", "mexc", "okx")["status"] == "unknown"


async def test_transfer_status_conflict(monkeypatch):
    rows = [_tr("binance", "ABC", "BSC", True, True, "0xAAA"), _tr("gate", "ABC", "BSC", False, True, "0xbbb")]

    async def fake_all(force=False):
        return rows, {"bybit": "статусы только с API-ключом (read-only) в .env"}

    monkeypatch.setattr(transfers, "all_statuses", fake_all)
    r = await transfers.transfer_status("abc")
    assert r["contract_conflicts"] == {"BSC": ["0xaaa", "0xbbb"]} and r["closed"] == ["gate BSC: депозит закрыт"]


async def test_public_transfer_lists(monkeypatch):
    async def fake(url, params=None, headers=None):
        if "binance" in url:
            return {"data": [{"coin": "ABC", "networkList": [{"network": "BSC", "depositEnable": True,
                                                              "withdrawEnable": False, "withdrawFee": "0.5",
                                                              "contractAddress": "0xabc"}]}]}
        if "gateio" in url:
            return [{"currency": "ABC", "chains": [{"name": "BSC", "addr": "0xabc", "deposit_disabled": True,
                                                    "withdraw_disabled": False}]}]
        raise AssertionError(url)

    monkeypatch.setattr(transfers.http, "get_json", fake)
    b = (await transfers.binance())[0]
    assert (b["deposit"], b["withdraw"], b["fee"], b["chain"]) == (True, False, 0.5, "BSC")
    g = (await transfers.gate())[0]
    assert (g["deposit"], g["withdraw"]) == (False, True)


def _v(venue, kind, price, vol=1e6, f8=None, index=None, mark=None, ex=None):
    return {"venue": venue, "ex": ex or venue.split()[0].lower(), "kind": kind, "price": price, "vol24_usd": vol,
            "funding_8h": f8, "index": index, "mark": mark}


def test_perp_and_spot_perp_pairs_and_index():
    vs = [_v("Gate фьюч", "perp", 1.00, f8=-0.05, index=1.0, mark=1.0),
          _v("MEXC фьюч", "perp", 1.024, f8=0.30, index=1.0, mark=1.024),
          _v("Bybit фьюч", "perp", 1.01, f8=0.01, index=0.95, mark=1.01),
          _v("Dead фьюч", "perp", 2.0, vol=10),
          _v("Binance спот", "spot", 1.00), _v("DEX bsc", "dex", 0.97)]
    pp = spreads.perp_pair(vs)
    assert pp["long"]["venue"] == "Gate фьюч" and pp["short"]["venue"] == "MEXC фьюч" and pp["gross_pct"] == 2.4
    assert pp["carry_8h_pct"] == 0.35
    sp = spreads.spot_perp_pair(vs)
    assert sp["spot"]["venue"] == "DEX bsc" and sp["perp"]["venue"] == "MEXC фьюч" and sp["gross_pct"] == 5.57
    assert sp["scheme"].startswith("купить спот") and sp["carry_8h_pct"] == 0.30
    ix = spreads.index_checks(vs)
    assert any(f.startswith("MEXC фьюч: перп +2.40%") for f in ix["flags"])
    assert any("Bybit фьюч: индекс -5.00%" in f for f in ix["flags"])
    assert any("марк" in f and "MEXC" in f for f in ix["flags"])
    assert not any(v["kind"] == "spot" for v in (pp["long"], pp["short"]))  # no spot↔spot anywhere


def test_spread_persistence_capacity_verdict():
    a = [(i * 300000, 1.0) for i in range(24)]
    b = [(i * 300000, 1.0 if i < 20 else 1.03) for i in range(24)]
    h = spreads.persistence(a, b, 3.0)
    assert h["held_minutes"] == 20 and not h["held_whole_window"]
    cap = spreads.capacity([(1.0, 100), (1.02, 100)], [(1.06, 150), (1.0, 100)], 0.06)
    assert cap["fits_usd"] == round(100 * 1.0 + 50 * 1.02) and cap["profit_usd"] > 0
    pair = {"gross_pct": 3.0, "net_pct": 2.76, "carry_8h_pct": -1.2}
    v = spreads.verdict(pair, h, {"fits_usd": 5000, "profit_usd": 100})
    assert v["enter"] is False and any("съест спред" in r for r in v["reasons"])
    pair["carry_8h_pct"] = 0.1
    v = spreads.verdict(pair, h, {"fits_usd": 5000, "profit_usd": 100})
    assert v["enter"] is True
    assert spreads.verdict(pair, h, {"fits_usd": 151})["enter"] is False
    trows = [_tr("mexc", "XYZ", "BSC", False, True), _tr("mexc", "XYZ", "ETH", False, True)]
    assert "закрыты все депозиты" in spreads.spot_context(trows, "XYZ", {"ex": "mexc", "kind": "spot"})


def test_onchain_helpers():
    pools = onchain.best_pools([
        {"baseToken": {"address": "0xAbC", "symbol": "X"}, "priceUsd": "1.5", "liquidity": {"usd": 50000}, "chainId": "bsc"},
        {"baseToken": {"address": "0xabc"}, "priceUsd": "1.6", "liquidity": {"usd": 90000}, "chainId": "bsc"},
        {"baseToken": {"address": "0xdef"}, "priceUsd": "1", "liquidity": {"usd": 100}},
    ])
    assert list(pools) == ["0xabc"] and pools["0xabc"]["price_usd"] == 1.6
    now = time.time()
    iso = lambda dt: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - dt))  # noqa: E731
    trades = [{"attributes": {"block_timestamp": iso(600), "kind": "sell", "volume_in_usd": "30000", "tx_from_address": "0xw"}},
              {"attributes": {"block_timestamp": iso(60), "kind": "sell", "volume_in_usd": "20000", "tx_from_address": "0xw"}},
              {"attributes": {"block_timestamp": iso(30), "kind": "buy", "volume_in_usd": "5000", "tx_from_address": "0xw"}},
              {"attributes": {"block_timestamp": iso(90000), "kind": "sell", "volume_in_usd": "9e6", "tx_from_address": "0xold"}}]
    s = onchain.wallet_sells(trades, 1_000_000, now)
    assert s == [{"wallet": "0xw", "net_sold_usd": 45000, "sells": 2, "minutes": 10, "pct_of_liquidity": 4.5}]


def test_book_stats_and_combos():
    bids = [[99.5, 10], [95, 500], [90, 1]]
    asks = [[100.5, 5], [104, 3]]
    b = oicard.book_stats(bids, asks, 100)
    assert b["book_delta_usd"] == round(995 - 502.5)
    assert b["walls"][0]["side"] == "below" and b["walls"][0]["price"] == 95
    c = combos({"oi_chg_pct": 30, "price_chg_pct": 5, "book_delta_usd": 1, "long_pct": 50, "oi_mcap_pct": 8,
                "per_exchange": [{"book_delta_usd": 5}, {"book_delta_usd": 7}]})
    assert [x.split()[1] for x in c] == ["#1", "#2", "#3", "#5"]
    c = combos({"oi_chg_pct": 30, "price_chg_pct": -4, "book_delta_usd": -1, "long_pct": 70, "funding_pct": 0.6,
                "oi_mcap_pct": 12})
    assert [x.split()[1] for x in c] == ["#6", "#7", "#8"]
    assert combos({"oi_chg_pct": -10, "price_chg_pct": 3, "long_pct": 35})[0].split()[1] == "#4"
    assert "OI падает + цена растёт" in classify({"oi_chg_pct": -10, "price_chg_pct": 3})["scenario"]


def test_oi_format_roundtrip():
    card = {"ex": "bybit", "base": "WAL", "oi_chg_pct": 34.91, "oi_fast_chg_pct": 12.0, "oi_coins_from": 116.68e6,
            "oi_coins": 136.23e6, "oi_added_usd": 2.12e6, "price": 0.06026, "price_chg_pct": 0.77,
            "funding_pct": -0.3427, "long_pct": 40.2, "mark_side": "short", "oi_mcap_pct": 5.2,
            "book_delta_usd": 62100, "ad_spot_usd": 104300, "ad_perp_usd": 6.5e6,
            "walls": [{"side": "below", "price": 0.055, "dist_pct": -8.7, "usd": 40000, "x_avg": 24}]}
    text = fmt.oi_alert("WAL", card, 53, classify(card))
    plain = text.replace("<b>", "").replace("</b>", "")
    a = parse_alert(plain)
    assert a["ticker"] == "WAL" and a["exchange"] == "Bybit" and a["oi_chg_pct"] == 34.91
    assert a["funding_pct"] == -0.3427 and a["long_pct"] == 40.2 and a["book_delta_usd"] == 62100
    assert a["ad_perp_usd"] == 6.5e6 and a["signal_no"] == 53 and a["walls"][0]["x_avg"] == 24


def _cfg(tmp_path, **kw):
    return SignalsConfig(db_path=str(tmp_path / "s.db"), **kw)


def _perp(ex, base, fr, price=1.0, vol=1e6, nxt=None, oi=None):
    return {"ex": ex, "kind": "perp", "base": base, "symbol": base + "USDT", "price": price, "oi_usd": oi,
            "funding_pct": fr, "interval_h": 8, "next_funding_ms": nxt, "vol24_usd": vol}


async def test_funding_scanner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = SignalStore(cfg.db_path)
    rows = [_perp("binance", "ABC", -0.8), _perp("bybit", "ABC", -0.2), _perp("gate", "BTC", 0.01)]

    async def snap(exs=None, kinds=("perp",)):
        return [dict(r) for r in rows], {}

    monkeypatch.setattr(sc.feeds, "snapshot", snap)
    s = sc.FundingScanner(cfg, store)
    a = await s.scan()
    assert [x.base for x in a] == ["ABC"] and a[0].data["kind"] == "extreme" and "▼ Binance" in a[0].text
    assert "Разница фандинга" in a[0].text
    assert await s.scan() == []  # cooldown, no change
    rows[1]["funding_pct"] = -1.4  # bybit jumps by 1.2pp
    a = await s.scan()
    assert a[0].data["kind"] == "jump" and "было -0.2000%" in a[0].text
    rows[2]["next_funding_ms"] = int((time.time() + 600) * 1000)
    rows[2]["funding_pct"] = 0.9
    a = await s.scan()
    assert any(x.data.get("kind") == "settlement" for x in a)


async def test_oi_scanner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = SignalStore(cfg.db_path)
    now = int(time.time())
    store.add_oi(now - 4 * 3600, [("bybit", "WAL", 10_000_000, 1_000_000, 0.10), ("bybit", "FLAT", 1e7, 1e6, 0.1)])
    cur = [{**_perp("bybit", "WAL", -0.3, price=0.11, oi=14_000_000 * 0.11)},
           {**_perp("bybit", "FLAT", 0.0, price=0.1, oi=1e7 * 0.1)}]

    async def rows(self):
        return [dict(r) for r in cur]

    async def enrich(self, c):
        return c | {"long_pct": 40.0, "book_delta_usd": 5000}

    monkeypatch.setattr(sc.OIScanner, "rows", rows)
    monkeypatch.setattr(sc.OIScanner, "_enrich", enrich)
    s = sc.OIScanner(cfg, store)
    a = await s.scan()
    assert [x.base for x in a] == ["WAL"]
    assert a[0].data["oi_chg_pct"] == 40.0 and a[0].data["price_chg_pct"] == 10.0 and a[0].data["signal_no"] == 1
    assert "шорт-сквиз" in a[0].data["pattern"] and "#OI #WAL" in a[0].text
    assert await s.scan() == []  # cooldown


async def test_status_scanner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, status_alerts=True)
    store = SignalStore(cfg.db_path)
    state = [_tr("gate", "ABC", "BSC", False, True)]

    async def fake_all(force=False):
        return [dict(r) for r in state], {}

    async def snap(exs=None, kinds=("perp",)):
        return [_perp("bybit", "ABC", 0.01)], {}

    monkeypatch.setattr(sc.transfers, "all_statuses", fake_all)
    monkeypatch.setattr(sc.feeds, "snapshot", snap)
    s = sc.StatusScanner(cfg, store)
    assert await s.scan() == []  # baseline
    state[0]["deposit"] = True
    a = await s.scan()
    assert a[0].base == "ABC" and "🟢 открыт депозит · BSC" in a[0].text
    assert await s.scan() == []


async def test_spread_scanner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = SignalStore(cfg.db_path)

    def r(ex, kind, base, price, fr=None, index=None):
        return {"ex": ex, "kind": kind, "base": base, "symbol": base + "USDT", "price": price, "vol24_usd": 9e5,
                "funding_pct": fr, "interval_h": 8, "next_funding_ms": None, "oi_usd": None, "mark": price,
                "index": index}

    rows = [r("gate", "perp", "XYZ", 1.00, -0.01, 1.0), r("mexc", "perp", "XYZ", 1.02, 0.05, 1.0),
            r("binance", "spot", "XYZ", 1.0), r("mexc", "spot", "XYZ", 1.10),  # spot↔spot 10% must be ignored
            r("bybit", "perp", "BTC", 100.0, 0.01, 100.0), r("okx", "perp", "BTC", 100.01, 0.01, 100.0)]

    async def snap(exs=None, kinds=("perp", "spot")):
        return [dict(x) for x in rows], {}

    async def fake_all(force=False):
        return [], {}

    async def no_dex(*a, **k):
        return None

    async def book(pool, v):
        return ([(v["price"], 5000)], [(v["price"], 5000)])

    monkeypatch.setattr(sc.feeds, "snapshot", snap)
    monkeypatch.setattr(sc.transfers, "all_statuses", fake_all)
    monkeypatch.setattr(sc, "refresh_dex", no_dex)
    monkeypatch.setattr(sc.spreads, "_book", book)
    s = sc.SpreadScanner(cfg, store)
    try:
        a = await s.scan()
    finally:
        await s.close()
    kinds = {(x.base, x.data["type"]) for x in a}
    assert ("XYZ", "perp_perp") in kinds and ("XYZ", "spot_perp") in kinds and ("XYZ", "index") in kinds
    assert not any(x.base == "BTC" for x in a)
    pp = next(x for x in a if x.data["type"] == "perp_perp")
    assert "фьюч↔фьюч 2.00%" in pp.text and "перенос: +0.0600% за 8ч в твою пользу" in pp.text
    sp = next(x for x in a if x.data["type"] == "spot_perp")
    assert sp.data["cheap"] == "Gate фьюч" and sp.data["dear"] == "MEXC спот" and sp.data["gross_pct"] == -9.09
    assert "лонг фьюч + продать спот" in sp.text


def test_store_and_reader(tmp_path):
    path = str(tmp_path / "r.db")
    assert "error" in read_signals(path)
    st = SignalStore(path)
    st.add_alert("oi", "WAL", "bybit", {"oi_chg_pct": 40, "pattern": "шорт-сквиз", "ex": "bybit"}, "t", None)
    st.add_alert("spreads", "WAL", "mexc>gate", {"gross_pct": 5, "route": {"status": "open"}}, "t", None)
    st.add_alert("oi", "ABC", "binance", {"oi_chg_pct": 25}, "t", None)
    top = read_signals(path)
    assert top["coins"] == 2 and top["top"][0]["base"] == "WAL" and top["top"][0]["feeds"] == ["oi", "spreads"]
    one = read_signals(path, symbol="$wal")
    assert one["alerts"] == 2 and {r["feed"] for r in one["recent"]} == {"oi", "spreads"}
    k, ts = "a|b|c", int(time.time())
    for t in (ts - 900, ts - 600, ts - 300, ts):
        st.add_spread(k, t, 5.0)
    st.add_spread(k, ts - 5000, 9.0)
    assert st.spread_since(k, ts, 450) == (ts - 900, 5.0)


async def test_poster_dry(tmp_path, capsys):
    p = Poster("", None, SignalStore(str(tmp_path / "p.db")))
    assert p.dry and await p.send("oi", "hello") is None
    await p.close()
    assert "hello" in capsys.readouterr().out


# ------------------------------------------------------------------ on-chain flows

import gzip  # noqa: E402
import json  # noqa: E402

from bottom.tools import chainflows as cf  # noqa: E402

HOT = "0x28c6c06298d514db089934071355e5743bf21d60"
DEP = "0x8b9000000000000000000000000000000000abcd"
USER = "0x00d0000000000000000000000000000000001234"
OKX_HOT = "0xe7c0000000000000000000000000000000000001"
MM = "0x7390000000000000000000000000000000000002"
TOKEN = "0x1111111111111111111111111111111111111111"


def _book(tmp_path, store=None):
    p = tmp_path / "w.json.gz"
    data = {"evm": {HOT: {"e": "Binance", "k": "exchange", "t": "hot"}, DEP: {"e": "Kraken", "k": "exchange", "t": "deposit"},
                    OKX_HOT: {"e": "OKX", "k": "exchange", "t": "reserve"}, MM: {"e": "Flowdesk", "k": "mm", "t": "mm"}},
            "tron": {}, "solana": {}}
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)
    return cf.WalletBook(store, str(p))


def test_classify_and_learn(tmp_path):
    store = SignalStore(str(tmp_path / "f.db"))
    b = _book(tmp_path, store)
    assert cf.classify(b, "evm", USER, DEP) == {"kind": "deposit", "exchange": "Kraken"}
    assert cf.classify(b, "evm", HOT, USER) == {"kind": "withdrawal", "exchange": "Binance", "to_mm": None}
    assert cf.classify(b, "evm", OKX_HOT, MM)["to_mm"] == "Flowdesk"
    assert cf.classify(b, "evm", MM, DEP)["kind"] == "deposit"
    assert cf.classify(b, "evm", HOT, DEP) == {"kind": "deposit", "exchange": "Kraken", "from_exchange": "Binance"}
    newdep = "0x9990000000000000000000000000000000000009"
    assert cf.classify(b, "evm", newdep, HOT) == {"kind": "sweep", "exchange": "Binance"}  # learned
    assert cf.classify(b, "evm", USER, newdep) == {"kind": "deposit", "exchange": "Binance"}
    assert cf.classify(b, "evm", newdep, HOT) is None  # now internal
    assert _book(tmp_path, store).label("evm", newdep)["e"] == "Binance"  # persisted
    assert b.describe("evm", DEP) == "Kraken Deposit (0x8b9)" and b.describe("evm", HOT).startswith("Binance · Hot Wallet")


def test_parse_log():
    lg = {"address": TOKEN.upper().replace("X", "x"), "topics": [cf.TRANSFER_TOPIC, "0x" + "0" * 24 + USER[2:],
                                                               "0x" + "0" * 24 + DEP[2:]],
          "data": hex(5 * 10**18), "transactionHash": "0xtx", "blockNumber": "0x10"}
    e = cf.parse_log(lg)
    assert e["from"] == USER and e["to"] == DEP and e["raw"] == 5 * 10**18 and e["block"] == 16
    assert cf.parse_log({**lg, "topics": lg["topics"] + ["0x1"]}) is None


class FakeRpc:
    def __init__(self, logs):
        self._logs = logs

    async def block(self, chain):
        return 1000

    async def logs(self, chain, contracts, frm, to):
        out, self._logs = self._logs, []
        return out

    async def decimals(self, chain, token):
        return 18

    async def balance_of(self, chain, token, addr):
        return 0

    async def native_balance(self, chain, addr):
        return 2 * 10**18


def _log(frm, to, amount):
    return {"address": TOKEN, "topics": [cf.TRANSFER_TOPIC, "0x" + "0" * 24 + frm[2:], "0x" + "0" * 24 + to[2:]],
            "data": hex(int(amount * 10**18)), "transactionHash": "0x" + str(abs(hash((frm, to, amount)))),
            "blockNumber": "0x3e8"}


async def test_flow_scanner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, flow_chains=("ETH",))
    store = SignalStore(cfg.db_path)
    store.put("por:ts", time.time())
    store.put("px", {"_ts": time.time(), "SYRUP": 0.2, "ETH": 3000})
    store.put("cgstats", {"_ts": time.time(), "SYRUP": [250e6, 14e6]})

    async def fake_all(force=False):
        return [_tr("binance", "SYRUP", "ETH", True, True, TOKEN)], {}

    monkeypatch.setattr(sc.transfers, "all_statuses", fake_all)
    s = sc.FlowScanner(cfg, store)
    s.book = _book(tmp_path, store)
    s.rpc = FakeRpc([_log(USER, DEP, 6_000_000), _log(HOT, USER, 2_000_000), _log(USER, DEP, 100)])
    a = await s.scan()
    assert [x.data["kind"] for x in a] == ["deposit", "withdrawal"]
    d = a[0]
    assert d.data["usd"] == 1_200_000 and d.data["whale"] and d.data["pct_vol"] == round(1.2e6 / 14e6 * 100, 2)
    assert "🔴 Deposit 🐋 | $SYRUP" in d.text and "Kraken Deposit (0x8b9)" in d.text and "завёл весь запас" in d.text
    assert "от капы" in d.text and "etherscan.io/address/" in d.text
    w = a[1]
    assert "🟢 Withdrawal | $SYRUP" in w.text and "Binance · Hot Wallet" in w.text and "⛽ native: 2.0000 ETH" in w.text
    assert len(store.flows(base="SYRUP")) == 2  # the $20 one is not stored
    summ = cf.flows_summary(store, "syrup")
    assert summ["deposits_usd"] == 1_200_000 and summ["withdrawals_usd"] == 400_000 and summ["read"].startswith("заводят")


def test_build_wallets_helpers():
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location("bw", pathlib.Path(__file__).parent.parent / "scripts" / "build_wallets.py")
    bw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bw)
    assert bw.entity_of("Binance: Hot Wallet 20") == ("Binance", "exchange")
    assert bw.entity_of("Wintermute") == ("Wintermute", "mm")
    assert bw.entity_of("Uniswap V3: Router") is None
    assert bw.wtype("Bitget Dep: 0x000483C56FE", "exchange") == "deposit"
    assert bw.wtype("Binance 14", "exchange") == "hot" and bw.wtype("Kraken: Cold Wallet", "exchange") == "cold"
    assert bw.skip_tag("Bybit Exploiter 3") and not bw.skip_tag("Bybit: Hot Wallet 2")
    assert bw.family("TK86Qm97uM848dMk8G7xNbJB7zG1uW3h1n") == "tron" and bw.family(HOT) == "evm"


def test_wallet_book_shipped():
    b = cf.WalletBook()
    assert len(b.w["evm"]) > 10000 and b.label("evm", HOT)["e"] == "Binance"


def test_mcp_server_roundtrip():
    import io

    from bottom.mcp_server import Server

    class TB:
        tools = {"echo": SimpleNamespaceTool()}

        async def execute(self, name, args):
            return json.dumps({"you_said": args.get("x")})

    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
             json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"x": 5}}}),
             json.dumps({"jsonrpc": "2.0", "id": 4, "method": "bogus"}), "not json"]
    sent = []
    srv = Server(TB())
    srv.send = sent.append
    import asyncio

    asyncio.run(srv.serve(io.StringIO("\n".join(lines) + "\n")))
    by = {m.get("id"): m for m in sent}
    assert by[1]["result"]["protocolVersion"] == "2025-06-18"
    assert by[2]["result"]["tools"][0]["name"] == "echo"
    assert json.loads(by[3]["result"]["content"][0]["text"]) == {"you_said": 5}
    assert by[4]["error"]["code"] == -32601 and by[None]["error"]["code"] == -32700


class SimpleNamespaceTool:
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}}
