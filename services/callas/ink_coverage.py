"""Красочность (ink coverage) из задания callas ``inkcoverage``.

Служба возвращает по каждой странице список красок с полем ``percent`` —
это **среднее по рамке страницы покрытие краской в процентах** (сплошная
заливка всей рамки одной краской — 100). Это НЕ TAC (максимальная сумма
красок в точке): страница, целиком залитая 100/100/100/100, даёт сумму 400,
а маленькое пятно 400 % на белой странице — почти 0. Перелив TAC ищется
отдельно (вид ``ink_temp`` визуализатора), здесь он не вычисляется.

Определения, на которые опираются пороги:

* значение страницы — ``total_sum_percent``: сумма средних покрытий всех
  красок страницы (триадных и плашечных); страница без красок даёт 0;
* ``document_min_percent`` / ``document_max_percent`` — минимум и максимум
  ``total_sum_percent`` по проверенным страницам (``min_page`` / ``max_page`` —
  номера этих страниц). Блок ``document`` службы (средние по всему документу)
  в DTO не переносится, чтобы не смешивать его со страничными значениями;
* флаг ``underflow`` («Недолив») — минимум ниже ``min_percent``;
  флаг ``overflow`` («Перелив») — максимум выше ``max_percent``.

Флаги пока не меняют статус заказа: это основа для отображения на карточке.
"""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from services.callas.errors import CallasProtocolError

logger = logging.getLogger("services.callas")

INK_COVERAGE_BOXES = ("MediaBox", "CropBox", "TrimBox", "BleedBox", "ArtBox")
FLAG_LABELS = {"underflow": "Недолив", "overflow": "Перелив"}
_PROCESS_INKS = frozenset({"cyan", "magenta", "yellow", "black"})


@dataclass(frozen=True, slots=True)
class InkCoverageInk:
    name: str
    kind: str  # "process" | "spot"
    percent: float  # среднее покрытие по рамке, %

    def to_dto(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "percent": self.percent}


@dataclass(frozen=True, slots=True)
class InkCoveragePage:
    page: int
    inks: tuple[InkCoverageInk, ...]
    process_sum_percent: float | None
    total_sum_percent: float

    def to_dto(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "total_sum_percent": self.total_sum_percent,
            "process_sum_percent": self.process_sum_percent,
            "inks": [ink.to_dto() for ink in self.inks],
        }


@dataclass(frozen=True, slots=True)
class InkCoverageThresholds:
    min_percent: float | None = None
    max_percent: float | None = None

    def __post_init__(self) -> None:
        values = (self.min_percent, self.max_percent)
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in values):
            raise ValueError("Пороги красочности должны быть конечными неотрицательными числами.")
        if all(v is not None for v in values) and self.min_percent > self.max_percent:
            raise ValueError("Минимальный порог красочности не может превышать максимальный.")

    def to_dto(self) -> dict[str, Any]:
        return {"min_percent": self.min_percent, "max_percent": self.max_percent}


