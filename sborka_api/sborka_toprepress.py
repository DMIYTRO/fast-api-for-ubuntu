#!/usr/bin/env python3
"""Отправка заказа в препресс через API sborka.ua (action=toprepress)."""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from collections.abc import Iterable

API_URL = "https://sborka.ua/api.php"
SYSTEM_CA_FILE = "/etc/ssl/cert.pem"
API_KEY_FILE = Path(__file__).with_name("sborka_api_key.txt")


def load_api_key() -> str:
    """Загружает API-ключ из файла sborka_api_key.txt рядом со скриптом."""
    try:
        api_key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise RuntimeError(
            f"Не удалось прочитать файл API-ключа: {API_KEY_FILE}"
        ) from error

    if not api_key:
        raise RuntimeError(f"Файл API-ключа пуст: {API_KEY_FILE}")

    return api_key


def send_to_prepress(
    order: str | int,
    comment_prepress: str = "",
    color: str | None = None,
    timeout: int = 20,
    ) -> tuple[int, str]:
    """
    Отправляет POST-запрос на action=toprepress.
    
    Параметры:
      - order: номер заказа (по одному)
      - comment_prepress: комментарий
      - color: если "black", будет 24 сборка и к комментарию припишется "печать чб"
    """
    api_key = load_api_key()

    query_params = {
        "action": "toprepress",
        "api_key": api_key,
    }

    post_payload = {
        "api_key": api_key,
        "order": str(order).strip(),
        "comment_prepress": comment_prepress,
    }

    if color:
        post_payload["color"] = color

    query = urllib.parse.urlencode(query_params)
    payload = urllib.parse.urlencode(post_payload).encode("utf-8")

    request = urllib.request.Request(
        f"{API_URL}?{query}",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    ssl_context = ssl.create_default_context(cafile=SYSTEM_CA_FILE)
    with urllib.request.urlopen(
        request,
        timeout=timeout,
        context=ssl_context,
    ) as response:
        return response.status, response.read().decode("utf-8", errors="replace").strip()


def send_orders_to_prepress(
    orders: Iterable[str | int],
    timeout: int = 20,
) -> tuple[int, str]:
    """Отправляет каждый заказ отдельным запросом без общего комментария."""
    order_ids = [str(order).strip() for order in orders if str(order).strip()]
    if not order_ids:
        raise ValueError("Не передан ни один заказ")

    items: list[dict[str, object]] = []
    for order_id in order_ids:
        item_status, item_body = send_to_prepress(order_id, timeout=timeout)
        items.append({"order": order_id, "http_status": item_status, "response": item_body})
    return 200, json.dumps({"per_order_requests": True, "items": items}, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Отправка заказа в препресс на sborka.ua (action=toprepress)."
    )
    parser.add_argument(
        "orders",
        nargs="+",
        help="Номер одного или нескольких заказов.",
    )
    parser.add_argument(
        "--comment",
        "-c",
        default="",
        help="Комментарий препресса (comment_prepress).",
    )
    parser.add_argument(
        "--black",
        action="store_true",
        help="Флаг ЧБ печати (color=black, 24 сборка + приписка 'печать чб').",
    )
    args = parser.parse_args()

    color_val = "black" if args.black else None

    print(f"Заказы: {', '.join(args.orders)}")
    print(f"Комментарий: {args.comment or '(без комментария)'}")
    if color_val:
        print("Режим: Ч/Б (color=black)")

    try:
        if len(args.orders) > 1:
            if args.comment:
                parser.error("Для пакетной отправки комментарий не используется")
            status, body = send_orders_to_prepress(args.orders)
        else:
            status, body = send_to_prepress(
                order=args.orders[0],
                comment_prepress=args.comment,
                color=color_val,
            )
        print(f"\nHTTP {status}")
        print("Ответ сервера:")
        
        try:
            parsed_json = json.loads(body)
            print(json.dumps(parsed_json, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print(body or "(пустой ответ)")

        return 0

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace").strip()
        print(f"HTTP {error.code}; ответ: {body or '(пустой)'}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"Ошибка запроса: {error}", file=sys.stderr)
        return 1
    except RuntimeError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
