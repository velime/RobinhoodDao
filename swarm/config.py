"""Settings loaded from environment variables (.env supported)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# Crypto news feeds and influencers on X. Override with X_ACCOUNTS in .env.
DEFAULT_X_ACCOUNTS = (
    # news / on-chain alerts
    "WatcherGuru,tier10k,WuBlockchain,lookonchain,EmberCN,whale_alert,"
    "Cointelegraph,CoinDesk,TheBlock__,"
    # traders / influencers
    "CryptoHayes,HsakaTrades,GCRClassic,CryptoKaleo,Pentosh1,CryptoDonAlt,"
    "blknoiz06,AltcoinSherpa,inversebrah,cobie"
)


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _float(name: str, default: float) -> float:
    return float(os.getenv(name) or default)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name) or default)


@dataclass(frozen=True)
class RiskRules:
    min_rr: float = 1.3
    min_stop_pct_major: float = 1.5
    min_stop_pct_alt: float = 3.0
    min_stop_atr_mult: float = 1.5
    majors: tuple[str, ...] = ("BTC", "ETH")


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "openai"  # "openai" (any OpenAI-compatible API) or "anthropic"
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Settings:
    telegram_token: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)
    llm_fallback: LLMConfig | None = None
    llm_cooldown_sec: int = 600
    max_steps_per_question: int = 12
    daily_steps_per_user: int = 50
    admin_ids: tuple[int, ...] = ()
    exchanges: tuple[str, ...] = ()
    coingecko_api_key: str = ""
    tavily_api_key: str = ""
    cryptopanic_api_key: str = ""
    twitterapi_io_key: str = ""
    x_accounts: tuple[str, ...] = ()
    timezone: str = "Europe/Kyiv"
    db_path: str = "swarm.db"
    log_level: str = "INFO"
    risk: RiskRules = field(default_factory=RiskRules)


def _llm(prefix: str) -> LLMConfig | None:
    model = os.getenv(f"{prefix}MODEL", "")
    provider = os.getenv(f"{prefix}PROVIDER", "openai").lower()
    if not model and provider != "anthropic":
        return None
    key = os.getenv(f"{prefix}API_KEY", "")
    if provider == "anthropic":
        key = key or os.getenv("ANTHROPIC_API_KEY", "")
    return LLMConfig(
        provider=provider,
        base_url=os.getenv(f"{prefix}BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=key,
        model=model or ("claude-opus-5" if provider == "anthropic" else ""),
    )


def load_settings() -> Settings:
    return Settings(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        llm=_llm("LLM_") or LLMConfig(),
        llm_fallback=_llm("FALLBACK_LLM_"),
        llm_cooldown_sec=_int("LLM_COOLDOWN_SEC", 600),
        max_steps_per_question=_int("MAX_STEPS_PER_QUESTION", 12),
        daily_steps_per_user=_int("DAILY_STEPS_PER_USER", 50),
        admin_ids=tuple(int(x) for x in _list("ADMIN_IDS")),
        exchanges=tuple(
            _list("EXCHANGES", "binance,bybit,okx,bitget,gate,mexc,kucoin,bingx,hyperliquid")
        ),
        coingecko_api_key=os.getenv("COINGECKO_API_KEY", ""),
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        cryptopanic_api_key=os.getenv("CRYPTOPANIC_API_KEY", ""),
        twitterapi_io_key=os.getenv("TWITTERAPI_IO_KEY", ""),
        x_accounts=tuple(a.lstrip("@") for a in _list("X_ACCOUNTS", DEFAULT_X_ACCOUNTS)),
        timezone=os.getenv("TIMEZONE", "Europe/Kyiv"),
        db_path=os.getenv("DB_PATH", "swarm.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        risk=RiskRules(
            min_rr=_float("MIN_RR", 1.3),
            min_stop_pct_major=_float("MIN_STOP_PCT_MAJOR", 1.5),
            min_stop_pct_alt=_float("MIN_STOP_PCT_ALT", 3.0),
            min_stop_atr_mult=_float("MIN_STOP_ATR_MULT", 1.5),
            majors=tuple(s.upper() for s in _list("MAJORS", "BTC,ETH")),
        ),
    )