@dataclass(frozen=True, slots=True)
class InkCoverageFlag:
    kind: str  # "underflow" | "overflow"
    page: int
    percent: float
    threshold: float

    @property
    def label(self) -> str:
        return FLAG_LABELS[self.kind]

    def describe(self) -> str:
        """Подпись для оператора: «Перелив: 262% на стр. 2 (порог 250%)»."""
        return (
            f"{self.label}: {_fmt(self.percent)}% на стр. {self.page} "
            f"(порог {_fmt(self.threshold)}%)"
        )

    def to_dto(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "page": self.page,
            "percent": self.percent,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class InkCoverageResult:
    status: str  # "completed" | "failed" | "not_checked"
    pages: tuple[InkCoveragePage, ...] = ()
    resolution: int | None = None
    box: str | None = None
    document_min_percent: float | None = None
    document_max_percent: float | None = None
    min_page: int | None = None
    max_page: int | None = None
    error: str | None = None

    @classmethod
    def failed(cls, error: str) -> "InkCoverageResult":
        return cls(status="failed", error=error or "Проверка красочности не выполнена.")

    @classmethod
    def not_checked(cls) -> "InkCoverageResult":
        return cls(status="not_checked")


def parse_ink_coverage(payload: Mapping[str, Any], *, job_id: str | None = None) -> InkCoverageResult:
    """Разбирает блок ``ink_coverage`` ответа службы (или весь ответ задания)."""
    if not isinstance(payload, Mapping):
        raise CallasProtocolError("Callas вернул некорректный отчёт красочности.", job_id=job_id)
    block = payload.get("ink_coverage", payload)
    if not isinstance(block, Mapping) or not isinstance(block.get("pages"), list):
        raise CallasProtocolError(
            "Callas не вернул страницы в отчёте красочности.", job_id=job_id
        )
    pages: list[InkCoveragePage] = []
    for raw in block["pages"]:
        if not isinstance(raw, Mapping):
            raise CallasProtocolError("Некорректная страница в отчёте красочности.", job_id=job_id)
        number = raw.get("page")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise CallasProtocolError("Некорректный номер страницы в отчёте красочности.", job_id=job_id)
        raw_inks = raw.get("inks")
        if raw_inks is not None and not isinstance(raw_inks, list):
            raise CallasProtocolError("Некорректный список красок в отчёте красочности.", job_id=job_id)
        inks = tuple(_parse_ink(item, job_id) for item in (raw_inks or []))
        process_sum = _number(raw.get("process_sum_percent"))
        total_sum = _number(raw.get("total_sum_percent"))
        if any(key in raw and _number(raw[key]) is None for key in ("process_sum_percent", "total_sum_percent")):
            raise CallasProtocolError("Некорректная сумма красок в отчёте красочности.", job_id=job_id)
        if total_sum is None and raw_inks is None:
            raise CallasProtocolError("В отчёте нет измерений красочности страницы.", job_id=job_id)
        if process_sum is None and raw_inks is not None:
            process_sum = round(sum(ink.percent for ink in inks if ink.kind == "process"), 4)
        if total_sum is None:
            total_sum = round(sum(ink.percent for ink in inks), 4)
        pages.append(
            InkCoveragePage(
                page=number,
                inks=inks,
                process_sum_percent=process_sum,
                total_sum_percent=total_sum,
            )
        )
    if len({p.page for p in pages}) != len(pages):
        raise CallasProtocolError("Повтор номера страницы в отчёте красочности.", job_id=job_id)
    pages.sort(key=lambda item: item.page)
    resolution = block.get("resolution")
    if not isinstance(resolution, int) or isinstance(resolution, bool):
        resolution = None
    box = block.get("box")
    if not isinstance(box, str) or not box:
        box = None
    min_page = min(pages, key=lambda item: item.total_sum_percent, default=None)
    max_page = max(pages, key=lambda item: item.total_sum_percent, default=None)
    return InkCoverageResult(
        status="completed",
        pages=tuple(pages),
        resolution=resolution,
        box=box,
        document_min_percent=min_page.total_sum_percent if min_page else None,
        document_max_percent=max_page.total_sum_percent if max_page else None,
        min_page=min_page.page if min_page else None,
        max_page=max_page.page if max_page else None,
    )


def evaluate(result: InkCoverageResult, thresholds: InkCoverageThresholds | None) -> list[InkCoverageFlag]:
    """Флаги недолива/перелива по минимуму и максимуму страниц документа."""
    if thresholds is None or result.status != "completed" or not result.pages:
        return []
    flags: list[InkCoverageFlag] = []
    if thresholds.min_percent is not None and result.document_min_percent is not None and result.document_min_percent < thresholds.min_percent:
        flags.append(
            InkCoverageFlag(
                kind="underflow",
                page=int(result.min_page or 0),
                percent=result.document_min_percent,
                threshold=thresholds.min_percent,
            )
        )
    if thresholds.max_percent is not None and result.document_max_percent is not None and result.document_max_percent > thresholds.max_percent:
        flags.append(
            InkCoverageFlag(
                kind="overflow",
                page=int(result.max_page or 0),
                percent=result.document_max_percent,
                threshold=thresholds.max_percent,
            )
        )
    return flags


def to_dto(
    result: InkCoverageResult,
    thresholds: InkCoverageThresholds | None = None,
    flags: Iterable[InkCoverageFlag] | None = None,
) -> dict[str, Any]:
    """Ключ ``ink_coverage`` DTO заказа. Флаги вычисляются, если не переданы."""
    if flags is None:
        flags = evaluate(result, thresholds)
    return {
        "status": result.status,
        "resolution": result.resolution,
        "box": result.box,
        "document": {
            "min_percent": result.document_min_percent,
            "max_percent": result.document_max_percent,
            "min_page": result.min_page,
            "max_page": result.max_page,
        },
        "pages": [page.to_dto() for page in result.pages],
        "flags": [flag.to_dto() for flag in flags],
        "thresholds": thresholds.to_dto() if thresholds is not None else None,
        "error": result.error,
    }


def _parse_ink(item: Any, job_id: str | None) -> InkCoverageInk:
    if not isinstance(item, Mapping):
        raise CallasProtocolError("Некорректная краска в отчёте красочности.", job_id=job_id)
    name = item.get("name")
    percent = _number(item.get("percent"))
    if not isinstance(name, str) or not name or percent is None:
        raise CallasProtocolError("Некорректная краска в отчёте красочности.", job_id=job_id)
    kind = item.get("kind")
    if kind not in ("process", "spot"):
        kind = "process" if name.strip().lower() in _PROCESS_INKS else "spot"
    return InkCoverageInk(name=name, kind=kind, percent=percent)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _fmt(value: float) -> str:
    rounded = round(value)
    return str(int(rounded)) if abs(value - rounded) < 0.05 else f"{value:.1f}"


def thresholds_for(
    direction: str,
    *,
    min_percent_override: float | None = None,
    max_percent_override: float | None = None,
) -> InkCoverageThresholds:
    """Пороги из профиля направления с необязательным переопределением из настроек."""
    from config.profiles import get_profile

    profile = get_profile(direction)
    return InkCoverageThresholds(
        min_percent=profile.ink_min_percent if min_percent_override is None else min_percent_override,
        max_percent=profile.ink_max_percent if max_percent_override is None else max_percent_override,
    )


def build_ink_coverage_checker(
    client: Any,
    *,
    min_percent_override: float | None = None,
    max_percent_override: float | None = None,
    resolution: int = 72,
    box: str = "TrimBox",
) -> Callable[[Path, str], dict[str, Any]]:
    """``(pdf_path, direction) -> DTO ink_coverage``; никогда не бросает исключений.

    Папка задания на общем ресурсе удаляется после разбора ответа.
    """

    def check(pdf_path: Path, direction: str) -> dict[str, Any]:
        thresholds = None
        job_id = None
        try:
            thresholds = thresholds_for(
                direction,
                min_percent_override=min_percent_override,
                max_percent_override=max_percent_override,
            )
            job_id = deterministic_ink_job_id(Path(pdf_path), resolution, box)
            result = client.ink_coverage(Path(pdf_path), resolution=resolution, box=box, job_id=job_id)
        except Exception as exc:  # noqa: BLE001 - сбой проверки не должен ронять заказ
            message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
            logger.warning("callas: красочность %s не рассчитана: %s", Path(pdf_path).name, message)
            return to_dto(InkCoverageResult.failed(message), thresholds)
        if job_id is not None:
            _cleanup_job_dir(client, job_id)
        return to_dto(result, thresholds)

    return check


def deterministic_ink_job_id(pdf_path: Path, resolution: int, box: str) -> str:
    from services.callas.client import _sha256_file, deterministic_job_id

    return deterministic_job_id("callas-ink", _sha256_file(pdf_path), "", str(resolution), box)


def _cleanup_job_dir(client: Any, job_id: str) -> None:
    job_dir = getattr(client, "job_dir", None)
    if job_dir is None:
        return
    try:
        shutil.rmtree(job_dir(job_id), ignore_errors=True)
    except Exception:  # noqa: BLE001
        logger.debug("callas: не удалось удалить папку задания %s", job_id)


# Имена без коллизий для реэкспорта из services.callas.
evaluate_ink_coverage = evaluate
ink_coverage_to_dto = to_dto

__all__ = [
    "FLAG_LABELS",
    "INK_COVERAGE_BOXES",
    "InkCoverageFlag",
    "InkCoverageInk",
    "InkCoveragePage",
    "InkCoverageResult",
    "InkCoverageThresholds",
    "build_ink_coverage_checker",
    "thresholds_for",
    "evaluate",
    "evaluate_ink_coverage",
    "ink_coverage_to_dto",
    "parse_ink_coverage",
    "to_dto",
]
