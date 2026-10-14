"""Deposit / withdrawal status of a coin on every network, per exchange.

Public bulk lists (all coins in one request, no key): Binance, Gate, KuCoin, Bitget.
Exchanges that only expose it with an API key (Bybit, OKX, MEXC, BingX) are read via ccxt
when <EXCHANGE>_API_KEY / <EXCHANGE>_API_SECRET are set (read-only keys are enough).

Row: {"ex", "base", "chain" (canonical), "chain_raw", "deposit": bool|None, "withdraw": bool|None,
      "fee": float|None, "contract": str|None}
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from . import http

log = logging.getLogger(__name__)

CHAIN_ALIASES = {
    "ETH": "ETH", "ERC20": "ETH", "ETHEREUM": "ETH", "ETH-ERC20": "ETH",
    "BSC": "BSC", "BEP20": "BSC", "BEP20(BSC)": "BSC", "BNB SMART CHAIN": "BSC", "BNBSMARTCHAIN": "BSC", "BSC_BNB": "BSC",
    "SOL": "SOL", "SOLANA": "SOL", "SPL": "SOL",
    "TRX": "TRX", "TRC20": "TRX", "TRON": "TRX",
    "ARB": "ARB", "ARBITRUM": "ARB", "ARBITRUM ONE": "ARB", "ARBONE": "ARB", "ARBEVM": "ARB", "ARBITRUMONE": "ARB",
    "BASE": "BASE", "BASEEVM": "BASE", "BASE MAINNET": "BASE",
    "MATIC": "POLYGON", "POLYGON": "POLYGON", "POL": "POLYGON", "MATICEVM": "POLYGON", "POLYGON POS": "POLYGON",
    "AVAXC": "AVAXC", "AVAX_C": "AVAXC", "AVAXC-CHAIN": "AVAXC", "C-CHAIN": "AVAXC", "AVAX C-CHAIN": "AVAXC",
    "OP": "OP", "OPTIMISM": "OP", "OPETH": "OP", "OPTIMISM_ETH": "OP",
    "SUI": "SUI", "TON": "TON", "APT": "APT", "APTOS": "APT",
}
# canonical chain → DexScreener chainId / GeckoTerminal network id
DEXSCREENER_CHAIN = {"ETH": "ethereum", "BSC": "bsc", "SOL": "solana", "BASE": "base", "ARB": "arbitrum",
                     "POLYGON": "polygon", "AVAXC": "avalanche", "OP": "optimism", "TRX": "tron", "SUI": "sui",
                     "TON": "ton", "APT": "aptos"}
GECKO_NETWORK = {"ethereum": "eth", "bsc": "bsc", "solana": "solana", "base": "base", "arbitrum": "arbitrum",
                 "polygon": "polygon_pos", "avalanche": "avax", "optimism": "optimism", "tron": "tron",
                 "sui": "sui-network", "ton": "ton", "aptos": "aptos"}
CHAIN_FROM_DEXSCREENER = {v: k for k, v in DEXSCREENER_CHAIN.items()}
# rough time for a transfer to be credited (network + exchange confirmations), minutes
TRANSFER_MINUTES = {"SOL": 2, "TRX": 3, "BSC": 3, "BASE": 5, "ARB": 5, "OP": 5, "POLYGON": 10, "AVAXC": 3,
                    "SUI": 2, "TON": 2, "APT": 2, "ETH": 8}


def chain_id(raw: str | None) -> str:
    s = (raw or "").strip().upper()
    return CHAIN_ALIASES.get(s, CHAIN_ALIASES.get(s.replace(" ", ""), s))


def _b(x) -> bool | None:
    if isinstance(x, bool):
        return x
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return bool(x)
    return str(x).strip().lower() in ("true", "1", "yes", "allowed", "enable", "enabled")


def _fee(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _row(ex, base, chain_raw, deposit, withdraw, fee=None, contract=None) -> dict:
    return {"ex": ex, "base": base.upper(), "chain": chain_id(chain_raw), "chain_raw": chain_raw,
            "deposit": deposit, "withdraw": withdraw, "fee": _fee(fee), "contract": (contract or None) or None}


async def binance() -> list[dict]:
    data = await http.get_json("https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll")
    out = []
    for c in data.get("data") or []:
        for n in c.get("networkList") or []:
            out.append(_row("binance", c["coin"], n.get("network"), _b(n.get("depositEnable")),
                            _b(n.get("withdrawEnable")), n.get("withdrawFee"), n.get("contractAddress")))
    return out


async def gate() -> list[dict]:
    data = await http.get_json("https://api.gateio.ws/api/v4/spot/currencies")
    out = []
    for c in data:
        if c.get("delisted"):
            continue
        base = c["currency"].split("_")[0]
        for n in c.get("chains") or []:
            out.append(_row("gate", base, n.get("name"), not n.get("deposit_disabled"),
                            not n.get("withdraw_disabled"), None, n.get("addr")))
    return out


async def kucoin() -> list[dict]:
    data = await http.get_json("https://api.kucoin.com/api/v3/currencies")
    out = []
    for c in data.get("data") or []:
        for n in c.get("chains") or []:
            out.append(_row("kucoin", c["currency"], n.get("chainName") or n.get("chainId"),
                            _b(n.get("isDepositEnabled")), _b(n.get("isWithdrawEnabled")),
                            n.get("withdrawalMinFee"), n.get("contractAddress")))
    return out


async def bitget() -> list[dict]:
    data = await http.get_json("https://api.bitget.com/api/v2/spot/public/coins")
    out = []
    for c in data.get("data") or []:
        for n in c.get("chains") or []:
            out.append(_row("bitget", c["coin"], n.get("chain"), _b(n.get("rechargeable")),
                            _b(n.get("withdrawable")), n.get("withdrawFee"), n.get("contractAddress")))
    return out


async def _ccxt_keyed(ex_id: str) -> list[dict]:
    key, secret = os.getenv(f"{ex_id.upper()}_API_KEY"), os.getenv(f"{ex_id.upper()}_API_SECRET")
    if not key or not secret:
        return []
    import ccxt.async_support as ccxt

    cfg = {"apiKey": key, "secret": secret, "enableRateLimit": True}
    if pw := os.getenv(f"{ex_id.upper()}_API_PASSWORD"):
        cfg["password"] = pw
    ex = getattr(ccxt, ex_id)(cfg)
    try:
        cur = await ex.fetch_currencies()
    finally:
        await ex.close()
    out = []
    for code, c in (cur or {}).items():
        for net_code, n in (c.get("networks") or {}).items():
            info = n.get("info") or {}
            out.append(_row(ex_id, code, n.get("network") or net_code, n.get("deposit"), n.get("withdraw"), n.get("fee"),
                            info.get("contractAddress") or info.get("ctAddr") or info.get("contract")))
    return out


PUBLIC = {"binance": binance, "gate": gate, "kucoin": kucoin, "bitget": bitget}
KEYED = ("bybit", "okx", "mexc", "bingx")

_cache: dict = {"ts": 0.0, "rows": [], "errors": {}}
CACHE_SEC = 600


async def all_statuses(force: bool = False) -> tuple[list[dict], dict]:
    """Every exchange's list (cached 10 min). Returns (rows, {exchange: error/"нужен API-ключ"})."""
    if not force and time.time() - _cache["ts"] < CACHE_SEC and _cache["rows"]:
        return _cache["rows"], _cache["errors"]
    names = list(PUBLIC) + list(KEYED)
    jobs = [PUBLIC[n]() for n in PUBLIC] + [_ccxt_keyed(n) for n in KEYED]
    res = await asyncio.gather(*(asyncio.wait_for(j, 30) for j in jobs), return_exceptions=True)
    rows, errors = [], {}
    for n, r in zip(names, res):
        if isinstance(r, Exception):
            errors[n] = f"{type(r).__name__}: {str(r)[:100]}"
        elif not r and n in KEYED:
            errors[n] = "статусы только с API-ключом (read-only) в .env"
        else:
            rows.extend(r)
    if rows:
        _cache.update(ts=time.time(), rows=rows, errors=errors)
    return rows, errors


