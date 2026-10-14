"""The five scanners. Each one is self-sufficient: it fetches what it needs itself, keeps its own state in the
signals db, and can run alone. `scan()` returns alerts; the runner posts and records them."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from ..tools import derivatives as deriv_flow
from ..tools import chainflows as cf
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
        st = await ensure_stats(self.store)
        if st.get(c["base"]):
            c["oi_mcap_pct"] = round(c["oi_usd"] / st[c["base"]][0] * 100, 2)
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


async def coin_stats() -> dict:
    """symbol → [market cap, 24h volume] of the largest coin with that symbol in the top 1000 (CoinGecko)."""
    out: dict = {"_ts": time.time()}
    for page in range(1, 5):
        try:
            rows = await feeds.http.get_json("https://api.coingecko.com/api/v3/coins/markets",
                                             {"vs_currency": "usd", "per_page": 250, "page": page})
        except Exception:  # noqa: BLE001
            break
        for c in rows:
            s, mc = (c.get("symbol") or "").upper(), c.get("market_cap") or 0
            if mc and mc > (out.get(s) or [0])[0]:
                out[s] = [mc, c.get("total_volume") or 0]
    return out


def cached_stats(store: SignalStore) -> dict:
    return store.get("cgstats", {}) or {}


async def ensure_stats(store: SignalStore) -> dict:
    st = cached_stats(store)
    if not st or time.time() - st.get("_ts", 0) > 3600:
        fresh = await coin_stats()
        if len(fresh) > 1:
            store.put("cgstats", fresh)
            st = fresh
    return st


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
    """perp↔perp across all exchanges, spot↔perp (CEX spot and DEX of the real contract), index checks."""

    feed, every_attr = "spreads", "spreads_every"

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.pool = ExchangePool(tuple(cfg.exchanges) or ("binance", "bybit", "okx", "bitget", "gate", "mexc", "kucoin"))

    async def close(self) -> None:
        await self.pool.close()

    def _dex(self, base: str, now: float) -> list[dict]:
        out = []
        for k, p in self.store.get_prefix(f"dexp:{base}:").items():
            ch = k.split(":", 2)[2]
            if now - p.get("ts", 0) < 1800 and p.get("price_usd"):
                out.append({"venue": f"DEX {ch} {p.get('dex')}", "ex": "dex", "kind": "dex", "base": base,
                            "chain": p.get("chain"), "canon_chain": ch, "pool": p.get("pair_address"),
                            "contract": p.get("contract"), "price": p["price_usd"],
                            "liquidity_usd": p.get("liquidity_usd"), "vol24_usd": p.get("volume_24h_usd"),
                            "url": p.get("url")})
        return out

    async def scan(self) -> list[Alert]:
        rows, errors = await feeds.snapshot(list(self.cfg.exchanges) or None, ("perp", "spot"))
        if errors:
            log.info("feeds failed: %s", errors)
        cfg = self.cfg
        perp_bases = {r["base"] for r in rows if r["kind"] == "perp" and (r.get("vol24_usd") or 0) >= cfg.spread_min_vol}
        trows, _ = await transfers.all_statuses()
        await refresh_dex(self.store, trows, perp_bases, cfg.dex_lookups_per_cycle)
        by_base: dict[str, list] = {}
        for r in rows:
            if r["base"] in perp_bases:
                by_base.setdefault(r["base"], []).append(r)
        now = time.time()
        ts, gap = int(now), int(self.every * 2.5)
        cands = []
        for base, rs in by_base.items():
            vs = spreads.venues_from_rows(rs, base) + self._dex(base, now)
            idx = spreads.index_checks(vs, idx_dev=cfg.index_dev_pct, perp_dev=cfg.index_dev_pct,
                                       spot_dev=cfg.index_dev_pct, min_vol=cfg.spread_min_vol)
            pp = spreads.perp_pair(vs, cfg.spread_min_vol)
            if pp and cfg.perp_spread_min_pct <= pp["gross_pct"] <= cfg.spread_max_pct:
                cands.append(("perp_perp", base, pp, pp["long"], pp["short"], idx))
            sp = spreads.spot_perp_pair(vs, cfg.spread_min_vol)
            if sp and cfg.spot_perp_min_pct <= abs(sp["gross_pct"]) <= cfg.spread_max_pct:
                cheap, dear = (sp["spot"], sp["perp"]) if sp["gross_pct"] > 0 else (sp["perp"], sp["spot"])
                cands.append(("spot_perp", base, sp, cheap, dear, idx))
            if idx["flags"]:
                cands.append(("index", base, None, None, None, idx))
        for kind, base, pair, cheap, dear, _ in cands:
            if pair:
                self.store.add_spread(f"{kind}|{base}|{cheap['venue']}|{dear['venue']}", ts, abs(pair["gross_pct"]))
        self.store.db.commit()
        alerts = []
        order = sorted(cands, key=lambda c: -(abs(c[2]["gross_pct"]) if c[2] else 0.5))
        for kind, base, pair, cheap, dear, idx in order:
            if kind == "index":
                sig = sorted(f.split(":")[0] for f in idx["flags"])
                last = self.store.get(f"ix:alert:{base}")
                if last and now - last["ts"] < 2 * 3600 and last["sig"] == sig:
                    continue
                self.store.put(f"ix:alert:{base}", {"ts": now, "sig": sig})
                alerts.append(Alert("spreads", base, "index", fmt.index_alert(base, idx), {"type": "index", **idx}, 0.5))
                continue
            key = f"{kind}|{base}|{cheap['venue']}|{dear['venue']}"
            last = self.store.get(f"sp:alert:{key}")
            g = abs(pair["gross_pct"])
            if last and now - last["ts"] < 3600 and g < last["pct"] * 1.3:
                continue
            start, top = self.store.spread_since(key, ts, gap)
            s = {"type": kind, "pair": pair, "held_min": round((ts - start) / 60) if start else 0, "max_pct": top,
                 "context": spreads.spot_context(trows, base, pair["spot"]) if kind == "spot_perp" else None,
                 "index_flags": idx["flags"]}
            if "dex" not in (cheap["kind"], dear["kind"]):
                try:
                    (asks, _), (_, bids) = await asyncio.gather(spreads._book(self.pool, cheap), spreads._book(self.pool, dear))
                    s["capacity"] = spreads.capacity(asks, bids, spreads.TAKER_FEE_PCT)
                    if s["capacity"]["fits_usd"] < 300:
                        continue
                except Exception:  # noqa: BLE001
                    pass
            else:
                dex = cheap if cheap["kind"] == "dex" else dear
                s["capacity"] = {"fits_usd": round((dex.get("liquidity_usd") or 0) * 0.005)}
            self.store.put(f"sp:alert:{key}", {"ts": now, "pct": g})
            data = {"type": kind, "gross_pct": pair["gross_pct"], "carry_8h_pct": pair.get("carry_8h_pct"),
                    "cheap": cheap["venue"], "dear": dear["venue"], "held_min": s["held_min"], "max_pct": top,
                    "capacity": s.get("capacity"), "context": s["context"], "index_flags": idx["flags"]}
            alerts.append(Alert("spreads", base, f"{cheap['ex']}>{dear['ex']}", fmt.spread_alert(base, s), data, g))
            if len(alerts) >= cfg.max_posts_per_cycle:
                break
        return alerts


# -------------------------------------------------------------------- transfers


class StatusScanner(Scanner):
    """Deposit/withdrawal status per network (used by spreads and Bottom); posts open/close changes only
    when SIG_STATUS_ALERTS=true. The first run only records the baseline."""

    feed, every_attr = "status", "transfers_every"

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
        if not self.cfg.status_alerts:
            return []
        alerts = [Alert("status", base, ex, fmt.transfer_alert(ex, base, ch),
                        {"changes": [{"chain": c, "what": w, "opened": o} for c, w, o in ch]},
                        sum(1 for *_, o in ch if o))
                  for (ex, base), ch in changes.items()]
        alerts.sort(key=lambda a: -a.priority)
        return alerts[: self.cfg.max_posts_per_cycle * 2]


# ------------------------------------------------------------------------ flows


class FlowScanner(Scanner):
    """Token deposits to and withdrawals from exchanges, on-chain, with wallets (like an on-chain desk)."""

    feed, every_attr = "flows", "flows_every"

    def __init__(self, cfg, store):
        super().__init__(cfg, store)
        self.book = cf.WalletBook(store)
        self.rpc = cf.Rpc()

    async def _prices(self) -> dict:
        px = self.store.get("px", {}) or {}
        if time.time() - px.get("_ts", 0) < 300:
            return px
        rows, _ = await feeds.snapshot(None, ("spot", "perp"))
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(r["base"], []).append(r["price"])
        from statistics import median
        px = {b: median(v) for b, v in by.items()}
        if px:
            px["_ts"] = time.time()
            self.store.put("px", px)
        return px

    async def _tokens(self, px: dict, stats: dict) -> dict[str, dict[str, str]]:
        """chain → {contract: base} for liquid non-stable coins listed on exchanges."""
        trows, _ = await transfers.all_statuses()
        out: dict[str, dict[str, str]] = {}
        for r in trows:
            ch = r["chain"]
            if ch not in cf.EVM_RPC and ch not in ("TRX", "SOL"):
                continue
            if ch not in self.cfg.flow_chains or not r["contract"] or r["base"] in cf.STABLES or r["base"] not in px:
                continue
            c = r["contract"].lower() if r["contract"].startswith("0x") else r["contract"]
            out.setdefault(ch, {})[c] = r["base"]
        for ch, toks in out.items():  # keep the most traded
            if len(toks) > self.cfg.flows_max_tokens:
                keep = sorted(toks.items(), key=lambda kv: -((stats.get(kv[1]) or [0, 0])[1]))[: self.cfg.flows_max_tokens]
                out[ch] = dict(keep)
        return out

    async def _decimals(self, chain: str, token: str) -> int | None:
        k = f"dec:{chain}:{token}"
        d = self.store.get(k)
        if d is None:
            try:
                d = await self.rpc.decimals(chain, token)
            except Exception:  # noqa: BLE001
                return None
            self.store.put(k, d)
        return d

    async def _evm(self, chain: str, toks: dict[str, str]) -> list[dict]:
        try:
            latest = await self.rpc.block(chain)
        except Exception as e:  # noqa: BLE001
            log.info("%s block failed: %s", chain, e)
            return []
        step = cf.MAX_BLOCKS[chain]
        cur = self.store.get(f"flows:cursor:{chain}") or latest - step
        if latest - cur > step * self.cfg.flows_calls_per_chain * 4:
            cur = latest - step * self.cfg.flows_calls_per_chain  # too far behind: stay real-time
        out, calls = [], 0
        contracts = sorted(toks)
        while cur < latest and calls < self.cfg.flows_calls_per_chain:
            to = min(latest, cur + step)
            try:
                logs = await self.rpc.logs(chain, contracts, cur + 1, to)
            except Exception as e:  # noqa: BLE001
                if step > 1 and any(k in str(e).lower() for k in ("range", "limit", "too many", "exceed", "large")):
                    step = max(1, step // 2)
                    continue
                log.info("%s getLogs failed: %s", chain, e)
                break
            for lg in logs:
                e = cf.parse_log(lg)
                if e and e["token"] in toks:
                    e.update(chain=chain, base=toks[e["token"]], ts=time.time())
                    out.append(e)
            cur, calls = to, calls + 1
        self.store.put(f"flows:cursor:{chain}", cur)
        return out

    async def _tron(self, toks: dict[str, str]) -> list[dict]:
        wallets = sorted(a for a, _ in self.book.all_owned("tron"))
        if not wallets:
            return []
        i = self.store.get("flows:tron:i", 0) % len(wallets)
        batch = (wallets[i:] + wallets[:i])[: self.cfg.tron_wallets_per_cycle]
        self.store.put("flows:tron:i", i + len(batch))
        out = []
        for w in batch:
            since = self.store.get(f"flows:tron:{w}") or int((time.time() - 900) * 1000)
            try:
                rows = await cf.tron_transfers(w, since, os.getenv("TRONGRID_API_KEY", ""))
            except Exception as e:  # noqa: BLE001
                log.info("trongrid %s failed: %s", w, e)
                continue
            for x in rows:
                base = toks.get(x["token"]) if x.get("token") else None
                if base:
                    out.append(x | {"chain": "TRX", "base": base, "ts": (x.get("ts") or 0) / 1000 or time.time()})
                since = max(since, (x.get("ts") or 0) + 1)
            self.store.put(f"flows:tron:{w}", since)
        return out

    async def _solana(self, toks: dict[str, str]) -> list[dict]:
        key = os.getenv("HELIUS_API_KEY", "")
        wallets = sorted(a for a, _ in self.book.all_owned("solana"))
        if not key or not wallets:
            return []
        i = self.store.get("flows:sol:i", 0) % len(wallets)
        batch = (wallets[i:] + wallets[:i])[: self.cfg.tron_wallets_per_cycle]
        self.store.put("flows:sol:i", i + len(batch))
        out = []
        for w in batch:
            seen = set(self.store.get(f"flows:sol:{w}") or [])
            try:
                rows = await cf.solana_transfers(w, key)
            except Exception as e:  # noqa: BLE001
                log.info("helius %s failed: %s", w, e)
                continue
            for x in rows:
                if x["tx"] in seen or not toks.get(x["token"]):
                    continue
                out.append(x | {"chain": "SOL", "base": toks[x["token"]], "ts": (x.get("ts") or 0) / 1000 or time.time()})
            self.store.put(f"flows:sol:{w}", sorted({x["tx"] for x in rows})[-200:])
        return out

    async def scan(self) -> list[Alert]:
        if not self.store.get("por:ts") or time.time() - self.store.get("por:ts") > 86400:
            try:
                n = await self.book.add_binance_por()
                log.info("binance proof-of-reserves: +%d wallets", n)
            except Exception as e:  # noqa: BLE001
                log.info("binance PoR failed: %s", e)
            self.store.put("por:ts", time.time())
        px = await self._prices()
        if not px:
            log.info("flows: no prices (exchanges unreachable)")
            return []
        stats = await ensure_stats(self.store)
        toks = await self._tokens(px, stats)
        events: list[dict] = []
        for ch in self.cfg.flow_chains:
            if ch in cf.EVM_RPC and toks.get(ch):
                events += await self._evm(ch, toks[ch])
        if "TRX" in self.cfg.flow_chains and toks.get("TRX"):
            events += await self._tron(toks["TRX"])
        if "SOL" in self.cfg.flow_chains and toks.get("SOL"):
            events += await self._solana(toks["SOL"])
        cands = []
        for e in events:
            fam = cf.family(e["chain"])
            cls = cf.classify(self.book, fam, e["from"], e["to"])
            if not cls or cls["kind"] == "sweep":
                continue
            if "amount" not in e:
                dec = await self._decimals(e["chain"], e["token"])
                if dec is None:
                    continue
                e["amount"] = e["raw"] / 10 ** dec
            usd = e["amount"] * px.get(e["base"], 0)
            if usd < self.cfg.flow_store_usd:
                continue
            f = {"ts": e["ts"], "chain": e["chain"], "base": e["base"], "token": e["token"], "kind": cls["kind"],
                 "exchange": cls["exchange"], "frm": e["from"], "to": e["to"], "amount": e["amount"], "usd": usd,
                 "tx": e["tx"]}
            if self.store.add_flow(f) and usd >= self.cfg.flow_min_usd:
                cands.append(f | {"cls": cls})
        cands.sort(key=lambda f: -f["usd"])
        alerts = []
        for f in cands[: self.cfg.max_posts_per_cycle]:
            x = await self._enrich(f, px, stats)
            alerts.append(Alert("flows", f["base"], f["exchange"], fmt.flow_alert(f, x, self.book),
                                {k: v for k, v in f.items() if k != "cls"} | x, f["usd"]))
        return alerts

    async def _enrich(self, f: dict, px: dict, stats: dict) -> dict:
        x: dict = {}
        st = stats.get(f["base"])
        if st:
            x["mcap"], x["vol24"] = st
            x["pct_mcap"] = round(f["usd"] / st[0] * 100, 2) if st[0] else None
            x["pct_vol"] = round(f["usd"] / st[1] * 100, 2) if st[1] else None
        x["whale"] = f["usd"] >= self.cfg.flow_whale_usd or (x.get("pct_vol") or 0) >= 5
        evm = f["chain"] in cf.EVM_RPC
        who = f["frm"] if f["kind"] == "deposit" else f["to"]
        if evm:
            try:
                left = await self.rpc.balance_of(f["chain"], f["token"], who)
                dec = await self._decimals(f["chain"], f["token"]) or 0
                x["wallet_left"] = left / 10 ** dec
                x["wallet_left_usd"] = x["wallet_left"] * px.get(f["base"], 0)
                if f["kind"] == "withdrawal":
                    nat = await self.rpc.native_balance(f["chain"], who) / 1e18
                    x["native"] = nat
                    x["native_usd"] = nat * px.get(cf.NATIVE[f["chain"]], 0)
            except Exception:  # noqa: BLE001
                pass
        series = self.store.flows(base=f["base"], days=self.cfg.flows_series_days, kind=f["kind"])
        if series:
            wallets: dict[str, float] = {}
            exs: dict[str, float] = {}
            for r in series:
                w = r["frm"] if f["kind"] == "deposit" else r["to"]
                wallets[w] = wallets.get(w, 0) + r["usd"]
                exs[r["exchange"]] = exs.get(r["exchange"], 0) + r["usd"]
            x["series"] = {"since": series[0]["ts"], "usd": sum(r["usd"] for r in series), "n": len(series),
                           "wallets": sorted(wallets.items(), key=lambda kv: -kv[1]),
                           "exchanges": sorted(exs.items(), key=lambda kv: -kv[1])}
        if f["kind"] == "withdrawal":
            mine = [r for r in series if r["to"] == f["to"]]
            x["receiver"] = {"n": len(mine), "usd": sum(r["usd"] for r in mine)}
        return x


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
            "flows": FlowScanner, "dex": DexScanner, "status": StatusScanner}
