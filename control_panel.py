"""FastAPI control panel for running Image Magic against order folders."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import traceback
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from config.profiles import DEFAULT_DIRECTION, PROFILES, get_profile
from core.history_db import save_order_audit
from core.report_builder import build_orders_html_report
from processing import BatchProcessor


PROJECT_DIR = Path(__file__).resolve().parent
UI_PATH = PROJECT_DIR / "web_ui" / "control-panel.html"
DEFAULT_PORT = 8006


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


def resolve_input_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = DEFAULT_INPUT_DIR / candidate
    candidate = candidate.resolve()
    if not any(_is_inside(candidate, root) for root in ALLOWED_ROOTS):
        raise HTTPException(
            status_code=403,
            detail="Папка находится за пределами разрешённой рабочей области.",
        )
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Папка с заказами не найдена.")
    return candidate


class CheckOptions(BaseModel):
    input_path: str
    direction: str = DEFAULT_DIRECTION
    approve_corrections: bool = False
    create_pdfs: bool = True
    generate_previews: bool = True
    copy_failures: bool = True


class CheckJob:
    def __init__(self, options: CheckOptions, input_dir: Path) -> None:
        self.id = uuid4().hex[:12]
        self.options = options
        self.input_dir = input_dir
        self.status: Literal["queued", "running", "completed", "failed"] = "queued"
        self.stage = "Ожидание запуска"
        self.progress = 0
        self.created_at = datetime.now(timezone.utc)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.total_orders = 0
        self.passed_orders = 0
        self.failed_orders = 0
        self.unparsed_files = 0
        self.unsupported_files = 0
        self.created_pdfs = 0
        self.created_previews = 0
        self.report_path: Path | None = None
        self.error: str | None = None
        self.log: list[str] = []

    def add_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"{stamp} · {message}")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "input_path": str(self.input_dir),
            "direction": self.options.direction,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "total_orders": self.total_orders,
            "passed_orders": self.passed_orders,
            "failed_orders": self.failed_orders,
            "unparsed_files": self.unparsed_files,
            "unsupported_files": self.unsupported_files,
            "created_pdfs": self.created_pdfs,
            "created_previews": self.created_previews,
            "report_ready": bool(self.report_path and self.report_path.is_file()),
            "error": self.error,
            "log": self.log[-30:],
        }


JOBS: "OrderedDict[str, CheckJob]" = OrderedDict()
JOBS_LOCK = threading.Lock()
MAX_JOBS = 30


def _set_stage(job: CheckJob, stage: str, progress: int) -> None:
    job.stage = stage
    job.progress = progress
    job.add_log(stage)


def _run_check(job: CheckJob) -> None:
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    profile = get_profile(job.options.direction)
    input_dir = job.input_dir
    pdf_dir = input_dir / "PDF"
    preview_dir = input_dir / "Previews"
    troubles_dir = input_dir / "Troubles"
    report_path = input_dir / "output_report" / "report.html"
    processor = BatchProcessor(input_dir, pdf_dir, profile=profile)

    try:
        _set_stage(job, "Поиск и проверка файлов", 15)
        orders = processor.inspect_orders()
        pending = [
            item
            for order in orders
            for item in order.files
            if item.resample_decision == "ask_confirmation"
        ]
        if pending and job.options.approve_corrections:
            for item in pending:
                processor.confirm_resample(item, True)
            job.add_log(f"Подтверждено коррекций размера: {len(pending)}")
        elif pending:
            job.add_log(
                f"Ожидают ручного решения и не будут включены в PDF: {len(pending)}"
            )
        job.total_orders = len(orders)
        job.passed_orders = sum(order.passed for order in orders)
        job.failed_orders = len(orders) - job.passed_orders
        job.unparsed_files = len(processor.unparsed)
        job.unsupported_files = len(processor.unsupported)
        job.add_log(
            f"Найдено заказов: {job.total_orders}; подходят: {job.passed_orders}"
        )

        if job.options.copy_failures:
            _set_stage(job, "Подготовка папки проблемных файлов", 35)
            processor.copy_failed_to_troubles(orders, troubles_dir)

        created_pdf_map: dict[str, Path] = {}
        if job.options.create_pdfs:
            _set_stage(job, "Создание и контроль PDF", 50)
            for order, path, error in processor.create_pdfs(
                [order for order in orders if order.passed]
            ):
                if error:
                    job.add_log(f"PDF заказа {order.order_id}: {error}")
                    processor.copy_pdf_failure_to_troubles(order, troubles_dir, error)
                else:
                    created_pdf_map[order.order_id] = path
            job.created_pdfs = len(created_pdf_map)

        if job.options.generate_previews and created_pdf_map:
            _set_stage(job, "Создание превью", 70)
            page_names: dict[Path, list[str]] = {}
            for order in orders:
                pdf_path = created_pdf_map.get(order.order_id)
                if pdf_path:
                    ordered = sorted(
                        order.files,
                        key=lambda item: 0 if item.parsed.side == "face" else 1,
                    )
                    page_names[pdf_path] = [item.path.stem for item in ordered]
            preview_results = processor.generate_previews_for_all(
                list(created_pdf_map.values()),
                preview_dir,
                pdf_page_names_map=page_names,
            )
            job.created_previews = sum(len(items) for _, items, _ in preview_results)
            for pdf_path, _, error in preview_results:
                if error:
                    job.add_log(f"Превью {pdf_path.name}: {error}")

        _set_stage(job, "Сохранение истории", 84)
        db_path = input_dir / "audit_history.db"
        for order in orders:
            save_order_audit(
                order=order,
                pdf_path=created_pdf_map.get(order.order_id),
                previews_count=sum(
                    1
                    for item in order.files
                    if (preview_dir / f"{item.path.stem}_preview.png").is_file()
                ),
                db_path=db_path,
                profile=profile,
            )

        _set_stage(job, "Сборка интерактивного отчёта", 94)
        build_orders_html_report(
            orders=orders,
            output_html_path=report_path,
            preview_dir=preview_dir,
            profile=profile,
        )
        job.report_path = report_path
        job.status = "completed"
        job.progress = 100
        job.stage = "Проверка завершена"
        job.add_log("Отчёт готов")
    except Exception as exc:
        job.status = "failed"
        job.stage = "Проверка остановлена"
        job.error = str(exc)
        job.add_log(f"Ошибка: {exc}")
        job.add_log(traceback.format_exc().splitlines()[-1])
    finally:
        job.finished_at = datetime.now(timezone.utc)


app = FastAPI(
    title="Image Magic Control",
    version="1.0.0",
    description="Панель запуска допечатной проверки заказов.",
)


@app.get("/", include_in_schema=False)
def control_panel() -> FileResponse:
    return FileResponse(UI_PATH)


@app.get("/api/config")
def get_config() -> dict[str, object]:
    return {
        "default_input_path": str(DEFAULT_INPUT_DIR),
        "allowed_roots": [str(root) for root in ALLOWED_ROOTS],
        "profiles": [
            {"id": key, "name": value.name} for key, value in PROFILES.items()
        ],
    }


@app.get("/api/folders")
def list_folders(path: str) -> dict[str, object]:
    current = resolve_input_path(path)
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
            if any(_is_inside(current.parent, root) for root in ALLOWED_ROOTS)
            else None
        ),
        "items": [{"name": item.name, "path": str(item)} for item in folders],
    }


@app.post("/api/checks", status_code=202)
def start_check(options: CheckOptions) -> dict[str, object]:
    if options.direction not in PROFILES:
        raise HTTPException(status_code=422, detail="Неизвестный профиль проверки.")
    input_dir = resolve_input_path(options.input_path)
    job = CheckJob(options, input_dir)
    with JOBS_LOCK:
        JOBS[job.id] = job
        while len(JOBS) > MAX_JOBS:
            JOBS.popitem(last=False)
    threading.Thread(target=_run_check, args=(job,), daemon=True).start()
    return job.as_dict()


@app.get("/api/checks")
def list_checks() -> dict[str, object]:
    with JOBS_LOCK:
        items = [job.as_dict() for job in reversed(JOBS.values())]
    return {"items": items}


def _get_job(job_id: str) -> CheckJob:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Запуск проверки не найден.")
    return job


@app.get("/api/checks/{job_id}")
def get_check(job_id: str) -> dict[str, object]:
    return _get_job(job_id).as_dict()


@app.get("/runs/{job_id}/report", include_in_schema=False)
def open_report(job_id: str) -> HTMLResponse:
    job = _get_job(job_id)
    if not job.report_path or not job.report_path.is_file():
        raise HTTPException(status_code=404, detail="Отчёт ещё не готов.")
    report_html = job.report_path.read_text(encoding="utf-8")
    preview_prefix = f"/runs/{job_id}/Previews/"
    report_html = report_html.replace("../Previews/", preview_prefix)
    return HTMLResponse(report_html)


@app.get("/runs/{job_id}/{folder}/{file_path:path}", include_in_schema=False)
def open_result_file(job_id: str, folder: str, file_path: str) -> FileResponse:
    job = _get_job(job_id)
    allowed = {"PDF", "Previews", "output_report"}
    if folder not in allowed:
        raise HTTPException(status_code=404, detail="Файл не найден.")
    base = (job.input_dir / folder).resolve()
    target = (base / file_path).resolve()
    if not _is_inside(target, base) or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден.")
    return FileResponse(target)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT)
