"""Telegram CLI (все источники читаются через твой аккаунт).

  python -m swarm.tg login              — войти в аккаунт (один раз, создаёт файл сессии)
  python -m swarm.tg scan [опции]       — один раз проверить источники: крипта или нет
  python -m swarm.tg collect [--once]   — собирать сообщения без бота (--once: только догрузить и выйти)
  python -m swarm.tg search [ТИКЕР]     — посмотреть, что собрано (упоминания тикера / что обсуждают)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from ..config import load_settings
from .classify import CRYPTO, NOT_CRYPTO, REVIEW, UNAVAILABLE, Verdict
from .client import LABELS, Collector, kind_of, load_dialogs, make_client, resolve, scan_source, verdict_label
from .sources import Source, load_sources, save_sources
from .store import TgStore

LLM_CLASSIFIER = (
    "Ты классифицируешь Telegram-каналы и чаты. Ответь одним словом: CRYPTO — если источник в основном о "
    "криптовалютах, трейдинге, ончейне, аирдропах, мемкоинах, DeFi, арбитраже; OTHER — если нет."
)


def save_env(path: str, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file (create it if missing)."""
    lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
    for k, v in values.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == k:
                lines[i] = f"{k}={v}"
                break
        else:
            lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _api(s) -> tuple[int, str]:
    """api_id/api_hash from .env, or ask once and save to .env."""
    if s.tg_api_id and s.tg_api_hash:
        return s.tg_api_id, s.tg_api_hash
    print(
        "\nНужны api_id и api_hash твоего Telegram (один раз):\n"
        "  1) открой https://my.telegram.org и войди по номеру телефона\n"
        "  2) API development tools → заполни форму (название любое) → Create application\n"
        "  3) скопируй App api_id и App api_hash\n"
    )
    while True:
        raw_id = input("api_id (цифры): ").strip()
        if raw_id.isdigit():
            break
        print("api_id — это только цифры, попробуй ещё раз")
    api_hash = input("api_hash: ").strip()
    save_env(".env", {"TG_API_ID": raw_id, "TG_API_HASH": api_hash})
    print("Сохранено в .env — больше спрашивать не буду.\n")
    return int(raw_id), api_hash


async def _connect(s, receive_updates: bool = False):
    """Connected and authorized client; logs in interactively if needed."""
    api_id, api_hash = _api(s)
    client = make_client(api_id, api_hash, s.tg_session, receive_updates=receive_updates)
    await client.connect()
    if not await client.is_user_authorized():
        print(
            "Вход в твой Telegram (один раз). Введи номер в формате +380XXXXXXXXX.\n"
            "Код придёт в приложение Telegram (чат «Telegram»). Если включён облачный пароль — введи и его.\n"
        )
        await client.start()  # спросит телефон, код и пароль 2FA
    me = await client.get_me()
    print(f"✅ Аккаунт: {me.first_name} (@{me.username})")
    return client


def _sources(s) -> list[Source]:
    src = load_sources(s.tg_sources_file)
    if not src:
        print(f"⚠️ Нет источников в {s.tg_sources_file}")
    return src


async def cmd_login(s) -> None:
    client = await _connect(s)
    print(f"Сессия сохранена: {s.tg_session}.session — не публикуй этот файл, это доступ к аккаунту.")
    await client.disconnect()


async def _llm_review(s, item: dict) -> str | None:
    from ..llm import make_backend

    try:
        backend = make_backend(s)
    except Exception:  # noqa: BLE001
        return None
    texts = "\n---\n".join(t[:300] for t in item.get("texts", [])[:12])
    prompt = f"Название: {item['source'].title}\nОписание: {item.get('about')}\nСообщения:\n{texts}"
    try:
        res = await backend.new_session(LLM_CLASSIFIER, [], prompt).step([], allow_tools=False)
    except Exception as e:  # noqa: BLE001
        logging.warning("LLM-классификация недоступна: %s", e)
        return None
    word = res.text.strip().upper()
    return CRYPTO if word.startswith("CRYPTO") else NOT_CRYPTO if word.startswith("OTHER") else None


def apply_verdict(src: Source, label: str) -> None:
    """Scan result → enabled flag. The list is already hand-picked, so doubtful ones stay on."""
    src.verdict = label
    if src.manual:
        return
    src.enabled = label in (CRYPTO, REVIEW)


KIND_RU = {"channel": "канал", "group": "чат", "forum": "чат с темами"}


