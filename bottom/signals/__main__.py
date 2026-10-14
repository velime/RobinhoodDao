"""Run scanners: `python -m bottom.signals oi` (one), `python -m bottom.signals all`, `--once`, `--dry`.

Every scanner is independent: run them as separate processes (one window each) or all in one process —
a scanner that crashes is restarted after a pause and never stops the others.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from ..tools import http
from .config import TOPICS, SignalsConfig
from .poster import Poster
from .scanners import SCANNERS, Scanner
from .store import SignalStore

log = logging.getLogger("bottom.signals")


async def run_cycle(sc: Scanner, poster: Poster, store: SignalStore) -> int:
    alerts = await sc.scan()
    for a in alerts:
        mid = await poster.send(a.feed, a.text)
        store.add_alert(a.feed, a.base, a.ex, a.data, a.text, mid)
    return len(alerts)


async def run_loop(name: str, cfg: SignalsConfig, dry: bool, once: bool) -> None:
    store = SignalStore(cfg.db_path)
    poster = Poster(cfg.bot_token, cfg.chat_id, store, dry=dry)
    sc = SCANNERS[name](cfg, store)
    log.info("%s: every %ss → %s", name, sc.every, "консоль (dry)" if poster.dry else f"тема «{TOPICS[name]}»")
    try:
        while True:
            t0 = time.monotonic()
            try:
                n = await run_cycle(sc, poster, store)
                log.info("%s: %d алерт(ов) за %.1fс", name, n, time.monotonic() - t0)
            except Exception:  # noqa: BLE001
                log.exception("%s: цикл упал, повтор через минуту", name)
                await asyncio.sleep(60)
            if once:
                break
            store.prune()
            await asyncio.sleep(max(5, sc.every - (time.monotonic() - t0)))
    finally:
        await sc.close()
        await poster.close()


async def amain(names: list[str], dry: bool, once: bool) -> None:
    cfg = SignalsConfig.load()
    if not dry and (not cfg.bot_token or cfg.chat_id is None):
        log.warning("SIGNALS_CHAT_ID или токен бота не заданы — пишу алерты в консоль (dry)")
    try:
        await asyncio.gather(*(run_loop(n, cfg, dry, once) for n in names))
    finally:
        await http.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m bottom.signals", description="Сканеры Bottom")
    p.add_argument("scanner", choices=[*SCANNERS, "all"], help="какой сканер запустить")
    p.add_argument("--once", action="store_true", help="один проход и выход")
    p.add_argument("--dry", action="store_true", help="не отправлять в Telegram, печатать в консоль")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    names = list(SCANNERS) if a.scanner == "all" else [a.scanner]
    try:
        asyncio.run(amain(names, a.dry, a.once))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
