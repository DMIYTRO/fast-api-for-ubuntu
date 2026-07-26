"""Authenticated FastAPI application for the Image Magic operator console."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import asyncio
import json
import logging
import os
from pathlib import Path
import resource
import shutil
import time
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from config.profiles import DEFAULT_DIRECTION, PROFILES
from core.return_reasons import load_return_reasons
from server.auth import AuthService, create_auth_router, require_session
from server.database import Database, configure_database, get_db, upgrade_database
from server.errors import APIError, error_payload, install_error_handlers
from server.logging_config import configure_logging
from server.models import OrderAction, OrderResult
from server.settings import Settings
from services import (
    InvalidRunStateError,
    ProcessingOptions,
    RunCoordinator,
    RunNotFoundError,
)
from services.repository import InMemoryRunRepository, RunRepository

try:
    from services.sql_repository import SqlRunRepository
except ImportError:  # The in-memory fallback keeps development imports usable.
    SqlRunRepository = None  # type: ignore[assignment,misc]


PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"
LEGACY_UI_PATH = PROJECT_DIR / "web_ui" / "control-panel.html"
DEFAULT_PORT = 8006
logger = logging.getLogger("image_magic.web")


def _configured_roots() -> tuple[Path, ...]:
    configured = os.environ.get("IMAGE_MAGIC_ALLOWED_ROOTS", "")
    roots = [
        Path(value).expanduser().resolve()
        for value in configured.split(os.pathsep)
        if value.strip()
    ]
    return tuple(roots or [PROJECT_DIR.resolve()])


ALLOWED_ROOTS = _configured_roots()
DEFAULT_INPUT_DIR = Path(
    os.environ.get("IMAGE_MAGIC_INPUT_DIR", PROJECT_DIR / "input_files")
).expanduser().resolve()


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_input_path(
    value: str,
    *,
    allowed_roots: tuple[Path, ...] | None = None,
    default_input_dir: Path | None = None,
) -> Path:
    roots = allowed_roots or ALLOWED_ROOTS
    default_dir = default_input_dir or DEFAULT_INPUT_DIR
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = default_dir / candidate
    candidate = candidate.resolve()
    if not any(_is_inside(candidate, root) for root in roots):
        raise APIError(
            403,
            "folder_not_allowed",
            "Папка находится за пределами разрешённой рабочей области.",
        )
    if not candidate.is_dir():
        raise APIError(404, "folder_not_found", "Папка с заказами не найдена.")
    return candidate


class CheckOptions(BaseModel):
    input_path: str
    direction: str = DEFAULT_DIRECTION
    approve_corrections: bool = False
    correction_policy: Literal["ask", "auto", "reject"] = "ask"
    create_pdfs: bool = True
    generate_previews: bool = True
    copy_failures: bool = True


class CorrectionRequest(BaseModel):
    decision: Literal["confirm", "reject"]


class OrderActionRequest(BaseModel):
    order_ids: list[str] = Field(min_length=1)
    run_id: str | None = None
    comment: str | None = None


def _json_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_payload(code, message))


def _get_coordinator(request: Request) -> RunCoordinator:
    return request.app.state.coordinator


def _get_roots(request: Request) -> tuple[Path, ...]:
    return request.app.state.allowed_roots


def _get_default_input(request: Request) -> Path:
    return request.app.state.default_input_dir


def _get_run_or_404(coordinator: RunCoordinator, run_id: str) -> dict[str, Any]:
    try:
        return coordinator.get_run(run_id)
    except RunNotFoundError as exc:
        raise APIError(404, "run_not_found", "Запуск проверки не найден.") from exc


def _orders_from_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    orders = run.get("orders") or {}
    values = list(orders.values()) if isinstance(orders, dict) else list(orders)
    run_id = str(run.get("id") or "")
    for order in values:
        order_id = str(order.get("order_id") or order.get("id") or "")
        order["html_url"] = f"/runs/{run_id}/report"
        order["json_url"] = f"/api/checks/{run_id}/export.json"
        if order.get("actions"):
            latest = order["actions"][-1]
            order["action_result"] = {
                "order_id": order_id,
                "status": latest.get("status"),
                "action": latest.get("action"),
            }
        if order.get("pdf_path"):
            order["pdf_url"] = f"/api/orders/{order_id}/pdf?run_id={run_id}"
        for index, item in enumerate(order.get("files") or []):
            file_id = str(item.get("id") or f"{run_id}:{order_id}:{index}")
            item["id"] = file_id
            item["filename"] = item.get("filename") or item.get("name")
            parsed = item.get("parsed") or {}
            item["side"] = item.get("side") or parsed.get("side")
            item["format"] = item.get("format") or item.get("actual_format")
            item["width_mm"] = item.get("width_mm") or item.get("actual_width_mm")
            item["height_mm"] = item.get("height_mm") or item.get("actual_height_mm")
            item["source_url"] = f"/api/files/{file_id}/source"
            if item.get("preview_path") or order.get("preview_paths"):
                item["preview_url"] = f"/api/files/{file_id}/preview"
    return values


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    options = run.get("options") or {}
    run["input_path"] = run.get("input_path") or options.get("input_path")
    run["direction"] = run.get("direction") or options.get("direction")
    run["html_url"] = f"/runs/{run['id']}/report"
    run["json_url"] = f"/api/checks/{run['id']}/export.json"
    run["report_ready"] = run.get("status") in {"completed", "failed", "cancelled"}
    return run


def _safe_result_path(run: dict[str, Any], raw_path: str | None) -> Path:
    if not raw_path:
        raise APIError(404, "file_not_ready", "Файл ещё не готов.")
    root = Path(run["options"]["input_path"]).resolve()
    target = Path(raw_path).resolve()
    if not _is_inside(target, root) or not target.is_file():
        raise APIError(404, "file_not_found", "Файл не найден.")
    return target


def _find_order(
    coordinator: RunCoordinator, order_id: str, run_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    runs = [_get_run_or_404(coordinator, run_id)] if run_id else coordinator.list_runs()
    runs = sorted(runs, key=lambda item: item.get("created_at") or "", reverse=True)
    for run in runs:
        orders = run.get("orders") or {}
        if isinstance(orders, dict) and order_id in orders:
            return run, orders[order_id]
        for order in _orders_from_run(run):
            if str(order.get("order_id") or order.get("id")) == order_id:
                return run, order
    raise APIError(404, "order_not_found", "Заказ не найден.")


def _find_file(
    coordinator: RunCoordinator, file_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    for run in sorted(
        coordinator.list_runs(),
        key=lambda item: item.get("created_at") or "",
        reverse=True,
    ):
        for order in _orders_from_run(run):
            for index, item in enumerate(order.get("files") or []):
                candidates = {
                    str(item.get("id") or ""),
                    str(item.get("file_id") or ""),
                    f"{run['id']}:{order.get('order_id')}:{index}",
                }
                if file_id in candidates:
                    return run, item
    raise APIError(404, "file_not_found", "Файл не найден.")


def _html_report(run: dict[str, Any]) -> str:
    rows = []
    for order in _orders_from_run(run):
        errors = [*order.get("errors", []), *order.get("processing_errors", [])]
        warnings = order.get("warnings", [])
        rows.append(
            "<article><h2>Заказ {}</h2><p>Клиент: {} · Статус: {}</p>"
            "<h3>Ошибки</h3><ul>{}</ul><h3>Предупреждения</h3><ul>{}</ul></article>".format(
                _escape(order.get("order_id")),
                _escape(order.get("customer_id")),
                _escape(order.get("status")),
                "".join(f"<li>{_escape(value)}</li>" for value in errors) or "<li>Нет</li>",
                "".join(f"<li>{_escape(value)}</li>" for value in warnings) or "<li>Нет</li>",
            )
        )
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Image Magic — отчёт</title><style>
body{{font:15px/1.5 system-ui;margin:32px;color:#172033;background:#f3f5f9}}
header,article{{max-width:1000px;margin:0 auto 18px;background:white;padding:22px;border-radius:12px}}
h1,h2{{margin-top:0}}li{{margin:5px 0}}.meta{{color:#667085}}
</style></head><body><header><h1>Отчёт Image Magic</h1>
<p class="meta">Папка: {} · Статус: {} · Создан: {}</p></header>{}</body></html>""".format(
        _escape(run.get("options", {}).get("input_path")),
        _escape(run.get("status")),
        _escape(run.get("created_at")),
        "".join(rows) or "<article>Заказы не найдены.</article>",
    )


