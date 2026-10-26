"""Market data: CEX prices (spot, perps, Binance Alpha), candles, order books, derivatives, OI, spreads, funding,
transfers."""

from __future__ import annotations

import asyncio
import logging

from ..schema import SYM
from ..symbols import normalize_base
from . import alpha, awtrader, oicard, spreads, transfers
from . import derivatives as deriv
from . import exchanges as exch
from .indicators import summarize_candles

log = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]


MARKETS = ["auto", "perp", "spot", "alpha"]


async def candle_summary(pool: exch.ExchangePool, symbol: str, timeframe: str = "1h", limit: int = 200,
                         market: str = "auto") -> dict:
    """Candles with indicators and levels. auto: Binance perp first (it has taker volume for delta/CVD), then any
    exchange's perp, then spot; perp / spot: only that market; alpha: Binance Alpha."""
    limit = max(30, min(int(limit), 500))
    if timeframe not in TIMEFRAMES:
        return {"error": f"timeframe должен быть одним из {TIMEFRAMES}"}
    if market not in MARKETS:
        return {"error": f"market должен быть одним из {MARKETS}"}
    if market == "alpha":
        return await alpha_candles(symbol, timeframe, limit)
    bn = await pool.get("binance") if market != "spot" else None
    found = exch.find_market(bn, normalize_base(symbol), "perp") if bn else None
    meta: dict = {}
    candles: list[dict] = []
    if found:
        m, mult = found
        try:
            candles = await deriv.binance_klines(m["id"], timeframe, limit)
            for c in candles:
                for k in ("o", "h", "l", "c"):
                    c[k] /= mult
                c["v"] *= mult
                c["taker_buy"] *= mult
            meta = {"exchange": "binance", "market": "perp", "symbol": m["symbol"]}
        except Exception as e:  # noqa: BLE001
            log.info("binance klines failed: %s", e)
            candles = []
    if not candles:
        kinds = ("perp", "spot") if market == "auto" else (market,)
        candles, meta = await exch.ohlcv(pool, symbol, timeframe, limit, kinds)
    if not candles:
        return meta
    return {"source": meta, "timeframe": timeframe, **with_range(summarize_candles(candles), candles, timeframe)}


async def alpha_candles(symbol: str, timeframe: str, limit: int) -> dict:
    tok = await alpha.lookup(symbol)
    if tok is None:
        return {"error": f"{normalize_base(symbol)} нет на Binance Alpha"}
    candles = await alpha.klines(tok["alpha_id"], timeframe, limit)
    if not candles:
        return {"error": f"свечи Alpha {tok['alpha_id']} не получены"}
    meta = {"exchange": "binance_alpha", "market": "alpha", "symbol": tok["alpha_id"], "chain": tok["chain"],
            "contract": tok["contract"]}
    return {"source": meta, "timeframe": timeframe, **with_range(summarize_candles(candles), candles, timeframe)}


async def overview(pool: exch.ExchangePool, symbol: str) -> dict:
    """market_overview of the CEXes plus the Binance Alpha price when the ticker is listed there."""
    out, tok = await asyncio.gather(exch.market_overview(pool, symbol), alpha.lookup(symbol))
    if tok:
        med = out.get("median_price")
        tok["dev_from_cex_median_pct"] = round((tok["price"] - med) / med * 100, 2) if med and tok["price"] else None
        out["binance_alpha"] = tok
        if tok["dev_from_cex_median_pct"] is not None and abs(tok["dev_from_cex_median_pct"]) > 3:
            out.setdefault("possible_different_token_or_thin_market", []).append(
                f"Binance Alpha {tok['symbol']} ({tok['chain']}): {tok['dev_from_cex_median_pct']}% от медианы CEX — "
                "возможно, другой токен с тем же тикером")
    return out


async def book(pool: exch.ExchangePool, symbol: str, market: str = "perp", exchange: str | None = None) -> dict:
    if market == "alpha":
        return await alpha.alpha_book(symbol)
    return await exch.orderbook(pool, symbol, market, exchange or None)


def with_range(s: dict, candles: list[dict], timeframe: str) -> dict:
    """Add the position in the last 24h range, from the same series."""
    per_day = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "4h": 6, "1d": 1}[timeframe]
    day = candles[-per_day:]
    hi, lo = max(c["h"] for c in day), min(c["l"] for c in day)
    s["range_24h"] = {"high": hi, "low": lo,
                      "position_pct": round((candles[-1]["c"] - lo) / (hi - lo) * 100, 1) if hi > lo else None}
    if len(candles) < per_day:  # 1m: 500 candles cover ~8h, not a day
        s["range_24h"]["covers_hours"] = round(len(candles) * 24 / per_day, 1)
    return s


