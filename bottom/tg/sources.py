"""Registry of Telegram sources (channels, groups, forum groups): channels/sources.json.

Identity is the numeric chat id (-100…): usernames change and disappear,
ids do not. Every source is read through the user's account, which is a
member of all of them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

HELP = (
    "Источники Telegram. Все читаются через твой аккаунт (он в них состоит). "
    "enabled — читать или нет; manual: true — scan не будет менять enabled; "
    "verdict заполняет python -m bottom.tg scan."
)
KINDS = ("channel", "group", "forum")


@dataclass
class Source:
    id: int
    title: str = ""
    username: str | None = None
    kind: str = "channel"  # channel | group | forum (group with topics)
    public: bool = False
    enabled: bool = True
    verdict: str | None = None  # crypto | not_crypto | review | unavailable
    manual: bool = False
    note: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else self.title or str(self.id)


def load_sources(path: str) -> list[Source]:
    if not os.path.exists(path):
        return []
    data = json.load(open(path, encoding="utf-8"))
    out, seen = [], set()
    for raw in data.get("sources", []):
        known = {k: raw[k] for k in Source.__dataclass_fields__ if k in raw and k != "extra"}
        src = Source(**known)
        src.id = int(src.id)
        src.extra = {k: v for k, v in raw.items() if k not in Source.__dataclass_fields__}
        if src.id not in seen:
            seen.add(src.id)
            out.append(src)
    return out


def save_sources(path: str, sources: list[Source]) -> None:
    rows = []
    for s in sources:
        d = asdict(s)
        d.update(d.pop("extra") or {})
        rows.append(d)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"_help": HELP, "sources": rows}, f, ensure_ascii=False, indent=1)
        f.write("\n")


def internal_id(chat_id: int) -> int:
    """-1001234567890 → 1234567890 (the id used in t.me/c/… links)."""
    s = str(abs(chat_id))
    return int(s[3:]) if s.startswith("100") and len(s) > 10 else int(s)


def message_link(username: str | None, chat_id: int, msg_id: int, topic_id: int | None = None) -> str:
    if username:
        return f"https://t.me/{username}/{msg_id}"
    # private: opens only for members
    mid = f"{topic_id}/{msg_id}" if topic_id else str(msg_id)
    return f"https://t.me/c/{internal_id(chat_id)}/{mid}"
