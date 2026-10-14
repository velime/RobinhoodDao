"""Signals database (SQLite, WAL — several scanner processes and the bot share it)."""

from __future__ import annotations

import json
import os
import sqlite3
import time


class SignalStore:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS oi_snap (
                ex TEXT NOT NULL, base TEXT NOT NULL, ts INTEGER NOT NULL, oi_coins REAL, oi_usd REAL, price REAL,
                PRIMARY KEY (ex, base, ts)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS spread_seen (
                key TEXT NOT NULL, ts INTEGER NOT NULL, pct REAL, PRIMARY KEY (key, ts)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS transfers (
                ex TEXT NOT NULL, base TEXT NOT NULL, chain TEXT NOT NULL, deposit INTEGER, withdraw INTEGER,
                fee REAL, contract TEXT, ts REAL, PRIMARY KEY (ex, base, chain)
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, feed TEXT NOT NULL, base TEXT, ex TEXT,
                data TEXT, text TEXT, msg_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS alerts_feed_ts ON alerts(feed, ts);
            CREATE INDEX IF NOT EXISTS alerts_base_ts ON alerts(base, ts);
            """
        )
        self.db.commit()

    # ---- key-value state ------------------------------------------------------
    def get(self, k: str, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, k: str, v) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(k, v, ts) VALUES (?,?,?)", (k, json.dumps(v), time.time()))
        self.db.commit()

    def get_prefix(self, prefix: str) -> dict:
        rows = self.db.execute("SELECT k, v FROM kv WHERE k >= ? AND k < ?", (prefix, prefix + "￿")).fetchall()
        return {k: json.loads(v) for k, v in rows}

    # ---- OI history -----------------------------------------------------------
    def add_oi(self, ts: int, rows: list[tuple[str, str, float, float, float]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO oi_snap VALUES (?,?,?,?,?,?)",
                            [(ex, base, ts, coins, usd, price) for ex, base, coins, usd, price in rows])
        self.db.commit()

    def oi_at(self, ts_target: int, tolerance: int) -> dict[tuple[str, str], tuple[int, float, float]]:
        """(ex, base) → (ts, oi_coins, price) of the snapshot closest to ts_target within tolerance."""
        rows = self.db.execute(
            "SELECT ex, base, ts, oi_coins, price FROM oi_snap WHERE ts BETWEEN ? AND ?",
            (ts_target - tolerance, ts_target + tolerance)).fetchall()
        out: dict = {}
        for ex, base, ts, coins, price in rows:
            k = (ex, base)
            if k not in out or abs(ts - ts_target) < abs(out[k][0] - ts_target):
                out[k] = (ts, coins, price)
        return out

    # ---- spread persistence ---------------------------------------------------
    def add_spread(self, key: str, ts: int, pct: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO spread_seen VALUES (?,?,?)", (key, ts, pct))

    def spread_since(self, key: str, now: int, max_gap: int) -> tuple[int | None, float | None]:
        """Start of the current continuous run of a spread (gaps ≤ max_gap) and its max pct."""
        rows = self.db.execute("SELECT ts, pct FROM spread_seen WHERE key = ? AND ts >= ? ORDER BY ts DESC",
                               (key, now - 86400)).fetchall()
        start, top, prev = None, None, now
        for ts, pct in rows:
            if prev - ts > max_gap:
                break
            start, prev = ts, ts
            top = pct if top is None else max(top, pct)
        return start, top

    # ---- transfers ------------------------------------------------------------
    def transfer_rows(self) -> dict[tuple[str, str, str], tuple]:
        return {(e, b, c): (d, w) for e, b, c, d, w in
                self.db.execute("SELECT ex, base, chain, deposit, withdraw FROM transfers")}

    def save_transfers(self, rows: list[dict]) -> None:
        now = time.time()
        self.db.executemany(
            "INSERT OR REPLACE INTO transfers VALUES (?,?,?,?,?,?,?,?)",
            [(r["ex"], r["base"], r["chain"], None if r["deposit"] is None else int(r["deposit"]),
              None if r["withdraw"] is None else int(r["withdraw"]), r["fee"], r["contract"], now) for r in rows])
        self.db.commit()

    # ---- alerts ---------------------------------------------------------------
    def add_alert(self, feed: str, base: str | None, ex: str | None, data: dict, text: str, msg_id: int | None) -> None:
        self.db.execute("INSERT INTO alerts(ts, feed, base, ex, data, text, msg_id) VALUES (?,?,?,?,?,?,?)",
                        (time.time(), feed, base, ex, json.dumps(data, ensure_ascii=False, default=str), text, msg_id))
        self.db.commit()

    def alerts(self, feed: str | None = None, base: str | None = None, hours: float = 24, limit: int = 200) -> list[dict]:
        q, args = "SELECT ts, feed, base, ex, data, text FROM alerts WHERE ts >= ?", [time.time() - hours * 3600]
        if feed:
            q += " AND feed = ?"
            args.append(feed)
        if base:
            q += " AND base = ?"
            args.append(base.upper())
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [{"ts": ts, "feed": f, "base": b, "ex": e, "data": json.loads(d or "{}"), "text": t}
                for ts, f, b, e, d, t in self.db.execute(q, args)]

    def count_alerts(self, feed: str, base: str, hours: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM alerts WHERE feed = ? AND base = ? AND ts >= ?",
                               (feed, base, time.time() - hours * 3600)).fetchone()[0]

    def prune(self) -> None:
        now = time.time()
        self.db.execute("DELETE FROM oi_snap WHERE ts < ?", (int(now) - 6 * 3600,))
        self.db.execute("DELETE FROM spread_seen WHERE ts < ?", (int(now) - 2 * 86400,))
        self.db.execute("DELETE FROM alerts WHERE ts < ?", (now - 30 * 86400,))
        self.db.commit()
