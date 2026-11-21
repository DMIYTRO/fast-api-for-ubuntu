"""Overprint итогового PDF: технические краски и влияние на статус заказа.

Проверка — профиль callas «Image Magic/Overprint - any object» (``PrepressAnalyzer.check_overprint``,
разбор отчёта — ``services.callas.prepress_reports.overprint_report``). Здесь только чистые
функции без зависимостей от клиента callas: их используют расчёт статуса
(``services.pdf_routing.completed_status``), группы проблем и журнал заказа.

Решение оператора (2026-11-21): overprint на любом объекте, кроме технических красок
(белила, лак, контур реза и т. п.), — ошибка заказа (красная группа) в любой политике
проверки. Overprint только на технических красках — справка, статус не меняется.
Сбой проверки — предупреждение: заказ не краснеет из-за сбоя callas, но и не
считается проверенным.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from config.overprint import DEFAULT_OVERPRINT_IGNORE_SPOTS, DEFAULT_OVERPRINT_PROFILE  # noqa: F401 — реэкспорт

_SEPARATORS_RE = re.compile(r"[\s_\-.]+")

STATUS_ERROR = "error"
STATUS_WARNING = "warning"


def normalize_spot_name(name: object) -> str:
    """Регистр не важен, «ё» = «е», пробелы, «_», «-» и «.» — один пробел."""
    text = str(name or "").casefold().replace("ё", "е")
    return _SEPARATORS_RE.sub(" ", text).strip()


def is_technical_spot(name: object, ignore: Iterable[str]) -> bool:
    """Имя краски совпадает с техническим токеном или начинается с него и не-буквы.

    Сравнение по нормализованному имени (``normalize_spot_name``): совпадение целиком
    (в том числе без разделителей: «Cut Contour» = «CutContour») или токен в начале имени,
    за которым идёт не буква: «White 2», «White_ink», «Лак-1», «Varnish Gloss», «UV-Lack».
    «Whiteboard», «Warm Red», «Diesel» не совпадают.
    """
    value = normalize_spot_name(name)
    if not value:
        return False
    compact = value.replace(" ", "")
    for token in ignore:
        key = normalize_spot_name(token)
        if not key:
            continue
        if compact == key.replace(" ", ""):
            return True
        if value.startswith(key) and len(value) > len(key) and not value[len(key)].isalpha():
            return True
    return False


def overprint_block(prepress: object) -> Mapping[str, Any]:
    if not isinstance(prepress, Mapping):
        return {}
    block = prepress.get("overprint")
    return block if isinstance(block, Mapping) else {}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def overprint_status_effect(prepress: object) -> str | None:
    """Влияние проверки overprint на итоговый статус: ``error``, ``warning`` или ``None``.

    ``error`` — проверка выполнена и нашла overprint не только на технических красках;
    ``warning`` — проверка не выполнена (сбой callas): заказ не краснеет, но и не зелёный.
    Справочные находки (только технические краски), пропуск и отсутствие блока — ``None``.
    """
    block = overprint_block(prepress)
    status = block.get("status")
    if status == "ok":
        return STATUS_ERROR if _count(block.get("blocking_count")) else None
    if status == "failed":
        return STATUS_WARNING
    return None


def overprint_failed_message(block: Mapping[str, Any]) -> str:
    return f"Проверка overprint не выполнена: {block.get('error') or 'неизвестная ошибка'}"


def overprint_issue_groups(prepress: object) -> list[dict[str, Any]]:
    """Сгруппированные строки overprint для ``issue_groups`` (одна на тип, не на объект)."""
    block = overprint_block(prepress)
    status = block.get("status")
    groups: list[dict[str, Any]] = []

    def group(code: str, severity: str, message: str, count: int, pages: object, samples: object) -> None:
        listed_pages = [page for page in pages or () if isinstance(page, int)] if isinstance(pages, list) else []
        listed_samples = [str(item) for item in samples or ()][:3] if isinstance(samples, list) else []
        groups.append({
            "code": code, "severity": severity, "source": "callas", "message": message,
            "count": count or 1, "objects": count or None, "pages": listed_pages,
            "files": [], "samples": listed_samples,
        })

    if status == "failed":
        group("prepress:overprint:failed", STATUS_WARNING, overprint_failed_message(block), 1, [], [])
    elif status == "ok":
        blocking = _count(block.get("blocking_count"))
        technical = _count(block.get("technical_count"))
        if blocking and block.get("message"):
            group("prepress:overprint", STATUS_ERROR, str(block["message"]), blocking,
                  block.get("blocking_pages"), block.get("samples"))
        if technical and block.get("technical_message"):
            group("prepress:overprint", "info", str(block["technical_message"]), technical,
                  block.get("technical_pages"), block.get("technical_samples"))
    return groups
