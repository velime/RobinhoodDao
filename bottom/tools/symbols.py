"""Ticker normalization."""

from __future__ import annotations

import re

_QUOTES = ("USDT", "USDC", "USD", "BUSD", "FDUSD")
# Perp contracts that are quoted per 1000/1M tokens on some exchanges
MULTIPLIER_PREFIXES = {"1000": 1000, "10000": 10_000, "1000000": 1_000_000, "1M": 1_000_000}


def normalize_base(raw: str) -> str:
    """'$hype', 'HYPEUSDT', 'hype/usdt', 'HYPE-USDT-SWAP' -> 'HYPE'."""
    s = raw.strip().upper().lstrip("$#")
    s = re.split(r"[/:\-_ ]", s)[0] if re.search(r"[/:\-_ ]", s) else s
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            s = s[: -len(q)]
            break
    return s


def strip_multiplier(base: str) -> tuple[str, int]:
    """'1000PEPE' -> ('PEPE', 1000)."""
    for p, m in sorted(MULTIPLIER_PREFIXES.items(), key=lambda kv: -len(kv[0])):
        if base.startswith(p) and len(base) > len(p) and not base[len(p)].isdigit():
            return base[len(p):], m
    return base, 1
