"""Telegram CLI.

  python -m swarm.tg login                 — войти в свой аккаунт (один раз, создаёт файл сессии)
  python -m swarm.tg scan [опции]          — один раз проверить каналы и оставить только крипто
  python -m swarm.tg collect               — один проход сборщика постов (проверка)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from ..config import load_settings
from .classify import CRYPTO, NOT_CRYPTO, REVIEW, UNAVAILABLE, Verdict, parse_channel_list
from .client import LABELS, Collector, make_client, scan_channel, subscribed_channels, verdict_label
from .store import TgStore

LLM_CLASSIFIER = (
    "Ты классифицируешь Telegram-каналы. Ответь одним словом: CRYPTO — если канал в основном о "
    "криптовалютах, трейдинге крипты, ончейне, аирдропах, мемкоинах, DeFi; OTHER — если нет."
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


async def _connect(s):
    """Connected and authorized client; logs in interactively if needed."""
    api_id, api_hash = _api(s)
    client = make_client(api_id, api_hash, s.tg_session)
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
    posts = "\n---\n".join(p["text"][:300] for p in item.get("posts", [])[:12])
    prompt = f"Канал: {item.get('title')}\nОписание: {item.get('about')}\nПосты:\n{posts}"
    try:
        res = await backend.new_session(LLM_CLASSIFIER, [], prompt).step([], allow_tools=False)
    except Exception as e:  # noqa: BLE001
        logging.warning("LLM-классификация недоступна: %s", e)
        return None
    word = res.text.strip().upper()
    return CRYPTO if word.startswith("CRYPTO") else NOT_CRYPTO if word.startswith("OTHER") else None


def _write_report(path: str, items: list[dict], kept: list[str]) -> None:
    order = {CRYPTO: 0, REVIEW: 1, NOT_CRYPTO: 2, UNAVAILABLE: 3}
    items = sorted(items, key=lambda x: (order[x["verdict"].label], -(x["verdict"].post_ratio)))
    lines = [
        "# Проверка Telegram-каналов",
        "",
        f"Всего: {len(items)} · оставлено: {len(kept)} · "
        + " · ".join(f"{LABELS[k]}: {sum(1 for i in items if i['verdict'].label == k)}"
                     for k in (CRYPTO, REVIEW, NOT_CRYPTO, UNAVAILABLE)),
        "",
        "| Канал | Итог | Посты про крипту | Подписчики | Последний пост | Частые слова | Почему |",
        "|---|---|---|---|---|---|---|",
    ]
    for i in items:
        v = i["verdict"]
        lines.append(
            f"| @{i['username']} {('— ' + i['title']) if i.get('title') else ''} | {verdict_label(v)}"
            f"{' (LLM)' if i.get('llm') else ''} | {round(v.post_ratio * 100)}% из {v.posts} | "
            f"{i.get('subscribers') or '—'} | {i.get('last_post') or '—'} | {', '.join(v.top_terms[:4])} | {v.reason} |"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


async def cmd_scan(s, args) -> None:
    names: list[str] = []
    if args.input and os.path.exists(args.input):
        names = parse_channel_list(open(args.input, encoding="utf-8").read())
    client = await _connect(s)
    if args.include_dialogs:
        seen = {n.lower() for n in names}
        mine = [n for n in await subscribed_channels(client) if n.lower() not in seen]
        print(f"Из твоих подписок добавлено каналов: {len(mine)}")
        names += mine
    print(f"Проверяю {len(names)} каналов (по {args.posts} последних постов)…")

    items = []
    for i, name in enumerate(names, 1):
        try:
            item = await scan_channel(client, name, args.posts)
        except Exception as e:  # noqa: BLE001
            item = {"username": name, "verdict": Verdict(UNAVAILABLE, 0, 0, 0, reason=f"ошибка: {e}")}
        v = item["verdict"]
        if v.label == CRYPTO and args.inactive_days and (item.get("days_since_last_post") or 0) > args.inactive_days:
            v.label, v.reason = REVIEW, f"не постит {item['days_since_last_post']} дн."
        if v.label == REVIEW and args.llm and item.get("posts"):
            llm = await _llm_review(s, item)
            if llm:
                v.label, item["llm"] = llm, True
        items.append(item)
        print(f"[{i}/{len(names)}] @{item['username']}: {verdict_label(v)} — {v.reason}")
        await asyncio.sleep(1.0)
    await client.disconnect()

    keep_labels = {CRYPTO} | ({REVIEW} if args.keep_review else set())
    kept = [i["username"] for i in items if i["verdict"].label in keep_labels]
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("# Крипто-каналы (сгенерировано: python -m swarm.tg scan)\n")
        f.write("\n".join(kept) + "\n")
    _write_report(args.report, items, kept)
    print(f"\n✅ Оставлено {len(kept)} из {len(items)}. Список: {args.output} · отчёт: {args.report}")
    review = [i["username"] for i in items if i["verdict"].label == REVIEW]
    if review and not args.keep_review:
        print(f"❓ Пограничные (не взяты, проверь вручную в отчёте): {', '.join('@' + r for r in review)}")


async def cmd_collect(s) -> None:
    names = parse_channel_list(open(s.tg_channels_file, encoding="utf-8").read())
    client = await _connect(s)
    col = Collector(client, TgStore(s.db_path), names, s.tg_poll_minutes)
    n = await col.run_once()
    print(f"✅ Собрано {n} постов из {len(names)} каналов в {s.db_path}")
    await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m swarm.tg")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sc = sub.add_parser("scan")
    sc.add_argument("--input", default="channels/all.txt", help="список каналов")
    sc.add_argument("--output", default="channels/crypto.txt", help="куда записать крипто-каналы")
    sc.add_argument("--report", default="channels/report.md")
    sc.add_argument("--posts", type=int, default=50, help="сколько последних постов смотреть")
    sc.add_argument("--include-dialogs", action="store_true", help="добавить каналы, на которые подписан аккаунт")
    sc.add_argument("--llm", action="store_true", help="пограничные каналы доразобрать моделью из .env")
    sc.add_argument("--keep-review", action="store_true", help="оставить и пограничные")
    sc.add_argument("--inactive-days", type=int, default=90, help="не постит дольше — в пограничные (0 = выкл)")
    sub.add_parser("collect")
    args = p.parse_args()

    s = load_settings()
    logging.basicConfig(level=s.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if args.cmd == "login":
        asyncio.run(cmd_login(s))
    elif args.cmd == "scan":
        asyncio.run(cmd_scan(s, args))
    else:
        asyncio.run(cmd_collect(s))


if __name__ == "__main__":
    main()
