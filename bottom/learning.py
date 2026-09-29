"""Learning from mistakes.

1. Journal: every setup Bottom publishes (validated by calc_setup) is recorded.
2. Evaluator: hourly candles decide what happened — not filled / stop / TP1 → BE / TP2 —
   with MFE/MAE and the result in R.
3. Feedback: users reply /wrong to a bot answer and say what was wrong.
4. Review: once a day the model reads closed setups and feedback and proposes short,
   checkable lessons, each citing the cases it rests on.
5. Promotion is earned, not asserted (agent-memory / memory-engineering methodology):
   - a proposed lesson starts as a *candidate* and is NOT injected into the prompt;
   - it becomes *active* only when supported by >= 2 distinct cases on >= 2 distinct
     review days, or when an admin adopts it (/adopt, /lesson);
   - evidence is checked: a lesson may only cite cases shown in that review;
   - contradicting evidence never deletes a lesson — it is marked *contested* and
     still shown with the tag until a human resolves it (/adopt or /forget);
   - forgetting policy: candidates expire after 30 days, active auto lessons expire
     after 60 days without re-confirmation, at most 30 active lessons (oldest-confirmed
     auto lessons evicted first; admin lessons are never evicted).
6. Use: active and contested lessons plus the track record go into the system prompt,
   and the track_record tool lets the model check its own history before a setup.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

log = logging.getLogger(__name__)

FILL_WINDOW_H = 72  # a limit entry not reached in 3 days → not filled
MAX_OPEN_H = 7 * 24  # a filled trade still open after 7 days → closed at market
MAX_ACTIVE_LESSONS = 30
PROMOTE_MIN_CASES = 2
PROMOTE_MIN_DAYS = 2
CANDIDATE_TTL_DAYS = 30
ACTIVE_TTL_DAYS = 60

REVIEW_PROMPT = """\
Ты — наставник крипто-аналитика Bottom. Ниже его прошлые сетапы с фактическим исходом по свечам, \
жалобы пользователей на его ответы и его текущие уроки.
Задача: сформулировать короткие, конкретные, проверяемые уроки, которые улучшат будущие ответы.
Правила:
- Урок — одно правило-действие, до 200 символов: что делать иначе и в какой ситуации. Без воды.
- Опирайся только на факты ниже (исходы, MFE/MAE, текст жалоб). Один случай — слабое основание: \
пиши «наблюдение», если подтверждение одно.
- Ищи закономерности: где стопы выбивает шумом (MAE до стопа, потом цель), где вход не \
исполняется (слишком далёкий лимит), где цели недостижимы (MFE меньше TP1), какие монеты/сетапы \
работают, в чём пользователи правы.
- Каждый урок опирается на конкретные случаи: в квадратных скобках укажи их номера (S12 — сетап, \
F3 — жалоба). Ссылаться можно только на случаи из этого списка. Без ссылок урок не принимается.
- Не дублируй существующие уроки: если новый случай подтверждает существующий урок или кандидата — \
подтверди его. Если факты противоречат уроку — отметь это (урок не удаляется, решает человек).
Формат ответа — только строки:
+ текст нового урока [S12, F3]
= #id [S15]
! #id почему факты противоречат уроку [S16]
Если сказать нечего — ответь одним словом: НЕТ."""


# --------------------------------------------------------------------------- evaluation (pure)


def evaluate_setup(setup: dict, candles: list[dict], now: float | None = None) -> dict:
    """Walk hourly candles after the setup was given. Returns status/outcome fields.

    Conservative rules: fill at the worst edge of the zone; if stop and target are
    touched in the same candle, the stop counts first; after TP1 half is closed and the
    stop moves to breakeven.
    """
    now = now or time.time()
    long = setup["direction"] == "long"
    lo, hi = setup["entry_low"], setup["entry_high"] or setup["entry_low"]
    entry = hi if long else lo
    stop = setup["stop"]
    targets = setup["targets"]
    risk = abs(entry - stop)
    created_ms = setup["created"] * 1000
    after = [c for c in candles if c["t"] + 3_600_000 > created_ms]

    fill_i = None
    for i, c in enumerate(after):
        if (c["t"] - created_ms) / 3_600_000 > FILL_WINDOW_H:
            break
        if (long and c["l"] <= hi) or (not long and c["h"] >= lo):
            fill_i = i
            break
    age_h = (now - setup["created"]) / 3600
    if fill_i is None:
        if age_h > FILL_WINDOW_H:
            return {"status": "closed", "outcome": "not_filled", "r": 0.0}
        return {"status": "open"}

    mfe = mae = 0.0
    tp1_hit = False
    stop_now = stop
    for c in after[fill_i:]:
        fav = (c["h"] - entry) if long else (entry - c["l"])
        adv = (entry - c["l"]) if long else (c["h"] - entry)
        mfe, mae = max(mfe, fav), max(mae, adv)
        hit_stop = (c["l"] <= stop_now) if long else (c["h"] >= stop_now)
        hit_tp1 = (c["h"] >= targets[0]) if long else (c["l"] <= targets[0])
        hit_tp2 = len(targets) > 1 and ((c["h"] >= targets[1]) if long else (c["l"] <= targets[1]))
        base = {"mfe_pct": round(mfe / entry * 100, 2), "mae_pct": round(mae / entry * 100, 2)}
        if not tp1_hit:
            if hit_stop:
                return {"status": "closed", "outcome": "stop", "r": -1.0, **base}
            if hit_tp1:
                tp1_hit = True
                stop_now = entry
                r1 = abs(targets[0] - entry) / risk
                if hit_tp2:
                    r2 = abs(targets[1] - entry) / risk
                    return {"status": "closed", "outcome": "tp2", "r": round(0.5 * r1 + 0.5 * r2, 2), **base}
                continue
        else:
            r1 = abs(targets[0] - entry) / risk
            if hit_stop:
                return {"status": "closed", "outcome": "tp1_then_be", "r": round(0.5 * r1, 2), **base}
            if hit_tp2:
                r2 = abs(targets[1] - entry) / risk
                return {"status": "closed", "outcome": "tp2", "r": round(0.5 * r1 + 0.5 * r2, 2), **base}
    last = after[-1]["c"]
    move_r = ((last - entry) if long else (entry - last)) / risk
    r_now = round(0.5 * abs(targets[0] - entry) / risk + 0.5 * move_r if tp1_hit else move_r, 2)
    res = {"mfe_pct": round(mfe / entry * 100, 2), "mae_pct": round(mae / entry * 100, 2)}
    if age_h > MAX_OPEN_H:
        return {"status": "closed", "outcome": "tp1_expired" if tp1_hit else "expired", "r": r_now, **res}
    return {"status": "open", "outcome": "tp1_open" if tp1_hit else "in_trade", "r": r_now, **res}


_REFS = re.compile(r"\[([^\]]*)\]\s*$")


def _split_refs(body: str) -> tuple[str, list[str]]:
    """'text [S12, F3]' → ('text', ['S12', 'F3'])."""
    m = _REFS.search(body)
    if not m:
        return body.strip(), []
    refs = re.findall(r"\b([SF]\d+)\b", m.group(1).upper())
    return body[: m.start()].strip(), list(dict.fromkeys(refs))


def parse_review(text: str) -> dict:
    """Parse the reviewer's lines.

    '+ lesson [S1, F2]' → new candidate; '= #id [S3]' → confirmation;
    '! #id reason [S4]' → contradiction (the lesson gets contested, not deleted).
    """
    out: dict = {"new": [], "confirm": [], "contest": []}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("+"):
            body, refs = _split_refs(line[1:])
            if 10 <= len(body) <= 300:
                out["new"].append((body, refs))
            continue
        m = re.match(r"^([=!])\s*#?(\d+)\s*(.*)$", line)
        if m:
            body, refs = _split_refs(m.group(3))
            if m.group(1) == "=":
                out["confirm"].append((int(m.group(2)), refs))
            else:
                out["contest"].append((int(m.group(2)), body, refs))
    return out


def published_setups(calc_results: list[dict], answer: str) -> list[dict]:
    """calc_setup OK results whose stop price actually appears in the final answer."""
    out, seen = [], set()
    for r in reversed(calc_results):  # the last validated version wins
        key = (r.get("symbol", "").upper(), r.get("direction"))
        if r.get("verdict") != "OK" or key in seen:
            continue
        stop = r.get("stop")
        variants = {f"{stop:g}", f"{stop:.10f}".rstrip("0").rstrip("."), str(stop)}
        if any(v in answer for v in variants):
            seen.add(key)
            out.append(r)
    return out


# --------------------------------------------------------------------------- storage


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

    def add_candidate(self, text: str, refs: list[str]) -> int:
        ev = [{"ref": r, "day": self._today()} for r in refs]
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

    def confirm(self, lesson_id: int, refs: list[str]) -> str | None:
        """Add supporting cases; promote a candidate once it has earned it. Returns new status."""
        les = self._lesson(lesson_id)
        if les is None or les["status"] in ("retired", "expired") or not refs:
            return None
        known = {e["ref"] for e in les["evidence"]}
        ev = les["evidence"] + [{"ref": r, "day": self._today()} for r in refs if r not in known]
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


class Learner:
    def __init__(self, journal: Journal, backend=None, pool=None):
        self.journal = journal
        self.backend = backend
        self.pool = pool
        self._block_cache: tuple[float, str] = (0.0, "")

    def lessons_block(self) -> str:
        """Text appended to the system prompt: track record + active lessons (cached 10 min)."""
        ts, text = self._block_cache
        if time.time() - ts < 600:
            return text
        tr = self.journal.track_record(days=60)
        lessons = self.journal.active_lessons()
        parts = []
        if tr["closed"]:
            parts.append(
                f"Твоя статистика за 60 дней: сетапов {tr['setups']}, закрыто {tr['closed']}, не исполнилось "
                f"{tr['not_filled']}, винрейт исполненных {tr['win_rate_filled_pct']}%, средний результат "
                f"{tr['avg_r_filled']}R. Исходы: {tr['by_outcome']}."
            )
        if lessons:
            parts.append("Уроки из твоих прошлых ошибок и отзывов (соблюдай; спорные — применяй осторожно):\n" + "\n".join(
                f"#{i} {t}" + (" (от админа)" if kind == "manual" else "")
                + (f" [СПОРНО: {reason}]" if status == "contested" else "")
                for i, t, kind, status, reason in lessons))
        text = ("\n\n# 10. Опыт\n" + "\n".join(parts)) if parts else ""
        self._block_cache = (time.time(), text)
        return text

    def invalidate(self) -> None:
        self._block_cache = (0.0, "")

    async def evaluate_open(self) -> int:
        from .tools.exchanges import ohlcv

        closed = 0
        for st in self.journal.open_setups():
            hours = int((time.time() - st["created"]) / 3600) + 3
            try:
                candles, _meta = await ohlcv(self.pool, st["symbol"], "1h", min(max(hours, 5), 500))
            except Exception as e:  # noqa: BLE001
                log.info("evaluate %s: %s", st["symbol"], e)
                continue
            if not candles:
                continue
            res = evaluate_setup(st, candles)
            self.journal.update_setup(st["id"], res)
            closed += res.get("status") == "closed"
        if closed:
            self.invalidate()
        return closed

    async def review(self) -> dict:
        """Daily: closed setups + feedback → candidates, confirmations, contests; then forgetting."""
        setups, fb = self.journal.unreviewed()
        forgotten = self.journal.apply_forgetting()
        if not setups and not fb:
            if forgotten:
                self.invalidate()
            return {"new": 0, "confirmed": 0, "promoted": 0, "contested": 0, "forgotten": forgotten}
        shown = {f"S{s[0]}" for s in setups} | {f"F{f[0]}" for f in fb}
        lines = ["## Сетапы с исходом"]
        for (i, sym, d, lo, hi, stop, tg, rr, outcome, r, mfe, mae, q, _a) in setups:
            lines.append(f"[S{i}] {sym} {d} вход {lo}–{hi} стоп {stop} цели {tg} план R:R {rr} → {outcome}, "
                         f"{r}R, MFE {mfe}%, MAE {mae}%. Вопрос: {q[:200]}")
        lines.append("\n## Жалобы пользователей")
        for (i, text, q, a) in fb:
            lines.append(f"[F{i}] Жалоба: {text}\nВопрос: {(q or '')[:300]}\nОтвет бота: {(a or '')[:700]}")
        lines.append("\n## Текущие уроки и кандидаты")
        lessons = self.journal.all_lessons()
        lines += [f"#{x['id']} ({x['status']}, случаев: {x['cases']}) {x['text']}" for x in lessons] or ["(нет)"]
        sess = self.backend.new_session(REVIEW_PROMPT, [], "\n".join(lines))
        res = await sess.step([], allow_tools=False)
        parsed = {"new": [], "confirm": [], "contest": []}
        if not res.text.strip().upper().startswith("НЕТ"):
            parsed = parse_review(res.text)
        # cite, don't invent: only cases actually shown in this review count as evidence
        new = confirmed = promoted = contested = 0
        for text, refs in parsed["new"]:
            refs = [r for r in refs if r in shown]
            if refs:
                self.journal.add_candidate(text, refs)  # candidates are never injected on day one
                new += 1
        for lid, refs in parsed["confirm"]:
            refs = [r for r in refs if r in shown]
            before = (self.journal._lesson(lid) or {}).get("status")
            after = self.journal.confirm(lid, refs)
            if after:
                confirmed += 1
                promoted += int(before == "candidate" and after == "active")
        for lid, reason, refs in parsed["contest"]:
            if [r for r in refs if r in shown] and self.journal.contest(lid, reason):
                contested += 1
        self.journal.mark_reviewed([s[0] for s in setups], [f[0] for f in fb])
        self.invalidate()
        result = {"new": new, "confirmed": confirmed, "promoted": promoted, "contested": contested,
                  "forgotten": forgotten}
        log.info("Обучение: %s", result)
        return result
