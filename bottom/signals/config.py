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
    "transfers": "🏦 Деп / Вывод",
    "dex": "🐋 Сливы DEX",
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
    spread_min_pct: float = 3.0
    spread_max_pct: float = 60.0
    spread_min_vol: float = 20_000
    spread_show_closed: bool = False
    dex_lookups_per_cycle: int = 10
    dex_min_usd: float = 20_000
    dex_min_liq_pct: float = 3.0
    dex_pools_per_cycle: int = 20

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
            spread_min_pct=_f("SIG_SPREAD_MIN_PCT", 3),
            spread_max_pct=_f("SIG_SPREAD_MAX_PCT", 60),
            spread_min_vol=_f("SIG_SPREAD_MIN_VOL", 20_000),
            spread_show_closed=os.getenv("SIG_SPREAD_SHOW_CLOSED", "").lower() in ("1", "true", "yes"),
            dex_lookups_per_cycle=_i("SIG_DEX_LOOKUPS_PER_CYCLE", 10),
            dex_min_usd=_f("SIG_DEX_MIN_USD", 20_000),
            dex_min_liq_pct=_f("SIG_DEX_MIN_LIQ_PCT", 3),
            dex_pools_per_cycle=_i("SIG_DEX_POOLS_PER_CYCLE", 20),
        )
