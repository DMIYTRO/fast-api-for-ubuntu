"""Исправление PDF заказа процесс-планом Callas и сборка превью «до / после».

Порядок: ``start`` ставит состояние ``processing`` → ``execute`` запускает план в режиме
``fix`` на сервере callas, сохраняет результат рядом с исходным PDF и строит три
картинки (оригинал, результат, наложение). Оператор принимает или отклоняет результат.

Исходный PDF не изменяется никогда. Всё, что создаёт сервис, лежит в
``<папка PDF>/CallasFix/<имя PDF>/``:

* ``state.json`` — состояние (переживает перезапуск сервиса);
* ``fixed.pdf`` — результат исправления (кандидат на следующую ревизию);
* ``compare/original|result|overlay`` — картинки сравнения.
"""

from __future__ import annotations

import filecmp
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from config.callas_plans import CALLAS_PLANS, CallasPlan
from core.fix_compare import FixCompareError, build_fix_comparison
from services.callas.errors import CallasError
from services.pdf_routing import STATUS_FOLDERS

logger = logging.getLogger(__name__)

STATE_NAME = "state.json"
FIXED_PDF_NAME = "fixed.pdf"
COMPARE_DIR = "compare"
IMAGE_KINDS = ("original", "result", "overlay")

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"


class CallasFixError(Exception):
    """Ошибка, текст которой можно показать оператору."""

    def __init__(self, message: str, *, status_code: int = 422, code: str = "callas_fix_error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def fix_dir_for(pdf_path: Path | str) -> Path:
    """Папка результата исправления.

    Для собранного PDF (лежит в ``PDF/`` или в папке статуса ``PDF/Print|Rework|Failed/``) —
    ``PDF/CallasFix/<имя>``. Для исходного
    PDF заказа, который лежит вне ``PDF/``, результат кладётся в ``PDF/CallasFix/<имя>.source``,
    чтобы не писать в папку с исходниками и не пересекаться с собранным PDF.
    """
    pdf = Path(pdf_path)
    if pdf.parent.name == "PDF":
        return pdf.parent / "CallasFix" / pdf.stem
    if pdf.parent.name in STATUS_FOLDERS and pdf.parent.parent.name == "PDF":
        # Собранный PDF, разложенный по папке статуса (Print/Rework/Failed).
        return pdf.parent.parent / "CallasFix" / pdf.stem
    return pdf.parent / "PDF" / "CallasFix" / f"{pdf.stem}.source"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def read_state_file(pdf_path: Path | str) -> dict[str, Any] | None:
    """Читает сохранённое состояние; ``None``, если исправления не было или файл повреждён."""
    path = fix_dir_for(pdf_path) / STATE_NAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_state(directory: Path, state: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"{STATE_NAME}.{uuid4().hex[:8]}.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, directory / STATE_NAME)


def comparison_payload(
    state: dict[str, Any] | None, base_url: str, *, source_only: bool = False
) -> dict[str, Any] | None:
    """Состояние → контракт интерфейса ``comparison`` (URL картинок строятся от ``base_url``)."""
    if not state:
        return None
    images = state.get("images") or {}
    return {
        "status": state.get("status") or STATUS_FAILED,
        "source_only": source_only,
        "plan": state.get("plan") or None,
        "error": state.get("error"),
        "decision": state.get("decision"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "summary": state.get("summary") or {},
        "original_url": f"{base_url}/files/original" if "original" in images else None,
        "result_url": f"{base_url}/files/result" if "result" in images else None,
        "overlay_url": f"{base_url}/files/overlay" if "overlay" in images else None,
        "pdf_url": f"{base_url}/files/pdf" if state.get("fixed_pdf") else None,
    }


class CallasFixService:
    def __init__(
        self,
        client: Any,
        plans: dict[str, CallasPlan] | None = None,
        *,
        compare: Callable[..., Any] = build_fix_comparison,
    ) -> None:
        self._client = client
        self._plans = plans if plans is not None else CALLAS_PLANS
        self._compare = compare
        self._lock = threading.Lock()
        self._active: set[str] = set()

    # ------------------------------------------------------------------ plans
    def plans(self, direction: str | None = None) -> list[CallasPlan]:
        return [
            plan
            for plan in self._plans.values()
            if not direction or direction in plan.directions
        ]

    def plan(self, key: str) -> CallasPlan | None:
        return self._plans.get(key)

    # ------------------------------------------------------------------ state
    def read_state(self, pdf_path: Path | str) -> dict[str, Any] | None:
        """Состояние с поправкой: ``processing`` без живой задачи — это прерванная перезапуском."""
        state = read_state_file(pdf_path)
        if state and state.get("status") == STATUS_PROCESSING:
            with self._lock:
                alive = str(fix_dir_for(pdf_path)) in self._active
            if not alive:
                state = {
                    **state,
                    "status": STATUS_FAILED,
                    "error": "Исправление прервано перезапуском сервиса. Запустите его заново.",
                    "finished_at": _now(),
                }
                _write_state(fix_dir_for(pdf_path), state)
        return state

    def start(self, pdf_path: Path | str, plan_key: str, *, direction: str | None = None) -> dict[str, Any]:
        """Проверяет запрос и записывает состояние ``processing``; саму работу делает ``execute``."""
        pdf = Path(pdf_path)
        plan = self._plans.get(plan_key)
        if plan is None:
            raise CallasFixError("Неизвестный план Callas.", code="unknown_callas_plan")
        if plan.mode != "fix":
            raise CallasFixError("Этот план не предназначен для исправления.", code="callas_plan_mode")
        if direction and direction not in plan.directions:
            raise CallasFixError("План недоступен для выбранного направления.", code="callas_plan_direction")
        if not pdf.is_file():
            raise CallasFixError("PDF заказа не найден.", status_code=404, code="pdf_not_found")
        key = str(fix_dir_for(pdf))
        with self._lock:
            if key in self._active:
                raise CallasFixError(
                    "Исправление этого заказа уже выполняется.", status_code=409, code="callas_fix_busy"
                )
            self._active.add(key)
        try:
            state = {
                "status": STATUS_PROCESSING,
                "plan": {"key": plan.key, "title": plan.title},
                "started_at": _now(),
                "finished_at": None,
                "error": None,
                "decision": None,
                "source": {"name": pdf.name, **_signature(pdf)},
                "images": {},
                "summary": {},
            }
            _write_state(fix_dir_for(pdf), state)
        except BaseException:
            with self._lock:
                self._active.discard(key)
            raise
        return state

    def execute(self, pdf_path: Path | str, plan_key: str) -> dict[str, Any]:
        """Выполняет исправление. Исключения не выпускает: ошибка попадает в состояние ``failed``."""
        pdf = Path(pdf_path)
        directory = fix_dir_for(pdf)
        key = str(directory)
        plan = self._plans[plan_key]
        state = read_state_file(pdf) or {}
        job_id = f"fix-{uuid4().hex[:16]}"
        try:
            result = self._client.process(
                pdf, plan.profile, "fix", job_id=job_id, report=("xml",), keep_name=True
            )
            fixed = directory / FIXED_PDF_NAME
            shutil.copyfile(result.output_pdf, fixed)
            compare_dir = directory / COMPARE_DIR
            if compare_dir.exists():
                shutil.rmtree(compare_dir, ignore_errors=True)
            comparison = self._compare(pdf, fixed, compare_dir)
            images = {
                kind: Path(getattr(comparison, f"{kind}_image")).name for kind in IMAGE_KINDS
            }
            summary = dict(comparison.summary)
            # Плоские поля, которые читает интерфейс (размер страницы до/после, мм).
            for side, field in (("original", "original_size_mm"), ("result", "result_size_mm")):
                described = summary.get(side) or {}
                summary[field] = [described.get("page_width_mm"), described.get("page_height_mm")]
            summary["callas"] = dict(result.summary)
            summary["callas_status"] = result.status
            state.update(
                status=STATUS_READY,
                finished_at=_now(),
                error=None,
                images=images,
                summary=summary,
                fixed_pdf=FIXED_PDF_NAME,
            )
            _write_state(directory, state)
        except Exception as exc:  # noqa: BLE001 — любое сбой должен дойти до оператора
            logger.exception("callas_fix.failed pdf=%s plan=%s", pdf.name, plan_key)
            message = str(exc).strip() or exc.__class__.__name__
            if not isinstance(exc, (CallasError, FixCompareError)):
                message = f"Исправление не выполнено: {message}"
            state.update(status=STATUS_FAILED, finished_at=_now(), error=message)
            _write_state(directory, state)
        finally:
            with self._lock:
                self._active.discard(key)
            self._cleanup_job(job_id)
        return state

    def _cleanup_job(self, job_id: str) -> None:
        job_dir = getattr(self._client, "job_dir", None)
        if job_dir is None:
            return
        try:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
        except Exception:  # noqa: BLE001 — уборка не должна ломать результат
            logger.warning("callas_fix.cleanup_failed job_id=%s", job_id, exc_info=True)

    # --------------------------------------------------------------- decision
    def decide(self, pdf_path: Path | str, decision: str) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise CallasFixError("Решение должно быть «accept» или «reject».", code="invalid_decision")
        pdf = Path(pdf_path)
        state = self.read_state(pdf)
        if state and state.get("status") == STATUS_ACCEPTED and decision == "accept":
            return state
        if not state or state.get("status") != STATUS_READY:
            raise CallasFixError(
                "Принять или отклонить можно только готовый результат исправления.",
                status_code=409,
                code="callas_fix_not_ready",
            )
        source = state.get("source") or {}
        current = _signature(pdf) if pdf.is_file() else None
        if current and (source.get("size"), source.get("mtime_ns")) != (
            current["size"],
            current["mtime_ns"],
        ):
            raise CallasFixError(
                "Исходный PDF изменился после исправления. Запустите исправление заново.",
                status_code=409,
                code="callas_fix_stale",
            )
        state.update(
            status=STATUS_ACCEPTED if decision == "accept" else STATUS_REJECTED,
            decision="accepted" if decision == "accept" else "rejected",
            decided_at=_now(),
        )
        _write_state(fix_dir_for(pdf), state)
        return state

    def accepted_pdf(self, pdf_path: Path | str) -> Path | None:
        """Принятый результат исправления или ``None``."""
        state = read_state_file(pdf_path)
        if not state or state.get("status") != STATUS_ACCEPTED:
            return None
        fixed = fix_dir_for(pdf_path) / str(state.get("fixed_pdf") or FIXED_PDF_NAME)
        return fixed if fixed.is_file() else None

    def publish_accepted(self, pdf_path: Path | str) -> Path | None:
        """Печатная копия принятого исправления рядом с исходным PDF или ``None``.

        Нужна на шаге «в печать»: результат копируется в ``<имя>_callas-fixed.pdf``, и
        дальше заказ идёт обычным путём (проверка PDF, PitStop, новая ревизия). Если
        исходный PDF изменился после принятия, печать отклоняется, а не подменяется.
        """
        accepted = self.accepted_pdf(pdf_path)
        if accepted is None:
            return None
        pdf = Path(pdf_path)
        if pdf.is_file() and filecmp.cmp(accepted, pdf, shallow=False):
            # Принятое исправление уже стало производственным PDF (после
            # повторной проверки и раскладки по папке статуса): публиковать нечего.
            return None
        state = read_state_file(pdf) or {}
        source = state.get("source") or {}
        current = _signature(pdf) if pdf.is_file() else None
        if current is None or (source.get("size"), source.get("mtime_ns")) != (
            current["size"],
            current["mtime_ns"],
        ):
            raise CallasFixError(
                "PDF заказа изменился после принятого исправления Callas. Запустите исправление заново.",
                status_code=409,
                code="callas_fix_stale",
            )
        target = fix_dir_for(pdf) / f"{pdf.stem}_callas-fixed.pdf"
        temporary = target.with_name(f".{target.stem}.{uuid4().hex[:8]}.tmp.pdf")
        shutil.copyfile(accepted, temporary)
        os.replace(temporary, target)
        return target

    def file_path(self, pdf_path: Path | str, kind: str) -> Path | None:
        """Путь к картинке сравнения (``original|result|overlay``) или к ``pdf`` результата."""
        state = read_state_file(pdf_path)
        if not state:
            return None
        directory = fix_dir_for(pdf_path)
        if kind == "pdf":
            name = state.get("fixed_pdf")
            target = directory / str(name) if name else None
        elif kind in IMAGE_KINDS:
            name = (state.get("images") or {}).get(kind)
            target = directory / COMPARE_DIR / str(name) if name else None
        else:
            return None
        return target if target is not None and target.is_file() else None
