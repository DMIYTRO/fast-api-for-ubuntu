"""Adapters for the Sborka.ua prepress and rework API actions."""

from __future__ import annotations

import json
import logging
import urllib.error
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


logger = logging.getLogger("image_magic.sborka")


class SborkaIntegrationError(RuntimeError):
    """Raised when sborka.ua cannot accept an order transition."""


def _load_module(api_dir: Path, script_name: str, module_name: str):
    script_path = api_dir / script_name
    if not script_path.is_file():
        return None

    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise SborkaIntegrationError(f"Не удалось загрузить {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_sender(
    api_dir: Path,
    *,
    timeout: int = 20,
) -> Callable[[str | Sequence[str], str | None], dict[str, Any]] | None:
    """Build a sender from the bundled ``sborka_toprepress`` script.

    An absent API key disables the integration, which keeps local development
    and existing installations safe until credentials are configured.
    """
    key_path = api_dir / "sborka_api_key.txt"
    if not (api_dir / "sborka_toprepress.py").is_file() or not key_path.is_file():
        logger.info("sborka integration disabled: credentials or script missing")
        return None
    module = _load_module(
        api_dir, "sborka_toprepress.py", "image_magic_sborka_toprepress"
    )
    assert module is not None

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


def build_rework_sender(
    api_dir: Path,
    *,
    timeout: int = 20,
) -> Callable[[str, str, str, bool, str], dict[str, Any]] | None:
    """Build a sender for ``action=touser``.

    Sborka accepts a single order per request.  ``preview_priem`` is the exact
    filename of the preview produced for that order; its upload to
    ``inbox/press/`` is managed outside this HTTP API.
    """
    key_path = api_dir / "sborka_api_key.txt"
    if not (api_dir / "sborka_touser.py").is_file() or not key_path.is_file():
        logger.info("sborka rework integration disabled: credentials or script missing")
        return None
    module = _load_module(
        api_dir, "sborka_touser.py", "image_magic_sborka_touser"
    )
    assert module is not None

    def send(
        order_id: str,
        comment: str,
        preview_filename: str,
        design: bool = True,
        design_cost: str = "0",
    ) -> dict[str, Any]:
        try:
            status, body = module.return_to_user(
                order=order_id,
                comment_prepress=comment.strip(),
                preview_priem=preview_filename.strip(),
                design=design,
                design_cost=design_cost,
                timeout=timeout,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace").strip()
            raise SborkaIntegrationError(
                f"Sborka вернула HTTP {exc.code}: {body or '(пустой ответ)'}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise SborkaIntegrationError(
                "Sborka не отвечает. Проверьте подключение к интернету и повторите попытку. "
                f"Причина: {exc}"
            ) from exc
        except Exception as exc:  # urllib and script configuration errors
            raise SborkaIntegrationError(
                f"Ошибка возврата заказа в Sborka: {exc}"
            ) from exc
        if status < 200 or status >= 300:
            raise SborkaIntegrationError(
                f"Sborka вернула HTTP {status}: {body or '(пустой ответ)'}"
            )
        try:
            response: Any = json.loads(body) if body else {}
        except json.JSONDecodeError:
            response = {"raw": body}
        return {
            "http_status": status,
            "response": response,
            "preview_priem": preview_filename.strip(),
            "design": design,
            "design_cost": str(design_cost),
        }

    return send
