"""HTTP-ручки исправления PDF планами Callas и превью «до / после».

Маршруты привязаны к запуску проверки, как остальные операции над заказом:
``/api/checks/{run_id}/orders/{order_id}/callas/fix``. Логика — в
``services/callas_fix.py``; здесь только транспорт.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from typing import Any, Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.preview_cache import preview_media_type
from server.errors import APIError
from services.callas_fix import (
    CallasFixError,
    CallasFixService,
    comparison_payload,
)

ResolveOrder = Callable[[Request, str, str, str | None], tuple[dict[str, Any], dict[str, Any], Path]]


class StartFixRequest(BaseModel):
    plan: str


class DecisionRequest(BaseModel):
    decision: Literal["accept", "reject"]


def _service(request: Request) -> CallasFixService:
    service = getattr(request.app.state, "callas_fix_service", None)
    if service is None:
        raise APIError(
            404,
            "callas_fix_disabled",
            "Исправление Callas выключено: не заданы адрес сервера и IMAGE_MAGIC_CALLAS_PROCESS_ENABLED.",
        )
    return service


def _raise(error: CallasFixError) -> None:
    raise APIError(error.status_code, error.code, str(error)) from error


def create_callas_fix_router(*, protected: Callable[..., Any], resolve_order: ResolveOrder) -> APIRouter:
    router = APIRouter()

    def base_url(run_id: str, order_id: str, file_id: str | None = None) -> str:
        base = f"/api/checks/{run_id}/orders/{order_id}/callas/fix"
        return f"{base}/source/{quote(file_id, safe='')}" if file_id else base

    def payload(state: dict[str, Any] | None, run_id: str, order_id: str, file_id: str | None):
        return comparison_payload(
            state, base_url(run_id, order_id, file_id), source_only=bool(file_id)
        )

    def record(request: Request, run_id: str, order: dict[str, Any], kind: str, **details: Any) -> None:
        request.app.state.coordinator.repository.append_event(run_id, kind, {
            "aggregate_id": order.get("aggregate_id"), "order_id": order.get("order_id"),
            **details,
        })

    def execute_recorded(request: Request, run_id: str, order: dict[str, Any], pdf: Path, plan: str, file_id: str | None) -> None:
        service = _service(request)
        try:
            service.execute(pdf, plan)
        finally:
            state = service.read_state(pdf) or {}
            record(request, run_id, order, "order.callas_fix.finished",
                   plan=plan, file_id=file_id, status=state.get("status", "failed"))

    @router.get("/api/callas/plans", dependencies=[Depends(protected)])
    def callas_plans(request: Request, direction: str | None = None) -> dict[str, Any]:
        service = _service(request)
        return {
            "plans": [
                {"key": plan.key, "title": plan.title, "requires_confirmation": plan.requires_confirmation}
                for plan in service.plans(direction)
            ]
        }

    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix",
        dependencies=[Depends(protected)],
    )
    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/source/{file_id}",
        dependencies=[Depends(protected)],
    )
    def callas_fix_status(
        request: Request, run_id: str, order_id: str, file_id: str | None = None
    ) -> dict[str, Any]:
        service = _service(request)
        _run, _order, pdf = resolve_order(request, run_id, order_id, file_id)
        return {"comparison": payload(service.read_state(pdf), run_id, order_id, file_id)}

    @router.post(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix",
        dependencies=[Depends(protected)],
        status_code=202,
    )
    @router.post(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/source/{file_id}",
        dependencies=[Depends(protected)],
        status_code=202,
    )
    def start_callas_fix(
        request: Request,
        background_tasks: BackgroundTasks,
        run_id: str,
        order_id: str,
        body: StartFixRequest,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        service = _service(request)
        run, _order, pdf = resolve_order(request, run_id, order_id, file_id)
        direction = str((run.get("options") or {}).get("direction") or "") or None
        try:
            state = service.start(pdf, body.plan, direction=direction)
        except CallasFixError as exc:
            _raise(exc)
        record(request, run_id, _order, "order.callas_fix.started", plan=body.plan, file_id=file_id, status="processing")
        background_tasks.add_task(execute_recorded, request, run_id, _order, pdf, body.plan, file_id)
        return {"comparison": payload(state, run_id, order_id, file_id)}

    @router.post(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/decision",
        dependencies=[Depends(protected)],
    )
    @router.post(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/source/{file_id}/decision",
        dependencies=[Depends(protected)],
    )
    def decide_callas_fix(
        request: Request,
        run_id: str,
        order_id: str,
        body: DecisionRequest,
        file_id: str | None = None,
    ) -> dict[str, Any]:
        service = _service(request)
        run, order, pdf = resolve_order(request, run_id, order_id, file_id)
        if file_id:
            raise APIError(
                409,
                "callas_fix_source_only",
                "У заказа нет собранного PDF: исправленный PDF можно скачать и проверить заново, "
                "принять его в печать из этого блока нельзя.",
            )
        try:
            with request.app.state.order_workflow.correction_guard(run_id, order):
                previous_state = service.read_state(pdf)
                state = service.decide(pdf, body.decision)
                if body.decision == "accept":
                    candidate = None
                    try:
                        candidate = service.publish_accepted(pdf)
                        if candidate is not None:
                            request.app.state.order_workflow.recheck_pdf_revision(run, order, candidate)
                    except Exception as exc:
                        if candidate is not None:
                            candidate.unlink(missing_ok=True)
                        if previous_state is not None:
                            service.restore_decision(pdf, previous_state)
                        raise APIError(409, "callas_fix_save_failed", str(exc)) from exc
        except CallasFixError as exc:
            _raise(exc)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(409, "callas_fix_save_failed", str(exc)) from exc
        record(request, run_id, order, f"order.callas_fix.{state.get('status')}", decision=body.decision, status=state.get("status"))
        return {"comparison": payload(state, run_id, order_id, None)}

    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/files/{kind}",
        dependencies=[Depends(protected)],
    )
    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/source/{file_id}/files/{kind}",
        dependencies=[Depends(protected)],
    )
    def callas_fix_file(
        request: Request, run_id: str, order_id: str, kind: str, file_id: str | None = None
    ) -> FileResponse:
        service = _service(request)
        _run, _order, pdf = resolve_order(request, run_id, order_id, file_id)
        target = service.file_path(pdf, kind)
        if target is None:
            raise APIError(404, "file_not_found", "Файл исправления не найден.")
        if kind == "pdf":
            return FileResponse(
                target,
                media_type="application/pdf",
                filename=target.name,
                content_disposition_type="inline",
            )
        return FileResponse(
            target,
            media_type=preview_media_type(target),
            headers={"Cache-Control": "private, no-cache, must-revalidate"},
        )

    return router
