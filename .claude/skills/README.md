# Навыки Claude Code для проекта Bottom

Эти навыки (Agent Skills) автоматически подхватываются Claude Code в любой
сессии с этим репозиторием: локально и в облаке. Claude сам выбирает нужный
навык по задаче.

Источник: https://github.com/agiprolabs/claude-trading-skills
(коммит 981e1d7, лицензия MIT — см. `LICENSE-agiprolabs.md`).

Взяты навыки, полезные для крипто-ресерчера и автоматических колов
(см. `docs/agent-spec.md`): API рыночных данных, индикаторы и волатильность,
микроструктура и ликвидность, стопы/тейки/размер позиции, ончейн-анализ,
бэктест и журнал сделок.

Не взяты: исполнение сделок и подпись транзакций (работают с приватными
ключами), Kalshi/Polymarket, налоги, заглушки.

Обновить навыки: скопировать свежие папки из исходного репозитория поверх.

---

## Навыки для проектирования агента

Источник: https://github.com/alirezarezvani/claude-skills (коммит 19392f7, лицензия MIT —
см. `LICENSE-alirezarezvani.md`). Скрипты проверены: сетевых запросов нет; `subprocess`
только для локального `git rev-parse` (skill-doctor) и локальных проверок (agent-harness).

| Навык | Зачем в Bottom |
|---|---|
| `agent-designer` | архитектура агента, схемы инструментов, оценка логов (`bottom/agent.py`, `bottom/tools/registry.py`) |
| `agent-workflow-designer` | паттерны sequential/parallel/router — например, `cross_check` как под-процесс |
| `agent-memory` | лестница памяти: урок повышается только повторением → применено в `bottom/learning.py` |
| `memory-engineering` | политика забывания и запрет авто-слияния противоречий → применено в `bottom/learning.py` |
| `agent-harness` | цикл цель→план→исполнение→проверка; «модель не судит сама себя» → `bottom/grounding.py` |
| `loop-library` | аудит ограниченных циклов → бюджет шагов, таймауты, кеш повторов в `bottom/agent.py` |
| `rag-architect` | если понадобится смысловой поиск по постам вместо SQL (`bottom/tg/store.py`) |
| `zero-hallucination-coder` | дисциплина Discuss→Map→Decompose→Execute→Verify при крупных изменениях |
| `skill-doctor` | оценка навыков Claude Code по истории локальных сессий (не промптов бота) |
| `mcp-server-builder` | если захочется отдать инструменты Bottom как MCP-сервер |
