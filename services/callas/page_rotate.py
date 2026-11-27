"""Поворот страниц PDF на ±90° через QuickFix callas pdfToolbox (совмещение сторон заказа).

Тот же профиль, что и у коррекции размера («Image Magic/Apply QuickFix config»,
``scripts/build_quickfix_profile.py``): JSON QuickFix ``rotate_pages`` передаётся в
переменной ``quickfix_config``. Проверено на pdfToolbox 17.0.682 (2026-11-27): ``rotate_by``
считается от отображаемой страницы (с учётом её ``/Rotate``), ``90`` — по часовой стрелке,
``-90`` — против; поворот «запекается» в содержимое: ``/Rotate`` результата 0, MediaBox,
CropBox, TrimBox и BleedBox повёрнуты вместе со страницей.

QuickFix с ошибкой завершается без ошибки и без изменений, поэтому успех определяется по
геометрии результата (как в ``page_resize.py``): число страниц, у повёрнутых страниц нет
``/Rotate``, размер страницы, TrimBox и BleedBox поменялись местами относительно
отображаемых размеров исходника; остальные страницы не изменились. Исходный PDF не
меняется; результат пишется в ``target``.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.pdf_inspector import inspect_pdf
from processing.pdf_resize_policy import QUICKFIX_VARIABLE, quickfix_variable
from server.settings import DEFAULT_PDF_RESIZE_PROFILE
from services.callas.client import deterministic_job_id

logger = logging.getLogger("image_magic.callas")

#: Допуск проверки результата, мм (округление точек PDF и боксов callas).
RESULT_TOLERANCE_MM = 0.1


class PdfRotateError(RuntimeError):
    """Поворот страниц PDF не выполнен; текст можно показать оператору."""


def build_rotate_quickfix(page_rotations: dict[int, int], total_pages: int) -> dict[str, Any]:
    """QuickFix ``rotate_pages``: по инструкции на угол (страницы через запятую или ``all``)."""
    if not page_rotations:
        raise ValueError("нет страниц для поворота")
    by_angle: dict[int, list[int]] = {}
    for page, degrees in sorted(page_rotations.items()):
        if int(degrees) not in {90, -90}:
            raise ValueError(f"страница {page}: поворот допускается только на ±90°, получено {degrees}")
        if not 1 <= int(page) <= total_pages:
            raise ValueError(f"в PDF нет страницы {page}")
        by_angle.setdefault(int(degrees), []).append(int(page))
    return {
        "quickfixes": [{
            "quickfix": "rotate_pages",
            "version": "1.0",
            "instructions": [
                {
                    "page_selector": "all" if len(pages) == total_pages else ",".join(map(str, pages)),
                    "rotate_by": degrees,
                }
                for degrees, pages in by_angle.items()
            ],
        }],
    }


def _box_mm(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (abs(box[2] - box[0]) * 25.4 / 72.0, abs(box[3] - box[1]) * 25.4 / 72.0)


def _displayed(size: tuple[float, float], rotation: int) -> tuple[float, float]:
    return (size[1], size[0]) if rotation % 180 else size


def _page_geometry(page) -> dict[str, tuple[float, float]]:
    """Отображаемые (с учётом /Rotate) размеры страницы, TrimBox и BleedBox, мм."""
    return {
        "размер страницы": _displayed((page.width_mm, page.height_mm), page.rotation),
        "TrimBox": _displayed(_box_mm(page.trimbox), page.rotation),
        "BleedBox": _displayed(_box_mm(page.bleedbox), page.rotation),
    }


def _close(actual: tuple[float, float], expected: tuple[float, float]) -> bool:
    return all(abs(a - e) <= RESULT_TOLERANCE_MM for a, e in zip(actual, expected))


def verify_rotated_pdf(source: Path, result: Path, page_rotations: dict[int, int]) -> None:
    """Проверить геометрию результата относительно исходника (см. docstring модуля)."""
    before = inspect_pdf(source)
    after = inspect_pdf(result)
    if after.page_count != before.page_count:
        raise PdfRotateError(
            f"после поворота в PDF {after.page_count} страниц; ожидалось {before.page_count}"
        )
    for old, new in zip(before.pages, after.pages):
        rotated = old.page_number in page_rotations
        expected = _page_geometry(old)
        if rotated:
            if new.rotation % 360:
                raise PdfRotateError(
                    f"callas не повернул страницу {new.page_number}: у результата /Rotate {new.rotation}"
                )
            expected = {name: (size[1], size[0]) for name, size in expected.items()}
        for name, actual in _page_geometry(new).items():
            if not _close(actual, expected[name]):
                what = "не повернул" if rotated else "изменил"
                raise PdfRotateError(
                    f"callas {what} страницу {new.page_number}: {name} {actual[0]:.2f}x{actual[1]:.2f} мм, "
                    f"ожидалось {expected[name][0]:.2f}x{expected[name][1]:.2f} мм"
                )


@dataclass(frozen=True)
class CallasPdfRotator:
    """``rotator(source, target, page_rotations, label)``: страницы {номер: ±90} повёрнуты."""

    client: Any
    profile: str = DEFAULT_PDF_RESIZE_PROFILE

    def __call__(
        self,
        source: Path,
        target: Path,
        page_rotations: dict[int, int],
        label: str,
    ) -> dict[str, Any]:
        total = inspect_pdf(source).page_count
        config = build_rotate_quickfix(page_rotations, total)
        variable = quickfix_variable(config)
        job_id = deterministic_job_id(
            f"rotate-{label}", str(source), variable, uuid.uuid4().hex[:12]
        )
        logger.info("callas.pdf_rotate.start label=%s job_id=%s pages=%s", label, job_id, page_rotations)
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
                raise PdfRotateError("callas не вернул PDF после поворота страниц")
            verify_rotated_pdf(Path(source), Path(result.output_pdf), page_rotations)
            shutil.copyfile(result.output_pdf, target)
            logger.info("callas.pdf_rotate.done label=%s job_id=%s", label, job_id)
            return {"job_id": job_id, "quickfix": config}
        finally:
            try:
                self.client.delete(job_id)
            except Exception:
                pass
            shutil.rmtree(self.client.job_dir(job_id), ignore_errors=True)
