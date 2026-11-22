"""HTTP-ручки «Получить заказ»: перенос файлов заказов из сетевых папок во входную папку.

``POST /api/order-fetch`` запускает задачу в фоне (202 + снимок прогресса), ``GET`` возвращает
снимок текущей или последней задачи, а без задач — ``{"state": "idle"}``.
Логика переноса — в ``services/order_fetch.py``; здесь только транспорт.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request

from server.errors import APIError
from services.order_fetch import FetchAlreadyRunning

logger = logging.getLogger("image_magic.order_fetch.api")

RUNNING_MESSAGE = "Получение заказов уже идёт"
DISABLED_MESSAGE = "Получение заказов недоступно: сервис не настроен."


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "order_fetch_service", None)
    if service is None:
        raise APIError(404, "order_fetch_disabled", DISABLED_MESSAGE)
    return service


def create_order_fetch_router(*, protected: Callable[..., Any]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/order-fetch", status_code=202, dependencies=[Depends(protected)])
    def start_order_fetch(request: Request) -> dict[str, Any]:
        service = _service(request)
        try:
            snapshot = service.start()
        except FetchAlreadyRunning as exc:
            raise APIError(409, "order_fetch_running", RUNNING_MESSAGE) from exc
        except Exception as exc:  # noqa: BLE001 — оператору нужна причина, а не 500 без текста
            logger.exception("order_fetch.start_failed")
            raise APIError(
                500, "order_fetch_failed", f"Не удалось запустить получение заказов: {exc}"
            ) from exc
        logger.info("order_fetch.started job_id=%s", (snapshot or {}).get("job_id"))
        return snapshot

    @router.get("/api/order-fetch", dependencies=[Depends(protected)])
    def order_fetch_status(request: Request) -> dict[str, Any]:
        snapshot = _service(request).status()
        return snapshot if snapshot is not None else {"state": "idle"}

    return router
