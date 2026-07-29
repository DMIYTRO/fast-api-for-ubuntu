# Sborka.ua API Integration

Набор скриптов для работы с API сайта `sborka.ua`.

## Структура проекта

Папка: `/Users/admin/Documents/sborka_api`

- **`sborka_api_key.txt`** — файл с ключом доступа к API (`api_key`).
- **`sborka_transfer.py`** — перенос заказов в указанную сборку (`action=updateOrdersSborkaId`).
- **`sborka_orderinfo.py`** — получение информации о заказах (`action=orderinfo`).
- **`sborka_toprepress.py`** — отправка заказа в препресс (`action=toprepress`).
- **`README.md`** — документация и примеры использования.

---

## 1. Информация о заказах (`sborka_orderinfo.py`)

Запрос информации по одному или нескольким заказам (`action=orderinfo`).

### Запуск из консоли:
```bash
python3 sborka_orderinfo.py 25625544 25625545
```

### Пример использования в Python (`requests`):
```python
import json
import requests

url = "https://sborka.ua/api.php?action=orderinfo&api_key=YOUR_API_KEY"

payload = {
    "api_key": "YOUR_API_KEY",
    "orders": json.dumps([25625544, 25625545])
}

response = requests.post(url, data=payload)
print(response.json())
```

---

## 2. Отправка заказа в препресс (`sborka_toprepress.py`)

Переводит заказ в статус «в препрессе» (`action=toprepress`).

### Параметры:
- `order` (номер заказа, по одному)
- `comment_prepress` (комментарий к заказу)
- `color=black` (опционально: отправка в 24 сборку + дописка "печать чб")

### Запуск из консоли:
```bash
# Обычная отправка с комментарием:
python3 sborka_toprepress.py 25625544 --comment "Проверено"

# Отправка для Ч/Б печати (color=black):
python3 sborka_toprepress.py 25625544 --comment "Печать по макету" --black
```

### Пример использования в Python (`requests`):
```python
import requests

url = "https://sborka.ua/api.php?action=toprepress&api_key=YOUR_API_KEY"

payload = {
    "api_key": "YOUR_API_KEY",
    "order": "25625544",
    "comment_prepress": "Проверено",
    # "color": "black"  # Раскомментировать если печать Ч/Б
}

response = requests.post(url, data=payload)
print(response.text)
```

Для нескольких заказов приложение отправляет отдельный запрос на каждый ID,
без общего комментария.

---

## 3. Перенос заказов в сборку (`sborka_transfer.py`)

Переносит указанные заказы в выбранную сборку (`action=updateOrdersSborkaId`).

### Запуск из консоли:
```bash
python3 sborka_transfer.py 25625544 25625545 --sborka 21
```

### Пример использования в Python (`requests`):
```python
import json
import requests

url = "https://sborka.ua/api.php?action=updateOrdersSborkaId&api_key=YOUR_API_KEY"

payload = {
    "api_key": "YOUR_API_KEY",
    "sborka_array": json.dumps({"21": [25625544, 25625545]})
}

response = requests.post(url, data=payload)
print(response.text)
```
