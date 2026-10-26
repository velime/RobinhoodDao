"""One-time self-training over all the history Bottom has: `python -m bottom selftrain [--agy] [export.json ...]`.

--agy (what selftrain.bat passes): the lessons are written by Gemini through Antigravity CLI whatever
LLM_PROVIDER says; the API model from .env (FALLBACK_LLM_* or LLM_*) stays as the fallback.

1. Reference exports (Telegram "result.json" of the calls topic) from the arguments or the references/
   folder go into the journal next to the calls shipped with Bottom; known messages are skipped.
2. Every reference call and every own setup is checked on real 1h candles with the strict evaluator.
3. The model reviews the closed calls in batches and writes lessons, each citing the calls it rests on.
   A lesson becomes active once it is backed by calls of at least two different days; one case stays a
   candidate. Lessons and the stats by idea type go into the "Опыт" block the bot reads before answering.
4. A report: what worked by idea type and direction, the lessons, and where to review them.

Run it on your own computer: exchanges must be reachable and a model must be set in .env (any mode).
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import sys

from ..config import ROOT, load_settings
from ..llm import FailoverBackend, make_backend, single
from . import reference
from .journal import Journal
from .learner import Learner

REF_DIR = os.path.join(ROOT, "references")
MAX_BATCHES = 40  # 40 batches × 40 calls is more than the whole history; a bound, not a target


def agy_backend(s):
    """agy as the reviewer, the API model from .env as its fallback."""
    from ..llm.agy import AgyBackend

    primary = AgyBackend(os.getenv("AGY_MODEL", ""))
    fb = s.llm_fallback or (s.llm if s.llm.provider in ("openai", "anthropic") and s.llm.model else None)
    return FailoverBackend(primary, single(fb), s.llm_cooldown_sec) if fb else primary


async def run(paths: list[str], use_agy: bool = False) -> int:
    s = load_settings()
    journal = Journal(s.db_path)
    from ..tools.market.exchanges import ExchangePool

    pool = ExchangePool(s.exchanges)
    try:
        if use_agy:
            backend = agy_backend(s)
        else:
            backend = make_backend(s) if (s.llm.model or s.llm.provider in ("claude_code", "agy")) else None
    except Exception as e:  # noqa: BLE001
        print(f"[!] Модель не подключилась ({e}) — посчитаю исходы, но уроки писать будет некому.")
        backend = None
    learner = Learner(journal, backend, pool)  # also loads the calls shipped with Bottom
    if backend is not None:
        print(f"Модель для уроков: {backend.name}")

    print("1/4 Референсы")
    for p in paths:
        recs, st = reference.parse_export(p)
        n = reference.store(journal.db, recs, reference.source_of(p))
        print(f"  {os.path.basename(p)}: колов {st.get('call', 0)}, наблюдений {st.get('watch', 0)}, "
              f"обновлений {st.get('update', 0)}; новых в журнале {n}")
    total = journal.db.execute("SELECT COUNT(*), MAX(created) FROM reference_calls").fetchone()
    print(f"  всего в журнале: {total[0]}")

    print("2/4 Исходы на реальных свечах (несколько минут: свечи по каждой монете)")
    try:
        # a one-time run retries calls an earlier run could not fetch candles for
        journal.db.execute("UPDATE reference_calls SET status='open' WHERE status='no_data'")
        journal.db.commit()
        if not any([await pool.get(x) for x in ("binance", "bybit", "okx")]):
            print("  [X] биржи не отвечают (Binance, Bybit, OKX) — проверь интернет/VPN и запусти снова")
            return 1
        opened = journal.db.execute("SELECT COUNT(*) FROM reference_calls WHERE status='open'").fetchone()[0]
        closed = await reference.evaluate(journal.db, pool, limit=max(opened, 1))
        own = await learner.evaluate_open()
    finally:
        await pool.close()
    rows = dict(journal.db.execute("SELECT status, COUNT(*) FROM reference_calls GROUP BY status").fetchall())
    print(f"  референс: закрыто сейчас {closed}; по статусам {rows}; свои сетапы закрыто {own}")

    print("3/4 Уроки")
    totals = {"new": 0, "confirmed": 0, "promoted": 0, "contested": 0}
    if backend is None:
        print("  пропущено: модель не задана (LLM_* в .env)")
    else:
        for i in range(MAX_BATCHES):
            try:
                res = await learner.review_reference()
            except Exception as e:  # noqa: BLE001
                print(f"  [!] модель ответила ошибкой ({type(e).__name__}: {str(e)[:200]}) — остановился, "
                      "повторный запуск продолжит с этого места")
                break
            if res is None:
                break
            for k in totals:
                totals[k] += res.get(k, 0)
            print(f"  пачка {i + 1}: {res}")
        try:
            own_res = await learner.review()
            print(f"  свои сетапы и жалобы: {own_res}")
        except Exception as e:  # noqa: BLE001
            print(f"  [!] разбор своих сетапов: {type(e).__name__}: {str(e)[:200]}")
    print(f"  итого: {totals}")

    print("4/4 Отчёт")
    st = reference.stats(journal.db, min_n=3)
    for k, v in list(st.items())[:15]:
        print(f"  {k}: {v['filled']} сделок, TP1 у {v['win_rate']}%, средний {v['avg_r']}R, "
              f"не дошли до входа {v['not_filled_pct']}%")
    lessons = journal.all_lessons()
    for status, title in (("active", "Активные уроки (бот их соблюдает)"),
                          ("candidate", "Кандидаты (ждут второго подтверждения)"), ("contested", "Спорные")):
        part = [x for x in lessons if x["status"] == status]
        if part:
            print(f"\n  {title}: {len(part)}")
            for x in part:
                print(f"   #{x['id']} ({x['cases']} случ.) {x['text']}")
    out = os.path.join(ROOT, "data", "selftrain_report.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"stats": st, "lessons": lessons, "review": totals}, fh, ensure_ascii=False, indent=1)
    print(f"\nОтчёт сохранён: {out}")
    print("Плохой урок убирается командой боту /forget <id>, кандидат принимается /adopt <id> (админ из ADMIN_IDS).")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    os.chdir(ROOT)
    args = sys.argv[1:]
    use_agy = "--agy" in args
    paths = [a for a in args if a != "--agy"] or sorted(glob.glob(os.path.join(REF_DIR, "*.json")))
    sys.exit(asyncio.run(run(paths, use_agy)))


if __name__ == "__main__":
    main()
