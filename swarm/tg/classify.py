"""Decide whether a Telegram channel is about crypto, from its title, bio and posts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# Crypto vocabulary (RU + EN). Word stems — matched case-insensitively.
_TERMS = [
    r"крипт", r"биткоин", r"битк", r"\bbtc\b", r"\beth\b", r"эфир", r"\bsol\b", r"солан", r"solana",
    r"токен", r"монет", r"альт", r"шорт", r"лонг", r"\bперп", r"фьюч", r"фандинг", r"funding",
    r"ликвид", r"тейк", r"\bстоп", r"бирж", r"binance", r"bybit", r"\bokx\b", r"mexc", r"bitget",
    r"\bgate\b", r"\bdex\b", r"\bcex\b", r"аирдроп", r"эйрдроп", r"airdrop", r"\bдроп", r"ретродроп",
    r"\bнод", r"ончейн", r"onchain", r"on-chain", r"кошел", r"wallet", r"мемкоин", r"memecoin", r"\bмем",
    r"памп", r"\bpump", r"дамп", r"листинг", r"listing", r"лаунч", r"launch", r"defi", r"\bnft",
    r"стейк", r"stak", r"фарм", r"\bfarm", r"web3", r"трейд", r"trad", r"сигнал", r"\bколл", r"\bcall",
    r"флип", r"\bflip", r"\bape\b", r"degen", r"деген", r"\bиксы\b", r"\bx\d{2,3}\b", r"пресейл", r"presale",
    r"блокчейн", r"blockchain", r"usdt", r"usdc", r"hyperliquid", r"pump\.fun", r"gmgn", r"axiom",
    r"photon", r"dexscreener", r"\bton\b", r"\btron\b", r"\bbsc\b", r"\bbase\b", r"арбитраж", r"скальп",
    r"scalp", r"\bоi\b", r"\boi\b", r"\bentry\b", r"\bвход", r"\btp\d?\b", r"\bsl\b", r"\bdao\b",
    r"\bкит", r"whale", r"смарт.?мани", r"smart money", r"rekt", r"ликвидац", r"плеч", r"leverage",
]
_TERM_RE = re.compile("|".join(_TERMS), re.I)
_CASHTAG_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
_CONTRACT_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}(?:pump)?\b")
_LINK_RE = re.compile(r"(dexscreener|gmgn|pump\.fun|axiom|photon|birdeye|coinmarketcap|coingecko|tradingview)", re.I)

CRYPTO = "crypto"
NOT_CRYPTO = "not_crypto"
REVIEW = "review"
UNAVAILABLE = "unavailable"


@dataclass
class Verdict:
    label: str
    post_ratio: float  # share of posts with crypto markers
    header_hits: int  # crypto markers in title + bio
    posts: int
    top_terms: list[str] = field(default_factory=list)
    reason: str = ""


def post_is_crypto(text: str) -> bool:
    return bool(_TERM_RE.search(text) or _CASHTAG_RE.search(text) or _CONTRACT_RE.search(text) or _LINK_RE.search(text))


def classify(title: str, about: str, posts: list[str]) -> Verdict:
    header = f"{title}\n{about}"
    header_hits = len(_TERM_RE.findall(header))
    texts = [p for p in posts if p and p.strip()]
    hits = [p for p in texts if post_is_crypto(p)]
    ratio = len(hits) / len(texts) if texts else 0.0
    terms = Counter(m.group(0).lower() for p in texts for m in _TERM_RE.finditer(p))
    top = [t for t, _ in terms.most_common(6)]

    if not texts:
        label = CRYPTO if header_hits >= 2 else REVIEW
        reason = "нет текстовых постов — решение по названию/описанию"
    elif ratio >= 0.35 or (ratio >= 0.2 and header_hits >= 1):
        label, reason = CRYPTO, f"{round(ratio * 100)}% постов про крипту"
    elif ratio < 0.1 and header_hits == 0:
        label, reason = NOT_CRYPTO, f"только {round(ratio * 100)}% постов с крипто-лексикой"
    else:
        label, reason = REVIEW, f"пограничный: {round(ratio * 100)}% постов, {header_hits} маркеров в описании"
    return Verdict(label, round(ratio, 3), header_hits, len(texts), top, reason)


def extract_cashtags(text: str) -> list[str]:
    return [m.upper() for m in _CASHTAG_RE.findall(text)]


def parse_channel_list(text: str) -> list[str]:
    """Links / @names / names → unique usernames (case-insensitive, order kept)."""
    seen, out = set(), []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.search(r"(?:t\.me/|telegram\.me/|@)?([A-Za-z0-9_]{4,32})/?$", line)
        if not m or "+" in line or "joinchat" in line:
            continue
        name = m.group(1)
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out
