"""Parser for PitStop Server JSON reports."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .models import (
    PitStopCounts,
    PitStopIssue,
    PitStopLocation,
    PitStopReport,
    PitStopSeverity,
)


class PitStopReportError(ValueError):
    """The report is absent, malformed, or has an unsupported shape."""


_ISSUE_SECTIONS = {
    "criticalfailures": PitStopSeverity.CRITICAL_FAILURE,
    "errors": PitStopSeverity.ERROR,
    "warnings": PitStopSeverity.WARNING,
    "noncriticalfailures": PitStopSeverity.NONCRITICAL_FAILURE,
    "informations": PitStopSeverity.INFORMATION,
    "fixes": PitStopSeverity.FIX,
    "signoffs": PitStopSeverity.SIGNOFF,
}


def parse_pitstop_report(path: Path) -> PitStopReport:
    try:
        with path.open(encoding="utf-8-sig") as report_file:
            payload = json.load(report_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PitStopReportError(f"не удалось прочитать JSON-отчёт PitStop: {path}") from exc
    return parse_pitstop_payload(payload)


def parse_pitstop_payload(payload: object) -> PitStopReport:
    root = _mapping(payload, "корень отчёта")
    preflight = _mapping(root.get("preflightReport"), "preflightReport")
    process = _optional_mapping(root.get("processInfo"))
    general = _optional_mapping(root.get("generalDocInfo"))
    properties = _optional_mapping(general.get("documentProperties"))

    counts = PitStopCounts(
        errors=_count(preflight, "errorsNumber"),
        warnings=_count(preflight, "warningsNumber"),
        critical_failures=_count(preflight, "criticalfailuresNumber"),
        noncritical_failures=_count(preflight, "noncriticalfailuresNumber"),
        fixes=_count(preflight, "fixesNumber"),
        informations=_count(preflight, "informationsNumber"),
        signoffs=_count(preflight, "signoffsNumber"),
    )

    issues: list[PitStopIssue] = []
    for section_name, severity in _ISSUE_SECTIONS.items():
        issues.extend(_parse_issue_section(preflight.get(section_name), severity))

    return PitStopReport(
        counts=counts,
        issues=tuple(issues),
        profile_name=_optional_text(process.get("preflightProfile")),
        pages=_optional_positive_int(properties.get("numPages")),
        preflighted_at=_optional_datetime(process.get("preflightDateTime")),
    )


def _parse_issue_section(
    section: object, severity: PitStopSeverity
) -> list[PitStopIssue]:
    if section is None:
        return []
    container = _mapping(section, severity.value)
    raw_items = container.get("preflightReportItem", [])
    if isinstance(raw_items, Mapping):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raise PitStopReportError(
            f"{severity.value}.preflightReportItem должен быть списком"
        )

    parsed: list[PitStopIssue] = []
    for index, raw_item in enumerate(raw_items):
        item = _mapping(raw_item, f"{severity.value}[{index}]")
        locations = item.get("location", [])
        if isinstance(locations, Mapping):
            locations = [locations]
        if not isinstance(locations, list):
            raise PitStopReportError(f"location в {severity.value}[{index}] некорректен")
        parsed.append(
            PitStopIssue(
                severity=severity,
                action_id=_action_id(item.get("actionID")),
                message=_optional_text(item.get("message")) or "PitStop: сообщение отсутствует",
                locations=tuple(
                    _parse_location(location, severity, index)
                    for location in locations
                ),
            )
        )
    return parsed


def _parse_location(
    raw: object, severity: PitStopSeverity, item_index: int
) -> PitStopLocation:
    location = _mapping(raw, f"location в {severity.value}[{item_index}]")
    raw_page = location.get("page")
    if isinstance(raw_page, bool) or not isinstance(raw_page, int) or raw_page < 0:
        raise PitStopReportError("номер страницы PitStop должен быть целым числом >= 0")
    return PitStopLocation(
        page=raw_page + 1,
        min_x=_optional_number(location.get("minX")),
        min_y=_optional_number(location.get("minY")),
        max_x=_optional_number(location.get("maxX")),
        max_y=_optional_number(location.get("maxY")),
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PitStopReportError(f"{label} должен быть JSON-объектом")
    return value


def _optional_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PitStopReportError(f"{key} должен быть целым числом >= 0")
    return value


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PitStopReportError("координата PitStop должна быть числом")
    return float(value)


def _optional_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _action_id(value: object) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, str)):
        return value
    return None
