"""HTTP-ручки «Показать разрешение»: разметка ppi изображений PDF заказа.

``/api/checks/{run_id}/orders/{order_id}/lowres-preview`` — построить (или вернуть из кэша),
``…/image`` и ``…/pdf`` — скачать картинку и размеченный PDF, ``…/attach`` — приложить
картинку к возврату (тем же механизмом, что и «Загрузить превью для возврата»).
Логика — в ``services/lowres_preview.py``; здесь только транспорт.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from server.errors import APIError
from services.lowres_preview import LowResPreviewError, LowResPreviewService, output_paths
from services.return_preview import publish_custom_return_preview

logger = logging.getLogger(__name__)

#: (request, run_id, order_id) -> (run, order, проверенная входная папка запуска)
ResolveOrder = Callable[[Request, str, str], tuple[dict[str, Any], dict[str, Any], Path]]
StorageId = Callable[[dict[str, Any], dict[str, Any]], str]

IN_PROGRESS_MESSAGE = "Заказ ещё проверяется: дождитесь окончания обработки и повторите."
NO_PDF_MESSAGE = (
    "У заказа нет PDF (ни итогового, ни диагностического): разметку разрешения строить не из чего."
)
STALE_MESSAGE = "Разметка устарела: PDF заказа изменился. Нажмите «Показать разрешение» ещё раз."


def _service(request: Request) -> LowResPreviewService:
    service = getattr(request.app.state, "lowres_preview_service", None)
    if service is None:
        raise APIError(
            404,
            "lowres_preview_disabled",
            "Разметка разрешения недоступна: не задан сервер callas "
            "(IMAGE_MAGIC_CALLAS_URL и IMAGE_MAGIC_CALLAS_PROCESS_ENABLED).",
        )
    return service


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _order_pdf(order: dict[str, Any], input_root: Path) -> Path:
    """Текущий PDF заказа: производственный (текущая ревизия) или диагностический."""
    raw = order.get("pdf_path")
    if not raw:
        raise APIError(409, "lowres_no_pdf", NO_PDF_MESSAGE)
    pdf = Path(str(raw)).resolve()
    if not _inside(pdf, input_root) or not pdf.is_file():
        raise APIError(404, "file_not_ready", "PDF заказа не найден: возможно, он уже перемещён.")
    return pdf


def create_lowres_preview_router(
    *, protected: Callable[..., Any], resolve_order: ResolveOrder, storage_id: StorageId,
) -> APIRouter:
    router = APIRouter()

    def base_url(run_id: str, order_id: str) -> str:
        return f"/api/checks/{quote(run_id, safe='')}/orders/{quote(order_id, safe='')}/lowres-preview"

    def refuse_in_progress(request: Request, run_id: str, order: dict[str, Any]) -> None:
        probe = getattr(request.app.state.coordinator, "order_in_progress", None)
        ref = str(order.get("aggregate_id") or order.get("order_id") or "")
        if probe is not None and probe(run_id, ref):
            raise APIError(409, "order_in_progress", IN_PROGRESS_MESSAGE)

    def record(request: Request, run_id: str, order: dict[str, Any], kind: str, **details: Any) -> None:
        try:
            request.app.state.coordinator.repository.append_event(run_id, kind, {
                "aggregate_id": order.get("aggregate_id"), "order_id": order.get("order_id"),
                **{key: value for key, value in details.items() if value is not None},
            })
        except Exception:  # noqa: BLE001 — журнал не должен терять готовый результат
            logger.warning("lowres_preview.event_failed run_id=%s kind=%s", run_id, kind, exc_info=True)

    def payload(result: dict[str, Any], run_id: str, order_id: str, order: dict[str, Any]) -> dict[str, Any]:
        base = base_url(run_id, order_id)
        version = str(result.get("source_sha256") or "")[:12]
        return {
            **result,
            "pdf_kind": order.get("pdf_kind"),
            "pdf_revision": order.get("current_pdf_revision"),
            "image_url": f"{base}/image?v={version}",
            "pdf_url": f"{base}/pdf?v={version}",
            "attach_url": f"{base}/attach",
        }

    def raise_api(error: LowResPreviewError) -> None:
        raise APIError(error.status_code, error.code, str(error)) from error

    @router.post(
        "/api/checks/{run_id}/orders/{order_id}/lowres-preview",
        dependencies=[Depends(protected)],
    )
    def generate_lowres_preview(request: Request, run_id: str, order_id: str) -> dict[str, Any]:
        service = _service(request)
        run, order, input_root = resolve_order(request, run_id, order_id)
        refuse_in_progress(request, run_id, order)
        pdf = _order_pdf(order, input_root)
        try:
            result = service.generate(pdf, input_root=input_root, storage_id=storage_id(run, order))
        except LowResPreviewError as exc:
            logger.warning("lowres_preview.failed run_id=%s order_id=%s code=%s", run_id, order_id, exc.code)
            raise_api(exc)
        if not result.get("cached"):
            record(
                request, run_id, order, "order.lowres_preview.generated",
                status="ready", file=pdf.name, pages=result.get("pages"),
                marked_images=result.get("marked_images"), images=result.get("images"),
                threshold_ppi=result.get("threshold_ppi"), pdf_kind=order.get("pdf_kind"),
                pdf_revision=order.get("current_pdf_revision"),
            )
        return payload(result, run_id, order_id, order)

    def stored_file(request: Request, run_id: str, order_id: str, kind: str) -> Path:
        _service(request)
        run, order, input_root = resolve_order(request, run_id, order_id)
        target = output_paths(input_root, storage_id(run, order))[kind].resolve()
        if not _inside(target, input_root) or not target.is_file():
            raise APIError(404, "lowres_preview_not_found", "Разметка разрешения ещё не построена.")
        return target

    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/lowres-preview/image",
        dependencies=[Depends(protected)],
    )
    def lowres_preview_image(request: Request, run_id: str, order_id: str) -> FileResponse:
        target = stored_file(request, run_id, order_id, "image")
        return FileResponse(
            target,
            media_type="image/jpeg",
            filename=target.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-cache, must-revalidate"},
        )

    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/lowres-preview/pdf",
        dependencies=[Depends(protected)],
    )
    def lowres_preview_pdf(request: Request, run_id: str, order_id: str) -> FileResponse:
        target = stored_file(request, run_id, order_id, "pdf")
        return FileResponse(
            target,
            media_type="application/pdf",
            filename=target.name,
            headers={"Cache-Control": "private, no-cache, must-revalidate"},
        )

    @router.post(
        "/api/checks/{run_id}/orders/{order_id}/lowres-preview/attach",
        dependencies=[Depends(protected)],
    )
    def attach_lowres_preview(request: Request, run_id: str, order_id: str) -> dict[str, Any]:
        """Сделать картинку разметки пользовательским превью возврата (оно побеждает при возврате)."""
        service = _service(request)
        run, order, input_root = resolve_order(request, run_id, order_id)
        refuse_in_progress(request, run_id, order)
        preview_id = storage_id(run, order)
        pdf = _order_pdf(order, input_root)
        current = service.cached(pdf, input_root=input_root, storage_id=preview_id)
        if current is None:
            raise APIError(409, "lowres_preview_stale", STALE_MESSAGE)
        image = output_paths(input_root, preview_id)["image"]
        try:
            content = image.read_bytes()
        except OSError as exc:
            raise APIError(404, "lowres_preview_not_found", "Разметка разрешения ещё не построена.") from exc
        target = publish_custom_return_preview(
            preview_id, input_path=input_root, suffix=".jpg", content=content,
        )
        record(
            request, run_id, order, "order.lowres_preview.attached",
            status="attached", file=pdf.name, preview=target.name,
            marked_images=current.get("marked_images"), pdf_revision=order.get("current_pdf_revision"),
        )
        url = f"/api/checks/{run_id}/orders/{order_id}/return-preview"
        return {"filename": target.name, "url": url, "custom_preview_url": url}

    return router
