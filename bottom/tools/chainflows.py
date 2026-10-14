"""On-chain deposits to and withdrawals from exchanges, by token, with our own wallet book.

Wallet book: bottom/data/exchange_wallets.json.gz (exchange hot/cold/reserve/deposit wallets and market makers,
built by scripts/build_wallets.py from public label sets and proof-of-reserves lists) + Binance's live
proof-of-reserves list + deposit addresses learned while running (an unknown address that sweeps a token
into an exchange hot wallet is that exchange's deposit address).

EVM chains: one eth_getLogs per block range for the Transfer events of all watched token contracts at once,
over public RPCs (override with RPC_ETH=..., RPC_BSC=... in .env). Tron: TronGrid per exchange wallet.
Solana: Helius per exchange wallet when HELIUS_API_KEY is set.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time

from . import http

log = logging.getLogger(__name__)

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "exchange_wallets.json.gz")
EVM_RPC = {
    "ETH": ["https://ethereum-rpc.publicnode.com", "https://eth.llamarpc.com"],
    "BSC": ["https://bsc-rpc.publicnode.com", "https://bsc.drpc.org"],
    "BASE": ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
    "ARB": ["https://arbitrum-one-rpc.publicnode.com", "https://arb1.arbitrum.io/rpc"],
    "POLYGON": ["https://polygon-bor-rpc.publicnode.com", "https://polygon-rpc.com"],
    "OP": ["https://optimism-rpc.publicnode.com", "https://mainnet.optimism.io"],
    "AVAXC": ["https://avalanche-c-chain-rpc.publicnode.com", "https://api.avax.network/ext/bc/C/rpc"],
}
# blocks per eth_getLogs call (≈1–2 minutes of chain time)
MAX_BLOCKS = {"ETH": 10, "BSC": 40, "BASE": 60, "ARB": 400, "POLYGON": 60, "OP": 60, "AVAXC": 60}
NATIVE = {"ETH": "ETH", "BSC": "BNB", "BASE": "ETH", "ARB": "ETH", "POLYGON": "POL", "OP": "ETH", "AVAXC": "AVAX",
          "TRX": "TRX", "SOL": "SOL"}
CHAIN_NAMES = {"ETH": "Ethereum", "BSC": "BNB Chain", "BASE": "Base", "ARB": "Arbitrum", "POLYGON": "Polygon",
               "OP": "Optimism", "AVAXC": "Avalanche", "TRX": "Tron", "SOL": "Solana"}
EXPLORER = {"ETH": "https://etherscan.io", "BSC": "https://bscscan.com", "BASE": "https://basescan.org",
            "ARB": "https://arbiscan.io", "POLYGON": "https://polygonscan.com", "OP": "https://optimistic.etherscan.io",
            "AVAXC": "https://snowtrace.io", "TRX": "https://tronscan.org/#", "SOL": "https://solscan.io"}
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDD", "PYUSD", "BUSD", "USDS", "USD1", "RLUSD", "FRAX",
           "LUSD", "GUSD", "USDP", "EURC", "EURT", "WETH", "WBTC", "WBNB", "STETH", "WSTETH", "CBBTC", "BTCB"}
OWN = ("hot", "cold", "reserve", "exchange", "custody")
BINANCE_POR = "https://www.binance.com/bapi/apex/v1/public/apex/market/por/address"
BINANCE_NET = {"ETH": "ETH", "BSC": "BSC", "BEP20": "BSC", "ARBITRUM": "ARB", "ARB": "ARB", "BASE": "BASE",
               "MATIC": "POLYGON", "POLYGON": "POLYGON", "OPTIMISM": "OP", "OP": "OP", "AVAXC": "AVAXC",
               "TRX": "TRX", "TRON": "TRX", "SOL": "SOL"}


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def family(chain: str) -> str:
    return "tron" if chain == "TRX" else "solana" if chain == "SOL" else "evm"


def addr_url(chain: str, addr: str) -> str:
    base = EXPLORER.get(chain, "")
    return f"{base}/account/{addr}" if chain == "SOL" else f"{base}/address/{addr}"


def tx_url(chain: str, tx: str) -> str:
    return f"{EXPLORER.get(chain, '')}/{'tx' if chain != 'TRX' else 'transaction'}/{tx}"


class WalletBook:
    def __init__(self, store=None, path: str = BOOK_PATH):
        self.store = store
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        self.w: dict[str, dict] = {f: data.get(f, {}) for f in ("evm", "tron", "solana")}
        if store is not None:
            for fam, addr, ent, typ in store.learned_wallets():
                self.w[fam].setdefault(addr, {"e": ent, "k": "exchange", "t": typ, "learned": True})

    @staticmethod
    def key(fam: str, addr: str) -> str:
        return addr.lower() if fam == "evm" else addr

    def label(self, fam: str, addr: str) -> dict | None:
        return self.w[fam].get(self.key(fam, addr))

    def learn(self, fam: str, addr: str, entity: str, typ: str = "deposit") -> None:
        k = self.key(fam, addr)
        if k in self.w[fam]:
            return
        self.w[fam][k] = {"e": entity, "k": "exchange", "t": typ, "learned": True}
        if self.store is not None:
            self.store.add_learned_wallet(fam, k, entity, typ)

    def owned(self, fam: str, entity: str, types=OWN) -> list[str]:
        return [a for a, v in self.w[fam].items() if v["e"] == entity and v["t"] in types]

    def all_owned(self, fam: str, types=OWN) -> list[tuple[str, str]]:
        return [(a, v["e"]) for a, v in self.w[fam].items() if v["t"] in types and v.get("k") == "exchange"]

    async def add_binance_por(self) -> int:
        """Binance's own proof-of-reserves address list (public, refreshed by the caller daily)."""
        data = await http.get_json(BINANCE_POR)
        n = 0
        for r in data.get("data") or []:
            ch = BINANCE_NET.get(str(r.get("network", "")).upper())
            addr = r.get("address")
            if not ch or not addr:
                continue
            fam = family(ch)
            k = self.key(fam, addr)
            if k not in self.w[fam]:
                self.w[fam][k] = {"e": "Binance", "k": "exchange", "t": "reserve"}
                n += 1
        return n

    def describe(self, fam: str, addr: str) -> str:
        v = self.label(fam, addr)
        if not v:
            return short(addr)
        t = {"hot": "Hot Wallet", "cold": "Cold Wallet", "reserve": "Hot Wallet", "deposit": "Deposit",
             "custody": "Custody", "mm": "Market Maker", "exchange": "Wallet"}.get(v["t"], v["t"])
        return f"{v['e']} Deposit ({addr[:5]})" if v["t"] == "deposit" else f"{v['e']} · {t} ({addr[:5]})"


