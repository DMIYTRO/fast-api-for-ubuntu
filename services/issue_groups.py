"""Группировка однотипных проблем заказа в одну строку.

Сырые списки ``errors`` / ``warnings`` / ``processing_errors`` и
``pitstop.issues`` остаются в DTO без изменений; этот модуль строит поверх них
дополнительный ключ ``issue_groups``: по одной записи на тип проблемы
(``source``, ``severity``, ``code``) с количеством повторов, затронутыми
страницами, файлами и несколькими примерами исходных сообщений.

PitStop Server уже агрегирует отчёт по правилу: один ``preflightReportItem`` на
``actionID`` с текстом вида «RGB color is used (2x on pages 1-2)» и списком
``location`` по одному на объект. Поэтому для PitStop число объектов считается
по уникальным ``(page, bbox)``, а ``count`` — по числу записей отчёта.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

SOURCE_LOCAL = "local"
SOURCE_PITSTOP = "pitstop"

_SAMPLE_LIMIT = 3

# Таблица известных шаблонов локальных сообщений: (code, regex, подпись группы).
# Подпись используется, когда в группу попало несколько сообщений с разными
# числами; для единичного сообщения показывается оригинальный текст.
_LOCAL_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (code, re.compile(pattern, re.IGNORECASE), label)
    for code, pattern, label in (
        ("size_corrected", r"^размер автоматически скорректирован", "Размер автоматически скорректирован"),
        ("size_confirmation_required", r"^требуется подтверждение коррекции", "Требуется подтверждение коррекции размера"),
        ("size_mismatch", r"^размер \d", "Размер не соответствует ожидаемому"),
        ("file_too_large", r"^размер файла .*превышает лимит", "Размер файла превышает лимит"),
        ("correction_rejected", r"отказался от предложенной коррекции", "Коррекция размера отклонена пользователем"),
        ("correction_confirmed", r"^коррекция размера подтверждена", "Коррекция размера подтверждена пользователем"),
        ("low_dpi", r"^разрешение .*DPI", "Низкое разрешение"),
        ("colorspace_not_allowed", r"^цветовая модель", "Цветовая модель не входит в профиль"),
        ("colors_not_supported", r"^цветность .*не поддерживается", "Цветность не поддерживается"),
        ("unsupported_format", r"^формат .*исключён из обработки", "Формат файла исключён из обработки"),
        ("file_unreadable", r"^не удалось прочитать файл", "Не удалось прочитать файл"),
        ("multipage_file", r"^файл содержит \d+ страниц/изображений", "Многостраничный файл; разрешён только одностраничный"),
        ("tiff_alpha", r"^TIFF содержит альфа-канал", "TIFF содержит альфа-канал"),
        ("tiff_layers", r"несведённ(ых|ые|ый) сло", "TIFF содержит несведённые слои"),
        ("tiff_pages", r"^TIFF содержит \d+ страниц", "Многостраничный TIFF; разрешён только одностраничный"),
        ("orientation_mismatch", r"^ориентация сторон не совпадает", "Ориентация сторон не совпадает"),
        ("auto_rotate", r"будет автоматически повёрнут", "Сторона будет автоматически повёрнута"),
        ("pdf_page_size", r"^PDF страница \d+ имеет размер", "Размер страницы PDF не соответствует ожидаемому"),
        ("pdf_page_rotation", r"^PDF страница \d+ имеет недопустимый поворот", "Недопустимый поворот страницы PDF"),
        ("pdf_unknown_colorspace", r"^PDF страница \d+: неизвестная цветовая модель", "Неизвестная цветовая модель на странице PDF"),
        ("pdf_rgb_raster", r"^PDF страница \d+: растровое изображение", "Растровое изображение в PDF имеет RGB colorspace"),
        ("pdf_low_dpi", r"^PDF страница \d+: effective DPI", "Низкое эффективное разрешение в PDF"),
        ("pdf_too_many_pages", r"^PDF contains \d+ pages", "В PDF слишком много страниц"),
        ("pdf_no_pages", r"^PDF contains no pages", "PDF не содержит страниц"),
        ("pdf_encrypted", r"^Encrypted PDF", "Зашифрованный PDF не поддерживается"),
        ("pdf_repaired", r"^PDF required structural repair", "PDF потребовал структурного восстановления"),
        ("pdf_page_uninspectable", r"^Page \d+ cannot be inspected", "Страницу PDF не удалось проверить"),
        ("pdf_error", r"^PDF: ", "Ошибка обработки PDF"),
        ("duplex_pages_required", r"^для цветности с оборотом требуется", "Неверное число страниц для двусторонней печати"),
        ("two_page_pdf_one_sided", r"^двухстраничный PDF недопустим", "Двухстраничный PDF недопустим для односторонней печати"),
        ("two_page_pdf_extra_sides", r"^двухстраничный PDF является полным заказом", "Лишние стороны при двухстраничном PDF"),
        ("multiple_two_page_pdf", r"^найдено несколько двухстраничных PDF", "Несколько двухстраничных PDF в заказе"),
        ("missing_face", r"^не найден обязательный файл face", "Не найден файл face"),
        ("missing_back", r"^для двусторонней печати не найден файл back", "Не найден файл back"),
        ("extra_back", r"^для односторонней печати найден лишний файл back", "Лишний файл back"),
        ("single_file_required", r"^для односторонней печати разрешён только один", "Для односторонней печати разрешён один файл"),
        ("multiple_face", r"^найдено несколько файлов face", "Несколько файлов face"),
        ("multiple_back", r"^найдено несколько файлов back", "Несколько файлов back"),
        ("customer_mismatch", r"не совпадает номер клиента", "Номер клиента в именах файлов не совпадает"),
        ("name_size_mismatch", r"не совпадает размер в имени", "Размер в именах файлов не совпадает"),
        ("name_colors_mismatch", r"не совпадает цветность в имени", "Цветность в именах файлов не совпадает"),
        ("mixed_formats", r"^стороны заказа имеют разные форматы", "Стороны заказа имеют разные форматы"),
        ("preview_error", r"^(Превью|Производственное превью)", "Ошибка создания превью"),
        ("processing_timeout", r"^Превышено время обработки", "Превышено время обработки заказа"),
        ("pdf_build_missing", r"^BatchProcessor не вернул", "Обработчик не вернул результат"),
    )
)

# Известные проверки PitStop: ActionID -> (подпись группы, план исправления Callas).
# «Clipping objects found» (ActionID 2079) создаёт Enfocus PitStop, не callas; объекты
# сгруппированы в одну строку с числом и страницами, а исправление предлагает план
# «remove_clipping» (профиль callas «Remove all clipping paths»).
_PITSTOP_ACTIONS: dict[str, tuple[str, str]] = {
    "2079": ("Обтравка: Clipping objects found (PitStop)", "remove_clipping"),
}

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_SPACES_RE = re.compile(r"\s+")


def normalize_local_message(message: str) -> tuple[str, str]:
    """Вернуть ``(code, label)`` для локального сообщения.

    Известные шаблоны получают стабильный код и русскую подпись; для остальных
    код — это текст со схлопнутыми числами, а подпись — исходное сообщение.
    """
    text = _SPACES_RE.sub(" ", str(message or "")).strip()
    for code, pattern, label in _LOCAL_RULES:
        if pattern.search(text):
            return code, label
    collapsed = _NUMBER_RE.sub("#", text)
    collapsed = _SPACES_RE.sub(" ", collapsed).strip()
    return collapsed or "empty", text


_PITSTOP_SEVERITY = {
    "critical_failure": "error",
    "error": "error",
    "noncritical_failure": "warning",
    "warning": "warning",
}


def pitstop_severity(value: object) -> str:
    return _PITSTOP_SEVERITY.get(str(value or "").lower(), "info")


class _Group:
    __slots__ = (
        "code", "severity", "source", "label", "count", "objects",
        "pages", "files", "samples", "has_locations", "unknown_objects", "fix_plan",
    )

    def __init__(self, code: str, severity: str, source: str, label: str) -> None:
        self.code = code
        self.severity = severity
        self.source = source
        self.label = label
        self.count = 0
        self.objects: set[tuple[Any, ...]] = set()
        self.pages: set[int] = set()
        self.files: list[str] = []
        self.samples: list[str] = []
        self.has_locations = False
        self.unknown_objects = False
        self.fix_plan: str | None = None

    def add(
        self,
        message: str,
        *,
        file: str | None = None,
        locations: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self.count += 1
        if message and len(self.samples) < _SAMPLE_LIMIT and message not in self.samples:
            self.samples.append(message)
        if file and file not in self.files:
            self.files.append(file)
        locations = list(locations)
        if not locations:
            self.unknown_objects = True
        for location in locations:
            page = location.get("page")
            if isinstance(page, bool) or not isinstance(page, int):
                self.unknown_objects = True
                continue
            self.has_locations = True
            self.pages.add(page)
            bbox = location.get("bbox") or {}
            if not isinstance(bbox, Mapping) or any(bbox.get(key) is None for key in ("min_x", "min_y", "max_x", "max_y")):
                self.unknown_objects = True
                continue
            self.objects.add((
                page,
                bbox.get("min_x"), bbox.get("min_y"),
                bbox.get("max_x"), bbox.get("max_y"),
            ))

    def to_dict(self) -> dict[str, Any]:
        objects = len(self.objects) if self.has_locations and not self.unknown_objects else None
        pages = sorted(self.pages)
        base = self.samples[0] if self.count == 1 and self.samples and not self.fix_plan else self.label
        result = {
            "code": self.code,
            "severity": self.severity,
            "source": self.source,
            "message": format_group_message(
                base, count=self.count, objects=objects, pages=pages, files=self.files
            ),
            "count": self.count,
            "objects": objects,
            "pages": pages,
            "files": list(self.files),
            "samples": list(self.samples),
        }
        if self.fix_plan:
            result["fix_plan"] = self.fix_plan
        return result


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    rem10, rem100 = count % 10, count % 100
    if rem10 == 1 and rem100 != 11:
        return one
    if 2 <= rem10 <= 4 and not 12 <= rem100 <= 14:
        return few
    return many


def format_group_message(
    base: str,
    *,
    count: int,
    objects: int | None,
    pages: list[int],
    files: list[str],
) -> str:
    """``<base> — N объектов на страницах 1, 2`` и похожие сводки."""
    parts: list[str] = []
    if objects is not None:
        parts.append(f"{objects} {plural_ru(objects, 'объект', 'объекта', 'объектов')}")
    elif count > 1:
        parts.append(f"{count} {plural_ru(count, 'повтор', 'повтора', 'повторов')}")
    if pages:
        parts.append(
            f"на {plural_ru(len(pages), 'странице', 'страницах', 'страницах')} "
            + ", ".join(str(page) for page in pages)
        )
    if len(files) > 1:
        parts.append(f"в {len(files)} {plural_ru(len(files), 'файле', 'файлах', 'файлах')}")
    elif len(files) == 1 and count > 1:
        parts.append(f"в файле {files[0]}")
    if not parts:
        return base
    return f"{base} — {' '.join(parts)}"


def build_issue_groups(
    *,
    errors: Iterable[str] = (),
    warnings: Iterable[str] = (),
    processing_errors: Iterable[str] = (),
    files: Iterable[Mapping[str, Any]] = (),
    pitstop_issues: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], _Group] = {}

    def local(severity: str, message: object, file: str | None = None) -> None:
        text = _message_text(message)
        if not text:
            return
        code, label = normalize_local_message(text)
        key = (SOURCE_LOCAL, severity, code)
        group = groups.get(key)
        if group is None:
            group = groups[key] = _Group(code, severity, SOURCE_LOCAL, label)
        group.add(text, file=file)

    for message in errors:
        local("error", message)
    for message in processing_errors:
        local("error", message)
    for message in warnings:
        local("warning", message)
    for item in files:
        name = str(item.get("name") or item.get("filename") or "") or None
        if not name and item.get("path"):
            name = str(item["path"]).rsplit("/", 1)[-1]
        for message in item.get("errors") or ():
            local("error", message, name)
        for message in item.get("warnings") or ():
            local("warning", message, name)

    for issue in pitstop_issues:
        if not isinstance(issue, Mapping):
            continue
        message = _message_text(issue.get("message")) or "PitStop: сообщение отсутствует"
        severity = pitstop_severity(issue.get("severity"))
        action_id = issue.get("action_id")
        if action_id not in (None, ""):
            code = f"action:{action_id}"
        else:
            code = _NUMBER_RE.sub("#", message)
        key = (SOURCE_PITSTOP, severity, code)
        group = groups.get(key)
        if group is None:
            known = _PITSTOP_ACTIONS.get(str(action_id)) if action_id not in (None, "") else None
            group = groups[key] = _Group(code, severity, SOURCE_PITSTOP, known[0] if known else message)
            if known:
                group.fix_plan = known[1]
        locations = issue.get("locations") or ()
        group.add(
            message,
            locations=[loc for loc in locations if isinstance(loc, Mapping)],
        )

    order = {"error": 0, "warning": 1, "info": 2}
    return [
        group.to_dict()
        for group in sorted(
            groups.values(),
            key=lambda item: (order.get(item.severity, 3), item.source != SOURCE_LOCAL),
        )
    ]


def issue_groups_for_order_dto(dto: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Собрать ``issue_groups`` из уже готового DTO заказа."""
    pitstop = dto.get("pitstop") or {}
    revision = dto.get("revision_check")
    if isinstance(revision, Mapping):
        errors = list(revision.get("errors") or ())
        return build_issue_groups(
            errors=errors, warnings=revision.get("warnings") or (),
            processing_errors=[message for message in dto.get("processing_errors") or () if message not in errors],
            pitstop_issues=pitstop.get("issues") or () if isinstance(pitstop, Mapping) else (),
        )
    return build_issue_groups(
        errors=dto.get("errors") or (),
        warnings=dto.get("warnings") or (),
        processing_errors=dto.get("processing_errors") or (),
        files=[item for item in dto.get("files") or () if isinstance(item, Mapping)],
        pitstop_issues=pitstop.get("issues") or () if isinstance(pitstop, Mapping) else (),
    )


def attach_issue_groups(dto: dict[str, Any]) -> dict[str, Any]:
    dto["issue_groups"] = issue_groups_for_order_dto(dto)
    return dto


def _message_text(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("message") or value.get("code")
    return str(value).strip() if value is not None else ""
