"""Перевод RGB → CMYK итогового PDF процесс-планом callas.

По умолчанию профиль «Image Magic/NEW_Convert color to 4c for digital printing»: RGB, ICC-RGB
и Lab уходят в DeviceCMYK (PSO Coated v3, OutputIntent не встраивается), ICC-CMYK снимается
без пересчёта. Перед конвертацией ищем ICC/Lab/калиброванный цвет стандартным профилем
«List objects using ICC or Lab or calibrated color»: после перевода профиль снят, и
карточка заказа показывает значок «ICC off» (``icc_removed``). Значок правдивый: профиль считается
снятым, только если он был до конвертации, а в результате исчез (повторный анализ результата и
наличие OutputIntent в каталоге PDF до и после).

Конвертация нужна PDF, где найден RGB, а также PDF без RGB, но с ICC-профилем CMYK/Gray или
OutputIntent (PitStop 2078 «ICC based is used»): тот же профиль снимает их без пересчёта значений.
Если в PDF есть Lab, ICC не трогаем (цвет автоматически не переводится). Исходный файл заменяется результатом
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

from core.pdf_inspector import find_icc_pages, find_rgb_pages, scan_color_profiles
from server.settings import DEFAULT_RGB_TO_CMYK_PROFILE
from services.callas.analysis import run_analysis
from services.callas.client import deterministic_job_id
from services.callas.prepress_reports import icc_pages

logger = logging.getLogger("image_magic.callas")


STATUS_CONVERTED = "converted"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
REASON_RGB = "rgb"
REASON_ICC = "icc"
ICC_PROFILE = "PDF analysis/List objects using ICC or Lab or calibrated color"


@dataclass(frozen=True)
class CallasRgbToCmykConverter:
    """``converter(source, target, label)``: callas fix → результат в ``target``; возвращает summary.

    ``strip_icc`` — снимать ли ICC-профили CMYK/Gray и OutputIntent у PDF без RGB
    (``IMAGE_MAGIC_CALLAS_STRIP_ICC_ENABLED``).
    """

    client: Any
    profile: str = DEFAULT_RGB_TO_CMYK_PROFILE
    strip_icc: bool = True

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


def has_output_intent(path: Path | str) -> bool:
    """Есть ли в каталоге PDF выходной профиль (OutputIntents); нечитаемый PDF считается без него."""
    try:
        with pymupdf.open(str(path)) as document:
            kind, value = document.xref_get_key(document.pdf_catalog(), "OutputIntents")
    except Exception:  # noqa: BLE001
        return False
    return kind != "null" and value.strip() not in ("", "[]", "null")


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


def _verify_icc_removed(converter: Any, result: Path, label: str, record: dict[str, Any]) -> None:
    """Повторный анализ результата: ICC считается снятым, только если на страницах он исчез.

    Если повторный анализ не удался, остаётся допущение профиля конвертации (результат
    DeviceCMYK, ICC-CMYK снимается без пересчёта); об этом говорит ``icc_verified: False``.
    """
    pages_before = set(record.get("icc_pages") or [])
    if not pages_before:
        return
    try:
        remaining = set(converter.detect_icc(result, f"{label}-after"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("callas.rgb_to_cmyk.icc_verify_failed label=%s error=%s", label, exc)
        record["icc_verified"] = False
        record["icc_removed"] = True
        return
    record["icc_verified"] = True
    record["icc_remaining_pages"] = sorted(remaining)
    record["icc_removed"] = bool(pages_before - remaining)


def convert_rgb_to_cmyk(
    pdf: Path | str, converter: Callable[[Path, Path, str], Any], label: str
) -> dict[str, Any] | None:
    """Конвертирует *pdf* на месте. ``None`` — ни RGB, ни снимаемого ICC не найдено, ничего не делали.

    Есть RGB: перевод в CMYK (``reason: "rgb"``). RGB нет, но есть ICC-профиль CMYK/Gray или
    OutputIntent: тот же профиль callas снимает его без пересчёта значений (``reason: "icc"``,
    см. ``_strip_icc_profiles``).
    """
    source = Path(pdf)
    profile = getattr(converter, "profile", None)
    try:
        pages = find_rgb_pages(source)
    except Exception:  # noqa: BLE001 — нечитаемый PDF ловят другие проверки
        logger.warning("callas.rgb_to_cmyk.detect_failed pdf=%s", source.name, exc_info=True)
        return None
    if not pages:
        return _strip_icc_profiles(source, converter, label, profile)
    record: dict[str, Any] = {
        "status": STATUS_FAILED,
        "reason": REASON_RGB,
        "from": "RGB",
        "to": "CMYK",
        "profile": profile,
        "pages": list(pages),
        "error": None,
    }
    _run_conversion(source, converter, label, record, strip_icc=False)
    return record


def _strip_icc_profiles(
    source: Path, converter: Callable[[Path, Path, str], Any], label: str, profile: str | None
) -> dict[str, Any] | None:
    """RGB нет: снимает ICC-профили CMYK/Gray и OutputIntent профилем конвертации callas.

    ``None`` — снимать нечего или снятие выключено (``IMAGE_MAGIC_CALLAS_STRIP_ICC_ENABLED``).
    Lab остаётся нетронутым (инвариант: цветовые пространства автоматически не переводятся):
    запись со ``status: "skipped"`` и ``skip_reason: "lab"``, PDF не меняется.
    """
    if not getattr(converter, "strip_icc", True):
        return None
    try:
        scan = scan_color_profiles(source)
        output_intent = has_output_intent(source)
    except Exception:  # noqa: BLE001 — нечитаемый PDF ловят другие проверки
        logger.warning("callas.strip_icc.detect_failed pdf=%s", source.name, exc_info=True)
        return None
    if not scan.icc_pages and not output_intent:
        return None
    record: dict[str, Any] = {
        "status": STATUS_FAILED,
        "reason": REASON_ICC,
        "from": "ICC",
        "to": "DeviceCMYK",
        "profile": profile,
        "pages": list(scan.icc_pages),
        "icc_present": bool(scan.icc_pages),
        "icc_pages": list(scan.icc_pages),
        "error": None,
    }
    if scan.lab_pages:
        record["status"] = STATUS_SKIPPED
        record["skip_reason"] = "lab"
        record["lab_pages"] = list(scan.lab_pages)
        record["skip_message"] = (
            "В PDF есть цветовое пространство Lab: ICC-профиль не снимали, "
            "чтобы не менять цвет автоматически."
        )
        return record
    _run_conversion(source, converter, label, record, strip_icc=True)
    return record


def _run_conversion(
    source: Path,
    converter: Callable[[Path, Path, str], Any],
    label: str,
    record: dict[str, Any],
    *,
    strip_icc: bool,
) -> None:
    """Гонит *source* через конвертер и подменяет его результатом после проверок; пишет в *record*."""
    temporary = source.with_name(f".{source.stem}.cmyk.{uuid.uuid4().hex[:8]}.tmp.pdf")
    try:
        # callas отклоняет результат, если size/mtime входа изменились за время работы
        # (на сетевых дисках mtime «плывёт» после записи). Отдаём ему локальный снимок.
        with tempfile.TemporaryDirectory(prefix="callas-cmyk-") as snapshot_dir:
            snapshot = Path(snapshot_dir) / source.name
            shutil.copyfile(source, snapshot)
            if not strip_icc:
                _detect_icc(converter, snapshot, label, record)
            record["output_intent_before"] = has_output_intent(snapshot)
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
        record["icc_removed"] = False
        if strip_icc:
            _verify_icc_removed_locally(temporary, record)
        elif record.get("icc_present") and hasattr(converter, "detect_icc"):
            _verify_icc_removed(converter, temporary, label, record)
        # Выходной профиль (OutputIntent) был и пропал из результата: тоже «профиль снят».
        record["output_intent_removed"] = bool(
            record.pop("output_intent_before", False) and not has_output_intent(temporary)
        )
        record["icc_removed"] = bool(record["icc_removed"] or record["output_intent_removed"])
        if strip_icc and not record["icc_removed"]:
            if record["icc_pages"]:
                raise RuntimeError("ICC-профиль остался в результате конвертации")
            # Был только OutputIntent, и профиль его не убрал: менять в PDF нечего.
            record["status"] = STATUS_SKIPPED
            record["skip_reason"] = "output_intent"
            record["skip_message"] = "OutputIntent профилем конвертации не снимается: PDF не менялся."
            return
        os.replace(temporary, source)
        record["status"] = STATUS_CONVERTED
        if isinstance(summary, dict):
            record["fixups_applied"] = summary.get("fixups_applied")
    except Exception as exc:  # noqa: BLE001 — сбой не роняет заказ
        logger.warning("callas.rgb_to_cmyk.failed label=%s error=%s", label, exc)
        record["status"] = STATUS_FAILED
        record["error"] = str(exc).strip() or exc.__class__.__name__
    finally:
        record.pop("output_intent_before", None)
        temporary.unlink(missing_ok=True)


def _verify_icc_removed_locally(result: Path, record: dict[str, Any]) -> None:
    """Локальный повторный поиск ICC CMYK/Gray в результате: снят, если исчез хотя бы на одной странице."""
    pages_before = set(record.get("icc_pages") or [])
    if not pages_before:
        return
    remaining = set(find_icc_pages(result))
    record["icc_remaining_pages"] = sorted(remaining & pages_before)
    record["icc_removed"] = bool(pages_before - remaining)


def color_failure_warning(record: dict[str, Any] | None) -> str | None:
    """Предупреждение оператору для неудавшейся цветовой обработки; ``None``, если сбоя нет."""
    if not record or record.get("status") != STATUS_FAILED:
        return None
    error = record.get("error")
    if record.get("reason") == REASON_ICC:
        return f"Не удалось снять ICC-профиль: {error}. PDF оставлен как есть."
    return f"Не удалось перевести RGB в CMYK: {error}. PDF оставлен в RGB."


def rgb_warning(text: str) -> bool:
    """Предупреждение именно про RGB (его снимает успешная конвертация)."""
    import re

    value = str(text)
    return bool(
        re.match(r"^цветовая модель (?:(?!не входит).)*rgb", value, re.IGNORECASE)
        or re.match(r"^PDF страница \d+: растровое изображение имеет RGB", value)
    )
