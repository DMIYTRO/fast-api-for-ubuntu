# Image Magic — TODO и команды

## Быстрый запуск веб-интерфейса

```bash
cd "/Users/admin/Documents/Image Magic"

export IMAGE_MAGIC_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$CJpqfei7JqUSNm/C1hd91g$cplO/cr9B9BkTTUWrCH8xtoMx3QqQzdqoZNOKgcBT1U'

.venv/bin/python control_panel.py
```

Адрес интерфейса: <http://127.0.0.1:8006>

Остановить сервер: `Ctrl+C`.

## Пересборка интерфейса после изменений

```bash
cd "/Users/admin/Documents/Image Magic/frontend"
npm run build

cd "/Users/admin/Documents/Image Magic"
export IMAGE_MAGIC_PASSWORD_HASH='$argon2id$v=19$m=65536,t=3,p=4$CJpqfei7JqUSNm/C1hd91g$cplO/cr9B9BkTTUWrCH8xtoMx3QqQzdqoZNOKgcBT1U'
.venv/bin/python control_panel.py
```

## Первичная установка

```bash
cd "/Users/admin/Documents/Image Magic"

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

cd frontend
npm install
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
