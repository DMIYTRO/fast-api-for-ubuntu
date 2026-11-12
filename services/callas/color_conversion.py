"""Перевод RGB → CMYK итогового PDF процесс-планом callas («Convert colors»).

Конвертация нужна только PDF, где найден RGB. Исходный файл заменяется результатом
лишь после проверки: страниц столько же, RGB не осталось. При любом сбое PDF
остаётся как был, а причина попадает в ``color_conversion`` заказа.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pymupdf

from core.pdf_inspector import find_rgb_pages
from server.settings import DEFAULT_RGB_TO_CMYK_PROFILE
from services.callas.client import deterministic_job_id

logger = logging.getLogger("image_magic.callas")


STATUS_CONVERTED = "converted"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class CallasRgbToCmykConverter:
    """``converter(source, target, label)``: callas fix → результат в ``target``; возвращает summary."""

    client: Any
    profile: str = DEFAULT_RGB_TO_CMYK_PROFILE

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
        summary = converter(source, temporary, label)
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
