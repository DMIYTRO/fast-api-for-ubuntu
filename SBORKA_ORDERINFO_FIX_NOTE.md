# 📄 Заметка об ошибке интеграции и её исправлении (`Sborka OrderInfo API`)

> [!WARNING]
> **Причина длительной отладки:** Изначально формировался некорректный параметр запроса `orders` (`orders=25661092` через запятую вместо `orders=[25661092]` в формате JSON-массива). Из-за этого API `sborka.ua` возвращал пустой ответ `HTTP 200 (Body: '')`, а не ошибку, что затруднило первичную диагностику.

---

## 🛑 В чем заключалась ошибка
При формировании POST-запроса к `https://sborka.ua/api.php?action=orderinfo` скрипт передавал список номеров заказов в формате обычного текста через запятую:
```python
# ❌ НЕПРАВИЛЬНО (приводило к пустому ответу API):
post_payload = {
    "api_key": api_key,
    "orders": "25661092",
}
```

---

## ✅ Как правильно формировать запрос к Sborka API
Сервер `sborka.ua` требует, чтобы поле `orders` передавалось строго как **JSON-массив строк/чисел**:

```python
# ✅ ПРАВИЛЬНО:
import json

post_payload = {
    "api_key": api_key,
    "orders": json.dumps([25661092]), # Формирует строку '[25661092]'
}
```

### Пример curl-запроса:
```bash
curl -X POST 'https://sborka.ua/api.php?action=orderinfo&api_key=YOUR_API_KEY' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'api_key=YOUR_API_KEY' \
  --data-urlencode 'orders=[25661092]'
```

---

## 🛠 Выполненные исправления на VM 104 (`10.20.2.220`)
1. **Обновлен скрипт `/home/ubuntu/v2-web-platform-FastApi/sborka_api/sborka_orderinfo.py`**:
   * Список заказов приводится к формату `json.dumps(order_ids)`.
2. **Протестировано получение данных**:
   * Заказ `25661092` успешно вернул `post_text: "Сгиб : 1; "`.
3. **Сервер VM 104 и основной сервис `control_panel.py` перезапущены**.
