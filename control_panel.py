"""Authenticated FastAPI application for the Image Magic operator console."""

from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
import asyncio
import inspect
import json
import logging
import os
import re
from pathlib import Path, PureWindowsPath
from urllib.parse import quote
import resource
import shutil
import time
import threading
from typing import Any, Literal
from uuid import uuid4

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, select, tuple_
from sqlalchemy.orm import Session, joinedload, selectinload

from config.profiles import DEFAULT_DIRECTION, PROFILES
from core.return_reasons import load_return_reasons
from core.inspector import inspect_tiff_structure
from core.tool_runner import run_command
from server.auth import AuthService, create_auth_router, require_session
from server.callas_fix_routes import create_callas_fix_router
from server.database import Database, configure_database, get_db, upgrade_database
from server.errors import APIError, error_payload, install_error_handlers
from server.logging_config import configure_logging
from server.models import OrderAction, OrderResult, PitstopCheck
from server.schemas import CheckOptions, CorrectionRequest, OrderActionRequest
from server.settings import Settings
from services import (
    AmbiguousOrderError,
    FileLifecycle,
    FileLifecycleError,
    InvalidRunStateError,
    OrderActionCommand,
    OrderWorkflowService,
    ProcessingOptions,
    RunCoordinator,
    RunNotFoundError,
    BatchProcessorAdapter,
)
from services.pitstop.baseline import FixBaselinePolicy
from services.pitstop import (
    PitStopProfile,
    PitStopProfileCatalog,
    PitStopService,
    PitStopServiceSettings,
    SSHSettings,
    SSHTransport,
)
from services.order_kits import KitResolver
from services.sborka_integration import (
    build_order_info_fetcher,
    build_rework_sender,
    build_sender,
)
from services.check_policy import PolicySettings, set_strict_source_dirs, source_output_root
from services.callas_fix import comparison_payload as callas_fix_payload, read_state_file as read_callas_fix_state
from services.callas_source_fix import fixable_source_path, source_comparison as source_fix_comparison
from services.print_readiness import print_blockers, print_mode
from services.ftp_preview_uploader import build_ftp_preview_uploader
from services.return_preview import (
    custom_return_preview_path, publish_custom_return_preview, selected_custom_return_preview,
)
from core.preview_cache import full_preview_cache_path, preview_media_type, preview_variants
from services.repository import InMemoryRunRepository, RunRepository
from services.event_retention import run_event_retention

try:
    from services.sql_repository import SqlRunRepository
except ImportError:  # The in-memory fallback keeps development imports usable.
    SqlRunRepository = None  # type: ignore[assignment,misc]


