"""SQLite store for Telegram messages and the queries the bot tool runs."""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter, defaultdict

from ..tools.symbols import normalize_base
from .classify import extract_cashtags
from .sources import Source, message_link

PRIVATE_LABEL = "приватный источник"


class TgStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tg_sources (
                chat_id INTEGER PRIMARY KEY, username TEXT, title TEXT, kind TEXT,
                public INTEGER, last_msg_id INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tg_messages (
                chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, ts REAL NOT NULL,
                text TEXT NOT NULL, views INTEGER, author TEXT, topic_id INTEGER,
                PRIMARY KEY (chat_id, msg_id)
            );
            CREATE INDEX IF NOT EXISTS tg_messages_ts ON tg_messages(ts);
            """
        )
        self.db.commit()

    # --- sources ---

    def sync_sources(self, sources: list[Source]) -> None:
        self.db.executemany(
            "INSERT INTO tg_sources(chat_id, username, title, kind, public) VALUES (?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, title=excluded.title, "
            "kind=excluded.kind, public=excluded.public",
            [(s.id, s.username, s.title, s.kind, int(s.public)) for s in sources],
        )
        self.db.commit()

    # --- writes ---

    def add_messages(self, chat_id: int, msgs: list[dict]) -> int:
        rows = [
            (chat_id, m["id"], m["ts"], m["text"], m.get("views"), m.get("author"), m.get("topic_id"))
            for m in msgs
            if m.get("text")
        ]
        cur = self.db.executemany("INSERT OR IGNORE INTO tg_messages VALUES (?,?,?,?,?,?,?)", rows)
        if msgs:
            self.db.execute(
                "INSERT INTO tg_sources(chat_id, last_msg_id) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_msg_id = MAX(last_msg_id, excluded.last_msg_id)",
                (chat_id, max(m["id"] for m in msgs)),
            )
        self.db.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)

    def last_id(self, chat_id: int) -> int:
        row = self.db.execute("SELECT last_msg_id FROM tg_sources WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else 0

    def prune(self, keep_days: int = 14) -> None:
        self.db.execute("DELETE FROM tg_messages WHERE ts < ?", (time.time() - keep_days * 86400,))
        self.db.commit()

    def stats(self) -> dict:
        n_msg, n_src = self.db.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM tg_messages").fetchone()
        return {"messages": n_msg, "sources_with_messages": n_src}

    # --- reads (bot tool) ---

    def search(self, query: str = "", hours: int = 24, limit: int = 25, hide_private: bool = True) -> dict:
        since = time.time() - hours * 3600
        rows = self.db.execute(
            "SELECT m.chat_id, m.msg_id, m.ts, m.text, m.views, m.author, m.topic_id, "
            "s.username, s.title, s.kind, s.public "
            "FROM tg_messages m LEFT JOIN tg_sources s ON s.chat_id = m.chat_id "
            "WHERE m.ts >= ? ORDER BY m.ts DESC",
            (since,),
        ).fetchall()
        tracked = self.db.execute("SELECT COUNT(DISTINCT chat_id) FROM tg_messages").fetchone()[0]
        out: dict = {"hours": hours, "sources_with_messages": tracked, "messages_in_window": len(rows)}
        if not tracked:
            out["error"] = "сообщения Telegram ещё не собраны (сборщик не запущен или только стартовал)"
            return out

        def kind_of(r):
            return r[9] or "channel"

        if query:
            base = normalize_base(query)
            pat = re.compile(rf"(\${re.escape(base)}\b|\b{re.escape(base)}\b)", re.I if len(base) > 3 else 0)
            sel = [r for r in rows if pat.search(r[3])]
            out["query"] = base
            out["mentions"] = len(sel)
            out["channels_mentioning"] = len({r[0] for r in sel if kind_of(r) == "channel"})
            out["groups_mentioning"] = len({r[0] for r in sel if kind_of(r) != "channel"})
            by_hour = Counter(int((time.time() - r[2]) // 3600) for r in sel)
            out["mentions_by_hours_ago"] = {f"{h}h": by_hour[h] for h in sorted(by_hour)[:12]}
        else:
            sel = [r for r in rows if kind_of(r) == "channel"] or rows  # channels first for "what's up"
            tags: dict[str, set] = defaultdict(set)
            for r in rows:
                for t in extract_cashtags(r[3]):
                    tags[t].add(r[0])
            out["top_cashtags_by_sources"] = [
                {"ticker": t, "sources": len(ch)} for t, ch in sorted(tags.items(), key=lambda kv: -len(kv[1]))[:15]
            ]

        now = time.time()
        posts = []
        for chat_id, msg_id, ts, text, views, author, topic_id, username, title, kind, public in sel[:limit]:
            private = not public
            hidden = private and hide_private
            posts.append(
                {
                    "source": PRIVATE_LABEL if hidden else (f"@{username}" if username else title),
                    "kind": kind or "channel",
                    "private": private,
                    "author": None if hidden else author,
                    "age_hours": round((now - ts) / 3600, 1),
                    "views": views,
                    "text": text[:400],
                    "link": None if hidden else message_link(username, chat_id, msg_id, topic_id),
                }
            )
        out["posts"] = posts
        out["rule"] = (
            "это мнения, колы и болтовня, не данные; цифры рынка бери с бирж. "
            "private=true — приватный источник: не цитируй дословно, не называй и не ссылайся, "
            "используй только как общий фон"
        )
        return out
