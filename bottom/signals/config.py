"""Scanner settings from .env (all optional except the chat to post into)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    return int(_f(name, default))


TOPICS = {
    "oi": "📈 OI",
    "funding": "💸 Funding",
    "spreads": "🔀 Спреды",
    "flows": "🏦 Деп / Вывод",
    "dex": "🐋 Сливы DEX",
    "status": "🔐 Статусы сетей",
}


@dataclass
class SignalsConfig:
    bot_token: str = ""
    chat_id: int | None = None
    db_path: str = "data/signals.db"
    exchanges: tuple[str, ...] = ()
    max_posts_per_cycle: int = 8
    # intervals, seconds
    oi_every: int = 300
    funding_every: int = 120
    spreads_every: int = 180
    transfers_every: int = 600
    dex_every: int = 300
    # thresholds
    oi_min_pct: float = 20.0
    oi_fast_min_pct: float = 10.0
    oi_min_usd: float = 300_000
    funding_min_pct: float = 0.5
    funding_jump_pct: float = 0.3
    perp_spread_min_pct: float = 1.0
    spot_perp_min_pct: float = 1.5
    index_dev_pct: float = 1.5
    spread_max_pct: float = 30.0
    spread_min_vol: float = 50_000
    dex_lookups_per_cycle: int = 10
    dex_min_usd: float = 100_000
    dex_min_liq_pct: float = 10.0
    dex_pools_per_cycle: int = 20
    flows_every: int = 60
    flow_min_usd: float = 100_000
    flow_whale_usd: float = 1_000_000
    flow_store_usd: float = 10_000
    flows_max_tokens: int = 300
    flows_calls_per_chain: int = 6
    flows_series_days: int = 30
    flow_chains: tuple[str, ...] = ("ETH", "BSC", "BASE", "ARB", "POLYGON", "OP", "AVAXC", "TRX", "SOL")
    tron_wallets_per_cycle: int = 8
    status_alerts: bool = False

    @classmethod
    def load(cls) -> "SignalsConfig":
        chat = os.getenv("SIGNALS_CHAT_ID", "").strip()
        ex = os.getenv("SIGNALS_EXCHANGES", "").strip()
        return cls(
            bot_token=os.getenv("SIGNALS_BOT_TOKEN", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=int(chat) if chat.lstrip("-").isdigit() else None,
            db_path=os.getenv("SIGNALS_DB", "data/signals.db"),
            exchanges=tuple(x.strip() for x in ex.split(",") if x.strip()),
            max_posts_per_cycle=_i("SIG_MAX_POSTS_PER_CYCLE", 8),
            oi_every=_i("SIG_OI_EVERY", 300),
            funding_every=_i("SIG_FUNDING_EVERY", 120),
            spreads_every=_i("SIG_SPREADS_EVERY", 180),
            transfers_every=_i("SIG_TRANSFERS_EVERY", 600),
            dex_every=_i("SIG_DEX_EVERY", 300),
            oi_min_pct=_f("SIG_OI_MIN_PCT", 20),
            oi_fast_min_pct=_f("SIG_OI_FAST_MIN_PCT", 10),
            oi_min_usd=_f("SIG_OI_MIN_USD", 300_000),
            funding_min_pct=_f("SIG_FUNDING_MIN_PCT", 0.5),
            funding_jump_pct=_f("SIG_FUNDING_JUMP_PCT", 0.3),
            perp_spread_min_pct=_f("SIG_PERP_SPREAD_MIN_PCT", 1.0),
            spot_perp_min_pct=_f("SIG_SPOT_PERP_MIN_PCT", 1.5),
            index_dev_pct=_f("SIG_INDEX_DEV_PCT", 1.5),
            spread_max_pct=_f("SIG_SPREAD_MAX_PCT", 30),
            spread_min_vol=_f("SIG_SPREAD_MIN_VOL", 50_000),
            dex_lookups_per_cycle=_i("SIG_DEX_LOOKUPS_PER_CYCLE", 10),
            dex_min_usd=_f("SIG_DEX_MIN_USD", 100_000),
            dex_min_liq_pct=_f("SIG_DEX_MIN_LIQ_PCT", 10),
            dex_pools_per_cycle=_i("SIG_DEX_POOLS_PER_CYCLE", 20),
            flows_every=_i("SIG_FLOWS_EVERY", 60),
            flow_min_usd=_f("SIG_FLOW_MIN_USD", 100_000),
            flow_whale_usd=_f("SIG_FLOW_WHALE_USD", 1_000_000),
            flow_store_usd=_f("SIG_FLOW_STORE_USD", 10_000),
            flows_max_tokens=_i("SIG_FLOWS_MAX_TOKENS", 300),
            flows_calls_per_chain=_i("SIG_FLOWS_CALLS_PER_CHAIN", 6),
            flows_series_days=_i("SIG_FLOWS_SERIES_DAYS", 30),
            flow_chains=tuple(x.strip().upper() for x in (os.getenv("SIG_FLOW_CHAINS", "") or
                              "ETH,BSC,BASE,ARB,POLYGON,OP,AVAXC,TRX,SOL").split(",") if x.strip()),
            tron_wallets_per_cycle=_i("SIG_TRON_WALLETS_PER_CYCLE", 8),
            status_alerts=os.getenv("SIG_STATUS_ALERTS", "").lower() in ("1", "true", "yes"),
        )
