"""Разбор XML-отчёта pdfToolbox (задание ``process``) в список находок.

Структура отчёта (docs/callas-preflight-plan.md, раздел 1): блок ``profile_info``
описывает правила профиля (``display_name``), а в результатах идут ``<hits rule_id
severity>`` с вложенными ``<hit page llx lly urx ury>`` (координаты объекта в pt).
``rule_id`` вида ``RUL71`` — порядковый номер внутри одного отчёта, поэтому
устойчивый ключ находки — название правила (``rule``), а не ``rule_id``.

Разбор терпим к пространствам имён и порядку элементов. Защита от XXE и «взрыва
сущностей»: документы с DOCTYPE/ENTITY отклоняются до парсинга (pdfToolbox их не
пишет), внешние сущности стандартный парсер и так не раскрывает; размер ограничен.
Внешних зависимостей нет.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from services.callas.errors import CallasReportError

_MAX_REPORT_BYTES = 64 * 1024 * 1024
_FORBIDDEN_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_SEVERITY_ALIASES = {
    "information": "info",
    "informations": "info",
    "informational": "info",
    "infos": "info",
    "warnings": "warning",
    "errors": "error",
    "fixed": "fixup",
    "fixups": "fixup",
}


@dataclass(frozen=True, slots=True)
class CallasFinding:
    """Одна находка: правило (по названию), серьёзность, страница и рамка в pt."""

    rule: str
    severity: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    rule_id: str | None = None


def parse_report_xml(path: Path | str) -> list[CallasFinding]:
    """Читает XML-отчёт с диска и возвращает находки в порядке отчёта."""
    report_path = Path(path)
    try:
        size = report_path.stat().st_size
        if size > _MAX_REPORT_BYTES:
            raise CallasReportError("XML-отчёт callas слишком большой для разбора.")
        data = report_path.read_bytes()
    except OSError as exc:
        raise CallasReportError(
            f"Не удалось прочитать XML-отчёт callas {report_path.name}: {exc.strerror or exc}"
        ) from exc
    return parse_report_bytes(data)


def parse_report_root(data: bytes) -> ET.Element:
    """Безопасно разбирает XML-отчёт и возвращает корневой элемент."""
    if len(data) > _MAX_REPORT_BYTES:
        raise CallasReportError("XML-отчёт callas слишком большой для разбора.")
    if _FORBIDDEN_RE.search(data) or _utf16_forbidden(data):
        raise CallasReportError(
            "XML-отчёт callas отклонён: объявления DOCTYPE/ENTITY не допускаются."
        )
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise CallasReportError(f"XML-отчёт callas повреждён: {exc}") from exc


def read_report_root(path: Path | str) -> ET.Element:
    """Читает XML-отчёт с диска (с ограничением размера) и возвращает корень."""
    report_path = Path(path)
    try:
        if report_path.stat().st_size > _MAX_REPORT_BYTES:
            raise CallasReportError("XML-отчёт callas слишком большой для разбора.")
        return parse_report_root(report_path.read_bytes())
    except OSError as exc:
        raise CallasReportError(
            f"Не удалось прочитать XML-отчёт callas {report_path.name}: {exc.strerror or exc}"
        ) from exc


def parse_report_bytes(data: bytes) -> list[CallasFinding]:
    root = parse_report_root(data)

    names = _rule_names(root)
    findings: list[CallasFinding] = []
    for hits in _iter(root, "hits"):
        rule_id = _text(_attr(hits, "rule_id", "id"))
        rule = (
            names.get(rule_id or "")
            or _text(_attr(hits, "display_name", "name", "rule"))
            or rule_id
            or "Без названия"
        )
        severity = _severity(_attr(hits, "severity"))
        items = list(_iter(hits, "hit", skip_self=True))
        if not items:
            findings.append(CallasFinding(rule, severity, None, None, rule_id))
            continue
        for item in items:
            findings.append(
                CallasFinding(
                    rule=rule,
                    severity=severity,
                    page=_page(_attr(item, "page")),
                    bbox=_bbox(item),
                    rule_id=rule_id,
                )
            )
    return findings


# ------------------------------------------------------------------ helpers


def _utf16_forbidden(data: bytes) -> bool:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = data.decode("utf-16", errors="ignore")
        except UnicodeError:
            return True
        return bool(re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.IGNORECASE))
    return False


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _iter(root: ET.Element, name: str, *, skip_self: bool = False) -> Iterator[ET.Element]:
    for element in root.iter():
        if skip_self and element is root:
            continue
        if _local(element.tag) == name:
            yield element


def _attr(element: ET.Element, *names: str) -> str | None:
    lowered = {key.rsplit("}", 1)[-1].lower(): value for key, value in element.attrib.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


def _rule_names(root: ET.Element) -> dict[str, str]:
    """rule_id -> display_name из блока profile_info (атрибутами или дочерними элементами)."""
    names: dict[str, str] = {}
    for info in _iter(root, "profile_info"):
        for element in info.iter():
            rule_id = _text(_attr(element, "rule_id", "id"))
            if not rule_id:
                continue
            display = _text(_attr(element, "display_name"))
            if not display:
                for child in element:
                    if _local(child.tag) == "display_name":
                        display = _text(child.text)
                        break
            if display:
                names.setdefault(rule_id, display)
    return names


def _severity(value: str | None) -> str:
    text = (_text(value) or "unknown").lower()
    return _SEVERITY_ALIASES.get(text, text)


def _page(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        page = int(float(value))
    except ValueError:
        return None
    return page if page >= 1 else None


def _bbox(item: ET.Element) -> tuple[float, float, float, float] | None:
    try:
        values = [_attr(item, key) for key in ("llx", "lly", "urx", "ury")]
        if any(value is None for value in values):
            return None
        llx, lly, urx, ury = (float(value) for value in values)  # type: ignore[arg-type]
    except ValueError:
        return None
    if any(number != number or number in (float("inf"), float("-inf")) for number in (llx, lly, urx, ury)):
        return None
    return (llx, lly, urx, ury)