def classify(book: WalletBook, fam: str, frm: str, to: str) -> dict | None:
    """What a transfer means for exchanges. Learns deposit addresses from sweeps into hot wallets."""
    f, t = book.label(fam, frm), book.label(fam, to)
    fe = f["e"] if f and f.get("k") == "exchange" else None
    te = t["e"] if t and t.get("k") == "exchange" else None
    if fe and te and fe == te:
        return None  # internal (sweep, hot↔cold)
    if t and te and t["t"] in OWN and not f:
        book.learn(fam, frm, te)  # unknown → exchange hot wallet = a deposit address being swept
        return {"kind": "sweep", "exchange": te}
    if te and (not fe or f["t"] in OWN):
        if fe:
            return {"kind": "deposit", "exchange": te, "from_exchange": fe}
        return {"kind": "deposit", "exchange": te}
    if fe and f["t"] in OWN:
        return {"kind": "withdrawal", "exchange": fe, "to_mm": t["e"] if t and t.get("k") == "mm" else None}
    return None


class Rpc:
    def __init__(self):
        self.urls = {c: [u.strip() for u in os.getenv(f"RPC_{c}", "").split(",") if u.strip()] or urls
                     for c, urls in EVM_RPC.items()}
        self._i = {c: 0 for c in self.urls}
        self._id = 0

    async def call(self, chain: str, method: str, params: list):
        last = None
        for _ in range(len(self.urls[chain])):
            url = self.urls[chain][self._i[chain] % len(self.urls[chain])]
            self._id += 1
            try:
                r = await http.post_json(url, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
                if "error" in r:
                    raise RuntimeError(str(r["error"])[:200])
                return r["result"]
            except Exception as e:  # noqa: BLE001
                last = e
                self._i[chain] += 1
        raise last or RuntimeError("rpc failed")

    async def block(self, chain: str) -> int:
        return int(await self.call(chain, "eth_blockNumber", []), 16)

    async def logs(self, chain: str, contracts: list[str], frm: int, to: int) -> list[dict]:
        out = []
        for i in range(0, len(contracts), 100):
            out += await self.call(chain, "eth_getLogs", [{
                "fromBlock": hex(frm), "toBlock": hex(to), "address": contracts[i:i + 100],
                "topics": [TRANSFER_TOPIC]}])
        return out

    async def decimals(self, chain: str, token: str) -> int:
        return int(await self.call(chain, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)

    async def balance_of(self, chain: str, token: str, addr: str) -> int:
        data = "0x70a08231" + addr.lower().replace("0x", "").rjust(64, "0")
        r = await self.call(chain, "eth_call", [{"to": token, "data": data}, "latest"])
        return int(r, 16) if r and r != "0x" else 0

    async def native_balance(self, chain: str, addr: str) -> int:
        return int(await self.call(chain, "eth_getBalance", [addr, "latest"]), 16)


def parse_log(lg: dict) -> dict | None:
    t = lg.get("topics") or []
    if len(t) != 3 or t[0].lower() != TRANSFER_TOPIC:
        return None  # ERC-721 has 4 topics
    try:
        amount = int(lg.get("data") or "0x0", 16)
    except ValueError:
        return None
    return {"token": lg["address"].lower(), "from": "0x" + t[1][-40:], "to": "0x" + t[2][-40:], "raw": amount,
            "tx": lg.get("transactionHash"), "block": int(lg.get("blockNumber", "0x0"), 16)}


async def tron_transfers(addr: str, since_ms: int, key: str = "") -> list[dict]:
    headers = {"TRON-PRO-API-KEY": key} if key else None
    data = await http.get_json(f"https://api.trongrid.io/v1/accounts/{addr}/transactions/trc20",
                               {"limit": 100, "min_timestamp": since_ms, "order_by": "block_timestamp,asc"}, headers)
    out = []
    for x in data.get("data") or []:
        ti = x.get("token_info") or {}
        try:
            amt = int(x.get("value") or 0) / 10 ** int(ti.get("decimals") or 0)
        except (TypeError, ValueError):
            continue
        out.append({"token": ti.get("address"), "symbol": (ti.get("symbol") or "").upper(), "from": x.get("from"),
                    "to": x.get("to"), "amount": amt, "tx": x.get("transaction_id"), "ts": x.get("block_timestamp")})
    return out


async def solana_transfers(addr: str, key: str) -> list[dict]:
    data = await http.get_json(f"https://api.helius.xyz/v0/addresses/{addr}/transactions",
                               {"api-key": key, "limit": 50})
    out = []
    for tx in data or []:
        for t in tx.get("tokenTransfers") or []:
            out.append({"token": t.get("mint"), "from": t.get("fromUserAccount"), "to": t.get("toUserAccount"),
                        "amount": float(t.get("tokenAmount") or 0), "tx": tx.get("signature"),
                        "ts": (tx.get("timestamp") or 0) * 1000})
    return out


def flows_summary(store, base: str, days: float = 30) -> dict:
    """Tool: deposits/withdrawals of a coin by exchange, net, biggest wallets, recent large moves."""
    rows = store.flows(base=base.upper(), days=days)
    if not rows:
        return {"base": base.upper(), "days": days, "note": "крупных переводов на биржи и с бирж за период не видел "
                "(или сканер flows не запущен)"}
    by_ex: dict[str, dict] = {}
    senders: dict[str, float] = {}
    receivers: dict[str, float] = {}
    for r in rows:
        e = by_ex.setdefault(r["exchange"], {"deposits_usd": 0.0, "withdrawals_usd": 0.0, "n": 0})
        e["n"] += 1
        if r["kind"] == "deposit":
            e["deposits_usd"] += r["usd"]
            senders[r["frm"]] = senders.get(r["frm"], 0) + r["usd"]
        else:
            e["withdrawals_usd"] += r["usd"]
            receivers[r["to"]] = receivers.get(r["to"], 0) + r["usd"]
    dep = sum(e["deposits_usd"] for e in by_ex.values())
    wd = sum(e["withdrawals_usd"] for e in by_ex.values())
    return {
        "base": base.upper(), "days": days,
        "deposits_usd": round(dep), "withdrawals_usd": round(wd), "net_to_exchanges_usd": round(dep - wd),
        "read": "заводят на биржи больше, чем выводят — потенциальное давление продаж" if dep > wd * 1.5 else
                "выводят с бирж больше, чем заводят — накопление/уход с бирж" if wd > dep * 1.5 else "баланс",
        "by_exchange": {k: {kk: round(vv) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_ex.items()},
        "top_depositors": [{"wallet": a, "usd": round(u)} for a, u in sorted(senders.items(), key=lambda x: -x[1])[:5]],
        "top_receivers": [{"wallet": a, "usd": round(u)} for a, u in sorted(receivers.items(), key=lambda x: -x[1])[:5]],
        "largest": [{k: r[k] for k in ("kind", "exchange", "chain", "usd", "frm", "to", "tx")} |
                    {"hours_ago": round((time.time() - r["ts"]) / 3600, 1)}
                    for r in sorted(rows, key=lambda r: -r["usd"])[:8]],
    }
