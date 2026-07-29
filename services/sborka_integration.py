"""Small adapter for sending an order to sborka.ua prepress."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


logger = logging.getLogger("image_magic.sborka")


class SborkaIntegrationError(RuntimeError):
    """Raised when sborka.ua cannot accept an order for prepress."""


def build_sender(
    api_dir: Path,
    *,
    timeout: int = 20,
) -> Callable[[str | Sequence[str], str | None], dict[str, Any]] | None:
    """Build a sender from the bundled ``sborka_toprepress`` script.

    An absent API key disables the integration, which keeps local development
    and existing installations safe until credentials are configured.
    """
    script_path = api_dir / "sborka_toprepress.py"
    key_path = api_dir / "sborka_api_key.txt"
    if not script_path.is_file() or not key_path.is_file():
        logger.info("sborka integration disabled: credentials or script missing")
        return None

    import importlib.util

    spec = importlib.util.spec_from_file_location("image_magic_sborka_toprepress", script_path)
    if spec is None or spec.loader is None:
        raise SborkaIntegrationError(f"Не удалось загрузить {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def send(
        order_id: str | Sequence[str], comment: str | None = None
    ) -> dict[str, Any]:
        try:
            if isinstance(order_id, (list, tuple)):
                if comment:
                    raise ValueError("Пакетная отправка не поддерживает общий комментарий")
                status, body = module.send_orders_to_prepress(order_id, timeout=timeout)
            else:
                status, body = module.send_to_prepress(
                    order=order_id,
                    comment_prepress=(comment or "").strip(),
                    timeout=timeout,
                )
        except Exception as exc:  # urllib and script configuration errors
            raise SborkaIntegrationError(f"Ошибка отправки заказа в Sborka: {exc}") from exc
        if status < 200 or status >= 300:
            raise SborkaIntegrationError(f"Sborka вернула HTTP {status}: {body or '(пустой ответ)'}")
        try:
            response: Any = json.loads(body) if body else {}
        except json.JSONDecodeError:
            response = {"raw": body}
        return {"http_status": status, "response": response}

    return send
