"""Печать красного заказа со сборкой итогового PDF («сборка при печати»).

Заказ, не прошедший локальную проверку, получает только диагностический PDF «как есть»
(``pdf_kind == "diagnostic"``, ``PDF/Failed``): без RGB → CMYK, снятия ICC, полных
допечатных проверок и контроля итогового PDF. Такой PDF в печать не идёт.

Если ВСЕ локальные ошибки заказа — низкое разрешение (effective DPI ниже нормы), оператор
может отправить заказ в печать под свою ответственность: при подтверждении собирается
итоговый PDF обычным производственным путём (``BatchProcessor.create_forced_print_pdf``),
он проверяется PitStop как новая ревизия и только потом уходит в ``PDF/Print``.

Любая другая локальная ошибка (нарушение правила страниц, размер и коррекция, нечитаемый
файл, слои и альфа TIFF, недостающие стороны…) по-прежнему блокирует печать.

Жёлтый (или зелёный) заказ с диагностическим PDF — заказ с локальными ошибками, у которого
PitStop диагностического PDF не нашёл ничего хуже предупреждения (ошибка макета при
диагностическом PDF становится предупреждением, см. ``completed_status`` в
``services/batch_adapter.py``). Диагностический файл в печать тоже не идёт никогда: если все
локальные ошибки — низкое разрешение, при печати собирается итоговый PDF тем же путём
(``print_build``, без красного подтверждения), иначе печать заблокирована (``no_final_pdf``).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .issue_groups import normalize_local_message
from .overprint import has_blocking_overprint

#: ``created_by_action`` ревизии PDF и код блокера / записи журнала.
FORCED_PRINT_BUILD = "forced_print_build"
#: То же для жёлтого заказа с диагностическим PDF: сборка при печати без подтверждения.
PRINT_BUILD = "print_build"
#: Статусы, при которых заказ печатается без подтверждения (``print_readiness``).
_PRINTABLE = frozenset({"passed", "warning"})

#: Коды ``services/issue_groups`` для низкого разрешения (растр и PDF).
LOW_DPI_CODES = frozenset({"low_dpi", "pdf_low_dpi"})
#: Ошибки обработки, не мешающие сборке: превью строятся заново по новой ревизии.
IGNORED_PROCESSING_CODES = frozenset({"preview_error"})

_PDF_LOW_DPI = re.compile(
    r"effective DPI (\d+(?:[.,]\d+)?); требуется не менее (\d+(?:[.,]\d+)?)", re.IGNORECASE
)
_RASTER_LOW_DPI = re.compile(
    r"^разрешение (\d+(?:[.,]\d+)?)x(\d+(?:[.,]\d+)?) DPI; по обеим осям требуется не меньше "
    r"(\d+(?:[.,]\d+)?) DPI",
    re.IGNORECASE,
)


class ForcedPrintBuildError(RuntimeError):
    """Итоговый PDF для печати под ответственность оператора собрать нельзя (текст — оператору)."""


@dataclass(frozen=True)
class LowDpiAssessment:
    """Итог разбора локальных ошибок: только ли низкое разрешение."""

    eligible: bool
    #: Наименьшее найденное разрешение и норма профиля (для текста оператору).
    min_dpi: float | None = None
    required_dpi: float | None = None
    #: Коды ошибок, которые не являются низким разрешением (почему нельзя).
    blocking_codes: tuple[str, ...] = ()
    #: Сами сообщения о низком разрешении (для журнала).
    low_dpi_messages: tuple[str, ...] = ()


@dataclass
class ForcedPrintBuild:
    """Результат сборки итогового PDF при печати (``BatchProcessorAdapter.build_forced_print_pdf``)."""

    pdf_path: Path
    prepress: dict[str, Any] | None = None
    color_conversion: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    assessment: LowDpiAssessment | None = None


def _number(value: str) -> float:
    return float(value.replace(",", "."))


def _low_dpi_values(message: str) -> tuple[float, float] | None:
    """``(разрешение, норма)`` из сообщения о низком разрешении; ``None`` — не разобрано."""
    match = _PDF_LOW_DPI.search(message)
    if match:
        return _number(match.group(1)), _number(match.group(2))
    match = _RASTER_LOW_DPI.search(message)
    if match:
        return min(_number(match.group(1)), _number(match.group(2))), _number(match.group(3))
    return None


def assess_local_errors(
    errors: Iterable[str], *, processing_errors: Iterable[str] = ()
) -> LowDpiAssessment:
    """Все ли локальные ошибки — низкое разрешение.

    ``errors`` — ошибки заказа и его файлов; ``processing_errors`` — ошибки обработки
    (из них не мешают только ошибки превью). Без единой ошибки низкого разрешения
    заказ не подходит: печатать с ошибками здесь нечего.
    """
    blocking: list[str] = []
    low: list[str] = []
    values: list[tuple[float, float]] = []
    for message in errors:
        text = str(message or "").strip()
        if not text:
            continue
        code, _label = normalize_local_message(text)
        parsed = _low_dpi_values(text) if code in LOW_DPI_CODES else None
        if parsed is None:
            # Разрешение «не определено» — тоже не повод печатать вслепую.
            blocking.append(code if code not in LOW_DPI_CODES else f"{code}_unknown")
            continue
        low.append(text)
        values.append(parsed)
    for message in processing_errors:
        text = str(message or "").strip()
        if text and normalize_local_message(text)[0] not in IGNORED_PROCESSING_CODES:
            blocking.append(normalize_local_message(text)[0])
    if not low and not blocking:
        blocking.append("no_low_dpi_errors")
    if blocking:
        return LowDpiAssessment(False, blocking_codes=tuple(dict.fromkeys(blocking)))
    return LowDpiAssessment(
        True,
        min_dpi=min(value for value, _ in values),
        required_dpi=max(required for _, required in values),
        low_dpi_messages=tuple(low),
    )


def assess_order(order: Mapping[str, Any]) -> LowDpiAssessment:
    """Разбор DTO заказа: ошибки заказа, файлов и обработки; жёсткие запреты отдельно."""
    files = [item for item in order.get("files") or [] if isinstance(item, Mapping)]
    hard: list[str] = []
    if int(order.get("pending_confirmations") or 0) > 0 or any(
        item.get("resample_decision") == "ask_confirmation" for item in files
    ):
        hard.append("correction_confirmation_pending")
    if any(item.get("has_unflattened_layers") for item in files):
        hard.append("tiff_layers")
    if not files:
        hard.append("no_files")
    errors = [*list(order.get("errors") or []), *(e for item in files for e in item.get("errors") or [])]
    assessment = assess_local_errors(errors, processing_errors=order.get("processing_errors") or [])
    if hard:
        return LowDpiAssessment(
            False, blocking_codes=tuple(dict.fromkeys([*hard, *assessment.blocking_codes]))
        )
    return assessment


def assess_order_check(order: Any) -> LowDpiAssessment:
    """То же по заново осмотренному ``OrderCheck`` перед сборкой (повторная защита)."""
    from processing.models import has_page_rule_violation

    hard: list[str] = []
    if has_page_rule_violation(order):
        hard.append("page_rule_violation")
    if any(item.resample_decision == "ask_confirmation" for item in order.files):
        hard.append("correction_confirmation_pending")
    if any(item.has_unflattened_layers for item in order.files):
        hard.append("tiff_layers")
    if not order.files:
        hard.append("no_files")
    errors = [*order.errors, *(error for item in order.files for error in item.errors)]
    assessment = assess_local_errors(errors)
    if hard:
        return LowDpiAssessment(
            False, blocking_codes=tuple(dict.fromkeys([*hard, *assessment.blocking_codes]))
        )
    return assessment


def forced_build_assessment(order: Mapping[str, Any]) -> LowDpiAssessment | None:
    """Разбор для красного заказа с диагностическим PDF; ``None`` — не тот случай."""
    if order.get("status") != "error" or order.get("pdf_kind") != "diagnostic":
        return None
    return assess_order(order)


def print_build_assessment(order: Mapping[str, Any]) -> LowDpiAssessment | None:
    """Разбор для жёлтого (зелёного) заказа с диагностическим PDF; ``None`` — не тот случай.

    ``eligible`` — при печати собирается итоговый PDF (``PRINT_BUILD``); иначе печать
    заблокирована: диагностический PDF не печатается ни при каком статусе.
    """
    if order.get("status") not in _PRINTABLE or order.get("pdf_kind") != "diagnostic":
        return None
    return assess_order(order)


def diagnostic_blocked_message(assessment: LowDpiAssessment) -> str:
    """Почему жёлтый заказ с диагностическим PDF нельзя ни напечатать, ни собрать."""
    codes = ", ".join(assessment.blocking_codes) or "неизвестно"
    return (
        "Готового итогового PDF нет: у заказа только диагностический PDF «как есть», а "
        f"собрать итоговый при печати нельзя — кроме низкого разрешения есть другие ошибки ({codes}). "
        "Исправьте макет и повторите проверку."
    )


def _dpi(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"


def forced_build_message(order: Mapping[str, Any], assessment: LowDpiAssessment) -> str:
    """Текст подтверждения оператору."""
    message = (
        f"Заказ с ошибками (низкое разрешение: от {_dpi(assessment.min_dpi)} DPI при норме "
        f"{_dpi(assessment.required_dpi)}). Будет собран итоговый PDF и отправлен в печать "
        "под вашу ответственность."
    )
    if has_blocking_overprint(order.get("prepress")):
        message += (
            " Также найден overprint (наложение красок): он останется в итоговом PDF."
        )
    return message


def blocking_message(assessment: LowDpiAssessment) -> str:
    """Почему заново осмотренный заказ нельзя собрать для печати."""
    codes = ", ".join(assessment.blocking_codes) or "неизвестно"
    return (
        "Итоговый PDF не собран: кроме низкого разрешения у заказа есть другие ошибки "
        f"({codes}). Исправьте макет и повторите проверку; заказ не отправлен в печать."
    )
