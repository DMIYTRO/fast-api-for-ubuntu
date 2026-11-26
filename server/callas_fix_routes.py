"""HTTP-ручки исправления PDF планами Callas и превью «до / после».

Маршруты привязаны к запуску проверки, как остальные операции над заказом:
``/api/checks/{run_id}/orders/{order_id}/callas/fix``. Логика — в
``services/callas_fix.py``; здесь только транспорт.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote
from typing import Any, Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.preview_cache import preview_media_type
from server.errors import APIError
from services.callas_bleed import order_fix_context, summary_bleed_only
from services.callas_fix import (
    CallasFixError,
    CallasFixService,
    comparison_payload,
)
from services.callas_source_fix import (
    NOT_ACCEPTABLE_MESSAGE,
    entry_applied,
    order_accepts_source_fix,
    source_comparison,
    source_entry_for,
)

logger = logging.getLogger(__name__)

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


def _built_pdf_run(run_id: str, file_id: str | None) -> str | None:
    """Проверка, к которой привязано исправление: только для собранного PDF заказа.

    Исправления исходных файлов (``file_id``) намеренно переходят между проверками
    (сверка по подписи файла), поэтому для них фильтра по проверке нет.
    """
    return None if file_id else run_id


def _raise(error: CallasFixError) -> None:
    raise APIError(error.status_code, error.code, str(error)) from error


def create_callas_fix_router(*, protected: Callable[..., Any], resolve_order: ResolveOrder) -> APIRouter:
    router = APIRouter()

    def base_url(run_id: str, order_id: str, file_id: str | None = None) -> str:
        base = f"/api/checks/{run_id}/orders/{order_id}/callas/fix"
        return f"{base}/source/{quote(file_id, safe='')}" if file_id else base

    def payload(
        state: dict[str, Any] | None,
        run_id: str,
        order_id: str,
        file_id: str | None,
        run: dict[str, Any] | None = None,
        order: dict[str, Any] | None = None,
        pdf: Path | None = None,
    ):
        if file_id and run is not None and order is not None and pdf is not None:
            return source_comparison(
                state, base_url(run_id, order_id, file_id), run, order, source_entry_for(order, pdf)
            )
        return comparison_payload(
            state, base_url(run_id, order_id, file_id), source_only=bool(file_id)
        )

    def fresh_order(request: Request, run_id: str, order_id: str, file_id: str | None):
        """Запуск и заказ после действия координатора (для ответа с актуальным состоянием)."""
        run, order, _pdf = resolve_order(request, run_id, order_id, file_id)
        return run, order

    def record(request: Request, run_id: str, order: dict[str, Any], kind: str, **details: Any) -> None:
        request.app.state.coordinator.repository.append_event(run_id, kind, {
            "aggregate_id": order.get("aggregate_id"), "order_id": order.get("order_id"),
            **details,
        })

    def execute_recorded(
        request: Request,
        run_id: str,
        run: dict[str, Any],
        order: dict[str, Any],
        pdf: Path,
        plan: str,
        file_id: str | None,
    ) -> None:
        service = _service(request)
        # Формат из имени файла и вылет профиля — для вердикта «только вылеты».
        entry = source_entry_for(order, pdf) if file_id else None
        format_mm, bleed_mm = order_fix_context(run, order, entry)
        settings = getattr(request.app.state, "settings", None)
        try:
            service.execute(
                pdf, plan,
                format_mm=format_mm,
                bleed_mm=bleed_mm,
                strip_icc_cmyk=bool(getattr(settings, "strip_icc_cmyk", False)),
            )
        finally:
            state = service.read_state(pdf) or {}
            record(request, run_id, order, "order.callas_fix.finished",
                   plan=plan, file_id=file_id, status=state.get("status", "failed"),
                   **({"error_code": state["error_code"]} if state.get("error_code") else {}))

    @router.get("/api/callas/plans", dependencies=[Depends(protected)])
    def callas_plans(request: Request, direction: str | None = None) -> dict[str, Any]:
        service = _service(request)
        return {
            "plans": [
                {
                    "key": plan.key, "title": plan.title,
                    "requires_confirmation": plan.requires_confirmation,
                    "description": plan.description, "warning": plan.warning,
                    "requires_cmyk": plan.requires_cmyk,
                }
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
        run, order, pdf = resolve_order(request, run_id, order_id, file_id)
        state = service.read_state(pdf, run_id=_built_pdf_run(run_id, file_id))
        return {"comparison": payload(state, run_id, order_id, file_id, run, order, pdf)}

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
            state = service.start(
                pdf, body.plan, direction=direction,
                run_id=_built_pdf_run(run_id, file_id),
                order_id=None if file_id else str(_order.get("order_id") or order_id),
            )
        except CallasFixError as exc:
            _raise(exc)
        record(request, run_id, _order, "order.callas_fix.started", plan=body.plan, file_id=file_id, status="processing")
        if file_id and entry_applied(source_entry_for(_order, pdf)) and order_accepts_source_fix(run, _order):
            # Новый запуск сбрасывает решение: заказ снова проверяет исходный файл.
            try:
                request.app.state.coordinator.apply_source_fix(
                    run_id, order_id, source_path=pdf, content_path=None
                )
            except Exception:  # noqa: BLE001 — исправление уже запущено, ответ не теряем
                logger.warning("callas_fix.source_reset_failed run_id=%s file_id=%s", run_id, file_id, exc_info=True)
            else:
                record(request, run_id, _order, "order.callas_fix.reset", file_id=file_id, file=pdf.name, status="processing")
        background_tasks.add_task(execute_recorded, request, run_id, run, _order, pdf, body.plan, file_id)
        run, _order = fresh_order(request, run_id, order_id, file_id)
        return {"comparison": payload(state, run_id, order_id, file_id, run, _order, pdf)}

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
            return decide_source_fix(request, service, run, order, pdf, run_id, order_id, file_id, body.decision)
        try:
            with request.app.state.order_workflow.correction_guard(run_id, order):
                previous_state = service.read_state(pdf)
                state = service.decide(pdf, body.decision, run_id=run_id)
                if body.decision == "accept":
                    candidate = None
                    try:
                        candidate = service.publish_accepted(pdf, run_id=run_id)
                        if candidate is not None:
                            _updated, _pitstop, revision, sha256 = (
                                request.app.state.order_workflow.recheck_pdf_revision(
                                    run, order, candidate, callas_fix=True,
                                    bleed_only=summary_bleed_only(state),
                                )
                            )
                            service.mark_published(pdf, sha256=sha256, revision=revision)
                    except Exception as exc:
                        if candidate is not None:
                            candidate.unlink(missing_ok=True)
                        if previous_state is not None:
                            service.restore_decision(pdf, previous_state)
                        record(request, run_id, order, "order.callas_fix.save_failed", decision=body.decision, status="failed")
                        raise APIError(409, "callas_fix_save_failed", str(exc)) from exc
        except CallasFixError as exc:
            _raise(exc)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(409, "callas_fix_save_failed", str(exc)) from exc
        record(request, run_id, order, f"order.callas_fix.{state.get('status')}", decision=body.decision, status=state.get("status"))
        return {"comparison": payload(state, run_id, order_id, None)}

    def decide_source_fix(
        request: Request,
        service: CallasFixService,
        run: dict[str, Any],
        order: dict[str, Any],
        pdf: Path,
        run_id: str,
        order_id: str,
        file_id: str,
        decision: str,
    ) -> dict[str, Any]:
        """Решение по исправлению исходного PDF заказа, который ждёт решения по коррекции.

        Принятие: файл собирается из копии исправленного PDF (см.
        ``services/callas_source_fix.py``); исходник не меняется. Отклонение только
        отмечает результат отклонённым.
        """
        if decision == "accept" and not order_accepts_source_fix(run, order):
            raise APIError(409, "callas_fix_source_only", NOT_ACCEPTABLE_MESSAGE)
        try:
            with request.app.state.order_workflow.correction_guard(run_id, order):
                previous_state = service.read_state(pdf)
                state = service.decide(
                    pdf, decision, redecide_accepted=not entry_applied(source_entry_for(order, pdf))
                )
                if decision == "accept":
                    try:
                        content, fix = service.accepted_source_copy(pdf)
                        request.app.state.coordinator.apply_source_fix(
                            run_id, order_id, source_path=pdf, content_path=content, fix=fix
                        )
                    except Exception as exc:
                        if previous_state is not None:
                            service.restore_decision(pdf, previous_state)
                        record(request, run_id, order, "order.callas_fix.save_failed",
                               decision=decision, file_id=file_id, file=pdf.name, status="failed")
                        raise APIError(409, "callas_fix_save_failed", str(exc) or type(exc).__name__) from exc
        except CallasFixError as exc:
            _raise(exc)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(409, "callas_fix_save_failed", str(exc)) from exc
        record(request, run_id, order, f"order.callas_fix.{state.get('status')}",
               decision=decision, file_id=file_id, file=pdf.name, status=state.get("status"))
        run, order = fresh_order(request, run_id, order_id, file_id)
        return {"comparison": payload(service.read_state(pdf), run_id, order_id, file_id, run, order, pdf)}

    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/files/{kind}",
        dependencies=[Depends(protected)],
    )
    @router.get(
        "/api/checks/{run_id}/orders/{order_id}/callas/fix/source/{file_id}/files/{kind}",
        dependencies=[Depends(protected)],
    )
    def callas_fix_file(
        request: Request,
        run_id: str,
        order_id: str,
        kind: str,
        file_id: str | None = None,
        page: int = Query(1, ge=1, le=999),
    ) -> FileResponse:
        service = _service(request)
        _run, _order, pdf = resolve_order(request, run_id, order_id, file_id)
        target = service.file_path(pdf, kind, page=page, run_id=_built_pdf_run(run_id, file_id))
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
