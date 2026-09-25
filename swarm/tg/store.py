"""SQLite store for Telegram channel posts and the queries the bot tool runs."""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter, defaultdict

from ..tools.symbols import normalize_base
from .classify import extract_cashtags


class TgStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tg_posts (
                channel TEXT NOT NULL, msg_id INTEGER NOT NULL, ts REAL NOT NULL,
                text TEXT NOT NULL, views INTEGER, forwards INTEGER,
                PRIMARY KEY (channel, msg_id)
            );
            CREATE INDEX IF NOT EXISTS tg_posts_ts ON tg_posts(ts);
            CREATE TABLE IF NOT EXISTS tg_channels (
                username TEXT PRIMARY KEY, title TEXT, last_msg_id INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        self.db.commit()

    # --- writes ---

    def add_posts(self, channel: str, posts: list[dict]) -> int:
        rows = [(channel, p["id"], p["ts"], p["text"], p.get("views"), p.get("forwards")) for p in posts if p.get("text")]
        self.db.executemany("INSERT OR IGNORE INTO tg_posts VALUES (?,?,?,?,?,?)", rows)
        if posts:
            self.db.execute(
                "INSERT INTO tg_channels(username, last_msg_id) VALUES (?, ?) "
                "ON CONFLICT(username) DO UPDATE SET last_msg_id = MAX(last_msg_id, excluded.last_msg_id)",
                (channel, max(p["id"] for p in posts)),
            )
        self.db.commit()
        return len(rows)

    def set_title(self, channel: str, title: str) -> None:
        self.db.execute(
            "INSERT INTO tg_channels(username, title) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET title=excluded.title",
            (channel, title),
        )
        self.db.commit()

    def last_id(self, channel: str) -> int:
        row = self.db.execute("SELECT last_msg_id FROM tg_channels WHERE username=?", (channel,)).fetchone()
        return row[0] if row else 0

    def prune(self, keep_days: int = 14) -> None:
        self.db.execute("DELETE FROM tg_posts WHERE ts < ?", (time.time() - keep_days * 86400,))
        self.db.commit()

    # --- reads (bot tool) ---

    def search(self, query: str = "", hours: int = 24, limit: int = 25) -> dict:
        since = time.time() - hours * 3600
        rows = self.db.execute(
            "SELECT channel, msg_id, ts, text, views FROM tg_posts WHERE ts >= ? ORDER BY ts DESC", (since,)
        ).fetchall()
        total_channels = self.db.execute("SELECT COUNT(DISTINCT channel) FROM tg_posts").fetchone()[0]
        out: dict = {"hours": hours, "channels_tracked": total_channels, "posts_in_window": len(rows)}
        if not total_channels:
            out["error"] = "посты Telegram-каналов ещё не собраны (сборщик не запущен или только стартовал)"
            return out

        if query:
            base = normalize_base(query)
            pat = re.compile(rf"(\${re.escape(base)}\b|\b{re.escape(base)}\b)", re.I if len(base) > 3 else 0)
            hits = [r for r in rows if pat.search(r[3])]
            out["query"] = base
            out["mentions"] = len(hits)
            out["channels_mentioning"] = len({r[0] for r in hits})
            by_hour = Counter(int((time.time() - r[2]) // 3600) for r in hits)
            out["mentions_by_hours_ago"] = {f"{h}h": by_hour[h] for h in sorted(by_hour)[:12]}
            sel = hits
        else:
            sel = rows
            tags: dict[str, set] = defaultdict(set)
            for ch, _mid, _ts, text, _v in rows:
                for t in extract_cashtags(text):
                    tags[t].add(ch)
            out["top_cashtags_by_channels"] = [
                {"ticker": t, "channels": len(chs)} for t, chs in sorted(tags.items(), key=lambda kv: -len(kv[1]))[:15]
            ]
        now = time.time()
        out["posts"] = [
            {
                "channel": f"@{ch}",
                "age_hours": round((now - ts) / 3600, 1),
                "views": views,
                "text": text[:400],
                "link": f"https://t.me/{ch}/{mid}",
            }
            for ch, mid, ts, text, views in sel[:limit]
        ]
        out["rule"] = "это мнения и колы авторов каналов, не данные; цифры рынка бери с бирж"
        return out
