"""Message formats (Telegram HTML). The OI format keeps the familiar alert layout, so the same parser reads it."""

from __future__ import annotations

import html
import time

EX_NAMES = {"binance": "Binance", "bybit": "Bybit", "okx": "OKX", "bitget": "Bitget", "gate": "Gate", "mexc": "MEXC",
            "kucoin": "KuCoin", "bingx": "BingX", "hyperliquid": "Hyperliquid", "dex": "DEX"}


def e(s) -> str:
    return html.escape(str(s), quote=False)


def ex_name(x: str) -> str:
    return EX_NAMES.get(x, x.capitalize())


def money(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "n/a"
    s = "-" if v < 0 else ("+" if sign else "")
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{s}${a / div:.2f}{suf}"
    return f"{s}${a:.0f}"


def num(v: float | None) -> str:
    if v is None:
        return "n/a"
    a = abs(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{v / div:.2f}{suf}"
    return f"{v:.2f}"


def price(p: float | None) -> str:
    if p is None:
        return "n/a"
    if p >= 100:
        return f"${p:.2f}"
    if p >= 1:
        return f"${p:.4f}"
    digits = 6 if p >= 0.0001 else 10
    return f"${p:.{digits}f}"


def pct(v: float | None, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:+.{digits}f}%"


def countdown(next_ms: int | None, now: float | None = None) -> str:
    if not next_ms:
        return ""
    sec = max(0, next_ms / 1000 - (now or time.time()))
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}ч{m:02d}м" if h else f"{m}м"


def utc(ts: float | None = None) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts or time.time()))


def tags(*xs: str) -> str:
    return " ".join("#" + "".join(ch for ch in x if ch.isalnum() or ch == "_") for x in xs if x)


def oi_alert(base: str, card: dict, signal_no: int, reading: dict) -> str:
    """Layout compatible with bottom.tools.ultra.parse_alert."""
    c = card
    lines = [f"<b>${e(base)} | {ex_name(c['ex'])}</b>"]
    oi = f"OI: {pct(c['oi_chg_pct'])} за 4ч"
    if c.get("oi_fast_chg_pct") is not None:
        oi += f" | {pct(c['oi_fast_chg_pct'])} за 15м"
    lines += [oi, f"{num(c.get('oi_coins_from'))} → {num(c.get('oi_coins'))} ({money(c.get('oi_added_usd'), True)})", ""]
    lines.append(f"💲 Цена: {price(c['price'])}" + (f" ({pct(c['price_chg_pct'])})" if c.get("price_chg_pct") is not None else ""))
    if c.get("funding_pct") is not None:
        lines.append(f"⛽ Funding: {pct(c['funding_pct'], 4)}")
    if c.get("long_pct") is not None:
        lines.append(f"⚖️ L/S: {c['long_pct']:.1f}% 🟢 / {100 - c['long_pct']:.1f}% 🔴")
    if c.get("mark_side"):
        lines.append(f"🏷 Mark: {price(c.get('mark_price') or c['price'])} ({c['mark_side']})")
    lines.append(f"🏛 OI/MCap: {c['oi_mcap_pct']:.2f}%" if c.get("oi_mcap_pct") is not None else "🏛 OI/MCap: n/a")
    if c.get("book_delta_usd") is not None:
        d = c["book_delta_usd"]
        lines.append(f"{'📗' if d >= 0 else '📕'} Book Delta: {money(d, True)}")
    if c.get("ad_perp_usd") is not None or c.get("ad_spot_usd") is not None:
        sp = c.get("ad_spot_usd")
        spot = "спот n/a" if sp is None else f"{money(sp, True)} {'↑ (спот берут)' if sp > 0 else '↓ (спот продают)'}"
        pp = c.get("ad_perp_usd")
        perp = "" if pp is None else f"  (перп {money(pp, True)} {'↑' if pp > 0 else '↓'})"
        lines.append(f"🌊 A/D спот 4ч: {spot}{perp}")
    if c.get("walls"):
        lines += ["", f"🧱 Стакан {ex_name(c['ex'])}:"]
        for w in c["walls"][:5]:
            arrow = "↓" if w["side"] == "above" else "↑"
            lines.append(f"{arrow} {price(w['price'])} ({w['dist_pct']:+.1f}%) — {money(w['usd'])} ({w['x_avg']}x)")
    lines += ["", f"<b>{e(reading['pattern'])}</b>"]
    if reading.get("combos"):
        lines += [e(x) for x in reading["combos"][:3]]
    if reading.get("flags"):
        lines.append("⚠️ " + e("; ".join(reading["flags"][:3])))
    lines += ["", f"{utc()} | #{signal_no} сигнал", tags("OI", base)]
    return "\n".join(lines)