PROJECT_DIR = Path(__file__).resolve().parent
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"
LEGACY_UI_PATH = PROJECT_DIR / "web_ui" / "control-panel.html"
DEFAULT_HOST = os.environ.get("IMAGE_MAGIC_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("IMAGE_MAGIC_PORT", "8006"))
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


def _get_run_summary_or_404(coordinator: RunCoordinator, run_id: str) -> dict[str, Any]:
    """Cheap existence/status read: the run row without its orders."""
    try:
        return coordinator.get_run_summary(run_id)
    except RunNotFoundError as exc:
        raise APIError(404, "run_not_found", "Запуск проверки не найден.") from exc


# Upper bound of events read and sent to a client per SSE iteration.
SSE_EVENT_BATCH = 500
SSE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _parse_event_cursor(*values: str | None) -> int:
    """Largest valid non-negative event id among the given raw values (else 0)."""
    cursor = 0
    for value in values:
        try:
            cursor = max(cursor, int(value)) if value not in (None, "") else cursor
        except (TypeError, ValueError):
            continue
    return cursor


def _list_events_batch(
    coordinator: RunCoordinator, run_id: str, after_id: int, limit: int
) -> list[Any]:
    """Events after ``after_id`` read straight from the repository.

    ``RunCoordinator.events`` loads the entire run aggregate before every read,
    which is far too heavy for a per-second poll, so the poll talks to the
    repository. A ``limit`` is passed only when the repository supports it;
    otherwise the caller receives the full list and sends it in chunks.
    """
    lister = coordinator.repository.list_events
    try:
        supports_limit = "limit" in inspect.signature(lister).parameters
    except (TypeError, ValueError):
        supports_limit = False
    if supports_limit:
        return list(lister(run_id, after_id, limit=limit))
    return list(lister(run_id, after_id))


def _orders_from_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    orders = run.get("orders") or {}
    values = list(orders.values()) if isinstance(orders, dict) else list(orders)
    run_id = str(run.get("id") or "")
    input_path = Path((run.get("options") or {}).get("input_path", ""))
    for order in values:
        order_id = str(order.get("order_id") or order.get("id") or "")
        order_ref = _custom_preview_storage_id(run, order)
        order["html_url"] = f"/runs/{run_id}/report"
        order["json_url"] = f"/api/checks/{run_id}/export.json"
        if order.get("actions"):
            latest = order["actions"][-1]
            order["action_result"] = {
                "order_id": order_id,
                "status": latest.get("status"),
                "action": latest.get("action"),
                **({"pitstop": latest["pitstop"]} if latest.get("pitstop") else {}),
            }
        fix_base = f"/api/checks/{run_id}/orders/{order_ref}/callas/fix"
        if order.get("pdf_path"):
            order["pdf_url"] = f"/api/orders/{order_ref}/pdf?run_id={run_id}"
            fix_state = read_callas_fix_state(order["pdf_path"])
            if fix_state:
                order["callas_fix"] = {"comparison": callas_fix_payload(fix_state, fix_base)}
            order["print_blockers"] = print_blockers(order, fix_state)
            order["print_mode"] = print_mode(order["print_blockers"])
        elif input_path:
            order["print_blockers"] = print_blockers(order)
            order["print_mode"] = print_mode(order["print_blockers"])
            # Итогового PDF нет (например, ошибка размера): исправлять можно исходные файлы (PDF и растры).
            input_root = input_path.resolve()
            for index, entry in enumerate(order.get("files") or []):
                source = _source_pdf_path(input_root, entry)
                if source is None:
                    continue
                file_id = _order_file_id(run_id, order, index, entry)
                entry["callas_fixable"] = True
                entry["callas_fix_file_id"] = file_id
                fix_state = read_callas_fix_state(source)
                entry["callas_fix"] = {
                    "comparison": source_fix_comparison(
                        fix_state,
                        f"{fix_base}/source/{quote(file_id, safe='')}",
                        run,
                        order,
                        entry,
                    )
                }
        preview_storage_id = order_ref
        if input_path and custom_return_preview_path(preview_storage_id, input_path=input_path):
            order["custom_preview_url"] = (
                f"/api/checks/{run_id}/orders/{order_ref}/return-preview"
            )
        # pitstop_baseline — проверка исходного PDF (ревизия 1), её строка в БД сохраняется.
        for pitstop_key in ("pitstop", "pitstop_baseline"):
            pitstop = order.get(pitstop_key) or {}
            check_id = str(pitstop.get("check_id") or "")
            if check_id:
                reports = pitstop.get("reports") or {}
                pitstop["reports"] = {
                    "json_url": (
                        f"/api/pitstop-checks/{check_id}/reports/json"
                        if reports.get("json_url")
                        else None
                    ),
                    "xml_url": (
                        f"/api/pitstop-checks/{check_id}/reports/xml"
                        if reports.get("xml_url")
                        else None
                    ),
                }
        for index, item in enumerate(order.get("files") or []):
            file_id = str(item.get("file_result_id") or item.get("id") or f"{run_id}:{order_id}:{index}")
            item["id"] = file_id
            item["filename"] = item.get("filename") or item.get("name")
            parsed = item.get("parsed") or {}
            item["side"] = item.get("side") or parsed.get("side")
            item["format"] = item.get("format") or item.get("actual_format")
            item["width_mm"] = item.get("width_mm") or item.get("actual_width_mm")
            item["height_mm"] = item.get("height_mm") or item.get("actual_height_mm")
            item["source_url"] = f"/api/files/{file_id}/source"
            if (
                item.get("has_unflattened_layers")
                and str(item.get("filename") or "").lower().endswith((".tif", ".tiff"))
            ):
                item["layered_tiff_export_url"] = (
                    f"/api/checks/{run_id}/orders/{order_ref}/layered-tiff-export/{file_id}"
                )
            if item.get("preview_path") or order.get("preview_paths"):
                # Ревизия PDF в URL: после принятого исправления адрес меняется, и карточка
                # перезагружает превью вместо того, чтобы показывать закэшированное старое.
                revision = order.get("current_pdf_revision")
                item["preview_url"] = f"/api/files/{file_id}/preview" + (f"?rev={revision}" if revision else "")
                separator = "&" if revision else "?"
                item["thumbnail_url"] = f"{item['preview_url']}{separator}size=thumbnail"
                item["full_preview_url"] = f"{item['preview_url']}{separator}size=full"
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


def _layered_tiff_export_path(source: Path) -> Path:
    return source_output_root(source) / "PDF" / f"{source.stem}_layered-composite.pdf"


def _layered_tiff_export_preview_path(source: Path) -> Path:
    return source_output_root(source) / "Previews" / f"{source.stem}_layered-composite.png"


def _layered_tiff_export_urls(run_id: str, order_id: str, file_id: str) -> dict[str, str]:
    base = f"/api/checks/{run_id}/orders/{order_id}/layered-tiff-export/{file_id}"
    return {"pdf_url": f"{base}/artifact", "preview_url": f"{base}/preview"}


def _layered_tiff_export_item(
    coordinator: RunCoordinator,
    run_id: str,
    order_id: str,
    file_id: str,
    allowed_roots: tuple[Path, ...],
    *,
    verify_layers: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path, tuple[str, ...]]:
    run, order = _find_order(coordinator, order_id, run_id)
    input_root = Path((run.get("options") or {}).get("input_path", "")).resolve()
    if not input_root.is_dir() or not any(_is_inside(input_root, root) for root in allowed_roots):
        raise APIError(403, "folder_not_allowed", "Папка заказа находится за пределами разрешённой рабочей области.")
    files = order.get("files") or []
    order_number = str(order.get("order_id") or order.get("id") or "")
    item = next((
        entry for index, entry in enumerate(files)
        if str(entry.get("id") or entry.get("file_result_id") or f"{run_id}:{order_number}:{index}") == file_id
    ), None)
    if item is None:
        raise APIError(404, "file_not_found", "TIFF не найден в выбранном заказе.")
    source_value = item.get("path")
    if not source_value:
        raise APIError(404, "file_not_ready", "Исходный файл недоступен.")
    source = Path(source_value).resolve()
    if not _is_inside(source, input_root):
        raise APIError(403, "file_not_allowed", "Файл находится за пределами папки заказа.")
    if source.suffix.lower() not in {".tif", ".tiff"} or not source.is_file():
        raise APIError(422, "invalid_tiff", "Экспорт доступен только для существующего TIFF.")
    if verify_layers:
        structure = inspect_tiff_structure(str(source))
        if not structure.has_unflattened_layers:
            raise APIError(422, "tiff_has_no_layers", "В TIFF не обнаружены несведённые слои.")
    output = _layered_tiff_export_path(source)
    preview = _layered_tiff_export_preview_path(source)
    if not _is_inside(output.resolve(), input_root):
        raise APIError(403, "output_not_allowed", "Место сохранения находится за пределами папки заказа.")
    if not _is_inside(preview.resolve(), input_root):
        raise APIError(403, "output_not_allowed", "Место сохранения превью находится за пределами папки заказа.")
    magick = shutil.which("magick")
    if not magick:
        raise APIError(503, "imagemagick_missing", "ImageMagick (magick) не найден на сервере.")
    command = (
        magick,
        "-define", "tiff:ignore-layers=true",
        str(source),
        "-compress", "none",
    )
    return run, order, source, output, (*command, str(output))


def _generate_layered_tiff_export(
    app: FastAPI, job_key: tuple[str, str, str], source: Path, output: Path, preview: Path,
    command: tuple[str, ...],
) -> None:
    jobs = app.state.layered_tiff_export_jobs
    temporary = output.with_name(f".{output.stem}.{uuid4().hex}.tmp.pdf")
    temporary_preview = preview.with_name(f".{preview.stem}.{uuid4().hex}.tmp.png")
    jobs[job_key] = {"status": "processing", "error": None, "command": list(command)}
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        run_command([*command[:-1], str(temporary)], check=True)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ValueError("ImageMagick не создал PDF или создал пустой файл.")
        import pymupdf
        from services.callas.renderer import dpi_for_longest_side
        with pymupdf.open(temporary) as document:
            if document.page_count < 1:
                raise ValueError("Созданный PDF не содержит страниц.")
            page = document.load_page(0)
            dpi = dpi_for_longest_side(float(page.rect.width), float(page.rect.height), 1800)
        from services.callas.renderer import render_pdf_to_pngs
        render_pdf_to_pngs(temporary, dpi, [temporary_preview])
        os.replace(temporary, output)
        os.replace(temporary_preview, preview)
        jobs[job_key] = {
            "status": "completed", "error": None, "command": list(command),
            **_layered_tiff_export_urls(*job_key),
        }
    except Exception as exc:
        logger.exception("Layered TIFF export failed for %s", source.name)
        jobs[job_key] = {
            "status": "failed", "error": f"Не удалось создать тестовый PDF: {exc}",
            "command": list(command),
        }
    finally:
        temporary.unlink(missing_ok=True)
        temporary_preview.unlink(missing_ok=True)


def _custom_preview_storage_id(run: dict[str, Any], order: dict[str, Any]) -> str:
    """Use a customer-qualified filename only when bare IDs collide in this run."""
    real_id = str(order.get("order_id") or order.get("id") or "")
    identity = str(order.get("aggregate_id") or real_id)
    orders = run.get("orders") or {}
    values = orders.values() if isinstance(orders, dict) else orders
    matching_identities = {
        str(candidate.get("aggregate_id") or candidate.get("order_id") or candidate.get("id") or "")
        for candidate in values
        if str(candidate.get("order_id") or candidate.get("id") or "") == real_id
    }
    return identity if len(matching_identities) > 1 else real_id


def _safe_history_preview_path(root_path: str, raw_path: str | None) -> Path | None:
    """Return an available history preview only when it remains inside its run."""
    if not raw_path:
        return None
    root = Path(root_path).resolve()
    target = Path(raw_path).resolve()
    if not _is_inside(target, root) or not target.is_file():
        return None
    return target


def _history_preview_collisions(
    session: Session, order_keys: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    if not order_keys:
        return set()
    customer = func.coalesce(OrderResult.customer_id, "")
    duplicate_orders = (
        select(OrderResult.run_id, OrderResult.order_id)
        .where(tuple_(OrderResult.run_id, OrderResult.order_id).in_(order_keys))
        .group_by(OrderResult.run_id, OrderResult.order_id)
        .having(func.count(func.distinct(customer)) > 1)
    )
    return set(session.execute(duplicate_orders).tuples())


def _safe_rework_history_preview_path(
    root_path: str,
    preview_id: str,
    *,
    include_custom: bool = True,
    include_collage: bool = True,
) -> Path | None:
    """Find the return preview confined to its run."""
    if not preview_id or Path(preview_id).name != preview_id:
        return None
    root = Path(root_path)
    custom = custom_return_preview_path(preview_id, input_path=root) if include_custom else None
    if custom is not None:
        return _safe_history_preview_path(root_path, str(custom))
    if not include_collage:
        return None
    collages = preview_variants(root / "Previews" / "Return" / f"{preview_id}_return-preview")
    return _safe_history_preview_path(root_path, str(collages[0]) if collages else None)


def _history_custom_preview_id(order: OrderResult, collision: bool) -> str:
    if collision and order.customer_id:
        return f"{order.customer_id}:{order.order_id}"
    return order.order_id


def _find_order(
    coordinator: RunCoordinator, order_id: str, run_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    runs = [_get_run_or_404(coordinator, run_id)] if run_id else coordinator.list_runs()
    runs = sorted(runs, key=lambda item: item.get("created_at") or "", reverse=True)
    exact_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    number_matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in runs:
        orders = run.get("orders") or {}
        entries = (
            list(orders.items())
            if isinstance(orders, dict)
            else [(str(index), order) for index, order in enumerate(orders)]
        )
        for stored_key, order in entries:
            aggregate_id = str(order.get("aggregate_id") or stored_key)
            real_order_id = str(order.get("order_id") or order.get("id") or "")
            if order_id == aggregate_id or (
                order_id == str(stored_key) and order_id != real_order_id
            ):
                exact_matches.append((run, order))
            elif real_order_id == order_id:
                number_matches.append((run, order))

    # Keep the latest historical result for the same customer/order identity,
    # while refusing a bare order number shared by different customers.
    matches = exact_matches or number_matches
    if matches:
        identities = {
            (
                str(order.get("customer_id") or ""),
                str(order.get("order_id") or order.get("id") or ""),
            )
            for _, order in matches
        }
        run_counts: dict[str, int] = {}
        for matched_run, _order in matches:
            matched_run_id = str(matched_run.get("id") or "")
            run_counts[matched_run_id] = run_counts.get(matched_run_id, 0) + 1
        if len(identities) > 1 or any(count > 1 for count in run_counts.values()):
            raise APIError(
                409,
                "ambiguous_order",
                "Номер заказа неоднозначен; используйте aggregate_id.",
            )
        return matches[0]
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


def _order_file_id(run_id: str, order: dict[str, Any], index: int, entry: dict[str, Any]) -> str:
    order_number = str(order.get("order_id") or order.get("id") or "")
    return str(entry.get("id") or entry.get("file_result_id") or f"{run_id}:{order_number}:{index}")


def _source_pdf_path(input_root: Path, entry: dict[str, Any]) -> Path | None:
    """Исходный файл заказа (PDF или растр) внутри папки проверки или ``None``."""
    return fixable_source_path(input_root, entry)


def _callas_fix_order(
    request: Request, run_id: str, order_id: str, file_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    """Запуск, заказ и безопасный путь PDF для операций исправления Callas.

    Без ``file_id`` — собранный PDF заказа. С ``file_id`` — исходный PDF-файл заказа
    (нужен заказам с ошибками, у которых итоговый PDF не собран).
    """
    run, order = _find_order(_get_coordinator(request), order_id, run_id)
    input_root = Path((run.get("options") or {}).get("input_path", "")).resolve()
    if not input_root.is_dir() or not any(
        _is_inside(input_root, root) for root in _get_roots(request)
    ):
        raise APIError(
            403,
            "folder_not_allowed",
            "Папка заказа находится за пределами разрешённой рабочей области.",
        )
    if not file_id:
        return run, order, _safe_result_path(run, order.get("pdf_path"))
    entry = next(
        (
            item
            for index, item in enumerate(order.get("files") or [])
            if _order_file_id(run_id, order, index, item) == file_id
        ),
        None,
    )
    if entry is None:
        raise APIError(404, "file_not_found", "Файл не найден в выбранном заказе.")
    source = _source_pdf_path(input_root, entry)
    if source is None:
        raise APIError(
            422,
            "callas_fix_not_pdf",
            "Исправление Callas доступно только для существующего исходного файла заказа (PDF или растр).",
        )
    return run, order, source


_CALLAS_CLIENTS: dict[Any, Any] = {}
_CALLAS_CLIENTS_LOCK = threading.Lock()


def _shared_callas_client(settings: Settings):
    """One CallasClient per callas configuration for the whole process.

    The client's concurrency limit (``callas_max_concurrent_jobs``) is a semaphore
    inside the instance; separate instances for processing, ink coverage, merge,
    render and fixes would each get their own slots and exceed the global limit.
    """
    from services.callas import CallasClient
    from services.callas.client import CallasConfig

    config = CallasConfig.from_settings(settings)
    with _CALLAS_CLIENTS_LOCK:
        client = _CALLAS_CLIENTS.get(config)
        if client is None:
            client = _CALLAS_CLIENTS[config] = CallasClient(config)
        return client


def _callas_fix_service(settings: Settings):
    """Сервис исправления Callas или ``None``, если он выключен или не настроен."""
    if not (settings.callas_process_enabled and settings.callas_url):
        return None
    from config.callas_plans import CALLAS_PLANS, with_profile_overrides
    from services.callas_fix import CallasFixService

    from services.callas.fix_checks import build_post_checks
    from services.callas.prepress import PrepressAnalyzer

    client = _shared_callas_client(settings)
    analyzer = PrepressAnalyzer(
        client,
        high_ppi_limit=settings.high_ppi_limit,
        black_solid_threshold=settings.black_solid_area_threshold,
        black_solid_fix_threshold=settings.black_solid_fix_area_threshold,
    )
    plans = with_profile_overrides(
        CALLAS_PLANS, {"downsample_images": settings.callas_downsample_profile}
    )
    return CallasFixService(client, plans, post_checks=build_post_checks(analyzer))


def _configure_callas_pdf_renderer(settings: Settings) -> None:
    """Все рендеры PDF вне BatchProcessor (ревизии, сравнение, экспорт) — через callas."""
    if not settings.callas_url:
        return
    from services.callas.renderer import configure_pdf_renderer, make_callas_pdf_renderer

    configure_pdf_renderer(make_callas_pdf_renderer(_shared_callas_client(settings)))


def _callas_processor_factory(settings: Settings):
    """BatchProcessor factory: PDF merge and render always go through callas.

    Без IMAGE_MAGIC_CALLAS_URL процессор создаётся без клиента, и склейка/рендер
    PDF завершатся понятной технической ошибкой (без PyMuPDF/Ghostscript).
    """
    from functools import partial

    from processing.batch_processor import BatchProcessor

    if not settings.callas_url:
        logger.error(
            "callas pdfToolbox не настроен (IMAGE_MAGIC_CALLAS_URL пуст): "
            "создание PDF заказов будет завершаться ошибкой"
        )
        return partial(
            BatchProcessor,
            strip_icc_cmyk=settings.strip_icc_cmyk,
            auto_bleed_max_concurrent=settings.callas_auto_bleed_max_concurrent,
        )
    client = _shared_callas_client(settings)
    return partial(
        BatchProcessor,
        callas_client=client,
        pdf_color_converter=_callas_rgb_to_cmyk_converter(settings, client),
        pdf_prepress_checker=_callas_prepress_checker(settings, client),
        pdf_resizer=_callas_pdf_resizer(settings, client),
        callas_render=settings.callas_render_enabled,
        strip_icc_cmyk=settings.strip_icc_cmyk,
        auto_bleed_max_concurrent=settings.callas_auto_bleed_max_concurrent,
    )


def _callas_rgb_to_cmyk_converter(settings: Settings, client=None):
    """Конвертер RGB → CMYK через callas; None, если выключен или callas не настроен."""
    if not settings.callas_url or not settings.callas_rgb_to_cmyk_enabled:
        return None
    from services.callas.color_conversion import CallasRgbToCmykConverter

    return CallasRgbToCmykConverter(
        client or _shared_callas_client(settings),
        settings.callas_rgb_to_cmyk_profile,
        strip_icc=getattr(settings, "callas_strip_icc_enabled", True),
    )


def _callas_pdf_resizer(settings: Settings, client=None):
    """Коррекция размера PDF QuickFix callas; None, если выключена или callas не настроен."""
    if not settings.callas_url or not settings.callas_pdf_resize_enabled:
        return None
    from services.callas.page_resize import CallasPdfResizer

    return CallasPdfResizer(
        client or _shared_callas_client(settings), settings.callas_pdf_resize_profile
    )


def _callas_prepress_checker(settings: Settings, client=None):
    """Допечатные проверки (шрифты, ppi, плашка K100); None, если выключены или callas не настроен."""
    if not settings.callas_url or not settings.callas_prepress_enabled:
        return None
    from services.callas.prepress import PrepressAnalyzer

    return PrepressAnalyzer(
        client or _shared_callas_client(settings),
        high_ppi_limit=settings.high_ppi_limit,
        black_solid_threshold=settings.black_solid_area_threshold,
        black_solid_fix_threshold=settings.black_solid_fix_area_threshold,
    )


def _callas_pdf_merger(settings: Settings):
    """Callas-склейка итогового reviewed-PDF; None, если callas не настроен (сборка упадёт с ошибкой)."""
    if not settings.callas_url:
        return None
    from services.callas import make_callas_pdf_merger

    return make_callas_pdf_merger(_shared_callas_client(settings))


def _callas_page_extractor(settings: Settings):
    """Извлечение страниц PDF заданием callas ``split``; None, если callas не настроен.

    Резервного пути через PyMuPDF нет: без callas сборка печатных частей
    reviewed-PDF завершается понятной ошибкой.
    """
    if not (settings.callas_url and settings.callas_token):
        return None
    from services.callas import make_callas_page_extractor

    return make_callas_page_extractor(_shared_callas_client(settings))


def _ink_coverage_checker(settings: Settings):
    """Проверка красочности callas ``inkcoverage`` или None, если выключена/не настроена."""
    if not (settings.callas_inkcoverage_enabled and settings.callas_url):
        return None
    from services.callas.ink_coverage import build_ink_coverage_checker

    return build_ink_coverage_checker(
        _shared_callas_client(settings),
        min_percent_override=settings.ink_min_percent,
        max_percent_override=settings.ink_max_percent,
    )


def _pitstop_fix_baseline_policy(settings: Settings) -> FixBaselinePolicy:
    """Сравнение PitStop с проверкой PDF клиента (IMAGE_MAGIC_PITSTOP_FIX_BASELINE_*)."""
    return FixBaselinePolicy(
        enabled=settings.pitstop_fix_baseline_enabled,
        actions=settings.pitstop_fix_baseline_actions,
        profiles=settings.pitstop_fix_baseline_profiles,
        bleed_fix_all_actions=settings.pitstop_bleed_fix_all_actions,
    )


def _callas_auto_bleed(settings: Settings, callas_fix_service=None):
    """Поля вылета Callas в режиме «Автоматически»; None — выключено или Callas нет."""
    if callas_fix_service is None or not settings.callas_auto_bleed_enabled:
        return None
    from services.callas_auto_bleed import CallasAutoBleed

    return CallasAutoBleed(
        callas_fix_service, max_concurrent=settings.callas_auto_bleed_max_concurrent
    )


def _pitstop_adapter_factory(settings: Settings, callas_fix_service=None):
    required = {
        "IMAGE_MAGIC_PITSTOP_HOST": settings.pitstop_host,
        "IMAGE_MAGIC_PITSTOP_USERNAME": settings.pitstop_username,
        "IMAGE_MAGIC_PITSTOP_CLI_PATH": settings.pitstop_cli_path,
    }
    missing = [name for name, value in required.items() if not value]
    if missing or not settings.pitstop_profiles:
        profile_missing = (
            ("IMAGE_MAGIC_PITSTOP_PROFILE_DIGITAL/OFFSET",)
            if not settings.pitstop_profiles
            else ()
        )
        details = ", ".join([*missing, *profile_missing])
        raise ValueError(f"PitStop включён, но не настроены параметры: {details}")

    catalog = PitStopProfileCatalog(
        PitStopProfile(
            id=profile_id,
            label=label,
            windows_path=PureWindowsPath(windows_path),
        )
        for profile_id, label, windows_path in settings.pitstop_profiles
    )
    configured_profiles = {profile.id for profile in catalog.profiles}
    transport = SSHTransport(
        SSHSettings(
            host=settings.pitstop_host,
            username=settings.pitstop_username,
            port=settings.pitstop_port,
            known_hosts_file=settings.pitstop_known_hosts_file,
            identity_file=settings.pitstop_identity_file,
            connect_timeout_seconds=settings.pitstop_connect_timeout_seconds,
        )
    )

    def service_for(input_dir: Path, profile_id: str) -> PitStopService:
        if profile_id not in configured_profiles:
            raise ValueError(
                f"Для направления {profile_id!r} не настроен профиль PitStop."
            )
        resolved_input = Path(input_dir).resolve()
        return PitStopService(
            settings=PitStopServiceSettings(
                cli_path=PureWindowsPath(settings.pitstop_cli_path),
                mac_shared_root=settings.pitstop_mac_shared_root.resolve(),
                windows_shared_root=PureWindowsPath(
                    settings.pitstop_windows_shared_root
                ),
                report_root=resolved_input / "output_report" / "pitstop",
                command_timeout_seconds=settings.pitstop_command_timeout_seconds,
                max_concurrent=settings.pitstop_max_concurrent,
                transient_retries=settings.pitstop_transient_retries,
                retry_pause_seconds=settings.pitstop_retry_pause_seconds,
            ),
            profiles=catalog,
            transport=transport,
        )

    processor_factory = _callas_processor_factory(settings)
    ink_checker = _ink_coverage_checker(settings)
    auto_bleed = _callas_auto_bleed(settings, callas_fix_service)

    def factory(options: ProcessingOptions) -> BatchProcessorAdapter:
        service = service_for(Path(options.input_path), options.direction)
        return BatchProcessorAdapter(
            options,
            processor_factory=processor_factory,
            pitstop_service=service,
            ink_coverage_checker=ink_checker,
            pitstop_fix_baseline=_pitstop_fix_baseline_policy(settings),
            auto_bleed=auto_bleed,
            input_check_policy=settings.input_check_policy,
            strict_source_dirs=settings.strict_source_dirs,
            correction_limit_mm=settings.auto_correct_max_mm,
            correction_confirm_max_mm=settings.correction_confirm_max_mm,
            max_dpi=settings.max_dpi,
            downsample_high_dpi=settings.downsample_high_dpi_enabled,
            no_scale_name_markers=settings.no_scale_name_markers,
            order_info_fetcher=(
                build_order_info_fetcher(
                    settings.sborka_api_dir,
                    timeout=settings.sborka_timeout_seconds,
                )
                if settings.sborka_enabled
                else None
            ),
        )

    factory.pitstop_service_for = service_for
    return factory


def _default_adapter_factory(settings: Settings, callas_fix_service=None):
    """Construct the regular adapter with optional Sborka metadata enrichment."""
    fetcher = (
        build_order_info_fetcher(
            settings.sborka_api_dir, timeout=settings.sborka_timeout_seconds
        )
        if settings.sborka_enabled
        else None
    )
    processor_factory = _callas_processor_factory(settings)
    ink_checker = _ink_coverage_checker(settings)
    auto_bleed = _callas_auto_bleed(settings, callas_fix_service)
    return lambda options: BatchProcessorAdapter(
        options,
        processor_factory=processor_factory,
        order_info_fetcher=fetcher,
        ink_coverage_checker=ink_checker,
        auto_bleed=auto_bleed,
        input_check_policy=settings.input_check_policy,
        strict_source_dirs=settings.strict_source_dirs,
        correction_limit_mm=settings.auto_correct_max_mm,
        correction_confirm_max_mm=settings.correction_confirm_max_mm,
        max_dpi=settings.max_dpi,
        downsample_high_dpi=settings.downsample_high_dpi_enabled,
        no_scale_name_markers=settings.no_scale_name_markers,
    )


def create_app(
    *,
    settings: Settings | None = None,
    repository: RunRepository | None = None,
    allowed_roots: tuple[Path, ...] | None = None,
    default_input_dir: Path | None = None,
    adapter_factory=None,
    callas_fix_service=None,
    initialize_schema: bool = False,
) -> FastAPI:
    settings = settings or Settings.from_env()
    set_strict_source_dirs(settings.strict_source_dirs)
    log_path = configure_logging(settings)
    logger.info("application.initializing")
    if not initialize_schema:
        upgrade_database(settings)
    database = configure_database(settings)
    if initialize_schema:
        database.create_schema()
    repository = repository or _default_repository(database)
    coordinator_options: dict[str, Any] = {"autostart": False}
    callas_fix_service = (
        callas_fix_service
        if callas_fix_service is not None
        else _callas_fix_service(settings)
    )
    if adapter_factory is not None:
        processor_factory = adapter_factory
    elif settings.pitstop_enabled:
        processor_factory = _pitstop_adapter_factory(settings, callas_fix_service)
    else:
        processor_factory = _default_adapter_factory(settings, callas_fix_service)
    coordinator_options["adapter_factory"] = processor_factory
    coordinator = RunCoordinator(repository, **coordinator_options)
    auth_service = AuthService(settings)
    protected = require_session(settings, auth_service)

    def heartbeat_snapshot() -> tuple[dict[str, Any] | None, float, float]:
        # Runs on a worker thread. Reads only the run summary (no orders) and
        # does not take the coordinator lock. ``_active_run_id`` is a plain
        # attribute read; get_active_run() (lock + full aggregate) is the
        # fallback when the attribute is unavailable.
        active_id = getattr(coordinator, "_active_run_id", False)
        if active_id is False:
            active = coordinator.get_active_run()
        elif active_id:
            try:
                active = coordinator.get_run_summary(active_id)
            except RunNotFoundError:
                active = None
        else:
            active = None
        disk = shutil.disk_usage(PROJECT_DIR)
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return active, disk.free / (1024**3), max_rss / (1024**2)

    async def heartbeat() -> None:
        while True:
            try:
                active, disk_free_gb, max_rss_mb = await asyncio.to_thread(
                    heartbeat_snapshot
                )
                logger.info(
                    "application.heartbeat active_run_id=%s status=%s stage=%s "
                    "progress=%s disk_free_gb=%.1f max_rss_mb=%.1f",
                    active.get("id") if active else "-",
                    active.get("status") if active else "idle",
                    active.get("stage") if active else "-",
                    active.get("progress") if active else "-",
                    disk_free_gb,
                    max_rss_mb,
                )
            except Exception:
                logger.exception("application.heartbeat_failed")
            await asyncio.sleep(settings.log_heartbeat_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info("application.starting")
        coordinator.start_worker()
        heartbeat_task = asyncio.create_task(
            heartbeat(), name="image-magic-heartbeat"
        )
        retention_stop = asyncio.Event()
        retention_task = None
        if callable(getattr(repository, "archive_old_terminal_events", None)):
            retention_task = asyncio.create_task(
                run_event_retention(repository, retention_stop),
                name="image-magic-event-retention",
            )
        try:
            yield
        finally:
            logger.info("application.stopping")
            retention_stop.set()
            if retention_task is not None:
                await retention_task
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            application.state.order_workflow.shutdown()
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
    _configure_callas_pdf_renderer(settings)
    application.state.coordinator = coordinator
    application.state.order_workflow = OrderWorkflowService(
        coordinator,
        database.session_factory,
        lambda order_id, run_id: _find_order(coordinator, order_id, run_id),
        reviewed_pdf_pitstop_service_factory=getattr(
            processor_factory, "pitstop_service_for", None
        ),
        pdf_merger=_callas_pdf_merger(settings),
        pdf_color_converter=_callas_rgb_to_cmyk_converter(settings),
        pdf_prepress_checker=_callas_prepress_checker(settings),
        pdf_page_extractor=_callas_page_extractor(settings),
        pitstop_fix_baseline=_pitstop_fix_baseline_policy(settings),
        policy_settings=PolicySettings(
            input_check_policy=settings.input_check_policy,
            strict_source_dirs=settings.strict_source_dirs,
        ),
        accepted_pdf_provider=(
            callas_fix_service.publish_accepted if callas_fix_service else None
        ),
        prepress_sender=(
            build_sender(
                settings.sborka_api_dir,
                timeout=settings.sborka_timeout_seconds,
            )
            if settings.sborka_enabled
            else None
        ),
        rework_sender=(
            build_rework_sender(
                settings.sborka_api_dir,
                timeout=settings.sborka_timeout_seconds,
            )
            if settings.sborka_enabled
            else None
        ),
        preview_uploader=build_ftp_preview_uploader(
            PROJECT_DIR / "sborka_ftp_credentials.json",
            timeout=settings.sborka_timeout_seconds,
        ),
        kit_resolver=(
            KitResolver(
                build_order_info_fetcher(
                    settings.sborka_api_dir, timeout=settings.sborka_timeout_seconds
                )
            )
            if settings.sborka_enabled
            else None
        ),
    )
    application.state.log_path = log_path
    application.state.allowed_roots = allowed_roots or ALLOWED_ROOTS
    application.state.default_input_dir = default_input_dir or DEFAULT_INPUT_DIR
    application.state.layered_tiff_export_jobs = {}
    application.state.callas_fix_service = callas_fix_service
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
    application.include_router(
        create_callas_fix_router(protected=protected, resolve_order=_callas_fix_order)
    )

    @application.get("/api/config", dependencies=[Depends(protected)])
    def get_config(request: Request) -> dict[str, Any]:
        return {
            "default_input_path": str(_get_default_input(request)),
            "allowed_roots": [str(root) for root in _get_roots(request)],
            "profiles": [
                {"id": key, "name": value.name} for key, value in PROFILES.items()
            ],
            "return_reasons": load_return_reasons(),
            "auto_correct_max_mm": settings.auto_correct_max_mm,
            "correction_confirm_max_mm": settings.correction_confirm_max_mm,
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
        FileLifecycle(input_dir).initialize()
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
                check_policy=options.check_policy,
            )
        )
        return _public_run(result)

    @application.get("/api/checks", dependencies=[Depends(protected)])
    def list_checks(
        request: Request,
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        coordinator_value = _get_coordinator(request)
        items = coordinator_value.list_runs(
            limit=limit,
            offset=(page - 1) * limit,
            include_orders=False,
        )
        total = coordinator_value.count_runs()
        return {
            "items": [_public_run(item) for item in items],
            "page": page,
            "page_size": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        }

    @application.get("/api/checks/{run_id}", dependencies=[Depends(protected)])
    def get_check(
        request: Request, run_id: str,
        include_orders: bool = Query(default=True),
    ) -> dict[str, Any]:
        coordinator_value = _get_coordinator(request)
        try:
            run = (
                coordinator_value.get_run_summary(run_id)
                if not include_orders
                else coordinator_value.get_run(run_id)
            )
        except RunNotFoundError as exc:
            raise APIError(404, "run_not_found", "Запуск проверки не найден.") from exc
        return _public_run(run)

    @application.get(
        "/api/checks/{run_id}/kits", dependencies=[Depends(protected)]
    )
    async def get_check_kits(request: Request, run_id: str) -> dict[str, Any]:
        """Комплекты Sborka заказов проверки: ``{"kits": {номер заказа: комплект}}``.

        Один запрос ``orderinfo`` на запуск (кэш в ``KitResolver``); без Sborka — пусто.
        """
        run = _get_run_or_404(_get_coordinator(request), run_id)
        resolver = getattr(request.app.state.order_workflow, "kit_resolver", None)
        if resolver is None:
            return {"kits": {}}
        kits = await run_in_threadpool(resolver.for_run, run)
        return {"kits": kits}

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
    def list_orders(
        request: Request,
        run_id: str,
        page: int | None = Query(default=None, ge=1),
        page_size: int | None = Query(default=None, ge=1, le=100),
        status: str | None = Query(default=None),
        search: str | None = Query(default=None, max_length=200),
        active_only: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        coordinator_value = _get_coordinator(request)
        # Keep the original response contract for clients that do not opt in
        # to pagination. The dashboard always supplies pagination parameters.
        if all(value is None for value in (page, page_size, status, search, active_only)):
            run = _get_run_or_404(coordinator_value, run_id)
            return {"items": _orders_from_run(run)}
        page = page or 1
        page_size = page_size or 10
        status = status or "all"
        search = search or ""
        active_only = bool(active_only)
        if page_size not in {10, 50, 100}:
            raise APIError(422, "invalid_page_size", "Размер страницы должен быть 10, 50 или 100.")
        if status not in {"all", "passed", "warning", "error", "waiting_confirmation"}:
            raise APIError(422, "invalid_order_filter", "Фильтр заказов не распознан.")
        try:
            result = coordinator_value.list_orders_page(
                run_id, page=page, page_size=page_size, status=status,
                search=search, active_only=active_only,
            )
        except RunNotFoundError as exc:
            raise APIError(404, "run_not_found", "Запуск проверки не найден.") from exc
        result["items"] = _orders_from_run({
            "id": run_id,
            "status": result["run"].get("status"),
            "options": result["run"].get("options") or {},
            "orders": result["run"].get("orders") or {},
        })
        result.pop("run", None)
        return result

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}",
        dependencies=[Depends(protected)],
    )
    def get_order(request: Request, run_id: str, order_id: str) -> dict[str, Any]:
        _, order = _find_order(_get_coordinator(request), order_id, run_id)
        return order

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}/actions",
        dependencies=[Depends(protected)],
    )
    def get_order_actions(request: Request, run_id: str, order_id: str) -> dict[str, Any]:
        from services.order_actions import order_actions

        coordinator_value = _get_coordinator(request)
        run, order = _find_order(coordinator_value, order_id, run_id)
        matches = sum(str(item.get("order_id")) == str(order.get("order_id"))
                      for item in run["orders"].values())
        return {"schema_version": 1, "items": order_actions(
            coordinator_value.events(run_id), order, legacy_unambiguous=matches == 1,
        )}

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
        except AmbiguousOrderError as exc:
            raise APIError(409, "ambiguous_order", "Номер заказа неоднозначен; используйте aggregate_id.") from exc
        except RunNotFoundError as exc:
            raise APIError(404, "order_not_found", "Заказ не найден.") from exc
        except InvalidRunStateError as exc:
            raise APIError(409, "invalid_run_state", str(exc)) from exc

    event_polls: dict[
        tuple[asyncio.AbstractEventLoop, str, int],
        asyncio.Task[tuple[list[Any], bool, float | None]],
    ] = {}

    async def poll_events(
        coordinator: RunCoordinator, run_id: str, cursor: int,
    ) -> tuple[list[Any], bool, float | None]:
        events = await asyncio.to_thread(
            _list_events_batch, coordinator, run_id, cursor, SSE_EVENT_BATCH,
        )
        if events:
            return events, False, None
        summary = await asyncio.to_thread(coordinator.get_run_summary, run_id)
        terminal = summary["status"] in SSE_TERMINAL_STATUSES
        return events, terminal, None if terminal else asyncio.get_running_loop().time() + 1

    async def shared_event_poll(
        coordinator: RunCoordinator, run_id: str, cursor: int,
    ) -> tuple[list[Any], bool, float | None]:
        loop = asyncio.get_running_loop()
        key = (loop, run_id, cursor)
        task = event_polls.get(key)
        if task is None:
            task = loop.create_task(poll_events(coordinator, run_id, cursor))
            event_polls[key] = task

            def finished(result: asyncio.Task[tuple[list[Any], bool, float | None]]) -> None:
                if not result.cancelled() and result.exception() is None:
                    _, _, idle_until = result.result()
                    # Keep empty active polls until the next poll is due, so
                    # clients at the same cursor converge on a shared cadence.
                    if idle_until is not None:
                        loop.call_at(idle_until, event_polls.pop, key, None)
                        return
                event_polls.pop(key, None)

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    @application.get(
        "/api/checks/{run_id}/events", dependencies=[Depends(protected)]
    )
    async def stream_events(
        request: Request,
        run_id: str,
        last_event_id: str | None = Query(default=None, max_length=32),
        last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        coordinator_value = _get_coordinator(request)
        await asyncio.to_thread(_get_run_summary_or_404, coordinator_value, run_id)
        # ``?last_event_id=`` is what the client passes on its own reconnects
        # (EventSource cannot set headers); the Last-Event-ID header is what the
        # browser sends on native reconnects. The later of the two wins.
        cursor = _parse_event_cursor(last_event_id, last_event_id_header)

        async def generate():
            nonlocal cursor
            idle_polls = 0
            drained_after_terminal = False
            while not await request.is_disconnected():
                poll = poll_events if drained_after_terminal else shared_event_poll
                try:
                    events, terminal, idle_until = await poll(coordinator_value, run_id, cursor)
                except RunNotFoundError:
                    break
                if events:
                    idle_polls = 0
                    for position, event in enumerate(events, start=1):
                        cursor = event.id
                        yield event.as_sse()
                        if position % SSE_EVENT_BATCH == 0:
                            # Let other tasks run while a long backlog is sent.
                            await asyncio.sleep(0)
                    if len(events) >= SSE_EVENT_BATCH:
                        continue  # a full batch: more is probably waiting
                else:
                    idle_polls += 1
                    if idle_polls >= 15:
                        yield ": keep-alive\n\n"
                        idle_polls = 0
                    if terminal:
                        if drained_after_terminal:
                            break
                        # Read once more so an event written just before the
                        # status flipped is not lost, then finish.
                        drained_after_terminal = True
                        continue
                if idle_until is not None:
                    await asyncio.sleep(max(0, idle_until - asyncio.get_running_loop().time()))
                else:
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
        coordinator = _get_coordinator(request)
        match = coordinator.find_file_by_id(file_id)
        if match is None:
            run, item = _find_file(coordinator, file_id)
        else:
            run, item = match
        return FileResponse(_safe_result_path(run, item.get("path")))

    @application.get(
        "/api/files/{file_id}/preview", dependencies=[Depends(protected)]
    )
    def preview_file(
        request: Request,
        file_id: str,
        page: int | None = None,
        size: Literal["thumbnail", "full", "small", "large"] = "thumbnail",
    ) -> FileResponse:
        coordinator = _get_coordinator(request)
        match = coordinator.find_file_by_id(file_id)
        if match is None:
            # Compatibility for older/in-memory result IDs without a DB file ID.
            run, item = _find_file(coordinator, file_id)
        else:
            run, item = match
        previews_for_file = item.get("preview_paths") or []
        if page is not None:
            if page < 1 or page > len(previews_for_file):
                raise APIError(404, "preview_not_found", "Превью страницы не найдено.")
            preview = previews_for_file[page - 1]
        else:
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
        thumbnail = _safe_result_path(run, preview)
        if size in {"full", "large"}:
            try:
                rendition = full_preview_cache_path(thumbnail)
            except RuntimeError as exc:
                logger.error("preview.cache_configuration_error error=%s", exc)
                raise APIError(503, "preview_cache_unavailable", "Кеш превью на внешнем диске не настроен.") from exc
            if not rendition.is_file():
                # У ревизий PDF после исправления Callas (Previews/Final/<заказ>/rNNNN/) отдельного
                # детального превью нет: их превью уже строится в высоком разрешении, показываем его.
                if not re.fullmatch(r"r\d{4}", thumbnail.parent.name):
                    raise APIError(404, "preview_not_found", "Детальное превью ещё не создано.")
                return FileResponse(
                    thumbnail,
                    media_type=preview_media_type(thumbnail),
                    headers={"Cache-Control": "private, no-cache, must-revalidate"},
                )
            return FileResponse(
                rendition,
                media_type=preview_media_type(rendition),
                headers={"Cache-Control": "private, no-cache, must-revalidate"},
            )
        return FileResponse(
            thumbnail,
            media_type=preview_media_type(thumbnail),
            headers={"Cache-Control": "private, no-cache, must-revalidate"},
        )

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}/return-preview",
        dependencies=[Depends(protected)],
    )
    def uploaded_return_preview(
        request: Request, run_id: str, order_id: str
    ) -> Response:
        run, order = _find_order(_get_coordinator(request), order_id, run_id)
        preview_storage_id = _custom_preview_storage_id(run, order)
        with selected_custom_return_preview(
            preview_storage_id, input_path=Path(run["options"]["input_path"])
        ) as preview:
            if preview is None:
                raise APIError(
                    404,
                    "preview_not_found",
                    "Пользовательское превью не загружено.",
                )
            return Response(preview.read_bytes(), media_type=preview_media_type(preview))

    @application.post(
        "/api/checks/{run_id}/orders/{order_id}/return-preview",
        dependencies=[Depends(protected)],
    )
    async def upload_return_preview(
        request: Request, run_id: str, order_id: str
    ) -> dict[str, str]:
        """Store a small operator preview that takes precedence on FTP return."""
        run, order = await run_in_threadpool(
            _find_order, _get_coordinator(request), order_id, run_id
        )
        preview_storage_id = _custom_preview_storage_id(run, order)
        maximum_size = 1024 * 1024
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > maximum_size:
            raise APIError(
                413, "preview_too_large", "Размер превью не должен превышать 1 МБ."
            )
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > maximum_size:
                raise APIError(
                    413,
                    "preview_too_large",
                    "Размер превью не должен превышать 1 МБ.",
                )
        if not content:
            raise APIError(400, "preview_empty", "Выберите файл превью.")
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif content.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        else:
            raise APIError(
                415,
                "preview_invalid_format",
                "Можно загрузить только JPEG или PNG-превью.",
            )
        target = await run_in_threadpool(
            publish_custom_return_preview,
            preview_storage_id,
            input_path=Path(run["options"]["input_path"]),
            suffix=suffix,
            content=content,
        )
        return {
            "filename": target.name,
            "url": f"/api/checks/{run_id}/orders/{order_id}/return-preview",
        }

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}/layered-tiff-export/{file_id}",
        dependencies=[Depends(protected)],
    )
    def layered_tiff_export_status(
        request: Request, run_id: str, order_id: str, file_id: str,
    ) -> dict[str, Any]:
        _run, _order, _source, output, _command = _layered_tiff_export_item(
            _get_coordinator(request), run_id, order_id, file_id, _get_roots(request),
            verify_layers=False,
        )
        key = (run_id, order_id, file_id)
        result = dict(request.app.state.layered_tiff_export_jobs.get(key) or {})
        preview = _layered_tiff_export_preview_path(_source)
        if result.get("status") in {None, "not_started"} and output.is_file() and preview.is_file():
            result.update({
                "status": "completed",
                **_layered_tiff_export_urls(run_id, order_id, file_id),
            })
        return result or {"status": "not_started"}

    @application.post(
        "/api/checks/{run_id}/orders/{order_id}/layered-tiff-export/{file_id}",
        dependencies=[Depends(protected)],
    )
    def start_layered_tiff_export(
        request: Request, background_tasks: BackgroundTasks,
        run_id: str, order_id: str, file_id: str,
    ) -> dict[str, Any]:
        _run, _order, source, output, command = _layered_tiff_export_item(
            _get_coordinator(request), run_id, order_id, file_id, _get_roots(request)
        )
        key = (run_id, order_id, file_id)
        current = request.app.state.layered_tiff_export_jobs.get(key) or {}
        if current.get("status") == "processing":
            return current
        request.app.state.layered_tiff_export_jobs[key] = {
            "status": "processing", "error": None, "command": list(command)
        }
        background_tasks.add_task(
            _generate_layered_tiff_export, request.app, key, source, output,
            _layered_tiff_export_preview_path(source), command,
        )
        return dict(request.app.state.layered_tiff_export_jobs[key])

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}/layered-tiff-export/{file_id}/artifact",
        dependencies=[Depends(protected)],
    )
    def layered_tiff_export_artifact(
        request: Request, run_id: str, order_id: str, file_id: str,
    ) -> FileResponse:
        run, _order, _source, output, _command = _layered_tiff_export_item(
            _get_coordinator(request), run_id, order_id, file_id, _get_roots(request),
            verify_layers=False,
        )
        return FileResponse(
            _safe_result_path(run, str(output)),
            media_type="application/pdf",
            filename=output.name,
            content_disposition_type="inline",
        )

    @application.get(
        "/api/checks/{run_id}/orders/{order_id}/layered-tiff-export/{file_id}/preview",
        dependencies=[Depends(protected)],
    )
    def layered_tiff_export_preview(
        request: Request, run_id: str, order_id: str, file_id: str,
    ) -> FileResponse:
        run, _order, source, _output, _command = _layered_tiff_export_item(
            _get_coordinator(request), run_id, order_id, file_id, _get_roots(request),
            verify_layers=False,
        )
        preview = _layered_tiff_export_preview_path(source)
        if not _is_inside(preview.resolve(), Path(run["options"]["input_path"]).resolve()):
            raise APIError(403, "output_not_allowed", "Превью находится за пределами папки заказа.")
        return FileResponse(
            _safe_result_path(run, str(preview)),
            media_type="image/png",
            headers={"Cache-Control": "private, no-cache, must-revalidate"},
        )

    @application.get(
        "/api/orders/{order_id}/pdf", dependencies=[Depends(protected)]
    )
    def order_pdf(
        request: Request, order_id: str, run_id: str | None = None
    ) -> FileResponse:
        run, order = _find_order(_get_coordinator(request), order_id, run_id)
        return FileResponse(_safe_result_path(run, order.get("pdf_path")))

    @application.get(
        "/api/pitstop-checks/{check_id}/reports/{report_format}",
        dependencies=[Depends(protected)],
    )
    def pitstop_report(
        request: Request,
        check_id: str,
        report_format: Literal["json", "xml"],
    ) -> FileResponse:
        with request.app.state.database.session_factory() as session:
            check = session.get(PitstopCheck, check_id)
            if check is None:
                raise APIError(
                    404, "pitstop_check_not_found", "Проверка PitStop не найдена."
                )
            run_id = check.pdf_revision.order_result.run_id
            raw_path = (
                check.report_json_path
                if report_format == "json"
                else check.report_xml_path
            )
        run = _get_run_or_404(_get_coordinator(request), run_id)
        return FileResponse(
            _safe_result_path(run, raw_path),
            media_type=(
                "application/json"
                if report_format == "json"
                else "application/xml"
            ),
        )

    def prepare_action(
        request: Request, payload: OrderActionRequest, action: str
    ) -> dict[str, Any]:
        return request.app.state.order_workflow.prepare(
            OrderActionCommand(
                order_ids=tuple(payload.order_ids),
                run_id=payload.run_id,
                comment=payload.comment,
                design=payload.design,
                design_cost=payload.design_cost,
                conflict_strategy=payload.conflict_strategy,
                confirm_failed_processing=payload.confirm_failed_processing,
            ),
            action,
        )

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
        # Return the final result to the dashboard.  Returning ``pending`` here
        # made the UI refresh before the Sborka request and file transition
        # finished, leaving stale orders selected on completed runs.
        return request.app.state.order_workflow.prepare(
            OrderActionCommand(
                order_ids=tuple(payload.order_ids),
                run_id=payload.run_id,
                comment=payload.comment,
                design=payload.design,
                design_cost=payload.design_cost,
                conflict_strategy=payload.conflict_strategy,
            ),
            "reject",
        )

    @application.get("/api/order-history", dependencies=[Depends(protected)])
    def order_history(
        request: Request,
        action: Literal["all", "print", "reject"] = "all",
        status: Literal["prepared", "failed", "all"] = "prepared",
        date_from: str | None = None,
        date_to: str | None = None,
        search: str = "",
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        """Compact, preview-only history of orders handled by an operator."""
        try:
            start = datetime.fromisoformat(date_from) if date_from else None
            end = datetime.fromisoformat(date_to) if date_to else None
        except ValueError as exc:
            raise APIError(422, "invalid_date", "Укажите дату в формате ГГГГ-ММ-ДД.") from exc
        if start and end and start > end:
            raise APIError(422, "invalid_date_range", "Начальная дата позже конечной.")

        with request.app.state.database.session_factory() as session:
            statement = select(OrderAction).join(OrderResult)
            # Eager loading keeps the query count constant per page: the
            # preview paths need order.run and order.files for every row.
            page_loading = (
                selectinload(OrderAction.order_result).options(
                    joinedload(OrderResult.run),
                    selectinload(OrderResult.files),
                ),
            )
            if action != "all":
                statement = statement.where(OrderAction.action == action)
            if status != "all":
                statement = statement.where(OrderAction.status == status)
            if start:
                statement = statement.where(OrderAction.created_at >= start)
            if end:
                statement = statement.where(OrderAction.created_at < end.replace(hour=23, minute=59, second=59, microsecond=999999))
            query = search.strip()
            if query:
                statement = statement.where(
                    OrderResult.order_id.contains(query) | OrderResult.customer_id.contains(query)
                )
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            actions = session.scalars(
                statement.options(*page_loading)
                .order_by(desc(OrderAction.created_at)).offset((page - 1) * limit).limit(limit)
            ).unique().all()
            collisions = _history_preview_collisions(session, {
                (item.order_result.run_id, item.order_result.order_id)
                for item in actions if item.action == "reject"
            })
            items = []
            for item in actions:
                order = item.order_result
                previews = []
                collision = (order.run_id, order.order_id) in collisions
                rework_preview = (
                    _safe_rework_history_preview_path(
                        order.run.input_path,
                        _history_custom_preview_id(order, collision),
                        include_collage=not collision,
                    )
                    if item.action == "reject"
                    else None
                )
                if rework_preview is not None:
                    index = len(order.files)
                    preview_url = f"/api/order-history/{item.id}/previews/{index}"
                    previews.append(
                        {
                            "side": "return",
                            "url": preview_url,
                        }
                    )
                else:
                    for index, file in enumerate(order.files):
                        source = _safe_history_preview_path(
                            order.run.input_path, file.preview_path
                        )
                        if source is None:
                            continue
                        preview_url = f"/api/order-history/{item.id}/previews/{index}"
                        previews.append(
                            {
                                "side": file.side,
                                "url": preview_url,
                            }
                        )
                items.append(
                    {
                        "id": item.id,
                        "action": item.action,
                        "status": item.status,
                        "created_at": item.created_at.isoformat(),
                        "order_id": order.order_id,
                        "customer_id": order.customer_id,
                        "comment": item.comment,
                        "previews": previews,
                    }
                )
        return {
            "items": items,
            "page": page,
            "page_size": limit,
            "total": total,
            "total_pages": max(1, (total + limit - 1) // limit),
        }

    @application.get(
        "/api/order-history/{action_id}/previews/{file_index}",
        dependencies=[Depends(protected)],
    )
    def order_history_preview(
        request: Request, action_id: int, file_index: int
    ) -> Response:
        with request.app.state.database.session_factory() as session:
            action = session.get(OrderAction, action_id)
            if action is None:
                raise APIError(404, "history_not_found", "Запись истории не найдена.")
            order = action.order_result
            files = list(order.files)
            if file_index == len(files) and action.action == "reject":
                root_path = order.run.input_path
                order_key = (order.run_id, order.order_id)
                collision = bool(_history_preview_collisions(session, {order_key}))
                preview_id = _history_custom_preview_id(order, collision)
                if preview_id and Path(preview_id).name == preview_id:
                    with selected_custom_return_preview(
                        preview_id, input_path=Path(root_path)
                    ) as custom:
                        target = _safe_history_preview_path(root_path, str(custom)) if custom else None
                        if target is not None:
                            return Response(
                                target.read_bytes(), media_type=preview_media_type(target)
                            )
                if not collision:
                    target = _safe_rework_history_preview_path(
                        root_path, preview_id, include_custom=False
                    )
                    if target is not None:
                        return FileResponse(target)
            if file_index < 0 or file_index >= len(files):
                raise APIError(404, "preview_not_found", "Превью не найдено.")
            target = _safe_history_preview_path(
                action.order_result.run.input_path, files[file_index].preview_path
            )
            if target is None:
                raise APIError(404, "preview_not_found", "Превью не найдено.")
            return FileResponse(target)

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

    uvicorn.run(app, host=DEFAULT_HOST, port=DEFAULT_PORT)
