# Image Magic — быстрые команды

## Быстрый запуск веб-интерфейса

```bash
cd "/home/ubuntu/v2-web-platform-FastApi"

.venv/bin/python manage.py set-password
export IMAGE_MAGIC_PASSWORD_HASH='ХЕШ_ИЗ_ПРЕДЫДУЩЕЙ_КОМАНДЫ'

.venv/bin/python control_panel.py
```

Адрес интерфейса: <http://127.0.0.1:8006>

Остановить сервер: `Ctrl+C`.

## Production на `10.20.2.104`

Адрес интерфейса: <http://10.20.2.104/login?next=/>

```bash
cd "/home/ubuntu/v2-web-platform-FastApi"
git status --short

sudo systemctl restart fastapi-app
sudo nginx -t
sudo systemctl reload nginx

systemctl is-active fastapi-app
systemctl is-active nginx
```

Production работает по схеме `Nginx :80 -> Uvicorn :8000`. Локальный порт
`8006` к Nginx не относится.

## Пересборка интерфейса после изменений

```bash
cd "/home/ubuntu/v2-web-platform-FastApi/frontend"
npm run build

cd "/home/ubuntu/v2-web-platform-FastApi"
export IMAGE_MAGIC_PASSWORD_HASH='ХЕШ_ИЗ_ПРЕДЫДУЩЕЙ_КОМАНДЫ'
.venv/bin/python control_panel.py
```

## Первичная установка

```bash
cd "/home/ubuntu/v2-web-platform-FastApi"

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cd frontend
npm ci
npm run build
cd ..

.venv/bin/python manage.py set-password
export IMAGE_MAGIC_PASSWORD_HASH='ХЕШ_ИЗ_ПРЕДЫДУЩЕЙ_КОМАНДЫ'

.venv/bin/alembic upgrade head
.venv/bin/python control_panel.py
```

## Следующая доработка действий заказов

- Добавить локальные состояния действий: обработка, завершено, ожидает отправки, ошибка.
- Перемещать исходники и PDF в настроенные папки печати или доработки.
- Скрывать карточку только после успешного перемещения файлов.
- Сохранять JSON действия в локальную outbox-очередь до появления внешнего API.
- Добавить историю обработанных заказов и повтор неудачной операции.
- Защитить операции от повторного перемещения и дублирования JSON.