def funding_alert(base: str, rows: list[dict], trigger: set[str], kind: str, prev: dict) -> str:
    head = {"extreme": "🚨", "jump": "⚡", "update": "🚨"}[kind]
    title = f"{head} <b>{e(base)}/USDT</b>" + (" (обновление)" if kind == "update" else "") + \
            (" ⚡резкий скачок" if kind == "jump" else "")
    lines = [title, ""]
    rows = sorted(rows, key=lambda r: -abs(r["funding_pct"] or 0))

    def line(r, mark):
        ivl = f"/{r['interval_h']:g}ч" if r.get("interval_h") and r["interval_h"] != 8 else ""
        was = f" (было {pct(prev[r['ex']], 4)})" if kind == "jump" and r["ex"] in prev else ""
        cd = countdown(r.get("next_funding_ms"))
        return f"{mark}{ex_name(r['ex']):<11} {pct(r['funding_pct'], 4)}{ivl}  {price(r['price'])}  {cd}{was}".rstrip()

    for r in rows:
        if r["ex"] in trigger:
            lines.append(line(r, "▲ " if r["funding_pct"] > 0 else "▼ "))
    others = [r for r in rows if r["ex"] not in trigger]
    if others:
        lines.append("──────────────────")
        lines += [line(r, "  ") for r in others]
    fr = [r for r in rows if r.get("funding_pct") is not None]
    if len(fr) >= 2:
        lo, hi = min(fr, key=lambda r: r["funding_pct"]), max(fr, key=lambda r: r["funding_pct"])
        gap = (hi["price"] - lo["price"]) / lo["price"] * 100 if lo["price"] else 0
        lines += ["", f"↔️ Разница фандинга: лонг на {ex_name(lo['ex'])} ({pct(lo['funding_pct'], 4)}), шорт на "
                      f"{ex_name(hi['ex'])} ({pct(hi['funding_pct'], 4)}) = {hi['funding_pct'] - lo['funding_pct']:.4f}% "
                      f"за выплату; цены расходятся на {gap:+.2f}%"]
    lines += ["", utc(), tags("FUNDING", base)]
    return "\n".join(lines)


def settlement_alert(settle_ms: int, items: list[tuple[str, list[dict]]]) -> str:
    mins = max(0, round((settle_ms / 1000 - time.time()) / 60))
    lines = [f"⏰ <b>Расчёт через ~{mins} мин ({time.strftime('%H:%M UTC', time.gmtime(settle_ms / 1000))})</b>", ""]
    for base, rows in items:
        sign = "▲" if rows[0]["funding_pct"] > 0 else "▼"
        lines.append(f"{sign} <b>{e(base)}/USDT</b>")
        lines += [f"  {ex_name(r['ex'])}  {pct(r['funding_pct'], 4)} ({price(r['price'])})" for r in rows]
        lines.append("")
    lines.append(tags("FUNDING", "SETTLEMENT"))
    return "\n".join(lines)


def _f8(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.4f}%/8ч"


