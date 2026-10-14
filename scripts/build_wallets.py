"""Build bottom/data/exchange_wallets.json.gz — labeled exchange / market-maker wallets.

Sources (all public):
  --arkham   JSON extracted from a Telegram export of an on-chain alert channel (Arkham-labeled links)
  --ethlabels  eth-labels data/csv/accounts.csv            (github.com/dawsbot/eth-labels)
  --etherscan  etherscan-labels data/ directory           (github.com/brianleect/etherscan-labels)
  --defillama  DefiLlama-Adapters projects/ directory      (CEX proof-of-reserves adapters)
Binance proof-of-reserves addresses are also fetched live by the bot, and deposit addresses are learned
while it runs, so this file is only the starting point.

    python scripts/build_wallets.py --arkham wallets.json --ethlabels accounts.csv \
        --etherscan etherscan-labels/data --defillama DefiLlama-Adapters/projects
"""

from __future__ import annotations

import argparse
import csv
import glob
import gzip
import json
import os
import re
from collections import Counter

EXCHANGES = {
    "binance": "Binance", "binance us": "Binance US", "okx": "OKX", "okex": "OKX", "bybit": "Bybit",
    "coinbase": "Coinbase", "coinbase prime": "Coinbase Prime", "kraken": "Kraken", "kucoin": "KuCoin",
    "gate": "Gate", "gate.io": "Gate", "gate-io": "Gate", "gateio": "Gate", "htx": "HTX", "huobi": "HTX",
    "bitget": "Bitget", "mexc": "MEXC", "upbit": "Upbit", "bithumb": "Bithumb", "crypto.com": "Crypto.com",
    "crypto-com": "Crypto.com", "bitfinex": "Bitfinex", "bitstamp": "Bitstamp", "bingx": "BingX",
    "bitvavo": "Bitvavo", "robinhood": "Robinhood", "gemini": "Gemini", "backpack": "Backpack",
    "backpack exchange": "Backpack", "poloniex": "Poloniex", "bitmart": "BitMart", "lbank": "LBank",
    "whitebit": "WhiteBIT", "coinex": "CoinEx", "xt.com": "XT.com", "hashkey": "HashKey", "deribit": "Deribit",
    "bitpanda": "Bitpanda", "btcturk": "BtcTurk", "korbit": "Korbit", "coinone": "Coinone", "bitrue": "Bitrue",
    "bitrue-cex": "Bitrue", "coindcx": "CoinDCX", "revolut": "Revolut", "uphold": "Uphold", "bullish.com": "Bullish",
    "bullish": "Bullish", "bitso": "Bitso", "ascendex": "AscendEX", "btse": "BTSE", "woo": "WOO X", "phemex": "Phemex",
    "hyperliquid": "Hyperliquid", "paribu": "Paribu", "bitkub": "Bitkub", "indodax": "Indodax", "wazirx": "WazirX",
    "maicoin": "MaiCoin", "coinhako": "Coinhako", "bit2me": "Bit2Me", "changenow": "ChangeNOW", "nexo": "Nexo",
    "swissborg": "SwissBorg", "toobit": "Toobit", "weex": "WEEX", "bitunix": "Bitunix", "blofin": "BloFin",
}
MARKET_MAKERS = {
    "wintermute": "Wintermute", "flowdesk": "Flowdesk", "gsr": "GSR", "gsr markets": "GSR", "dwf labs": "DWF Labs",
    "amber group": "Amber", "amber": "Amber", "jump trading": "Jump", "cumberland": "Cumberland",
    "cumberland drw": "Cumberland", "falconx": "FalconX", "b2c2": "B2C2", "b2c2 group": "B2C2",
    "flow traders": "Flow Traders", "galaxy digital": "Galaxy", "auros": "Auros", "keyrock": "Keyrock",
    "selini": "Selini", "presto": "Presto", "qcp": "QCP", "efficient frontier": "Efficient Frontier",
}
CHAIN_IDS = {"1": "ETH", "56": "BSC", "8453": "BASE", "42161": "ARB", "10": "OP", "137": "POLYGON", "43114": "AVAXC"}
ARKHAM_CHAINS = {"Ethereum": "ETH", "BNB Chain": "BSC", "BSC": "BSC", "Base": "BASE", "Arbitrum": "ARB",
                 "Polygon": "POLYGON", "Optimism": "OP", "Avalanche": "AVAXC", "Solana": "SOL", "Tron": "TRX",
                 "Robinhood Chain": "ROBINHOOD", "Robinhood": "ROBINHOOD", "HyperEVM": "HYPEREVM", "Flare": "FLARE"}
