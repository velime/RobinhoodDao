"""Journal: published setups and their outcomes, users' /wrong feedback, lessons and their evidence (SQLite)."""

from __future__ import annotations

import json
import sqlite3
import time

MAX_ACTIVE_LESSONS = 30
PROMOTE_MIN_CASES = 2
PROMOTE_MIN_DAYS = 2
CANDIDATE_TTL_DAYS = 30
ACTIVE_TTL_DAYS = 60

class Journal:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS bot_messages (
                chat_id INTEGER, message_id INTEGER, conv TEXT, question TEXT, answer TEXT, created REAL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS setups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, conv TEXT, chat_id INTEGER, message_id INTEGER,
                symbol TEXT, direction TEXT, entry_low REAL, entry_high REAL, stop REAL, targets TEXT,
                rr_tp1 REAL, question TEXT, answer TEXT,
                status TEXT DEFAULT 'open', outcome TEXT, r REAL, mfe_pct REAL, mae_pct REAL, closed_at REAL,
                reviewed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, user_id INTEGER, chat_id INTEGER,
                message_id INTEGER, text TEXT, question TEXT, answer TEXT, reviewed INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL, text TEXT, kind TEXT, active INTEGER DEFAULT 1,
                retired_reason TEXT
            );
            """
        )
        have = {r[1] for r in self.db.execute("PRAGMA table_info(lessons)")}
        for col, decl in (("status", "TEXT"), ("evidence", "TEXT DEFAULT '[]'"), ("last_confirmed", "REAL"),
                          ("contest_reason", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE lessons ADD COLUMN {col} {decl}")
        self.db.execute("UPDATE lessons SET status = CASE WHEN active=1 THEN 'active' ELSE 'retired' END "
                        "WHERE status IS NULL")
        self.db.commit()

    # --- recording ---

    def record_message(self, chat_id: int, message_id: int, conv: str, question: str, answer: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO bot_messages VALUES (?,?,?,?,?,?)",
            (chat_id, message_id, conv, question[:2000], answer[:4000], time.time()),
        )
        self.db.commit()

    def record_setups(self, conv: str, chat_id: int, message_id: int, question: str, answer: str,
                      setups: list[dict]) -> list[int]:
        ids = []
        for r in setups:
            zone = r.get("entry_zone") or [None, None]
            cur = self.db.execute(
                "INSERT INTO setups(created, conv, chat_id, message_id, symbol, direction, entry_low, entry_high, "
                "stop, targets, rr_tp1, question, answer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), conv, chat_id, message_id, r["symbol"].upper(), r["direction"], zone[0], zone[1],
                 r["stop"], json.dumps([t["target"] for t in r["targets"]]), r.get("rr_tp1"),
                 question[:1500], answer[:3000]),
            )
            ids.append(cur.lastrowid)
        self.db.commit()
        return ids

    def add_feedback(self, chat_id: int, reply_to_message_id: int, user_id: int, text: str) -> bool:
        row = self.db.execute(
            "SELECT question, answer FROM bot_messages WHERE chat_id=? AND message_id=?", (chat_id, reply_to_message_id)
        ).fetchone()
        if not row:
            return False
        self.db.execute(
            "INSERT INTO feedback(created, user_id, chat_id, message_id, text, question, answer) VALUES (?,?,?,?,?,?,?)",
            (time.time(), user_id, chat_id, reply_to_message_id, text[:1000], row[0], row[1]),
        )
        self.db.commit()
        return True

    # --- lessons: candidate → active (earned), contested, expired, retired ---

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def add_lesson(self, text: str, kind: str = "manual") -> int:
        """An admin lesson is adopted by a human → active immediately."""
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO lessons(created, text, kind, status, evidence, last_confirmed, active) "
            "VALUES (?,?,?,?,?,?,1)",
            (now, text.strip(), kind, "active", "[]", now),
        )
        self.db.commit()
        return cur.lastrowid

    def add_candidate(self, text: str, refs: list[str], days: dict[str, str] | None = None) -> int:
        """days: when each case happened (reference calls) — otherwise the case counts as seen today."""
        ev = [{"ref": r, "day": (days or {}).get(r, self._today())} for r in refs]
        cur = self.db.execute(
            "INSERT INTO lessons(created, text, kind, status, evidence, last_confirmed, active) "
            "VALUES (?,?,?,?,?,?,0)",
            (time.time(), text.strip(), "auto", "candidate", json.dumps(ev), time.time()),
        )
        self.db.commit()
        return cur.lastrowid

    def _lesson(self, lesson_id: int) -> dict | None:
        r = self.db.execute("SELECT id, text, kind, status, evidence FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        return {"id": r[0], "text": r[1], "kind": r[2], "status": r[3], "evidence": json.loads(r[4] or "[]")} if r else None

    @staticmethod
    def promotable(evidence: list[dict]) -> bool:
        refs = {e["ref"] for e in evidence}
        days = {e["day"] for e in evidence}
        return len(refs) >= PROMOTE_MIN_CASES and len(days) >= PROMOTE_MIN_DAYS

    def confirm(self, lesson_id: int, refs: list[str], days: dict[str, str] | None = None) -> str | None:
        """Add supporting cases; promote a candidate once it has earned it. Returns new status."""
        les = self._lesson(lesson_id)
        if les is None or les["status"] in ("retired", "expired") or not refs:
            return None
        known = {e["ref"] for e in les["evidence"]}
        ev = les["evidence"] + [{"ref": r, "day": (days or {}).get(r, self._today())} for r in refs if r not in known]
        status = les["status"]
        if status == "candidate" and self.promotable(ev):
            status = "active"
        self.db.execute(
            "UPDATE lessons SET evidence=?, last_confirmed=?, status=?, active=? WHERE id=?",
            (json.dumps(ev), time.time(), status, int(status in ("active", "contested")), lesson_id),
        )
        self.db.commit()
        return status

    def contest(self, lesson_id: int, reason: str) -> bool:
        """Evidence contradicts a lesson: mark it, never delete — a human decides."""
        cur = self.db.execute(
            "UPDATE lessons SET status='contested', contest_reason=? WHERE id=? AND status IN ('active','candidate')",
            (reason[:300], lesson_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def adopt(self, lesson_id: int) -> bool:
        """Admin adopts a candidate or resolves a contested lesson in its favour."""
        cur = self.db.execute(
            "UPDATE lessons SET status='active', active=1, contest_reason=NULL, last_confirmed=? "
            "WHERE id=? AND status IN ('candidate','contested')",
            (time.time(), lesson_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def retire_lesson(self, lesson_id: int, reason: str = "") -> bool:
        cur = self.db.execute(
            "UPDATE lessons SET status='retired', active=0, retired_reason=? WHERE id=? AND status!='retired'",
            (reason, lesson_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def apply_forgetting(self, now: float | None = None) -> int:
        """TTL for candidates and unconfirmed auto lessons + capacity bound on active ones."""
        now = now or time.time()
        n = self.db.execute(
            "UPDATE lessons SET status='expired', active=0, retired_reason='не подтвердился за 30 дней' "
            "WHERE status='candidate' AND created < ?",
            (now - CANDIDATE_TTL_DAYS * 86400,),
        ).rowcount
        n += self.db.execute(
            "UPDATE lessons SET status='expired', active=0, retired_reason='не подтверждался 60 дней' "
            "WHERE kind='auto' AND status='active' AND COALESCE(last_confirmed, created) < ?",
            (now - ACTIVE_TTL_DAYS * 86400,),
        ).rowcount
        over = self.db.execute("SELECT COUNT(*) FROM lessons WHERE status IN ('active','contested')").fetchone()[0]
        if over > MAX_ACTIVE_LESSONS:
            n += self.db.execute(
                "UPDATE lessons SET status='expired', active=0, retired_reason='вытеснен новыми' WHERE id IN ("
                "SELECT id FROM lessons WHERE status='active' AND kind='auto' "
                "ORDER BY COALESCE(last_confirmed, created) ASC LIMIT ?)",
                (over - MAX_ACTIVE_LESSONS,),
            ).rowcount
        self.db.commit()
        return n

    def active_lessons(self) -> list[tuple[int, str, str, str, str | None]]:
        """Lessons that go into the prompt: active + contested (tagged). (id, text, kind, status, reason)."""
        return self.db.execute(
            "SELECT id, text, kind, status, contest_reason FROM lessons WHERE status IN ('active','contested') ORDER BY id"
        ).fetchall()

    def all_lessons(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, text, kind, status, evidence, contest_reason FROM lessons "
            "WHERE status IN ('active','contested','candidate') ORDER BY status, id"
        ).fetchall()
        return [
            {"id": r[0], "text": r[1], "kind": r[2], "status": r[3],
             "cases": len({e["ref"] for e in json.loads(r[4] or "[]")}), "reason": r[5]}
            for r in rows
        ]

    # --- evaluation ---

    def open_setups(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, created, symbol, direction, entry_low, entry_high, stop, targets FROM setups WHERE status='open'"
        ).fetchall()
        return [
            {"id": r[0], "created": r[1], "symbol": r[2], "direction": r[3], "entry_low": r[4], "entry_high": r[5],
             "stop": r[6], "targets": json.loads(r[7])}
            for r in rows
        ]

    def update_setup(self, setup_id: int, res: dict) -> None:
        self.db.execute(
            "UPDATE setups SET status=?, outcome=?, r=?, mfe_pct=?, mae_pct=?, closed_at=? WHERE id=?",
            (res.get("status", "open"), res.get("outcome"), res.get("r"), res.get("mfe_pct"), res.get("mae_pct"),
             time.time() if res.get("status") == "closed" else None, setup_id),
        )
        self.db.commit()

    # --- track record ---

    def track_record(self, symbol: str = "", days: int = 90) -> dict:
        since = time.time() - days * 86400
        sql = ("SELECT symbol, direction, outcome, r, rr_tp1, mfe_pct, mae_pct, created, status FROM setups "
               "WHERE created >= ?")
        params: list = [since]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol.upper().lstrip("$"))
        rows = self.db.execute(sql + " ORDER BY created DESC", params).fetchall()
        closed = [r for r in rows if r[8] == "closed"]
        filled = [r for r in closed if r[2] != "not_filled"]
        wins = [r for r in filled if (r[3] or 0) > 0]
        by_outcome: dict[str, int] = {}
        for r in closed:
            by_outcome[r[2]] = by_outcome.get(r[2], 0) + 1
        return {
            "symbol": symbol.upper() or None,
            "days": days,
            "setups": len(rows),
            "open": len(rows) - len(closed),
            "closed": len(closed),
            "not_filled": by_outcome.get("not_filled", 0),
            "win_rate_filled_pct": round(len(wins) / len(filled) * 100, 1) if filled else None,
            "avg_r_filled": round(sum(r[3] or 0 for r in filled) / len(filled), 2) if filled else None,
            "by_outcome": by_outcome,
            "recent": [
                {"symbol": r[0], "direction": r[1], "outcome": r[2] or r[8], "r": r[3], "planned_rr": r[4],
                 "mfe_pct": r[5], "mae_pct": r[6], "days_ago": round((time.time() - r[7]) / 86400, 1)}
                for r in rows[:10]
            ],
        }

    # --- review material ---

    def unreviewed(self) -> tuple[list[tuple], list[tuple]]:
        setups = self.db.execute(
            "SELECT id, symbol, direction, entry_low, entry_high, stop, targets, rr_tp1, outcome, r, mfe_pct, mae_pct, "
            "question, answer FROM setups WHERE status='closed' AND reviewed=0 ORDER BY closed_at LIMIT 25"
        ).fetchall()
        fb = self.db.execute(
            "SELECT id, text, question, answer FROM feedback WHERE reviewed=0 ORDER BY created LIMIT 20"
        ).fetchall()
        return setups, fb

    def mark_reviewed(self, setup_ids: list[int], feedback_ids: list[int]) -> None:
        self.db.executemany("UPDATE setups SET reviewed=1 WHERE id=?", [(i,) for i in setup_ids])
        self.db.executemany("UPDATE feedback SET reviewed=1 WHERE id=?", [(i,) for i in feedback_ids])
        self.db.commit()


# --------------------------------------------------------------------------- the loop
