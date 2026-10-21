import json
import time

import pytest

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


def _cfg(tmp_path, **kw):
    return SignalsConfig(db_path=str(tmp_path / "s.db"), **kw)


def _perp(ex, base, fr, price=1.0, vol=1e6, nxt=None, oi=None):
    return {"ex": ex, "kind": "perp", "base": base, "symbol": base + "USDT", "price": price, "oi_usd": oi,
            "funding_pct": fr, "interval_h": 8, "next_funding_ms": nxt, "vol24_usd": vol}


# ------------------------------------------------------------------ on-chain flows


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


async def test_aster_feeds_and_derivatives(monkeypatch):
    from bottom.tools import derivatives as dv
    from bottom.tools import feeds as fd

    async def fake(url, params=None, headers=None):
        if url.endswith("/fapi/v1/premiumIndex") and not params:
            return [{"symbol": "XAUUSDT", "markPrice": "4160.7", "indexPrice": "4161.0", "lastFundingRate": "0.00005",
                     "nextFundingTime": 1792339200000}, {"symbol": "BTCUSD1", "markPrice": "1"}]
        if url.endswith("/fapi/v1/ticker/24hr"):
            return [{"symbol": "XAUUSDT", "lastPrice": "4160.72", "quoteVolume": "430000"}]
        if url.endswith("/fapi/v1/fundingInfo"):
            return [{"symbol": "XAUUSDT", "fundingIntervalHours": 4}, {"symbol": "BTCUSDT", "fundingIntervalHours": 8}]
        if url.endswith("/api/v1/ticker/24hr"):
            return [{"symbol": "ABCUSDT", "lastPrice": "2", "quoteVolume": "50", "quoteAsset": "USDT"},
                    {"symbol": "ABCUSD1", "lastPrice": "2", "quoteVolume": "5", "quoteAsset": "USD1"}]
        if url.endswith("/fapi/v1/premiumIndex"):
            return {"symbol": "BTCUSDT", "markPrice": "81000", "indexPrice": "81040", "lastFundingRate": "0.00006",
                    "nextFundingTime": 4102444800000}
        if url.endswith("/fapi/v1/openInterest"):
            return {"symbol": "BTCUSDT", "openInterest": "5000"}
        if url.endswith("/fapi/v1/fundingRate"):
            return [{"fundingRate": "0.0001"}, {"fundingRate": "0.00006"}]
        raise AssertionError(url)

    monkeypatch.setattr(fd.http, "get_json", fake)
    monkeypatch.setattr(dv.http, "get_json", fake)
    perp = await fd.aster_perp()
    assert [(r["ex"], r["base"], r["price"], r["interval_h"], r["funding_pct"]) for r in perp] == [
        ("aster", "XAU", 4160.72, 4, 0.005)]  # USD1-quoted contracts are skipped
    assert perp[0]["vol24_usd"] == 430000 and perp[0]["index"] == 4161.0
    spot = await fd.aster_spot()
    assert [(r["base"], r["kind"]) for r in spot] == [("ABC", "spot")]
    assert ("aster", "perp") in fd.FETCHERS and ("aster", "spot") in fd.FETCHERS
    dv._aster_intervals.update(ts=0.0, data={})
    a = await dv._aster("BTCUSDT")
    assert a["funding_now_pct"] == 0.006 and a["oi_usd_now"] == 5000 * 81000 and a["funding_interval_hours"] == 8
    assert a["mark_index_premium_pct"] == -0.049 and a["last_payments_pct"] == [0.01, 0.006]