def spread_alert(base: str, s: dict) -> str:
    p = s["pair"]
    if s["type"] == "perp_perp":
        lo, hi = p["long"], p["short"]
        lines = [f"🔀 <b>{e(base)} · фьюч↔фьюч {p['gross_pct']:.2f}%</b>", "",
                 f"лонг {e(lo['venue'])} {price(lo['price'])} → шорт {e(hi['venue'])} {price(hi['price'])}",
                 f"фандинг: {e(lo['venue'])} {_f8(lo.get('funding_8h'))} · {e(hi['venue'])} {_f8(hi.get('funding_8h'))}"]
        if p.get("carry_8h_pct") is not None:
            c = p["carry_8h_pct"]
            lines.append(f"перенос: {c:+.4f}% за 8ч {'в твою пользу' if c >= 0 else 'против тебя'}")
        idx = [f"{e(v['venue'])} {price(v['index'])}" for v in (lo, hi) if v.get("index")]
        if idx:
            lines.append("индекс: " + " · ".join(idx))
    else:
        sp, pe = p["spot"], p["perp"]
        lines = [f"🔀 <b>{e(base)} · спот↔фьюч {p['gross_pct']:+.2f}%</b>", "",
                 f"спот {e(sp['venue'])} {price(sp['price'])}"
                 + (f" (ликв {money(sp.get('liquidity_usd'))})" if sp.get("liquidity_usd") else "")
                 + f" · фьюч {e(pe['venue'])} {price(pe['price'])}",
                 f"схема: {e(p['scheme'])}"]
        if p.get("carry_8h_pct") is not None:
            c = p["carry_8h_pct"]
            lines.append(f"фандинг {e(pe['venue'])}: {_f8(pe.get('funding_8h'))} → "
                         f"{'получаешь' if c >= 0 else 'платишь'} {abs(c):.4f}% за 8ч")
        if pe.get("index"):
            lines.append(f"индекс {e(pe['venue'])}: {price(pe['index'])}")
        if sp.get("url"):
            lines.append(f'<a href="{e(sp["url"])}">пул</a>')
    if s.get("context"):
        lines.append(f"⚠️ {e(s['context'])}")
    if s.get("index_flags"):
        lines.append("🧭 " + e("; ".join(s["index_flags"][:2])))
    lines.append(f"⌛️ Держится: {s.get('held_min') or 0} мин" + (f" · макс {s['max_pct']:.2f}%" if s.get("max_pct") else ""))
    if s.get("capacity"):
        c = s["capacity"]
        lines.append(f"📦 Помещается: ~{money(c.get('fits_usd'))}"
                     + (f", прибыль ~{money(c.get('profit_usd'))}" if c.get("profit_usd") else ""))
    lines += ["", utc(), tags("SPREAD", base)]
    return "\n".join(lines)


def index_alert(base: str, idx: dict) -> str:
    lines = [f"🧭 <b>{e(base)} · проверка индексов</b>", ""]
    lines += [f"• {e(f)}" for f in idx["flags"][:6]]
    lines.append("")
    if idx.get("median_index"):
        lines.append(f"медиана индексов: {price(idx['median_index'])}")
    if idx.get("median_spot"):
        lines.append(f"медиана спота: {price(idx['median_spot'])}")
    for r in idx["per_exchange"][:8]:
        if r.get("index"):
            lines.append(f"{e(r['venue'])}: last {price(r['last'])} · mark {price(r.get('mark'))} · index {price(r['index'])}")
    lines += ["", utc(), tags("INDEX", base)]
    return "\n".join(lines)


def transfer_alert(ex: str, base: str, changes: list[tuple[str, str, bool]]) -> str:
    lines = [f"🏦 <b>{ex_name(ex)} · ${e(base)}</b>", ""]
    for chain, what, opened in changes:
        lines.append(f"{'🟢 открыт' if opened else '🔴 закрыт'} {'депозит' if what == 'deposit' else 'вывод'} · {e(chain)}")
    lines += ["", utc(), tags("TRANSFERS", base, ex)]
    return "\n".join(lines)


