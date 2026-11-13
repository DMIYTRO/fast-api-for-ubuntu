"""Перевод RGB → CMYK итогового PDF процесс-планом callas.

По умолчанию профиль «Image Magic/NEW_Convert color to 4c for digital printing»: RGB, ICC-RGB
и Lab уходят в DeviceCMYK (PSO Coated v3, OutputIntent не встраивается), ICC-CMYK снимается
без пересчёта. Перед конвертацией ищем ICC/Lab/калиброванный цвет стандартным профилем
«List objects using ICC or Lab or calibrated color»: после перевода профиль снят, и
карточка заказа показывает значок «ICC off» (``icc_removed``).

Конвертация нужна только PDF, где найден RGB. Исходный файл заменяется результатом
лишь после проверки: страниц столько же, RGB не осталось. При любом сбое PDF
остаётся как был, а причина попадает в ``color_conversion`` заказа.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pymupdf

from core.pdf_inspector import find_rgb_pages
from server.settings import DEFAULT_RGB_TO_CMYK_PROFILE
from services.callas.analysis import run_analysis
from services.callas.client import deterministic_job_id
from services.callas.prepress_reports import icc_pages

logger = logging.getLogger("image_magic.callas")


STATUS_CONVERTED = "converted"
STATUS_FAILED = "failed"
ICC_PROFILE = "PDF analysis/List objects using ICC or Lab or calibrated color"


@dataclass(frozen=True)
class CallasRgbToCmykConverter:
    """``converter(source, target, label)``: callas fix → результат в ``target``; возвращает summary."""

    client: Any
    profile: str = DEFAULT_RGB_TO_CMYK_PROFILE

    def detect_icc(self, source: Path, label: str) -> list[int]:
        """Страницы с ICC/Lab/калиброванным цветом (до конвертации)."""
        _result, root = run_analysis(self.client, source, ICC_PROFILE, f"icc-{label}")
        return icc_pages(root)

    def __call__(self, source: Path, target: Path, label: str) -> dict[str, int]:
        job_id = deterministic_job_id(
            f"cmyk-{label}", str(source), self.profile, uuid.uuid4().hex[:12]
        )
        logger.info("callas.rgb_to_cmyk.start label=%s job_id=%s", label, job_id)
        try:
            result = self.client.process(
                Path(source), self.profile, "fix", job_id=job_id, report=("xml",), keep_name=True
            )
            if result.output_pdf is None:
                raise RuntimeError("callas не вернул PDF после конвертации в CMYK")
            shutil.copyfile(result.output_pdf, target)
            return dict(result.summary)
        finally:
            try:
                self.client.delete(job_id)
            except Exception:
                pass
            shutil.rmtree(self.client.job_dir(job_id), ignore_errors=True)


def _page_count(path: Path) -> int:
    with pymupdf.open(str(path)) as document:
        return document.page_count


def _detect_icc(converter: Any, snapshot: Path, label: str, record: dict[str, Any]) -> None:
    """ICC до конвертации; сбой определения не мешает самой конвертации."""
    detector = getattr(converter, "detect_icc", None)
    if detector is None:
        return
    try:
        pages = list(detector(snapshot, label))
    except Exception as exc:  # noqa: BLE001
        logger.warning("callas.rgb_to_cmyk.icc_detect_failed label=%s error=%s", label, exc)
        record["icc_error"] = str(exc).strip() or exc.__class__.__name__
        return
    record["icc_pages"] = pages
    record["icc_present"] = bool(pages)


def convert_rgb_to_cmyk(
    pdf: Path | str, converter: Callable[[Path, Path, str], Any], label: str
) -> dict[str, Any] | None:
    """Конвертирует *pdf* на месте. ``None`` — RGB не найден (или PDF не прочитан), ничего не делали."""
    source = Path(pdf)
    profile = getattr(converter, "profile", None)
    try:
        pages = find_rgb_pages(source)
    except Exception:  # noqa: BLE001 — нечитаемый PDF ловят другие проверки
        logger.warning("callas.rgb_to_cmyk.detect_failed pdf=%s", source.name, exc_info=True)
        return None
    if not pages:
        return None
    record: dict[str, Any] = {
        "status": STATUS_FAILED,
        "from": "RGB",
        "to": "CMYK",
        "profile": profile,
        "pages": list(pages),
        "error": None,
    }
    temporary = source.with_name(f".{source.stem}.cmyk.{uuid.uuid4().hex[:8]}.tmp.pdf")
    try:
        # callas отклоняет результат, если size/mtime входа изменились за время работы
        # (на сетевых дисках mtime «плывёт» после записи). Отдаём ему локальный снимок.
        with tempfile.TemporaryDirectory(prefix="callas-cmyk-") as snapshot_dir:
            snapshot = Path(snapshot_dir) / source.name
            shutil.copyfile(source, snapshot)
            _detect_icc(converter, snapshot, label, record)
            summary = converter(snapshot, temporary, label)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError("callas вернул пустой PDF")
        if _page_count(temporary) != _page_count(source):
            raise RuntimeError("после конвертации изменилось число страниц")
        remaining = find_rgb_pages(temporary)
        if remaining:
            raise RuntimeError(
                "после конвертации RGB остался на страницах: "
                + ", ".join(str(number) for number in remaining)
            )
        os.replace(temporary, source)
        record["status"] = STATUS_CONVERTED
        # ICC был в документе и после конвертации снят (результат DeviceCMYK).
        record["icc_removed"] = bool(record.get("icc_present"))
        if isinstance(summary, dict):
            record["fixups_applied"] = summary.get("fixups_applied")
    except Exception as exc:  # noqa: BLE001 — сбой не роняет заказ
        logger.warning("callas.rgb_to_cmyk.failed label=%s error=%s", label, exc)
        record["error"] = str(exc).strip() or exc.__class__.__name__
    finally:
        temporary.unlink(missing_ok=True)
    return record


def rgb_warning(text: str) -> bool:
    """Предупреждение именно про RGB (его снимает успешная конвертация)."""
    import re

    value = str(text)
    return bool(
        re.match(r"^цветовая модель (?:(?!не входит).)*rgb", value, re.IGNORECASE)
        or re.match(r"^PDF страница \d+: растровое изображение имеет RGB", value)
    )
