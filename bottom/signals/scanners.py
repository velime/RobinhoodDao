"""The five scanners. Each one is self-sufficient: it fetches what it needs itself, keeps its own state in the
signals db, and can run alone. `scan()` returns alerts; the runner posts and records them."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..tools import derivatives as deriv_flow
from ..tools import feeds, oicard, onchain, spreads, transfers
from ..tools.exchanges import ExchangePool
from ..tools.ultra import classify
from . import formats as fmt
from .config import SignalsConfig
from .store import SignalStore

log = logging.getLogger(__name__)


@dataclass
class Alert:
    feed: str
    base: str | None
    ex: str | None
    text: str
    data: dict = field(default_factory=dict)
    priority: float = 0.0


class Scanner:
    feed = ""
    every_attr = ""

    def __init__(self, cfg: SignalsConfig, store: SignalStore):
        self.cfg, self.store = cfg, store

    @property
    def every(self) -> int:
        return getattr(self.cfg, self.every_attr)

    async def scan(self) -> list[Alert]:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self) -> None:
        pass


# --------------------------------------------------------------------------- OI


class OIScanner(Scanner):
    """OI growth over 4h / 15m on every exchange with OI data; Binance/Bybit cards get book, L/S, A/D."""

    feed, every_attr = "oi", "oi_every"
    BINANCE_MIN_VOL = 2_000_000

    async def _binance_oi(self) -> list[dict]:
        rows = [r for r in await feeds.binance_perp() if (r["vol24_usd"] or 0) >= self.BINANCE_MIN_VOL]
        sem = asyncio.Semaphore(10)

        async def one(r):
            async with sem:
                try:
                    d = await feeds.http.get_json(f"{feeds.BINANCE_F}/fapi/v1/openInterest", {"symbol": r["symbol"]})
                except Exception:  # noqa: BLE001
                    return None
                _, mult = feeds.base_of(r["symbol"]) or (None, 1)
                r["oi_usd"] = float(d["openInterest"]) * r["price"] * mult
                return r

        return [r for r in await asyncio.gather(*(one(r) for r in rows)) if r]

    async def rows(self) -> list[dict]:
        others = [k[0] for k in feeds.FETCHERS if k[1] == "perp" and k[0] != "binance"]
        if self.cfg.exchanges:
            others = [x for x in others if x in self.cfg.exchanges]
        snap, errors = await feeds.snapshot(others, ("perp",))
        if errors:
            log.info("oi feeds failed: %s", errors)
        try:
            snap += await self._binance_oi()
        except Exception as e:  # noqa: BLE001
            log.info("binance oi failed: %s", e)
        return [r for r in snap if r.get("oi_usd")]

    async def _enrich(self, c: dict) -> dict:
        nat = await oicard.native_symbols()
        key = (c["ex"], c["base"])
        if c["ex"] in ("binance", "bybit") and key in nat:
            sym, mult = nat[key]
            fn = oicard._binance if c["ex"] == "binance" else oicard._bybit
            try:
                full = await asyncio.wait_for(fn(sym, mult), 20)
                for k in ("long_pct", "short_pct", "book_delta_usd", "walls", "mark_side", "funding_pct"):
                    if full.get(k) is not None:
                        c[k] = full[k]
            except Exception as e:  # noqa: BLE001
                log.info("enrich %s failed: %s", key, e)
        try:
            flow = await asyncio.wait_for(deriv_flow.spot_perp_flow(c["base"]), 15)
            c["ad_spot_usd"] = ((flow.get("spot") or {}).get("4h") or {}).get("net_usd")
            c["ad_perp_usd"] = ((flow.get("perp") or {}).get("4h") or {}).get("net_usd")
        except Exception:  # noqa: BLE001
            pass
        caps = self.store.get("mcap", {})
        if not caps or time.time() - caps.get("_ts", 0) > 3600:
            caps = await market_caps()
            self.store.put("mcap", caps)
        if caps.get(c["base"]):
            c["oi_mcap_pct"] = round(c["oi_usd"] / caps[c["base"]] * 100, 2)
        return c

    async def scan(self) -> list[Alert]:
        now = int(time.time())
        rows = await self.rows()
        self.store.add_oi(now, [(r["ex"], r["base"], r["oi_usd"] / r["price"], r["oi_usd"], r["price"]) for r in rows])
        ago4 = self.store.oi_at(now - 4 * 3600, 20 * 60)
        ago15 = self.store.oi_at(now - 15 * 60, 4 * 60)
        cands = []
        for r in rows:
            k = (r["ex"], r["base"])
            if r["oi_usd"] < self.cfg.oi_min_usd:
                continue
            coins = r["oi_usd"] / r["price"]
            c = {"ex": r["ex"], "base": r["base"], "price": r["price"], "oi_usd": r["oi_usd"], "oi_coins": coins,
                 "funding_pct": r.get("funding_pct")}
            if k in ago4 and ago4[k][1]:
                c["oi_coins_from"] = ago4[k][1]
                c["oi_chg_pct"] = round((coins - ago4[k][1]) / ago4[k][1] * 100, 2)
                c["oi_added_usd"] = (coins - ago4[k][1]) * r["price"]
                c["price_chg_pct"] = round((r["price"] - ago4[k][2]) / ago4[k][2] * 100, 2) if ago4[k][2] else None
            if k in ago15 and ago15[k][1]:
                c["oi_fast_chg_pct"] = round((coins - ago15[k][1]) / ago15[k][1] * 100, 2)
            hit4 = (c.get("oi_chg_pct") or 0) >= self.cfg.oi_min_pct
            hit15 = (c.get("oi_fast_chg_pct") or 0) >= self.cfg.oi_fast_min_pct
            if not (hit4 or hit15):
                continue
            last = self.store.get(f"oi:{r['ex']}:{r['base']}")
            if last and now - last["ts"] < 20 * 60:
                continue
            if last and now - last["ts"] < 4 * 3600 and (c.get("oi_chg_pct") or 0) < last["chg"] + 5 and not hit15:
                continue
            c.setdefault("oi_chg_pct", c.get("oi_fast_chg_pct"))
            c.setdefault("oi_coins_from", coins)
            cands.append(c)
        cands.sort(key=lambda c: -max(c.get("oi_chg_pct") or 0, 2 * (c.get("oi_fast_chg_pct") or 0)))
        alerts = []
        for c in cands[: self.cfg.max_posts_per_cycle]:
            c = await self._enrich(c)
            reading = classify(c)
            n = self.store.count_alerts("oi", c["base"], 24) + 1
            self.store.put(f"oi:{c['ex']}:{c['base']}", {"ts": now, "chg": c.get("oi_chg_pct") or 0})
            data = {k: v for k, v in c.items() if k != "walls"} | {"walls": c.get("walls"), "signal_no": n, **reading}
            alerts.append(Alert("oi", c["base"], c["ex"], fmt.oi_alert(c["base"], c, n, reading), data,
                                c.get("oi_chg_pct") or 0))
        return alerts


async def market_caps() -> dict:
    """symbol → largest market cap among the top 1000 coins (CoinGecko, 4 requests)."""
    caps: dict = {"_ts": time.time()}
    for page in range(1, 5):
        try:
            rows = await feeds.http.get_json("https://api.coingecko.com/api/v3/coins/markets",
                                             {"vs_currency": "usd", "per_page": 250, "page": page})
        except Exception:  # noqa: BLE001
            break
        for c in rows:
            s, mc = (c.get("symbol") or "").upper(), c.get("market_cap") or 0
            if mc and mc > caps.get(s, 0):
                caps[s] = mc
    return caps


# ---------------------------------------------------------------------- funding


class FundingScanner(Scanner):
    """Extreme funding, sudden jumps, and the list of extreme rates ~10 min before settlement."""

    feed, every_attr = "funding", "funding_every"

    async def scan(self) -> list[Alert]:
        exs = [k[0] for k in feeds.FETCHERS if k[1] == "perp"]
        if self.cfg.exchanges:
            exs = [x for x in exs if x in self.cfg.exchanges]
        rows, errors = await feeds.snapshot(exs, ("perp",))
        if errors:
            log.info("funding feeds failed: %s", errors)
        by_base: dict[str, list] = {}
        for r in rows:
            if r.get("funding_pct") is not None and (r.get("vol24_usd") or 0) >= 50_000:
                by_base.setdefault(r["base"], []).append(r)
        prev_all = self.store.get("fr:last", {})
        now = time.time()
        alerts, settle = [], {}
        mn, jump = self.cfg.funding_min_pct, self.cfg.funding_jump_pct
        for base, rs in by_base.items():
            prev = {r["ex"]: prev_all.get(f"{r['ex']}:{base}") for r in rs if f"{r['ex']}:{base}" in prev_all}
            extreme = {r["ex"] for r in rs if abs(r["funding_pct"]) >= mn}
            jumped = {r["ex"] for r in rs if r["ex"] in prev and abs(r["funding_pct"] - prev[r["ex"]]) >= jump
                      and abs(r["funding_pct"]) >= mn / 2}
            top = max(abs(r["funding_pct"]) for r in rs)
            last = self.store.get(f"fr:alert:{base}")
            kind = None
            if jumped:
                kind, trig = "jump", jumped
            elif extreme and (not last or now - last["ts"] > 3600):
                kind, trig = ("update" if last and now - last["ts"] < 6 * 3600 else "extreme"), extreme
            elif extreme and last and abs(top - last["top"]) >= jump:
                kind, trig = "update", extreme
            if kind:
                self.store.put(f"fr:alert:{base}", {"ts": now, "top": top})
                alerts.append(Alert("funding", base, None, fmt.funding_alert(base, rs, trig, kind, prev),
                                    {"kind": kind, "rows": rs, "trigger": sorted(trig)}, top))
            for r in rs:
                nxt = r.get("next_funding_ms")
                if nxt and abs(r["funding_pct"]) >= mn and 8 * 60 <= nxt / 1000 - now <= 12 * 60:
                    settle.setdefault(nxt // 60000 * 60000, {}).setdefault(base, []).append(r)
        self.store.put("fr:last", {f"{r['ex']}:{r['base']}": r["funding_pct"] for r in rows
                                   if r.get("funding_pct") is not None})
        for ts, items in settle.items():
            key = f"fr:settle:{ts}"
            if self.store.get(key):
                continue
            self.store.put(key, True)
            lst = sorted(items.items(), key=lambda kv: -max(abs(r["funding_pct"]) for r in kv[1]))[:15]
            alerts.append(Alert("funding", None, None, fmt.settlement_alert(ts, lst),
                                {"kind": "settlement", "settle_ms": ts, "bases": [b for b, _ in lst]}, 100))
        alerts.sort(key=lambda a: -a.priority)
        return alerts[: self.cfg.max_posts_per_cycle]


# ---------------------------------------------------------------------- spreads


async def refresh_dex(store: SignalStore, trows: list[dict], bases: set[str], lookups: int) -> None:
    """Rotate through the contracts the exchanges list and keep the latest DEX price per (base, chain)."""
    todo: dict[str, list[tuple[str, str]]] = {}
    for r in trows:
        if r["base"] in bases and r["contract"] and r["chain"] in transfers.DEXSCREENER_CHAIN:
            todo.setdefault(r["chain"], [])
            if (r["base"], r["contract"]) not in todo[r["chain"]]:
                todo[r["chain"]].append((r["base"], r["contract"]))
    flat = [(ch, b, c) for ch, lst in sorted(todo.items()) for b, c in lst]
    if not flat:
        return
    cur = store.get("dex:cursor", 0) % len(flat)
    batch = (flat[cur:] + flat[:cur])[: lookups * 30]
    store.put("dex:cursor", cur + len(batch))
    by_chain: dict[str, list] = {}
    for ch, b, c in batch:
        by_chain.setdefault(ch, []).append((b, c))
    for ch, items in by_chain.items():
        for i in range(0, len(items), 30):
            part = items[i:i + 30]
            try:
                pools = await onchain.dex_prices(ch, [c for _, c in part])
            except Exception as e:  # noqa: BLE001
                log.info("dex prices %s failed: %s", ch, e)
                continue
            for b, c in part:
                p = pools.get(c.lower() if c.startswith("0x") else c)
                if p:
                    store.put(f"dexp:{b}:{ch}", p | {"contract": c, "canon_chain": ch, "ts": time.time()})


class SpreadScanner(Scanner):
    """Spot/DEX transfer spreads with route check (deposit/withdraw), lifetime and depth."""

    feed, every_attr = "spreads", "spreads_every"

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.pool = ExchangePool(tuple(cfg.exchanges) or ("binance", "bybit", "okx", "bitget", "gate", "mexc", "kucoin"))

    async def close(self) -> None:
        await self.pool.close()

    async def scan(self) -> list[Alert]:
        rows, errors = await feeds.snapshot(list(self.cfg.exchanges) or None, ("spot",))
        if errors:
            log.info("spot feeds failed: %s", errors)
        trows, _ = await transfers.all_statuses()
        bases = {r["base"] for r in rows if (r.get("vol24_usd") or 0) >= self.cfg.spread_min_vol}
        await refresh_dex(self.store, trows, bases, self.cfg.dex_lookups_per_cycle)
        dexp = self.store.get_prefix("dexp:")
        venues: dict[str, list] = {}
        for r in rows:
            venues.setdefault(r["base"], []).append(
                {"venue": f"{fmt.ex_name(r['ex'])} spot", "ex": r["ex"], "kind": "spot", "base": r["base"],
                 "price": r["price"], "vol24_usd": r.get("vol24_usd")})
        now = time.time()
        for k, p in dexp.items():
            _, base, ch = k.split(":", 2)
            if now - p.get("ts", 0) < 1800 and base in venues:
                venues[base].append({"venue": f"DEX {ch} {p.get('dex')}", "ex": "dex", "kind": "dex", "base": base,
                                     "chain": p.get("chain"), "canon_chain": ch, "pool": p.get("pair_address"),
                                     "contract": p.get("contract"), "price": p.get("price_usd"),
                                     "liquidity_usd": p.get("liquidity_usd"), "vol24_usd": p.get("volume_24h_usd"),
                                     "url": p.get("url")})
        ts = int(now)
        gap = int(self.every * 2.5)
        cands = []
        for base, vs in venues.items():
            best = spreads.pairs(vs, self.cfg.spread_min_vol).get("transfer")
            if not best or not (self.cfg.spread_min_pct <= best["gross_pct"] <= self.cfg.spread_max_pct):
                continue
            buy, sell = best["buy"], best["sell"]
            if buy["kind"] == "dex" and sell["kind"] == "dex":
                continue
            if buy["kind"] == "dex":
                route = spreads.dex_route(trows, base, buy, sell["ex"], "to_cex")
            elif sell["kind"] == "dex":
                route = spreads.dex_route(trows, base, sell, buy["ex"], "from_cex")
            else:
                route = transfers.route(trows, base, buy["ex"], sell["ex"])
            key = f"{base}|{buy['venue']}|{sell['venue']}"
            self.store.add_spread(key, ts, best["gross_pct"])
            if route["status"] == "closed" and not self.cfg.spread_show_closed:
                continue
            cands.append((base, best, route, key))
        self.store.db.commit()
        alerts = []
        for base, best, route, key in sorted(cands, key=lambda c: -c[1]["gross_pct"]):
            start, top = self.store.spread_since(key, ts, gap)
            last = self.store.get(f"sp:alert:{key}")
            if last and now - last["ts"] < 3600 and best["gross_pct"] < last["pct"] * 1.3:
                continue
            buy, sell = best["buy"], best["sell"]
            s = {"buy": buy["venue"], "sell": sell["venue"], "buy_price": buy["price"], "sell_price": sell["price"],
                 "gross_pct": best["gross_pct"], "route": route, "vols": [buy.get("vol24_usd"), sell.get("vol24_usd")],
                 "held_min": round((ts - start) / 60) if start else 0, "max_pct": top,
                 "dex_url": (buy if buy["kind"] == "dex" else sell).get("url") if "dex" in (buy["kind"], sell["kind"]) else None}
            if buy["kind"] != "dex" and sell["kind"] != "dex" and len(alerts) < 5:
                try:
                    (asks, _), (_, bids) = await asyncio.gather(spreads._book(self.pool, buy), spreads._book(self.pool, sell))
                    s["capacity"] = spreads.capacity(asks, bids, spreads.TAKER_FEE_PCT)
                    if s["capacity"]["fits_usd"] < 200:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            elif "dex" in (buy["kind"], sell["kind"]):
                dex = buy if buy["kind"] == "dex" else sell
                s["capacity"] = {"fits_usd": round((dex.get("liquidity_usd") or 0) * 0.005)}
            self.store.put(f"sp:alert:{key}", {"ts": now, "pct": best["gross_pct"]})
            alerts.append(Alert("spreads", base, f"{buy['ex']}>{sell['ex']}", fmt.spread_alert(base, s), s,
                                best["gross_pct"]))
            if len(alerts) >= self.cfg.max_posts_per_cycle:
                break
        return alerts


# -------------------------------------------------------------------- transfers


class TransferScanner(Scanner):
    """Deposits/withdrawals opened or closed on any network (first run only records the baseline)."""

    feed, every_attr = "transfers", "transfers_every"

    async def scan(self) -> list[Alert]:
        rows, errors = await transfers.all_statuses(force=True)
        if not rows:
            log.info("transfer lists unavailable: %s", errors)
            return []
        old = self.store.transfer_rows()
        first = not old
        self.store.save_transfers(rows)
        if first:
            log.info("transfers baseline saved: %d rows", len(rows))
            return []
        perps = self.store.get("perp_bases")
        if not perps or time.time() - perps.get("_ts", 0) > 6 * 3600:
            prow, _ = await feeds.snapshot(None, ("perp",))
            perps = {"_ts": time.time(), "bases": sorted({r["base"] for r in prow})}
            self.store.put("perp_bases", perps)
        watch = set(perps.get("bases") or [])
        changes: dict[tuple[str, str], list] = {}
        for r in rows:
            k = (r["ex"], r["base"], r["chain"])
            if k not in old or (watch and r["base"] not in watch):
                continue
            od, ow = old[k]
            for what, now_v, old_v in (("deposit", r["deposit"], od), ("withdraw", r["withdraw"], ow)):
                if now_v is None or old_v is None or bool(now_v) == bool(old_v):
                    continue
                changes.setdefault((r["ex"], r["base"]), []).append((r["chain"], what, bool(now_v)))
        alerts = [Alert("transfers", base, ex, fmt.transfer_alert(ex, base, ch),
                        {"changes": [{"chain": c, "what": w, "opened": o} for c, w, o in ch]},
                        sum(1 for *_, o in ch if o))
                  for (ex, base), ch in changes.items()]
        alerts.sort(key=lambda a: -a.priority)
        return alerts[: self.cfg.max_posts_per_cycle * 2]


# -------------------------------------------------------------------------- DEX


class DexScanner(Scanner):
    """Wallets dumping into the main pool of coins that trade on CEX (rotating through pools)."""

    feed, every_attr = "dex", "dex_every"

    async def scan(self) -> list[Alert]:
        pools = {k: p for k, p in self.store.get_prefix("dexp:").items() if p.get("pair_address")}
        if not pools:  # the spread scanner is not running — find pools ourselves
            trows, _ = await transfers.all_statuses()
            prow, _ = await feeds.snapshot(None, ("perp",))
            bases = {r["base"] for r in prow if (r.get("vol24_usd") or 0) >= 1_000_000}
            await refresh_dex(self.store, trows, bases, self.cfg.dex_lookups_per_cycle)
            pools = {k: p for k, p in self.store.get_prefix("dexp:").items() if p.get("pair_address")}
        keys = sorted(pools)
        if not keys:
            return []
        cur = self.store.get("dexw:cursor", 0) % len(keys)
        batch = (keys[cur:] + keys[:cur])[: self.cfg.dex_pools_per_cycle]
        self.store.put("dexw:cursor", cur + len(batch))
        alerts = []
        for k in batch:
            p = pools[k]
            base = k.split(":")[1]
            try:
                trades = await onchain.pool_trades(p["chain"], p["pair_address"], 1000)
            except Exception as e:  # noqa: BLE001
                log.info("trades %s failed: %s", k, e)
                continue
            await asyncio.sleep(2.1)  # GeckoTerminal free limit ~30 requests a minute
            for s in onchain.wallet_sells(trades, p.get("liquidity_usd") or 0, window_min=30):
                if s["net_sold_usd"] < self.cfg.dex_min_usd or (s["pct_of_liquidity"] or 0) < self.cfg.dex_min_liq_pct:
                    continue
                key = f"dexw:{p['pair_address']}:{s['wallet']}"
                last = self.store.get(key)
                if last and time.time() - last["ts"] < 3600 and s["net_sold_usd"] < 2 * last["usd"]:
                    continue
                self.store.put(key, {"ts": time.time(), "usd": s["net_sold_usd"]})
                alerts.append(Alert("dex", base, "dex", fmt.dex_alert(base, p, s), {"pool": p, **s},
                                    s["pct_of_liquidity"] or 0))
        alerts.sort(key=lambda a: -a.priority)
        return alerts[: self.cfg.max_posts_per_cycle]


SCANNERS = {"oi": OIScanner, "funding": FundingScanner, "spreads": SpreadScanner,
            "transfers": TransferScanner, "dex": DexScanner}
