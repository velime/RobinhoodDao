"""SQLite storage: dialog memory and daily step usage."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

HISTORY_TURNS = 6
MAX_STORED_CHARS = 3000


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS history (
                conv TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS history_conv ON history(conv, ts);
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER NOT NULL, day TEXT NOT NULL, steps INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
            """
        )
        self.db.commit()

    # --- dialog memory ---

    def history(self, conv: str, turns: int = HISTORY_TURNS) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT role, text FROM history WHERE conv=? ORDER BY ts DESC LIMIT ?", (conv, turns * 2)
        ).fetchall()
        rows.reverse()
        # must start with a user turn
        while rows and rows[0][0] != "user":
            rows.pop(0)
        return [(r, t) for r, t in rows]

    def add_turn(self, conv: str, question: str, answer: str) -> None:
        now = time.time()
        self.db.executemany(
            "INSERT INTO history(conv, role, text, ts) VALUES (?,?,?,?)",
            [(conv, "user", question[:MAX_STORED_CHARS], now), (conv, "assistant", answer[:MAX_STORED_CHARS], now + 0.001)],
        )
        self.db.commit()

    def clear(self, conv: str) -> None:
        self.db.execute("DELETE FROM history WHERE conv=?", (conv,))
        self.db.commit()

    # --- daily steps ---

    @staticmethod
    def _day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def steps_used(self, user_id: int) -> int:
        row = self.db.execute("SELECT steps FROM usage WHERE user_id=? AND day=?", (user_id, self._day())).fetchone()
        return row[0] if row else 0

    def add_steps(self, user_id: int, n: int) -> None:
        if n <= 0:
            return
        self.db.execute(
            "INSERT INTO usage(user_id, day, steps) VALUES (?,?,?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET steps = steps + excluded.steps",
            (user_id, self._day(), n),
        )
        self.db.commit()