def same_contract(a: str | None, b: str | None) -> bool | None:
    if not a or not b:
        return None
    a, b = a.strip(), b.strip()
    return a.lower() == b.lower() if a.startswith("0x") or b.startswith("0x") else a == b


async def transfer_status(base: str) -> dict:
    """Tool: per exchange and network — deposit/withdraw open, fee, contract; contract conflicts."""
    rows, errors = await all_statuses()
    mine = [r for r in rows if r["base"] == base.upper()]
    if not mine:
        return {"base": base.upper(), "error": "монета не найдена в списках депозитов/выводов",
                "exchanges_without_data": errors}
    by_ex: dict[str, list] = {}
    for r in mine:
        by_ex.setdefault(r["ex"], []).append(
            {"chain": r["chain"], "deposit": r["deposit"], "withdraw": r["withdraw"], "fee": r["fee"],
             "contract": r["contract"]})
    contracts: dict[str, set] = {}
    for r in mine:
        if r["contract"]:
            contracts.setdefault(r["chain"], set()).add(r["contract"].lower() if r["contract"].startswith("0x") else r["contract"])
    conflicts = {c: sorted(v) for c, v in contracts.items() if len(v) > 1}
    closed = [f"{r['ex']} {r['chain']}: " + ", ".join(
        x for x, ok in (("депозит закрыт", r["deposit"]), ("вывод закрыт", r["withdraw"])) if ok is False)
        for r in mine if r["deposit"] is False or r["withdraw"] is False]
    return {
        "base": base.upper(),
        "by_exchange": by_ex,
        "closed": closed,
        "contract_conflicts": conflicts,
        "note": "contract_conflicts — на одной сети разные контракты: это может быть другой токен с тем же тикером"
                if conflicts else None,
        "exchanges_without_data": errors,
    }


def route(rows: list[dict], base: str, buy_ex: str, sell_ex: str) -> dict:
    """Can the coin bought on buy_ex be withdrawn and deposited to sell_ex? Common open networks."""
    b = {r["chain"]: r for r in rows if r["base"] == base and r["ex"] == buy_ex}
    s = {r["chain"]: r for r in rows if r["base"] == base and r["ex"] == sell_ex}
    if not b or not s:
        return {"status": "unknown", "why": f"нет данных о сетях для {buy_ex if not b else sell_ex}"}
    common = sorted(set(b) & set(s))
    if not common:
        return {"status": "closed", "why": "нет общей сети между биржами"}
    open_ = [c for c in common if b[c]["withdraw"] and s[c]["deposit"]]
    mismatch = [c for c in common if same_contract(b[c]["contract"], s[c]["contract"]) is False]
    if open_:
        best = min(open_, key=lambda c: TRANSFER_MINUTES.get(c, 15))
        return {"status": "open", "networks": open_, "fastest": best,
                "transfer_minutes": TRANSFER_MINUTES.get(best, 15), "withdraw_fee": b[best]["fee"],
                "contract_mismatch": mismatch or None}
    why = []
    for c in common:
        if not b[c]["withdraw"]:
            why.append(f"{buy_ex} {c}: вывод закрыт")
        if not s[c]["deposit"]:
            why.append(f"{sell_ex} {c}: депозит закрыт")
    return {"status": "closed", "why": "; ".join(why), "contract_mismatch": mismatch or None}
