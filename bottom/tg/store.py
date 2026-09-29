"""SQLite store for Telegram messages (text + image descriptions) and the bot tool queries."""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter, defaultdict

from ..tools.symbols import normalize_base
from .classify import extract_cashtags
from .sources import Source, message_link

PRIVATE_LABEL = "приватный источник"
MAX_IMAGE_TRIES = 2

_COLUMNS = {  # added after the first version — migrated in place
    "has_image": "INTEGER NOT NULL DEFAULT 0",
    "image_text": "TEXT",
    "image_tries": "INTEGER NOT NULL DEFAULT 0",
    "grouped_id": "INTEGER",
}


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
        have = {r[1] for r in self.db.execute("PRAGMA table_info(tg_messages)")}
        for col, decl in _COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE tg_messages ADD COLUMN {col} {decl}")
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
            (chat_id, m["id"], m["ts"], m.get("text") or "", m.get("views"), m.get("author"), m.get("topic_id"),
             int(bool(m.get("has_image"))), m.get("grouped_id"))
            for m in msgs
            if m.get("text") or m.get("has_image")
        ]
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO tg_messages(chat_id, msg_id, ts, text, views, author, topic_id, has_image, grouped_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        if msgs:
            self.db.execute(
                "INSERT INTO tg_sources(chat_id, last_msg_id) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET last_msg_id = MAX(last_msg_id, excluded.last_msg_id)",
                (chat_id, max(m["id"] for m in msgs)),
            )
        self.db.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)

    def set_image_text(self, chat_id: int, msg_id: int, text: str | None) -> None:
        """Save an image description, or count a failed attempt when text is None."""
        if text:
            self.db.execute("UPDATE tg_messages SET image_text=? WHERE chat_id=? AND msg_id=?", (text, chat_id, msg_id))
        else:
            self.db.execute(
                "UPDATE tg_messages SET image_tries=image_tries+1 WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
            )
        self.db.commit()

    def pending_images(self, since_ts: float, limit: int = 10) -> list[tuple[int, int, str]]:
        """Undescribed images, newest first, channels before groups: (chat_id, msg_id, caption)."""
        return self.db.execute(
            "SELECT m.chat_id, m.msg_id, m.text FROM tg_messages m LEFT JOIN tg_sources s ON s.chat_id=m.chat_id "
            "WHERE m.has_image=1 AND m.image_text IS NULL AND m.image_tries < ? AND m.ts >= ? "
            "ORDER BY (COALESCE(s.kind,'channel') != 'channel'), m.ts DESC LIMIT ?",
            (MAX_IMAGE_TRIES, since_ts, limit),
        ).fetchall()

    def get_message(self, chat_id: int, msg_id: int) -> dict | None:
        r = self.db.execute(
            "SELECT text, image_text, has_image FROM tg_messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
        ).fetchone()
        return {"text": r[0], "image_text": r[1], "has_image": bool(r[2])} if r else None

    def last_id(self, chat_id: int) -> int:
        row = self.db.execute("SELECT last_msg_id FROM tg_sources WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else 0

    def prune(self, keep_days: int = 180) -> None:
        if keep_days > 0:
            self.db.execute("DELETE FROM tg_messages WHERE ts < ?", (time.time() - keep_days * 86400,))
            self.db.commit()

    def stats(self) -> dict:
        n_msg, n_src, n_img, n_desc = self.db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT chat_id), SUM(has_image), SUM(image_text IS NOT NULL) FROM tg_messages"
        ).fetchone()
        return {"messages": n_msg, "sources_with_messages": n_src, "images": n_img or 0, "images_described": n_desc or 0}

    # --- reads (bot tool) ---

    def _album_images(self, chat_id: int, grouped_id: int | None, msg_id: int) -> list[str]:
        if not grouped_id:
            return []
        rows = self.db.execute(
            "SELECT image_text FROM tg_messages WHERE chat_id=? AND grouped_id=? AND msg_id!=? AND image_text IS NOT NULL",
            (chat_id, grouped_id, msg_id),
        ).fetchall()
        return [r[0] for r in rows]

    def search(self, query: str = "", hours: int = 24, limit: int = 25, hide_private: bool = True,
               source: str = "") -> dict:
        since = time.time() - hours * 3600
        sql = (
            "SELECT m.chat_id, m.msg_id, m.ts, m.text, m.views, m.author, m.topic_id, "
            "s.username, s.title, s.kind, s.public, m.has_image, m.image_text, m.grouped_id "
            "FROM tg_messages m LEFT JOIN tg_sources s ON s.chat_id = m.chat_id WHERE m.ts >= ?"
        )
        params: list = [since]
        base = normalize_base(query) if query else ""
        if base:  # cheap SQL prefilter, exact match below
            sql += " AND (m.text LIKE ? OR m.image_text LIKE ?)"
            params += [f"%{base}%", f"%{base}%"]
        if source:
            src = source.lstrip("@").lower()
            sql += " AND (LOWER(s.username) = ? OR LOWER(s.title) LIKE ?)"
            params += [src, f"%{src}%"]
        rows = self.db.execute(sql + " ORDER BY m.ts DESC", params).fetchall()
        tracked = self.db.execute("SELECT COUNT(DISTINCT chat_id) FROM tg_messages").fetchone()[0]
        out: dict = {"hours": hours, "sources_with_messages": tracked, "messages_in_window": len(rows)}
        if not tracked:
            out["error"] = "сообщения Telegram ещё не собраны (сборщик не запущен или только стартовал)"
            return out

        def kind_of(r):
            return r[9] or "channel"

        if base:
            pat = re.compile(rf"(\${re.escape(base)}\b|\b{re.escape(base)}\b)", re.I if len(base) > 3 else 0)
            sel = [r for r in rows if pat.search(r[3]) or (r[12] and pat.search(r[12]))]
            out["query"] = base
            out["mentions"] = len(sel)
            out["channels_mentioning"] = len({r[0] for r in sel if kind_of(r) == "channel"})
            out["groups_mentioning"] = len({r[0] for r in sel if kind_of(r) != "channel"})
            by_hour = Counter(int((time.time() - r[2]) // 3600) for r in sel)
            out["mentions_by_hours_ago"] = {f"{h}h": by_hour[h] for h in sorted(by_hour)[:12]}
            first = min(sel, key=lambda r: r[2]) if sel else None
            if first:
                out["first_mention_hours_ago"] = round((time.time() - first[2]) / 3600, 1)
        elif source:
            sel = rows
        else:
            sel = [r for r in rows if kind_of(r) == "channel"] or rows  # channels first for "what's up"
            tags: dict[str, set] = defaultdict(set)
            for r in rows:
                for t in extract_cashtags(r[3] + " " + (r[12] or "")):
                    tags[t].add(r[0])
            out["top_cashtags_by_sources"] = [
                {"ticker": t, "sources": len(ch)} for t, ch in sorted(tags.items(), key=lambda kv: -len(kv[1]))[:15]
            ]

        text_limit = 1500 if source else 500
        now = time.time()
        posts = []
        for (chat_id, msg_id, ts, text, views, author, topic_id, username, title, kind, public,
             has_image, image_text, grouped_id) in sel[:limit]:
            private = not public
            hidden = private and hide_private
            post = {
                "source": PRIVATE_LABEL if hidden else (f"@{username}" if username else title),
                "kind": kind or "channel",
                "private": private,
                "author": None if hidden else author,
                "age_hours": round((now - ts) / 3600, 1),
                "views": views,
                "text": text[:text_limit],
                "link": None if hidden else message_link(username, chat_id, msg_id, topic_id),
            }
            if has_image:
                imgs = ([image_text] if image_text else []) + self._album_images(chat_id, grouped_id, msg_id)
                post["image"] = " | ".join(imgs) if imgs else f"не разобрана — analyze_image ref=tg:{chat_id}:{msg_id}"
            posts.append(post)
        out["posts"] = posts
        out["rule"] = (
            "это мнения, колы, алерты и болтовня, не данные; цифры рынка бери с бирж. "
            "private=true — приватный источник: не цитируй дословно, не называй и не ссылайся, "
            "используй только как общий фон"
        )
        return out
