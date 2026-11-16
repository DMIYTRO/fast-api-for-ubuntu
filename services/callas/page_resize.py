"""Коррекция размера страниц PDF через QuickFix callas pdfToolbox.

JSON собирает ``processing.pdf_resize_policy``; профиль «Image Magic/Apply QuickFix config»
только применяет его. QuickFix с ошибкой в JSON завершается без ошибки и без изменений,
а ``fixups_applied`` у JavaScript-QuickFix всегда 0, поэтому успех определяется только
по геометрии результата: число страниц, MediaBox, TrimBox и BleedBox каждой страницы.
Исходный PDF не меняется; результат пишется в ``target``.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.pdf_inspector import inspect_pdf
from processing.pdf_resize_policy import (
    QUICKFIX_VARIABLE,
    PdfPagePlan,
    build_resize_quickfix,
    quickfix_variable,
)
from server.settings import DEFAULT_PDF_RESIZE_PROFILE
from services.callas.client import deterministic_job_id

logger = logging.getLogger("image_magic.callas")

#: Допуск проверки результата, мм (округление точек PDF и боксов callas).
RESULT_TOLERANCE_MM = 0.1


class PdfResizeError(RuntimeError):
    """Коррекция размера PDF не выполнена; текст можно показать оператору."""


def _box_mm(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return ((box[2] - box[0]) * 25.4 / 72.0, (box[3] - box[1]) * 25.4 / 72.0)


def _close(actual: tuple[float, float], expected: tuple[float, float]) -> bool:
    return all(abs(a - e) <= RESULT_TOLERANCE_MM for a, e in zip(actual, expected))


def verify_resized_pdf(
    path: Path, page_targets: dict[int, tuple[float, float]], bleed_mm: float
) -> None:
    """Проверить, что каждая страница результата имеет целевой формат и боксы."""
    inspection = inspect_pdf(path)
    if inspection.page_count != len(page_targets):
        raise PdfResizeError(
            f"после коррекции размера в PDF {inspection.page_count} страниц; "
            f"ожидалось {len(page_targets)}"
        )
    for page in inspection.pages:
        target = page_targets[page.page_number]
        trim = (target[0] - 2 * bleed_mm, target[1] - 2 * bleed_mm)
        checks = (
            ("размер страницы", (page.width_mm, page.height_mm), target),
            ("TrimBox", _box_mm(page.trimbox), trim),
            ("BleedBox", _box_mm(page.bleedbox), target),
        )
        for name, actual, expected in checks:
            if not _close(actual, expected):
                raise PdfResizeError(
                    f"callas не привёл страницу {page.page_number} к формату: {name} "
                    f"{actual[0]:.2f}x{actual[1]:.2f} мм, ожидалось "
                    f"{expected[0]:.2f}x{expected[1]:.2f} мм"
                )


@dataclass(frozen=True)
class CallasPdfResizer:
    """``resizer(source, target, page_plans, bleed_mm, label)``: страницы приводятся к формату
    способом из плана (``fill`` / ``stretch`` / ``reveal``)."""

    client: Any
    profile: str = DEFAULT_PDF_RESIZE_PROFILE

    def __call__(
        self,
        source: Path,
        target: Path,
        page_plans: dict[int, PdfPagePlan],
        bleed_mm: float,
        label: str,
    ) -> dict[str, Any]:
        config = build_resize_quickfix(page_plans, bleed_mm)
        page_targets = {number: plan.target_mm for number, plan in page_plans.items()}
        variable = quickfix_variable(config)
        job_id = deterministic_job_id(
            f"resize-{label}", str(source), variable, uuid.uuid4().hex[:12]
        )
        logger.info("callas.pdf_resize.start label=%s job_id=%s", label, job_id)
        try:
            result = self.client.process(
                Path(source),
                self.profile,
                "fix",
                variables={QUICKFIX_VARIABLE: variable},
                job_id=job_id,
                report=("xml",),
                keep_name=True,
            )
            if result.output_pdf is None:
                raise PdfResizeError("callas не вернул PDF после коррекции размера")
            verify_resized_pdf(Path(result.output_pdf), page_targets, bleed_mm)
            shutil.copyfile(result.output_pdf, target)
            logger.info("callas.pdf_resize.done label=%s job_id=%s", label, job_id)
            return {"job_id": job_id, "quickfix": config}
        finally:
            try:
                self.client.delete(job_id)
            except Exception:
                pass
            shutil.rmtree(self.client.job_dir(job_id), ignore_errors=True)
