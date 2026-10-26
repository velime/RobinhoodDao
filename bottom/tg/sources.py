"""Registry of Telegram sources (channels, groups, forum groups).

    channels/sources.json   public channels — part of the repository
    channels/private.json   private channels and chats of your account — local only, never committed

Both are read together; a source stays in the file it came from. Identity is the numeric chat id (-100…):
usernames change and disappear, ids do not. Every source is read through the user's account, which is a
member of all of them.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

HELP = (
    "Telegram-источники Bottom (читаются через твой аккаунт, он в них состоит). channels/sources.json — "
    "публичные каналы, часть репозитория; channels/private.json — свои приватные каналы и чаты, в git не попадает. "
    "enabled — читать или нет; manual: true — scan не будет менять enabled; verdict заполняет python -m bottom tg scan."
)
KINDS = ("channel", "group", "forum")
PRIVATE_FILE = "private.json"


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
    origin: str = field(default="", compare=False)  # the file it was read from; not saved

    @property
    def label(self) -> str:
        return f"@{self.username}" if self.username else self.title or str(self.id)


def load_sources(path: str) -> list[Source]:
    if not os.path.exists(path):
        return []
    data = json.load(open(path, encoding="utf-8"))
    out, seen = [], set()
    for raw in data.get("sources", []):
        known = {k: raw[k] for k in Source.__dataclass_fields__ if k in raw and k not in ("extra", "origin")}
        src = Source(**known)
        src.id = int(src.id)
        src.origin = path
        src.extra = {k: v for k, v in raw.items() if k not in Source.__dataclass_fields__}
        if src.id not in seen:
            seen.add(src.id)
            out.append(src)
    return out


def private_path(path: str) -> str:
    return os.path.join(os.path.dirname(path) or ".", PRIVATE_FILE)


def load_all(path: str) -> list[Source]:
    """The public list plus the local private one (an id listed in both counts once, the public entry wins)."""
    out = load_sources(path)
    seen = {s.id for s in out}
    out += [s for s in load_sources(private_path(path)) if s.id not in seen]
    return out


def save_all(path: str, sources: list[Source]) -> None:
    """Each source back to its own file. Sources found in the account (scan --include-dialogs) go to the private
    file — the public list changes only by hand."""
    priv = private_path(path)
    public, private = [], []
    for s in sources:
        (public if s.origin == path else private).append(s)
    save_sources(path, public)
    if private or os.path.exists(priv):
        save_sources(priv, private)


def save_sources(path: str, sources: list[Source]) -> None:
    rows = []
    for s in sources:
        d = asdict(s)
        d.pop("origin", None)
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