SCAN_CHAINS = {"etherscan": "ETH", "bscscan": "BSC", "basescan": "BASE", "arbiscan": "ARB", "optimism": "OP",
               "polygonscan": "POLYGON", "avalanche": "AVAXC"}
DL_CHAINS = {"ethereum": "ETH", "bsc": "BSC", "base": "BASE", "arbitrum": "ARB", "optimism": "OP", "polygon": "POLYGON",
             "avax": "AVAXC", "tron": "TRX", "solana": "SOL", "era": "ZKSYNC", "linea": "LINEA", "scroll": "SCROLL"}
DL_ENTITIES = {"binance": "Binance", "okex": "OKX", "bitget": "Bitget", "kucoin": "KuCoin", "gate-io": "Gate",
               "huobi": "HTX", "bitfinex": "Bitfinex", "bitstamp": "Bitstamp", "backpack": "Backpack",
               "gemini": "Gemini", "bitrue-cex": "Bitrue"}


def entity_of(name: str) -> tuple[str, str] | None:
    """('Binance: Hot Wallet 20') → ('Binance', 'exchange'); market makers → 'mm'."""
    n = re.sub(r"\s+\d+$", "", re.split(r"[:(]", name)[0]).strip().lower()
    n = re.sub(r"\s+(hot|cold|deposit|wallet|exchange).*$", "", n)
    for table, kind in ((EXCHANGES, "exchange"), (MARKET_MAKERS, "mm")):
        if n in table:
            return table[n], kind
        first = n.split()[0] if n else ""
        if first in table and kind == "exchange":
            return table[first], kind
    return None


SKIP = ("exploit", "hack", "charity", "deployer", "commerce", "dex", "router", "aggregat", "swap", "opensea",
        "celsius", "staking", "depositor", "contract", "token wallet", "bridge", "pegged", "launchpool", "nft",
        "web3", "wallet:", "proxy", "fee ", "user")


def skip_tag(tag: str) -> bool:
    t = tag.lower()
    return any(k in t for k in SKIP) and "hot wallet" not in t and "cold wallet" not in t


def wtype(tag: str, default: str) -> str:
    t = tag.lower()
    if " dep:" in t or t.startswith("dep:"):
        return "deposit"
    if "deposit funder" in t or "withdraw" in t:
        return "hot"
    if re.fullmatch(r"[a-z.\- ]+ \d+", t):  # 'Binance 14' — Etherscan's numbered exchange wallets are hot wallets
        return "hot"
    for k, v in (("deposit", "deposit"), ("cold", "cold"), ("hot", "hot"), ("custody", "custody"),
                 ("market maker", "mm"), ("internal", "hot"), ("safe", "cold"), ("multisig", "cold")):
        if k in t:
            return v
    return default


def family(addr: str) -> str | None:
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        return "evm"
    if re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", addr):
        return "tron"
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr):
        return "solana"
    return None


class Book:
    def __init__(self):
        self.w: dict[str, dict] = {"evm": {}, "tron": {}, "solana": {}}
        self.src = Counter()

    def add(self, addr: str, entity: str, kind: str, typ: str, chain: str | None, source: str) -> None:
        fam = family(addr)
        if not fam:
            return
        key = addr.lower() if fam == "evm" else addr
        cur = self.w[fam].get(key)
        if cur is None:
            cur = self.w[fam][key] = {"e": entity, "k": kind, "t": typ, "c": [], "s": []}
            self.src[source] += 1
        elif cur["t"] in ("exchange", "reserve") and typ not in ("exchange", "reserve"):
            cur["t"] = typ  # a more specific type wins
        if chain and chain not in cur["c"]:
            cur["c"].append(chain)
        if source not in cur["s"]:
            cur["s"].append(source)


