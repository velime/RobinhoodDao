import pytest

from bottom.tools import market
from bottom.tools.market import alpha
from bottom.tools.market import exchanges as exch


class FakeEx:
    def __init__(self, ex_id, mid, depth, wall=None):
        self.id = ex_id
        self.markets = {"X/USDT:USDT": {"symbol": "X/USDT:USDT", "id": "XUSDT", "base": "X", "quote": "USDT",
                                        "swap": True, "linear": True, "spot": False}}
        self.mid, self.depth, self.wall = mid, depth, wall

    async def fetch_order_book(self, symbol, limit=None):
        m, q = self.mid, self.depth / self.mid / 10
        bids = [[m * (1 - 0.001 * i), q] for i in range(1, 11)]
        asks = [[m * (1 + 0.001 * i), q] for i in range(1, 11)]
        if self.wall:
            bids.append([m * 0.99, self.wall / (m * 0.99)])
        return {"bids": bids, "asks": asks}


class FakePool:
    def __init__(self, exs):
        self.exs = {e.id: e for e in exs}
        self.ids = list(self.exs) + ["kucoin"]  # kucoin: no such market

    async def get(self, ex_id):
        if ex_id == "kucoin":
            k = FakeEx("kucoin", 1, 1)
            k.markets = {}
            return k
        return self.exs.get(ex_id)


async def test_orderbook_all_exchanges():
    pool = FakePool([FakeEx("binance", 1.00, 100_000, wall=80_000), FakeEx("bybit", 1.002, 50_000)])
    r = await exch.orderbook(pool, "x", "perp")
    c = r["combined"]
    assert c["exchanges"] == 2 and [p["exchange"] for p in r["per_exchange"]] == ["binance", "bybit"]
    assert c["total"]["within_2pct"]["bids_usd"] > c["total"]["within_2pct"]["asks_usd"]  # the bid wall
    assert list(c["depth_share_within_2pct"])[0] == "binance"
    assert c["biggest_walls"][0]["exchange"] == "binance" and c["biggest_walls"][0]["side"] == "bid"
    assert c["mid_spread_across_exchanges_pct"] == pytest.approx(0.2, abs=0.01)
    assert r["no_market_on"] == ["kucoin"]
    one = await exch.orderbook(pool, "X", "perp", "bybit")
    assert one["exchange"] == "bybit" and "within_5pct" in one
    assert "error" in await exch.orderbook(pool, "X", "spot")


TOKENS = [
    {"symbol": "KOGE", "name": "BNB48 Club Token", "alphaId": "ALPHA_22", "chainId": "56", "chainName": "BSC",
     "contractAddress": "0xe6df05ce8c8301223373cf5b969afcb1498c5528", "price": "48.01", "percentChange24h": "0.4",
     "volume24h": "250000000", "liquidity": "9000000", "marketCap": "160000000", "fdv": "160000000",
     "holders": "29000", "listingTime": 1745000000000, "listingCex": False},
    {"symbol": "KOGE", "name": "Fake KOGE", "alphaId": "ALPHA_900", "chainId": "CT_501", "chainName": "Solana",
     "contractAddress": "So1aNaFakeKoge111111111111111111111111111", "price": "0.01", "volume24h": "1000"},
    {"symbol": "ZK", "alphaId": "ALPHA_5", "chainId": "1", "contractAddress": "0xzk", "price": "0.05",
     "percentChange24h": "-21.5", "volume24h": "900000", "listingTime": 1760000000000, "listingCex": True},
]


@pytest.fixture
def fake_alpha(monkeypatch):
    calls = []

    async def get_json(url, params=None, headers=None):
        calls.append((url, params))
        if url == alpha.TOKEN_LIST:
            return {"code": "000000", "success": True, "data": TOKENS}
        if url == alpha.TICKER:
            return {"code": "000000", "data": {"lastPrice": "48.02", "priceChangePercent": "0.5", "quoteVolume": "1e8",
                                               "count": 51000}}
        if url == alpha.KLINES:
            rows = []
            for i in range(60):
                p = 48 + (i % 5) * 0.1
                rows.append([str(1760000000000 + i * 3600000), str(p), str(p + 0.2), str(p - 0.2), str(p + 0.05),
                             "1000", "0", "48000", "120", "600", "28800", "0"])
            return {"code": "000000", "data": rows}
        raise AssertionError(url)

    monkeypatch.setattr(alpha.http, "get_json", get_json)
    monkeypatch.setattr(alpha, "_list_cache", (0.0, []))
    return calls


async def test_binance_alpha_token_and_market(fake_alpha):
    r = await alpha.binance_alpha("$koge")
    t = r["token"]
    assert t["alpha_id"] == "ALPHA_22" and t["chain"] == "BSC" and t["listed"] == "2025-04-18"
    assert t["ticker_24h"]["trades_24h"] == 51000 and r["same_ticker_other_tokens"][0]["chain"] == "SOL"
    assert ("https://www.binance.com/bapi/defi/v1/public/alpha-trade/ticker", {"symbol": "ALPHA_22USDT"}) in fake_alpha
    by_contract = await alpha.binance_alpha("0xE6DF05CE8C8301223373CF5B969AFCB1498C5528")
    assert by_contract["token"]["symbol"] == "KOGE"
    assert (await alpha.binance_alpha("NOPE"))["found"] is False
    m = await alpha.binance_alpha()
    assert m["tokens_on_alpha"] == 3 and m["top_volume_24h"][0]["symbol"] == "KOGE"
    assert m["biggest_moves_24h"][0]["symbol"] == "ZK" and m["newest_listings"][0]["symbol"] == "ZK"


async def test_alpha_candles_and_book(fake_alpha, monkeypatch):
    r = await market.candle_summary(None, "KOGE", "1h", 60, "alpha")
    assert r["source"]["market"] == "alpha" and r["source"]["symbol"] == "ALPHA_22" and r["range_24h"]["high"]

    async def dex_prices(chain, contracts):
        assert chain == "BSC"
        return {contracts[0]: {"dex": "pancakeswap", "liquidity_usd": 9e6}}

    monkeypatch.setattr("bottom.tools.onchain.dex.dex_prices", dex_prices)
    b = await market.book(None, "KOGE", "alpha")
    assert b["market"] == "alpha" and list(b["dex_pools"].values())[0]["dex"] == "pancakeswap"


async def test_overview_flags_alpha_namesake(fake_alpha, monkeypatch):
    async def cex(pool, symbol):
        return {"base": "KOGE", "median_price": 0.5}

    monkeypatch.setattr(market.exch, "market_overview", cex)
    r = await market.overview(None, "KOGE")
    assert r["binance_alpha"]["alpha_id"] == "ALPHA_22"
    assert "другой токен" in r["possible_different_token_or_thin_market"][0]


async def test_alpha_down_is_not_fatal(monkeypatch):
    async def boom(url, params=None, headers=None):
        raise RuntimeError("403")

    monkeypatch.setattr(alpha.http, "get_json", boom)
    monkeypatch.setattr(alpha, "_list_cache", (0.0, []))
    assert await alpha.lookup("BTC") is None
    assert "error" in await alpha.binance_alpha("BTC")
