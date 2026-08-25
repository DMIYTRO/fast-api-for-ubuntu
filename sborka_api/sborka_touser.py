#!/usr/bin/env python3
"""Возврат заказа клиенту / на доработку через API sborka.ua (action=touser)."""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://sborka.ua/api.php"
SYSTEM_CA_FILE = "/etc/ssl/cert.pem"
API_KEY_FILE = Path(__file__).with_name("sborka_api_key.txt")
DEFAULT_EMPLOYEE_ID = "20"


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


def generate_preview_filename(order: str | int, employee_id: str = DEFAULT_EMPLOYEE_ID, ext: str = "png") -> str:
    """
    Формирует название файла превью для загрузки в inbox/press/.
    Формат: "номерЗамовлення_твійАйдіСпівробітника.ext" (например: "25661600_20.png")
    """
    clean_order = str(order).strip()
    clean_emp = str(employee_id).strip()
    clean_ext = ext.lstrip(".")
    return f"{clean_order}_{clean_emp}.{clean_ext}"


def return_to_user(
    order: str | int,
    comment_prepress: str = "Тестовый комментарий доработки",
    design_cost: float | int | str = 0,
    preview_priem: str | None = None,
    design: bool = False,
    employee_id: str = DEFAULT_EMPLOYEE_ID,
    timeout: int = 20,
) -> tuple[int, str]:
    """
    Отправляет POST-запрос на action=touser (возврат заказа).
    
    POST Параметры:
      - order: номер заказа
      - comment_prepress: комментарий
      - design_cost: стоимость доработки (по умолчанию 0)
      - preview_priem: имя файла превью в папки inbox/press/ (например "25661600_20.png")
      - design: если True (или "1"), предлагает платную доработку
    """
    api_key = load_api_key()

    if preview_priem is None:
        preview_priem = generate_preview_filename(order, employee_id=employee_id)

    query_params = {
        "action": "touser",
        "api_key": api_key,
    }

    post_payload = {
        "api_key": api_key,
        "order": str(order).strip(),
        "comment_prepress": comment_prepress,
        "design_cost": str(design_cost),
        "preview_priem": preview_priem,
    }

    if design:
        post_payload["design"] = "1"

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Возврат заказа клиенту / на доработку (action=touser)."
    )
    parser.add_argument(
        "order",
        help="Номер заказа (например 25661600).",
    )
    parser.add_argument(
        "--comment",
        "-c",
        default="Тестовый комментарий: требуется доработка макета",
        help="Комментарий доработки (comment_prepress).",
    )
    parser.add_argument(
        "--cost",
        default="0",
        help="Стоимость доработки (design_cost).",
    )
    parser.add_argument(
        "--preview",
        help="Имя файла превью (preview_priem). По умолчанию формируется как 'номерЗаказ_20.png'",
    )
    parser.add_argument(
        "--design",
        action="store_true",
        help="Передать design=1 (предложить доработку дизайнером).",
    )
    args = parser.parse_args()

    preview_file = args.preview or generate_preview_filename(args.order)

    print(f"Заказ: {args.order}")
    print(f"Комментарий: {args.comment}")
    print(f"Файл превью (inbox/press/): {preview_file}")
    print(f"Стоимость доработки: {args.cost}")
    print(f"Флаг доработки (design=1): {args.design}")

    try:
        status, body = return_to_user(
            order=args.order,
            comment_prepress=args.comment,
            design_cost=args.cost,
            preview_priem=preview_file,
            design=args.design,
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
