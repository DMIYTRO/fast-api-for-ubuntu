---
description: Влить ветку worktree в main и перезапустить прод fastapi-app
argument-hint: <ветка>
---

Задача: влить изменения из ветки `$ARGUMENTS` в main и перезапустить прод-сервис Image Magic.

Если имя ветки не указано (`$ARGUMENTS` пусто), покажи `git branch -vv` и `git worktree list` и спроси, какую ветку деплоить. Дальше не продолжай.

Игнорируемые ветки — не предлагай их для деплоя и не упоминай как кандидатов:
- `codex/callas-pdf` — устаревшая ветка (сентябрь 2026). Callas уже интегрирован в main по-другому, ветка конфликтует с main и содержит мусорные `tmp*/state.sqlite3`. Пользователь решил оставить её, но не деплоить. Если её всё же передали в `$ARGUMENTS`, не деплой: напомни об этом и спроси подтверждение.

Контекст:
- Прод — systemd-юнит `fastapi-app` на этой VM (10.20.2.104), работает прямо из `/home/ubuntu/v2-web-platform-FastApi` (ветка main), порт 8000.
- Worktree-ветки лежат в `/home/ubuntu/orca/workspaces/v2-web-platform-FastApi/`. Путь к worktree ветки найди через `git worktree list`.
- `frontend/dist` хранится в git.
- Миграции Alembic применяются автоматически при старте приложения.
- sudo требует пароль. Перезапуск выполняет пользователь сам командой `! sudo systemctl restart fastapi-app`.

Шаги:
1. В worktree ветки `$ARGUMENTS` проверь `git status`. Если есть незакоммиченные изменения, остановись и спроси пользователя. Если у ветки нет worktree, работай с самой веткой через `git log main..$ARGUMENTS`.
2. Если ветка не содержит новых коммитов относительно main, сообщи об этом и пропусти шаги 3–4.
3. Прогони тесты в worktree:
   - `.venv/bin/python -m pytest -q` (если в worktree нет `.venv`, используй `/home/ubuntu/v2-web-platform-FastApi/.venv/bin/python`)
   - `cd frontend && npm test -- --run && npm run build`
   Если сборка изменила `frontend/dist`, закоммить её. Если тесты падают, остановись и покажи вывод.
4. Запушь ветку, создай PR в main (`gh pr create --fill --base main --head $ARGUMENTS`; если PR уже существует, используй его) и смержи (`gh pr merge --merge`).
5. В `/home/ubuntu/v2-web-platform-FastApi`: убедись, что `git status` чистый, и выполни `git pull`. Запомни хеш HEAD до pull.
6. Посмотри, что изменилось: `git diff --stat <старый HEAD> HEAD -- requirements.txt alembic/ frontend/`
   - Если менялся `requirements.txt`, выполни `.venv/bin/python -m pip install -r requirements.txt`.
   - Если есть новые миграции в `alembic/versions`, сделай резервную копию: `cp image_magic.db image_magic.db.bak-$(date +%F-%H%M)`.
   - Если менялся `frontend/src`, а `frontend/dist` не обновлён, выполни `cd frontend && npm install && npm run build`.
7. Запиши текущие значения: `systemctl show fastapi-app -p MainPID -p ExecMainStartTimestamp -p ActiveState`.
   Попроси пользователя выполнить `! sudo systemctl restart fastapi-app` и дождись.
8. После перезапуска проверь, что MainPID сменился, время старта свежее, а состояние `active`.
   Если что-то не так, покажи `journalctl -u fastapi-app -n 50 --no-pager` и хвост `logs/image-magic.log`.
9. Проверь, что сервер отвечает: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/login`.
10. Дай короткий отчёт: что влито (коммиты), какие доп. шаги выполнены, PID до и после перезапуска. Напомни проверить изменения на НОВОМ запуске проверки (старые отчёты их не покажут) и обновить UI через Ctrl+F5.

Запреты:
- Не запускай `control_panel.py` или uvicorn вручную вместо перезапуска юнита.
- Не делай force-push и `reset --hard` в основном каталоге.
- Не трогай `image_magic.db`, кроме резервного копирования.