def from_arkham(book: Book, path: str) -> None:
    for addr, v in json.load(open(path, encoding="utf-8")).items():
        ent = entity_of(v["entity"])
        if not ent:
            continue
        for ch in v.get("chains") or [None]:
            book.add(addr, ent[0], ent[1], "mm" if ent[1] == "mm" else wtype(v["type"], "exchange"),
                     ARKHAM_CHAINS.get(ch, ch), "arkham-export")


def from_ethlabels(book: Book, path: str) -> None:
    for r in csv.DictReader(open(path, encoding="utf-8")):
        ch = CHAIN_IDS.get(r["chainId"])
        if not ch:
            continue
        if skip_tag(r["nameTag"]):
            continue
        ent = entity_of(r["nameTag"]) or (entity_of(r["label"]) if r["label"] != "exchange" else None)
        if not ent:
            continue
        # label-only rows (no name tag) come in thousands per exchange — those are deposit addresses
        typ = "mm" if ent[1] == "mm" else wtype(r["nameTag"], "exchange") if r["nameTag"] else "deposit"
        book.add(r["address"], ent[0], ent[1], typ, ch, "eth-labels")


def from_etherscan(book: Book, root: str) -> None:
    for f in glob.glob(os.path.join(root, "*", "accounts", "exchange.json")):
        ch = SCAN_CHAINS.get(f.split(os.sep)[-3])
        if not ch:
            continue
        for addr, tag in json.load(open(f, encoding="utf-8")).items():
            ent = None if skip_tag(tag) else entity_of(tag)
            if ent:
                book.add(addr, ent[0], ent[1], wtype(tag, "exchange"), ch, "etherscan-labels")


def from_defillama(book: Book, root: str) -> None:
    for folder, entity in DL_ENTITIES.items():
        f = os.path.join(root, folder, "index.js")
        if not os.path.exists(f):
            continue
        src = open(f, encoding="utf-8").read()
        for m in re.finditer(r"""["']?owners["']?\s*:\s*\[(.*?)\]""", src, re.S):
            head = src[: m.start()]
            keys = re.findall(r"""^\s*["']?([a-z_0-9]+)["']?\s*:\s*\{""", head, re.M)
            chain = DL_CHAINS.get(keys[-1]) if keys else None
            if not chain:
                continue
            for addr in re.findall(r"""["']([0-9A-Za-z]{25,66})["']""", m.group(1)):
                book.add(addr, entity, "exchange", "reserve", chain, "defillama-por")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arkham")
    ap.add_argument("--ethlabels")
    ap.add_argument("--etherscan")
    ap.add_argument("--defillama")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "bottom", "data", "exchange_wallets.json.gz"))
    a = ap.parse_args()
    book = Book()
    if a.arkham:
        from_arkham(book, a.arkham)
    if a.ethlabels:
        from_ethlabels(book, a.ethlabels)
    if a.etherscan:
        from_etherscan(book, a.etherscan)
    if a.defillama:
        from_defillama(book, a.defillama)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    out = {"_about": "Labeled exchange and market-maker wallets. e=entity, k=exchange|mm, "
                     "t=hot|cold|deposit|reserve|custody|mm|exchange, c=chains seen, s=sources",
           **book.w}
    for fam in book.w.values():  # deposits: entity and type are enough
        for v in fam.values():
            if v["t"] == "deposit":
                v.pop("s", None)
    with gzip.open(a.out, "wt", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    ents = Counter(v["e"] for fam in book.w.values() for v in fam.values())
    print(Counter(v["t"] for fam in book.w.values() for v in fam.values()))
    print({k: len(v) for k, v in book.w.items()}, dict(book.src))
    print(ents.most_common(40))


if __name__ == "__main__":
    main()