def _escape(value: Any) -> str:
    import html

    return html.escape(str(value if value is not None else "—"))


def _default_repository(database: Database) -> RunRepository:
    if SqlRunRepository is not None:
        return SqlRunRepository(database.session_factory)
    return InMemoryRunRepository()


def create_app(
    *,
    settings: Settings | None = None,
    repository: RunRepository | None = None,
    allowed_roots: tuple[Path, ...] | None = None,
    default_input_dir: Path | None = None,
    adapter_factory=None,
    initialize_schema: bool = False,
) -> FastAPI:
    settings = settings or Settings.from_env()
    log_path = configure_logging(settings)
    logger.info("application.initializing")
    if not initialize_schema:
        upgrade_database(settings)
    database = configure_database(settings)
    if initialize_schema:
        database.create_schema()
    repository = repository or _default_repository(database)
    coordinator_options: dict[str, Any] = {"autostart": False}
    if adapter_factory is not None:
        coordinator_options["adapter_factory"] = adapter_factory
    coordinator = RunCoordinator(repository, **coordinator_options)
    auth_service = AuthService(settings)
    protected = require_session(settings, auth_service)

    async def heartbeat() -> None:
        while True:
            active = coordinator.get_active_run()
            disk = shutil.disk_usage(PROJECT_DIR)
            max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            logger.info(
                "application.heartbeat active_run_id=%s status=%s stage=%s "
                "progress=%s disk_free_gb=%.1f max_rss_mb=%.1f",
                active.get("id") if active else "-",
                active.get("status") if active else "idle",
                active.get("stage") if active else "-",
                active.get("progress") if active else "-",
                disk.free / (1024**3),
                max_rss / (1024**2),
            )
            await asyncio.sleep(settings.log_heartbeat_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("application.starting")
        coordinator.start_worker()
        heartbeat_task = asyncio.create_task(
            heartbeat(), name="image-magic-heartbeat"
        )
        try:
            yield
        finally:
            logger.info("application.stopping")
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            coordinator.shutdown(timeout=None)
            database.dispose()
            logger.info("application.stopped")

    application = FastAPI(
        title="Image Magic Control",
        version="2.0.0",
        description="Локальный сервис допечатной проверки заказов.",
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.database = database
    application.state.coordinator = coordinator
    application.state.log_path = log_path
    application.state.allowed_roots = allowed_roots or ALLOWED_ROOTS
    application.state.default_input_dir = default_input_dir or DEFAULT_INPUT_DIR
    install_error_handlers(application)

    @application.middleware("http")
    async def journal_request(request: Request, call_next):
        request_id = uuid4().hex[:12]
        started = time.monotonic()
        client = request.client.host if request.client else "unknown"
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "http.failed request_id=%s method=%s path=%s client=%s "
                "duration_ms=%.1f",
                request_id,
                request.method,
                request.url.path,
                client,
                (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "http.completed request_id=%s method=%s path=%s status=%d "
            "client=%s duration_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            client,
            (time.monotonic() - started) * 1000,
        )
        response.headers["X-Request-ID"] = request_id
        return response

    @application.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Запрос не выполнен."
        return _json_error(exc.status_code, "http_error", detail)

    @application.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _json_error(422, "validation_error", "Проверьте заполненные поля.")

    application.include_router(create_auth_router(settings, auth_service))

    @application.get("/api/config", dependencies=[Depends(protected)])
    def get_config(request: Request) -> dict[str, Any]:
        return {
            "default_input_path": str(_get_default_input(request)),
            "allowed_roots": [str(root) for root in _get_roots(request)],
            "profiles": [
                {"id": key, "name": value.name} for key, value in PROFILES.items()
            ],
            "return_reasons": load_return_reasons(),
        }

    @application.get("/api/folders", dependencies=[Depends(protected)])
    def list_folders(request: Request, path: str = "") -> dict[str, Any]:
        current = resolve_input_path(
            path or str(_get_default_input(request)),
            allowed_roots=_get_roots(request),
            default_input_dir=_get_default_input(request),
        )
        folders = sorted(
            (
                child
                for child in current.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            ),
            key=lambda value: value.name.casefold(),
        )
        return {
            "path": str(current),
            "parent": (
                str(current.parent)
                if any(_is_inside(current.parent, root) for root in _get_roots(request))
                else None
            ),
            "items": [{"name": item.name, "path": str(item)} for item in folders],
        }

    @application.post(
        "/api/checks", status_code=202, dependencies=[Depends(protected)]
    )
    def start_check(
        request: Request, options: CheckOptions
    ) -> dict[str, Any]:
        if options.direction not in PROFILES:
            raise APIError(422, "unknown_profile", "Неизвестный профиль проверки.")
        input_dir = resolve_input_path(
            options.input_path,
            allowed_roots=_get_roots(request),
            default_input_dir=_get_default_input(request),
        )
        result = _get_coordinator(request).submit(
            ProcessingOptions(
                input_path=str(input_dir),
                direction=options.direction,
                approve_corrections=options.approve_corrections,
                correction_policy=(
                    "auto" if options.approve_corrections else options.correction_policy
                ),
                create_pdfs=options.create_pdfs,
                generate_previews=options.generate_previews,
                copy_failures=options.copy_failures,
            )
        )
        return _public_run(result)

    @application.get("/api/checks", dependencies=[Depends(protected)])
    def list_checks(request: Request) -> dict[str, Any]:
        items = sorted(
            _get_coordinator(request).list_runs(),
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )
        return {"items": [_public_run(item) for item in items]}

    @application.get("/api/checks/{run_id}", dependencies=[Depends(protected)])
    def get_check(request: Request, run_id: str) -> dict[str, Any]:
        return _public_run(_get_run_or_404(_get_coordinator(request), run_id))

    @application.post(
        "/api/checks/{run_id}/cancel", dependencies=[Depends(protected)]
    )
    def cancel_check(request: Request, run_id: str) -> dict[str, Any]:
        try:
            return _public_run(_get_coordinator(request).cancel(run_id))
        except RunNotFoundError as exc:
            raise APIError(404, "run_not_found", "Запуск проверки не найден.") from exc

    @application.get(
        "/api/checks/{run_id}/orders", dependencies=[Depends(protected)]
    )
    def list_orders(request: Request, run_id: str) -> dict[str, Any]:
        run = _get_run_or_404(_get_coordinator(request), run_id)
        return {"items": _orders_from_run(run)}

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}",
        dependencies=[Depends(protected)],
    )
    def get_order(request: Request, run_id: str, order_id: str) -> dict[str, Any]:
        run = _get_run_or_404(_get_coordinator(request), run_id)
        for order in _orders_from_run(run):
            if str(order.get("order_id") or order.get("id")) == order_id:
                return order
        raise APIError(404, "order_not_found", "Заказ не найден.")

    @application.post(
        "/api/checks/{run_id}/orders/{order_id}/correction",
        dependencies=[Depends(protected)],
    )
    def decide_correction(
        request: Request,
        run_id: str,
        order_id: str,
        payload: CorrectionRequest,
    ) -> dict[str, Any]:
        coordinator_value = _get_coordinator(request)
        try:
            if payload.decision == "confirm":
                return _public_run(
                    coordinator_value.confirm_correction(run_id, order_id)
                )
            return _public_run(coordinator_value.reject_correction(run_id, order_id))
        except RunNotFoundError as exc:
            raise APIError(404, "order_not_found", "Заказ не найден.") from exc
        except InvalidRunStateError as exc:
            raise APIError(409, "invalid_run_state", str(exc)) from exc

    @application.get(
        "/api/checks/{run_id}/events", dependencies=[Depends(protected)]
    )
    async def stream_events(
        request: Request,
        run_id: str,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        coordinator_value = _get_coordinator(request)
        _get_run_or_404(coordinator_value, run_id)
        try:
            cursor = max(0, int(last_event_id or "0"))
        except ValueError:
            cursor = 0

        async def generate():
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                events = coordinator_value.events(run_id, cursor)
                if events:
                    for event in events:
                        cursor = event.id
                        yield event.as_sse()
                    heartbeat = 0
                else:
                    heartbeat += 1
                    if heartbeat >= 15:
                        yield ": keep-alive\n\n"
                        heartbeat = 0
                run = coordinator_value.get_run(run_id)
                if run["status"] in {"completed", "failed", "cancelled"} and not events:
                    break
                await asyncio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @application.get(
        "/api/files/{file_id}/source", dependencies=[Depends(protected)]
    )
    def source_file(request: Request, file_id: str) -> FileResponse:
        run, item = _find_file(_get_coordinator(request), file_id)
        return FileResponse(_safe_result_path(run, item.get("path")))

    @application.get(
        "/api/files/{file_id}/preview", dependencies=[Depends(protected)]
    )
    def preview_file(request: Request, file_id: str) -> FileResponse:
        run, item = _find_file(_get_coordinator(request), file_id)
        preview = item.get("preview_path")
        if not preview:
            order_id = item.get("parsed", {}).get("order_id") if item.get("parsed") else None
            _, order = _find_order(_get_coordinator(request), str(order_id), run["id"])
            previews = order.get("preview_paths") or []
            side = item.get("parsed", {}).get("side") if item.get("parsed") else None
            preview = next(
                (path for path in previews if side and side in Path(path).stem),
                previews[0] if previews else None,
            )
        return FileResponse(_safe_result_path(run, preview))

    @application.get(
        "/api/orders/{order_id}/pdf", dependencies=[Depends(protected)]
    )
    def order_pdf(
        request: Request, order_id: str, run_id: str | None = None
    ) -> FileResponse:
        run, order = _find_order(_get_coordinator(request), order_id, run_id)
        return FileResponse(_safe_result_path(run, order.get("pdf_path")))

    def prepare_action(
        request: Request, payload: OrderActionRequest, action: str
    ) -> dict[str, Any]:
        if action == "reject" and not (payload.comment or "").strip():
            raise APIError(
                422, "comment_required", "Для возврата требуется комментарий."
            )
        coordinator_value = _get_coordinator(request)
        results = []
        with request.app.state.database.session_factory() as session:
            for order_id in payload.order_ids:
                run, order = _find_order(coordinator_value, order_id, payload.run_id)
                if action == "print" and not (
                    order.get("passed") or order.get("status") in {"passed", "warning"}
                ):
                    results.append(
                        {"order_id": order_id, "status": "rejected", "message": "Заказ не прошёл проверку."}
                    )
                    continue
                stored = session.scalar(
                    select(OrderResult)
                    .where(
                        OrderResult.run_id == run["id"],
                        OrderResult.order_id == order_id,
                    )
                    .order_by(desc(OrderResult.updated_at))
                )
                if stored is None:
                    results.append(
                        {"order_id": order_id, "status": "not_found"}
                    )
                    continue
                session.add(
                    OrderAction(
                        order_result_id=stored.id,
                        action=action,
                        comment=(payload.comment or "").strip() or None,
                        status="prepared",
                    )
                )
                results.append({"order_id": order_id, "status": "prepared"})
            session.commit()
        return {"items": results}

    @application.post(
        "/api/orders/prepare-print", dependencies=[Depends(protected)]
    )
    def prepare_print(
        request: Request, payload: OrderActionRequest
    ) -> dict[str, Any]:
        return prepare_action(request, payload, "print")

    @application.post(
        "/api/orders/prepare-reject", dependencies=[Depends(protected)]
    )
    def prepare_reject(
        request: Request, payload: OrderActionRequest
    ) -> dict[str, Any]:
        return prepare_action(request, payload, "reject")

    @application.get(
        "/api/checks/{run_id}/export.json", dependencies=[Depends(protected)]
    )
    def export_json(request: Request, run_id: str) -> JSONResponse:
        return JSONResponse(_get_run_or_404(_get_coordinator(request), run_id))

    @application.get(
        "/runs/{run_id}/report",
        response_class=HTMLResponse,
        dependencies=[Depends(protected)],
        include_in_schema=False,
    )
    def export_html(request: Request, run_id: str) -> HTMLResponse:
        run = _get_run_or_404(_get_coordinator(request), run_id)
        return HTMLResponse(_html_report(run))

    if (FRONTEND_DIST / "assets").is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=FRONTEND_DIST / "assets"),
            name="frontend-assets",
        )

    @application.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str) -> FileResponse:
        index = FRONTEND_DIST / "index.html"
        if index.is_file():
            return FileResponse(index)
        if LEGACY_UI_PATH.is_file():
            return FileResponse(LEGACY_UI_PATH)
        raise HTTPException(status_code=503, detail="Vue-интерфейс ещё не собран.")

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT)