def dex_alert(base: str, pool: dict, s: dict) -> str:
    lines = [f"🔴 <b>${e(base)} · слив · {money(s['net_sold_usd'])}</b>",
             f"{e(pool.get('chain'))} · {e(pool.get('dex'))}", "",
             f"Продал: {money(s['net_sold_usd'])} = {s['pct_of_liquidity']:.1f}% ликвидности ({money(pool.get('liquidity_usd'))})"
             f" · {s['sells']} сделок за {s['minutes']} мин",
             f"<code>{e(s['wallet'])}</code>"]
    if pool.get("url"):
        lines.append(f'<a href="{e(pool["url"])}">пул</a>')
    lines += ["", utc(), tags("DEX", base)]
    return "\n".join(lines)


def _link(chain: str, addr: str, text: str) -> str:
    from ..tools.chainflows import addr_url

    return f'<a href="{e(addr_url(chain, addr))}">{e(text)}</a>'


def flow_alert(f: dict, x: dict, book) -> str:
    """Deposit / withdrawal card: amount, wallets, weight, series, receiver stats."""
    from ..tools.chainflows import CHAIN_NAMES, NATIVE, family, short, tx_url

    fam, ch, base = family(f["chain"]), f["chain"], f["base"]
    dep = f["kind"] == "deposit"
    head = ("🔴 Deposit" if dep else "🟢 Withdrawal") + (" 🐋" if x.get("whale") else "")
    lines = [f"<b>{head} | ${e(base)}</b>", "", f"🪙 Объём: {num(f['amount'])} ({money(f['usd'])})"]
    if dep and x.get("wallet_left") is not None:
        left = x["wallet_left"]
        lines.append("👛 Остаток: 0 · завёл весь запас" if left <= f["amount"] * 0.01
                     else f"👛 Остаток: {num(left)} ({money(x.get('wallet_left_usd'))})")
    lines += ["", f"⬇️ From: {_link(ch, f['frm'], book.describe(fam, f['frm']))}",
              f"⏺️ To: {_link(ch, f['to'], book.describe(fam, f['to']))}",
              f"🔗 Network: {CHAIN_NAMES.get(ch, ch)}"]
    if x.get("pct_vol") is not None or x.get("pct_mcap") is not None:
        lines += ["", "📉 Вес:"]
        if x.get("pct_vol") is not None:
            lines.append(f"{x['pct_vol']:.2f}% от суточного объёма")
        if x.get("pct_mcap") is not None:
            lines.append(f"{x['pct_mcap']:.2f}% от капы ({money(x.get('mcap'))})")
    srs = x.get("series")
    if srs and srs["n"] > 1:
        since = time.strftime("%d.%m", time.gmtime(srs["since"]))
        lines += ["", f"🔁 {'Серия заводов' if dep else 'Отток'} с {since}: {money(srs['usd'])} ({srs['n']} транз)"]
        if not dep and len(srs["exchanges"]) > 1:
            lines.append("   • " + " · ".join(f"{e(k)} {money(v)}" for k, v in srs["exchanges"][:4]))
        for w, u in srs["wallets"][:3]:
            lines.append(f"{_link(ch, w, short(w))} {money(u)}")
        if len(srs["wallets"]) > 3:
            lines.append(f"и ещё {len(srs['wallets']) - 3} кошельков")
    if not dep:
        r = x.get("receiver") or {}
        lines += ["", f"💸 Этот получатель: {_link(ch, f['to'], short(f['to']))}",
                  f"   выводов: {r.get('n', 1)} · вывел всего: {money(r.get('usd') or f['usd'])}"]
        if x.get("wallet_left") is not None:
            lines.append(f"   на кошельке: {num(x['wallet_left'])} {e(base)} (~{money(x.get('wallet_left_usd'))})")
        if x.get("native") is not None:
            lines.append(f"   ⛽ native: {x['native']:.4f} {NATIVE.get(ch, '')} (~{money(x.get('native_usd'))})")
    lines += ["", f"🕒 {utc(f['ts'])} · <a href=\"{e(tx_url(ch, f['tx']))}\">tx</a>",
              tags("DEPOSIT" if dep else "WITHDRAWAL", base, f["exchange"])]
    return "\n".join(lines)
