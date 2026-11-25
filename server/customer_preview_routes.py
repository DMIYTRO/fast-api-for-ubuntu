"""HTTP-ручка схемы постпечатки, загруженной заказчиком в Sborka.

``GET /api/checks/{run_id}/orders/{order_id}/customer-preview`` отдаёт сохранённую
картинку (``order["customer_preview"]["file"]``, services/customer_preview.py) —
только если она лежит внутри входной папки запуска под ``Previews/Customer``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from server.errors import APIError
from services.customer_preview import PREVIEW_DIR

#: (request, run_id, order_id) -> (run, order, проверенная входная папка запуска)
ResolveOrder = Callable[[Request, str, str], tuple[dict[str, Any], dict[str, Any], Path]]

NOT_FOUND_MESSAGE = "Схема заказчика не найдена."
_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def stored_customer_preview(order: dict[str, Any], input_root: Path) -> Path | None:
    """Файл схемы заказа внутри ``<входная папка>/Previews/Customer`` или ``None``."""
    preview = order.get("customer_preview")
    if not isinstance(preview, dict) or not preview.get("file"):
        return None
    root = input_root.resolve().joinpath(*PREVIEW_DIR)
    raw = Path(str(preview["file"]))
    target = (raw if raw.is_absolute() else input_root / raw).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if target.suffix.lower() not in _MEDIA_TYPES or not target.is_file():
        return None
    return target


def create_customer_preview_router(
    *, protected: Callable[..., Any], resolve_order: ResolveOrder,
) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/customer-preview",
        dependencies=[Depends(protected)],
    )
    def customer_preview(request: Request, run_id: str, order_id: str) -> FileResponse:
        _run, order, input_root = resolve_order(request, run_id, order_id)
        target = stored_customer_preview(order, input_root)
        if target is None:
            raise APIError(404, "customer_preview_not_found", NOT_FOUND_MESSAGE)
        return FileResponse(
            target,
            media_type=_MEDIA_TYPES[target.suffix.lower()],
            filename=target.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-cache, must-revalidate"},
        )

    return router