def register(tb) -> None:
    s = tb.s
    tb.add(
        "market_overview",
        "Цены спот и перп на всех подключённых CEX: медиана, разброс между биржами, базис перп/спот по "
        "каждой бирже, суммарные объёмы 24ч, подозрительные расхождения (другой токен или тонкий рынок); если "
        "тикер есть на Binance Alpha — его цена, ликвидность и контракт (binance_alpha).",
        {"symbol": SYM}, ["symbol"], "📊 Сверяю цены на биржах",
        lambda symbol: overview(tb.pool, symbol),
    )

    tb.add(
        "derivatives",
        "Деривативы: фандинг по биржам (текущий и последние выплаты), OI и его изменение за 1ч/4ч/24ч, "
        "доля лонгов по аккаунтам (Binance/Bybit/OKX) и сводно, топ-трейдеры Binance, taker buy/sell.",
        {"symbol": SYM}, ["symbol"], "📈 Смотрю фандинг и OI",
        lambda symbol: deriv.derivatives(tb.pool, symbol),
    )

    tb.add(
        "candles",
        "Свечи и расчёты по ним: EMA20/50/200, RSI14, Stoch, ATR14 (и в %), ближайшие уровни поддержки/"
        "сопротивления (свинг-хаи/лоу = пулы ликвидности) и сколько раз их тестировали (level_touches), "
        "MACD (кросс, рост гистограммы), Боллинджер (ширина, позиция), Фибоначчи последнего импульса, "
        "позиция в 24ч диапазоне, всплеск объёма и числа сделок, дельта покупок/продаж и CVD (если "
        "Binance-перп или Alpha). 1m/5m/15m — для скальпа, 1h/4h/1d — для свинга. market: auto (перп, если нет — "
        "спот), perp, spot или alpha (Binance Alpha).",
        {
            "symbol": SYM,
            "timeframe": {"type": "string", "enum": TIMEFRAMES},
            "limit": {"type": "integer", "description": "Число свечей, 30–500 (по умолчанию 200)"},
            "market": {"type": "string", "enum": MARKETS},
        },
        ["symbol", "timeframe"], "🕯 Смотрю свечи и уровни",
        lambda symbol, timeframe="1h", limit=200, market="auto": candle_summary(tb.pool, symbol, timeframe, limit,
                                                                                 market),
    )

    tb.add(
        "orderbook",
        "Стаканы: глубина бидов/асков в ±1/2/5% от цены, перекос, спред, крупные стенки с дистанцией до цены. "
        "Без exchange — стаканы ВСЕХ подключённых бирж разом: сводная глубина, у какой биржи сколько глубины, "
        "крупнейшие стенки рынка с биржей, расхождение цен; с exchange — одна биржа подробно. market: perp, spot "
        "или alpha (Binance Alpha — своего стакана нет, отдаёт ликвидность DEX-пулов токена).",
        {
            "symbol": SYM,
            "market": {"type": "string", "enum": ["perp", "spot", "alpha"]},
            "exchange": {"type": "string", "description": "id биржи (binance, bybit, okx, mexc...). Пусто — все биржи"},
        },
        ["symbol", "market"], "📚 Смотрю стаканы",
        lambda symbol, market="perp", exchange=None: book(tb.pool, symbol, market, exchange),
    )

    tb.add(
        "spot_perp_flow",
        "Кто ведёт движение: чистые агрессивные покупки (taker buy − sell, $) на споте и на перпе Binance "
        "за 1ч и 4ч, доля покупок, соотношение объёмов перп/спот и вывод. Аналог A/D спот/перп из Ultra.",
        {"symbol": SYM}, ["symbol"], "🌊 Смотрю поток спот/перп",
        lambda symbol: deriv.spot_perp_flow(normalize_base(symbol)),
    )

    tb.add(
        "oi_card",
        "Своя карточка OI по монете (Binance и Bybit): OI и его изменение за 4ч и 15м, $ притока, цена 4ч, "
        "фандинг, L/S, Mark, Book Delta, стенки стакана, A/D спот/перп, OI/MCap; и разбор по методичке: "
        "паттерн, сценарий, комбинации 1–10, красные флаги. Для вопросов про OI, сквиз, «кто заходит».",
        {"symbol": SYM}, ["symbol"], "📈 Считаю OI",
        lambda symbol: oicard.oi_card(normalize_base(symbol), s.coingecko_api_key),
    )

    tb.add(
        "awtrader_spreads",
        "ОСНОВНОЙ источник для «какие спреды сейчас / лучший спред / где расхождение цен»: все фьючерс-фьючерс "
        "спреды рынка разом по 12 биржам (Binance, Bybit, OKX, Gate, Bitget, MEXC, KuCoin, BingX, LBank, Aster, "
        "Hyperliquid, Lighter) с исполнимыми ценами (buy по ask, sell по bid), фандингом и интервалом обеих сторон, "
        "переносом, оборотом. Фильтры: symbol, exchange, min_spread. Стаканы, статусы сетей и индексы тут не "
        "проверены: по выбранной монете затем spread_check.",
        {"symbol": {"type": "string", "description": "Тикер; пусто — весь рынок"},
         "min_spread": {"type": "number", "description": "Минимальный спред, % (по умолчанию 1)"},
         "exchange": {"type": "string", "description": "Только спреды с участием этой биржи, например aster"},
         "limit": {"type": "integer", "description": "Сколько строк (по умолчанию 15)"}},
        [], "🔀 Беру спреды рынка (awtrader)",
        lambda symbol="", min_spread=1.0, exchange="", limit=15: awtrader.spreads(
            symbol, max(0.1, float(min_spread)), max(1, min(int(limit), 50)), exchange),
    )

    tb.add(
        "funding_rates",
        "ОСНОВНОЙ источник по фандингу: таблица по 13 биржам разом (Binance, Bybit, OKX, Gate, Bitget, MEXC, "
        "KuCoin, BingX, Phemex, Aster, Hyperliquid, Lighter, LBank): ставка за интервал, интервал, пересчёт на 8 "
        "часов, минут до выплаты. С тикером — по всем биржам монеты и дельта-нейтральная идея; без тикера — самые "
        "экстремальные ставки рынка и самый большой разрыв между биржами. История выплат и OI — derivatives.",
        {"symbol": {"type": "string", "description": "Тикер; пусто — обзор рынка"},
         "top": {"type": "integer", "description": "Сколько монет в обзоре (по умолчанию 15)"}},
        [], "💸 Беру фандинг рынка (awtrader)",
        lambda symbol="", top=15: awtrader.funding(symbol, max(1, min(int(top), 40))),
    )

    tb.add(
        "spread_check",
        "Спреды монеты без переводов между биржами: фьюч↔фьюч по всем биржам (с переносом фандинга) и "
        "спот↔фьюч (спот CEX и DEX-пулы настоящего контракта против перпов), проверка индексов (перп к своему "
        "индексу, марк к индексу, индексы бирж между собой и к споту); сколько спред держится по истории 5м, "
        "сужается ли, сколько помещается в стаканы, вердикт. Только когда спрашивают про спред/арбитраж/индекс.",
        {"symbol": SYM, "hours": {"type": "integer", "description": "Окно истории спреда, ч (по умолчанию 6)"}},
        ["symbol"], "🔀 Проверяю спред",
        lambda symbol, hours=6: spreads.spread_check(tb.pool, symbol, max(1, min(int(hours), 24))),
    )

    tb.add(
        "transfer_status",
        "Депозиты и выводы монеты на биржах по всем сетям: открыто/закрыто, комиссия вывода, контракт; "
        "конфликты контрактов (другой токен с тем же тикером). Bybit/OKX/MEXC/BingX — только с API-ключом.",
        {"symbol": SYM}, ["symbol"], "🏦 Смотрю депы и выводы",
        lambda symbol: transfers.transfer_status(normalize_base(symbol)),
    )

    tb.add(
        "binance_alpha",
        "Binance Alpha (ончейн-листинги Binance до спота/фьючерсов): с тикером или контрактом — цена, изменение и "
        "объём 24ч, ликвидность, MCap/FDV, холдеры, сеть и контракт, дата листинга, есть ли уже на споте/фьючерсах "
        "Binance; токены с тем же тикером. Без тикера — рынок Alpha: топ по объёму, самые сильные движения, свежие "
        "листинги. Свечи — candles(market=alpha), глубина — orderbook(market=alpha).",
        {"query": {"type": "string", "description": "Тикер или адрес контракта; пусто — обзор Alpha"},
         "top": {"type": "integer", "description": "Сколько строк в обзоре (по умолчанию 15)"}},
        [], "🅰️ Смотрю Binance Alpha",
        lambda query="", top=15: alpha.binance_alpha(query, top),
    )
