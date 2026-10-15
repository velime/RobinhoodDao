# Папка Bottom для Claude Code

Здесь Claude Code работает как Bottom — на твоей подписке Claude, без API-ключа.

- `claude_setup.bat` — один раз: ставит Claude Code и создаёт здесь `CLAUDE.md` (правила Bottom),
  `.mcp.json` (подключение всех инструментов одним сервером `bottom`) и `.claude/settings.json` (разрешено
  только читать: инструменты Bottom, интернет, файлы `live/` и `inbox/`; запрещены команды и правка файлов).
- `claude_chat.bat` — открыть Bottom в терминале и спрашивать напрямую.
- `LLM_PROVIDER=claude_code` в `.env` + `start.bat` — Telegram-бот отвечает через Claude Code.

`live/` бот обновляет сам: `signals.md` (алерты наших парсеров), `telegram.md` (последние посты каналов),
`experience.md` (статистика сетапов и уроки). `inbox/` — картинки от пользователей.
Эти файлы генерируются и в git не попадают.