def write_report(path: str, items: list[dict]) -> None:
    order = {CRYPTO: 0, REVIEW: 1, NOT_CRYPTO: 2, UNAVAILABLE: 3}
    items = sorted(items, key=lambda x: (order[x["verdict"].label], -x["verdict"].post_ratio))
    enabled = sum(1 for i in items if i["source"].enabled)
    lines = [
        "# Проверка Telegram-источников",
        "",
        f"Всего: {len(items)} · читается: {enabled} · "
        + " · ".join(f"{LABELS[k]}: {sum(1 for i in items if i['verdict'].label == k)}"
                     for k in (CRYPTO, REVIEW, NOT_CRYPTO, UNAVAILABLE)),
        "",
        "| Источник | Тип | Доступ | Итог | Читаем | Сообщения про крипту | Последнее | Частые слова | Почему |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i in items:
        s, v = i["source"], i["verdict"]
        lines.append(
            f"| {s.label} — {s.title} | {KIND_RU.get(s.kind, s.kind)} | {'публичный' if s.public else 'приватный'} "
            f"| {verdict_label(v)}{' (LLM)' if i.get('llm') else ''} | {'да' if s.enabled else 'нет'}"
            f"{' (вручную)' if s.manual else ''} | {round(v.post_ratio * 100)}% из {v.posts} "
            f"| {i.get('last_post') or '—'} | {', '.join(v.top_terms[:4])} | {v.reason} |"
        )
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def cmd_scan(s, args) -> None:
    sources = _sources(s)
    client = await _connect(s)
    print("Загружаю список чатов аккаунта…")
    dialogs = await load_dialogs(client)
    if args.include_dialogs:
        known = {x.id for x in sources}
        new = [
            Source(id=cid, title=getattr(e, "title", ""), username=getattr(e, "username", None),
                   kind=kind_of(e) or "channel", public=bool(getattr(e, "username", None)), enabled=False)
            for cid, e in dialogs.items() if cid not in known
        ]
        print(f"Из твоих чатов добавлено новых источников: {len(new)}")
        sources += new
    print(f"Проверяю {len(sources)} источников (по {args.posts} последних сообщений)…")

    items = []
    for i, src in enumerate(sources, 1):
        try:
            ent = await resolve(client, src, dialogs)
            item = await scan_source(client, src, ent, args.posts)
        except Exception as e:  # noqa: BLE001
            item = {"source": src, "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason=f"ошибка: {e}")}
        v = item["verdict"]
        if v.label == CRYPTO and args.inactive_days and (item.get("days_since_last_post") or 0) > args.inactive_days:
            v.label, v.reason = REVIEW, f"молчит {item['days_since_last_post']} дн."
        if v.label == REVIEW and args.llm and item.get("texts"):
            llm = await _llm_review(s, item)
            if llm:
                v.label, item["llm"] = llm, True
        apply_verdict(src, v.label)
        items.append(item)
        print(f"[{i}/{len(sources)}] {src.label}: {verdict_label(v)} — {v.reason}")
        await asyncio.sleep(1.0)
    await client.disconnect()

    save_sources(s.tg_sources_file, sources)
    write_report(args.report, items)
    on = sum(1 for x in sources if x.enabled)
    print(f"\n✅ Будет читаться {on} из {len(sources)}. Список: {s.tg_sources_file} · отчёт: {args.report}")


async def cmd_collect(s, args) -> None:
    sources = _sources(s)
    client = await _connect(s, receive_updates=not args.once)
    store = TgStore(s.db_path)
    col = Collector(client, store, sources, resync_minutes=s.tg_resync_minutes)
    await col.start()
    if args.once:
        n = await col.sync_all()
        print(f"✅ Догружено {n} сообщений · всего в базе: {store.stats()}")
        await col.stop()
        return
    print("Слушаю источники. Новые сообщения сохраняются сразу. Остановить — Ctrl+C или закрыть окно.")
    await client.run_until_disconnected()


def cmd_search(s, args) -> None:
    store = TgStore(s.db_path)
    res = store.search(args.query or "", args.hours, limit=args.limit, hide_private=False)
    print(json.dumps(res, ensure_ascii=False, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m swarm.tg")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sc = sub.add_parser("scan")
    sc.add_argument("--report", default="channels/report.md")
    sc.add_argument("--posts", type=int, default=50, help="сколько последних сообщений смотреть")
    sc.add_argument("--include-dialogs", action="store_true", help="добавить все каналы и чаты аккаунта")
    sc.add_argument("--llm", action="store_true", help="пограничные доразобрать моделью из .env")
    sc.add_argument("--inactive-days", type=int, default=90, help="молчит дольше — в пограничные (0 = выкл)")
    co = sub.add_parser("collect")
    co.add_argument("--once", action="store_true", help="только догрузить пропущенное и выйти")
    se = sub.add_parser("search")
    se.add_argument("query", nargs="?", default="")
    se.add_argument("--hours", type=int, default=24)
    se.add_argument("--limit", type=int, default=15)
    args = p.parse_args()

    s = load_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if args.cmd == "login":
        asyncio.run(cmd_login(s))
    elif args.cmd == "scan":
        asyncio.run(cmd_scan(s, args))
    elif args.cmd == "collect":
        try:
            asyncio.run(cmd_collect(s, args))
        except KeyboardInterrupt:
            print("Остановлено.")
    else:
        cmd_search(s, args)


if __name__ == "__main__":
    main()
