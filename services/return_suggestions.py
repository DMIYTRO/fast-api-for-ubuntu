"""Подсказки причин возврата на доработку по найденным проблемам заказа.

``suggest_return_reasons`` — чистая функция над готовым DTO заказа: распознаёт сигналы
(низкое разрешение, число страниц, размер, ICC, шрифты…) и сопоставляет их с пунктами
справочника причин возврата (``core/return_reasons.load_return_reasons``) по правилам
``config/return_reason_rules.py``. Результат — ``dto["return_suggestions"]``:
``{"text", "category", "signal", "confidence", "preselect", "evidence"}`` по одной записи
на текст, в порядке правил (важности). ``preselect`` — только для ``high``: такие пункты
интерфейс отмечает сам, поэтому уверенность выставляется осторожно. Другие формулировки
правила (``ReturnReasonRule.alternatives``) идут сразу за основным текстом с тем же сигналом,
уверенностью не выше ``medium`` и без ``preselect``.

Источники — те же, что у ``issue_groups`` (``services/issue_groups.py``): локальные сообщения
(коды ``normalize_local_message``; при ``revision_check`` — сообщения новой ревизии PDF),
находки PitStop итогового PDF, перевод RGB→CMYK (сбой — ``rgb_left``; выполненный перевод или
предупреждение о растре RGB — только предложение ``rgb_converted``), красочность callas
(перелив и светлые участки) и overprint.
Не учитываются: находки PitStop, скрытые сравнением с исходником (``hidden_by_baseline``),
понижённые политикой проверки (``policy_demoted``), справочные (``information``/``fix``…),
незавершённая (технический сбой) проверка PitStop, локальные предупреждения (только ошибки;
исключение — предупреждение о растре RGB для ``rgb_converted``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from config.return_reason_rules import (
    EXTRA_TECHNICAL_SEPARATIONS,
    HIGH,
    MEDIUM,
    RULES,
    SIZE_EXACT_CATALOGUE,
    SIZE_MISMATCH,
    SIZE_REFERENCE_MARKERS,
    SPECIFIC_SIZE_SIGNALS,
)
from config.overprint import DEFAULT_OVERPRINT_IGNORE_SPOTS
from core.return_reasons import load_return_reasons
from services.issue_groups import normalize_local_message, pitstop_severity
from services.overprint import is_technical_spot, overprint_block, overprint_status_effect

logger = logging.getLogger("image_magic.return_suggestions")

_CONFIDENCE_RANK = {HIGH: 0, MEDIUM: 1, "low": 2}

# --------------------------------------------------------------------------- таблицы
# Все сопоставления и регулярные выражения подсказок — здесь.

#: Код локального сообщения (``services/issue_groups._LOCAL_RULES``) → сигнал.
_LOCAL_CODE_SIGNALS: Mapping[str, str] = {
    "low_dpi": "low_dpi",
    "pdf_low_dpi": "low_dpi",
    "two_page_pdf_one_sided": "page_count",
    "order_too_many_pages": "page_count",
    "pdf_too_many_pages": "page_count",
    "multiple_two_page_pdf": "page_count",
    "two_page_pdf_extra_sides": "page_count",
    "multipage_file": "page_count",
    "tiff_pages": "page_count",
    "duplex_pages_required": "page_count",
    "extra_back": "extra_back_one_sided",
    "single_file_required": "extra_back_one_sided",
    "missing_back": "missing_back",
    "missing_face": "missing_face",
    "tiff_layers": "tiff_layers",
    "tiff_alpha": "tiff_alpha",
    "file_unreadable": "file_unreadable",
    "size_mismatch": SIZE_MISMATCH,
    "pdf_page_size": SIZE_MISMATCH,
    "correction_rejected": SIZE_MISMATCH,
}

#: Сообщения, которые ``_LOCAL_RULES`` не различает (общий код ``pdf_error`` или без кода).
_LOCAL_PATTERN_SIGNALS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), signal)
    for pattern, signal in (
        (r"^PDF: размер \d", SIZE_MISMATCH),
        # Проверка новой ревизии PDF (services/pdf_revision_validation.py).
        (r"^PDF содержит \d+ страниц\w*; ожидается", "page_count"),
        (r"^PDF cannot be opened", "file_unreadable"),
        (r"^ни один файл заказа не удалось прочитать", "file_unreadable"),
        # Цветовая модель — только ошибкой (предупреждение «не входит в профиль» снимает
        # перевод RGB→CMYK; растр RGB в PDF — тоже предупреждение).
        # RGB — ``rgb_left`` (см. ``_local_signal``), прочие модели (Lab…) — ``color_model``.
        (r"^цветовая модель", "color_model"),
    )
)

#: «цветовая модель sRGB не входит в профиль …» — модель файла (не список профиля) — RGB.
_RGB_COLOUR_MODEL_RE = re.compile(r"^цветовая модель (?:(?!не входит).)*rgb", re.IGNORECASE)
#: Размер отклонён из-за разрешения: причина — низкое разрешение, а не размер.
_LOW_DPI_AFTER_CORRECTION_RE = re.compile(r"после коррекции эффективное разрешение", re.IGNORECASE)
_SIZE_TEXT_RE = re.compile(
    r"размер (\d+(?:\.\d+)?)x(\d+(?:\.\d+)?) мм[;,] ожидается (\d+(?:\.\d+)?)x(\d+(?:\.\d+)?) мм",
    re.IGNORECASE,
)
_EXPECTED_RE = re.compile(r"ожидается (\d+(?:\.\d+)?)x(\d+(?:\.\d+)?) мм", re.IGNORECASE)
_PDF_DPI_RE = re.compile(r"^PDF страница (\d+): effective DPI (\d+(?:\.\d+)?)", re.IGNORECASE)
_RASTER_DPI_RE = re.compile(r"^разрешение (\d+(?:\.\d+)?)x(\d+(?:\.\d+)?) DPI", re.IGNORECASE)

#: Пункт справочника «Розмір макета з урахуванням доливок повинен бути 94х54мм» (х или x).
_CATALOGUE_SIZE_RE = re.compile(
    r"^Розмір макета з урахуванням доливок повинен бути\s*(\d+)\s*[хxXХ×]\s*(\d+)\s*мм$"
)

#: PitStop: ActionID → сигнал (дополнительные условия — в ``_pitstop_signal``).
_PITSTOP_ACTION_SIGNALS: Mapping[str, str] = {
    "1150": "low_dpi",
    "1148": "low_dpi",
    "1164": SIZE_MISMATCH,
    "3202": "icc",
    "3156": "spot_colors",
    "3157": "spot_colors",
    "3159": "spot_colors",
    "3004": "transparency",
    "1180": "fonts",
    "1185": "fonts",
    "1128": "fonts",
    "1129": "fonts",
    "1131": "fonts",
    "1153": "ink_max",
    "3136": "white_overprint",
    "3140": "thin_lines",
}
_PITSTOP_RGB_RE = re.compile(r"^RGB color", re.IGNORECASE)
_PITSTOP_ICC_RE = re.compile(r"^ICC based is used(?! for transparency)", re.IGNORECASE)
_PITSTOP_SPOT_ALL_RE = re.compile(r"^Spot color All is used", re.IGNORECASE)
_PITSTOP_SEPARATIONS_RE = re.compile(r"separations are:\s*(.+)$", re.IGNORECASE)
_PITSTOP_WHITE_RE = re.compile(r"\bWhite\b", re.IGNORECASE)
#: «Image resolution is 114 x 128...59 x 63 ppi» — диапазон: берётся минимум всех чисел.
_PITSTOP_PPI_RE = re.compile(r"resolution is ([\d.\sx]+?)\s*ppi", re.IGNORECASE)
_PPI_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_PITSTOP_TRAILING_COUNT_RE = re.compile(r"\s*\(\d+x on pages? [^()]*\)\s*$", re.IGNORECASE)

#: Порог «Максимальна барвистість - 300%»: флаг «Перелив» ниже него — не этот пункт.
_INK_MAX_PERCENT = 300.0
#: «Мінімальна барвистість - 20%» — только флаг ``light_areas``: большая часть страницы
#: (``IMAGE_MAGIC_INK_LIGHT_MIN_AREA_PERCENT``) в светлой, но не нулевой заливке
#: (services/callas/ink_coverage.py). Старый ``underflow`` (среднее покрытие страницы ниже
#: 20 %) срабатывал на белых макетах и макетах с небольшой заливкой — не сигнал.
_INK_MIN_KINDS = frozenset({"light_areas"})
#: Предупреждение «PDF страница 1: растровое изображение имеет RGB colorspace».
_RGB_RASTER_WARNING_RE = re.compile(r"растровое изображение имеет RGB colorspace", re.IGNORECASE)
_TECHNICAL_SEPARATIONS = (*DEFAULT_OVERPRINT_IGNORE_SPOTS, *EXTRA_TECHNICAL_SEPARATIONS)
_EVIDENCE_LIMIT = 120

_missing_logged: set[str] = set()


# --------------------------------------------------------------------------- сбор сигналов


class _Hits:
    """Сигнал → (уверенность, пояснение): остаётся самое уверенное срабатывание, из равных — первое.

    Уверенность ``None`` — уверенность правила (лучшая из возможных для сигнала); детектор
    передаёт ``medium`` / ``low``, когда находка допускает другое толкование.
    """

    def __init__(self) -> None:
        self.items: dict[str, tuple[str | None, str]] = {}

    @staticmethod
    def _rank(confidence: str | None) -> int:
        return -1 if confidence is None else _CONFIDENCE_RANK[confidence]

    def add(self, signal: str, evidence: str, confidence: str | None = None) -> None:
        current = self.items.get(signal)
        if current is None or self._rank(confidence) < self._rank(current[0]):
            self.items[signal] = (confidence, evidence)


def _short(text: str, limit: int = _EVIDENCE_LIMIT) -> str:
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _number(value: str) -> str:
    return f"{round(float(value))}"


def _file_prefix(file: str | None) -> str:
    return f"{file}: " if file else ""


def _local_evidence(message: str, file: str | None) -> str:
    match = _PDF_DPI_RE.search(message)
    if match:
        return _short(f"{_file_prefix(file)}Стр. {match.group(1)}: effective DPI {_number(match.group(2))}")
    match = _RASTER_DPI_RE.search(message)
    if match:
        return _short(f"{_file_prefix(file)}{_number(match.group(1))}x{_number(match.group(2))} DPI")
    match = _SIZE_TEXT_RE.search(message)
    if match:
        actual = f"{float(match.group(1)):g}x{float(match.group(2)):g}"
        expected = f"{float(match.group(3)):g}x{float(match.group(4)):g}"
        return _short(f"{_file_prefix(file)}размер {actual} мм, ожидается {expected} мм")
    return _short(_file_prefix(file) + message.split(";", 1)[0].strip())


def _message_text(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("message") or value.get("code")
    return " ".join(str(value).split()) if value is not None else ""


def _file_name(item: Mapping[str, Any]) -> str | None:
    name = str(item.get("name") or item.get("filename") or "")
    if not name and item.get("path"):
        name = str(item["path"]).replace("\\", "/").rsplit("/", 1)[-1]
    return name or None


def _size_pair(values: object) -> tuple[float, float] | None:
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        return None
    try:
        width, height = float(values[0]), float(values[1])
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _local_errors(dto: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any] | None]]:
    """Локальные ошибки ``(сообщение, файл|None)`` — тот же выбор, что у ``issue_groups``."""
    files = [item for item in dto.get("files") or () if isinstance(item, Mapping)]
    revision = dto.get("revision_check")
    result: list[tuple[str, Mapping[str, Any] | None]] = []
    if isinstance(revision, Mapping):
        errors = [_message_text(item) for item in revision.get("errors") or ()]
        result.extend((message, None) for message in errors)
        result.extend(
            (text, None)
            for text in (_message_text(item) for item in dto.get("processing_errors") or ())
            if text not in errors
        )
    else:
        for key in ("errors", "processing_errors"):
            result.extend((_message_text(item), None) for item in dto.get(key) or ())
        for item in files:
            result.extend((_message_text(message), item) for message in item.get("errors") or ())
    return [(message, item) for message, item in result if message]


def _local_signal(message: str, dto: Mapping[str, Any]) -> str | None:
    code, _label = normalize_local_message(message)
    signal = _LOCAL_CODE_SIGNALS.get(code)
    if signal is None:
        signal = next(
            (value for pattern, value in _LOCAL_PATTERN_SIGNALS if pattern.search(message)), None
        )
    if signal == SIZE_MISMATCH and _LOW_DPI_AFTER_CORRECTION_RE.search(message):
        return "low_dpi"
    if signal == "color_model" and _RGB_COLOUR_MODEL_RE.search(message):
        # «не входит в профиль» снимает перевод RGB→CMYK, но RGB был — только предложить.
        return "rgb_converted" if _color_converted(dto) else "rgb_left"
    return signal


def _color_converted(dto: Mapping[str, Any]) -> bool:
    record = dto.get("color_conversion")
    return isinstance(record, Mapping) and record.get("status") == "converted"


def _rgb_raster_warning(dto: Mapping[str, Any]) -> tuple[str, Mapping[str, Any] | None] | None:
    """Первое предупреждение о растре RGB ``(сообщение, файл|None)`` — тот же выбор, что у ошибок."""
    revision = dto.get("revision_check")
    sources: list[tuple[object, Mapping[str, Any] | None]] = []
    if isinstance(revision, Mapping):
        sources.extend((message, None) for message in revision.get("warnings") or ())
    else:
        sources.extend((message, None) for message in dto.get("warnings") or ())
        for item in dto.get("files") or ():
            if isinstance(item, Mapping):
                sources.extend((message, item) for message in item.get("warnings") or ())
    for message, item in sources:
        text = _message_text(message)
        if _RGB_RASTER_WARNING_RE.search(text):
            return text, item
    return None


def _pitstop_signal(action_id: str, message: str) -> tuple[str, str | None] | None:
    """``(сигнал, уверенность|None)`` для находки PitStop; ``None`` — не подсказка."""
    if action_id == "2078":
        if _PITSTOP_RGB_RE.search(message):
            return "rgb_left", None
        if _PITSTOP_ICC_RE.search(message):
            return "icc", None
        return None  # ICC прозрачной группы, Lab и прочее
    signal = _PITSTOP_ACTION_SIGNALS.get(action_id)
    if signal is None:
        return None
    if action_id == "3159":
        # «Those 1 separations are: White» — техническая краска (белила, лак): не плашка клиента.
        match = _PITSTOP_SEPARATIONS_RE.search(message)
        if match:
            names = [name.strip() for name in match.group(1).split(",") if name.strip()]
            if names and all(is_technical_spot(name, _TECHNICAL_SEPARATIONS) for name in names):
                return None
        return signal, None
    if action_id == "3156":
        # «Spot color All» — однозначно; «…matching regular expression…» может быть и белилами.
        return signal, None if _PITSTOP_SPOT_ALL_RE.search(message) else MEDIUM
    if action_id == "3157":
        return signal, MEDIUM
    if action_id == "3136":
        # «White line art does not knock out» — про белый; «Object does not knock out» — не обязательно.
        return signal, None if _PITSTOP_WHITE_RE.search(message) else MEDIUM
    return signal, None


def _pitstop_evidence(action_id: str, message: str) -> str:
    match = _PITSTOP_PPI_RE.search(message)
    numbers = [float(value) for value in _PPI_NUMBER_RE.findall(match.group(1))] if match else []
    if numbers and action_id in {"1150", "1148"}:
        return f"PitStop {action_id}: {min(numbers):g} ppi"
    text = _PITSTOP_TRAILING_COUNT_RE.sub("", message)
    return _short(f"PitStop {action_id}: {text}" if action_id else f"PitStop: {text}")


def _pitstop_issues(dto: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    pitstop = dto.get("pitstop")
    if not isinstance(pitstop, Mapping) or pitstop.get("execution_status") != "completed":
        return ()  # нет проверки или технический сбой PitStop
    return [issue for issue in pitstop.get("issues") or () if isinstance(issue, Mapping)]


def _collect(dto: Mapping[str, Any]) -> tuple[_Hits, list[tuple[float, float]], bool]:
    """Сигналы, ожидаемые размеры файлов с проблемой размера и признак проблемы размера."""
    hits = _Hits()
    files = [item for item in dto.get("files") or () if isinstance(item, Mapping)]
    expected: list[tuple[float, float]] = []
    size_problem = False
    order_level_size = False

    for message, item in _local_errors(dto):
        signal = _local_signal(message, dto)
        if signal is None:
            continue
        hits.add(signal, _local_evidence(message, _file_name(item) if item else None))
        if signal != SIZE_MISMATCH:
            continue
        size_problem = True
        pair = _size_pair(item.get("expected_mm")) if item is not None else None
        if pair is None:
            match = _EXPECTED_RE.search(message)
            pair = (float(match.group(1)), float(match.group(2))) if match else None
        if pair is not None:
            expected.append(pair)
        elif item is None:
            order_level_size = True

    for issue in _pitstop_issues(dto):
        if issue.get("hidden_by_baseline") is True or issue.get("policy_demoted") is True:
            continue
        if pitstop_severity(issue.get("severity")) not in {"error", "warning"}:
            continue
        action_id = str(issue.get("action_id") or "").strip()
        message = _message_text(issue.get("message"))
        found = _pitstop_signal(action_id, message)
        if found is None:
            continue
        signal, confidence = found
        hits.add(signal, _pitstop_evidence(action_id, message), confidence)
        if signal == SIZE_MISMATCH:
            size_problem = order_level_size = True

    record = dto.get("color_conversion")
    if isinstance(record, Mapping) and record.get("status") == "failed" and record.get("reason", "rgb") == "rgb":
        error = _message_text(record.get("error"))
        hits.add("rgb_left", _short("RGB не переведён в CMYK" + (f": {error}" if error else "")))
    elif isinstance(record, Mapping) and record.get("status") == "converted" and record.get("reason", "rgb") == "rgb":
        pages = record.get("pages")
        where = (
            " (стр. " + ", ".join(str(page) for page in pages) + ")"
            if isinstance(pages, list) and pages else ""
        )
        hits.add("rgb_converted", f"RGB переведён в CMYK{where}")
    warning = _rgb_raster_warning(dto)
    if warning is not None:
        message, item = warning
        hits.add("rgb_converted", _local_evidence(message, _file_name(item) if item else None))

    ink = dto.get("ink_coverage")
    if isinstance(ink, Mapping):
        for flag in ink.get("flags") or ():
            if not isinstance(flag, Mapping) or flag.get("kind") not in {"overflow", *_INK_MIN_KINDS}:
                continue
            try:
                percent = float(flag.get("percent"))
            except (TypeError, ValueError):
                continue
            kind = flag.get("kind")
            page = flag.get("page")
            on_page = f" на стр. {page}" if page else ""
            if kind == "overflow":
                if percent > _INK_MAX_PERCENT:
                    hits.add("ink_max", f"Красочность {percent:g}%{on_page}")
            elif percent > 0:  # light_areas
                threshold = _flag_threshold(flag)
                lighter = f" светлее {threshold:g}%" if threshold is not None else ""
                hits.add("ink_min", f"Светлые участки{lighter}: {percent:g}% площади{on_page}")

    prepress = dto.get("prepress")
    if overprint_status_effect(prepress) == "error":
        block = overprint_block(prepress)
        by_kind = block.get("by_kind") if isinstance(block.get("by_kind"), Mapping) else {}
        white = by_kind.get("white") if isinstance(by_kind, Mapping) else None
        is_white = isinstance(white, int) and not isinstance(white, bool) and white > 0
        hits.add(
            "white_overprint",
            _short(str(block.get("message") or "Overprint на объектах, кроме технических красок")),
            None if is_white else MEDIUM,
        )

    if order_level_size or (size_problem and not expected):
        expected.extend(
            pair for pair in (_size_pair(item.get("expected_mm")) for item in files) if pair
        )
    return hits, expected, size_problem


def _flag_threshold(flag: Mapping[str, Any]) -> float | None:
    try:
        return float(flag.get("threshold"))
    except (TypeError, ValueError):
        return None


def _catalogue_size_items(reasons: Sequence[Mapping[str, Any]]) -> dict[tuple[int, int], Mapping[str, Any]]:
    result: dict[tuple[int, int], Mapping[str, Any]] = {}
    for reason in reasons:
        match = _CATALOGUE_SIZE_RE.match(str(reason.get("text") or "").strip())
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            result.setdefault((width, height), reason)
            result.setdefault((height, width), reason)
    return result


def _marker_signals(dto: Mapping[str, Any]) -> list[str]:
    names = [
        name.casefold()
        for name in (_file_name(item) for item in dto.get("files") or () if isinstance(item, Mapping))
        if name
    ]
    # Самая длинная метка выигрывает: «(SRA3P)» не должна давать ещё и «(SRA3)».
    for signal, marker in sorted(SIZE_REFERENCE_MARKERS.items(), key=lambda entry: -len(entry[1])):
        if any(marker.casefold() in name for name in names):
            return [signal]
    return []


# --------------------------------------------------------------------------- API


def suggest_return_reasons(
    order_dto: Mapping[str, Any], reasons: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Подсказки причин возврата для DTO заказа (см. описание модуля)."""
    if not isinstance(order_dto, Mapping):
        return []
    hits, expected, size_problem = _collect(order_dto)
    exact_size: Mapping[str, Any] | None = None
    if size_problem:
        size_evidence = hits.items.get(SIZE_MISMATCH, (None, "Размер не соответствует"))[1]
        for signal in _marker_signals(order_dto):
            hits.add(signal, size_evidence)
        catalogue = _catalogue_size_items(reasons)
        for width, height in expected:
            exact_size = catalogue.get((round(width), round(height)))
            if exact_size is not None:
                hits.add(SIZE_EXACT_CATALOGUE, size_evidence)
                break
        if any(signal in hits.items for signal in SPECIFIC_SIZE_SIGNALS):
            hits.items.pop(SIZE_MISMATCH, None)
    if not hits.items:
        return []

    by_text = {str(reason.get("text") or ""): reason for reason in reasons}

    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def emit(reason: Mapping[str, Any], signal: str, confidence: str, evidence: str) -> bool:
        text = str(reason.get("text") or "")
        if not text or text in seen:
            return False
        seen.add(text)
        result.append({
            "text": text,
            "category": str(reason.get("category") or ""),
            "signal": signal,
            "confidence": confidence,
            "preselect": confidence == HIGH,
            "evidence": evidence,
        })
        return True

    for rule in RULES:
        hit = hits.items.get(rule.signal)
        if hit is None:
            continue
        reason = exact_size if rule.dynamic else _catalogue_item(by_text, rule.signal, rule.text)
        if reason is None:
            continue
        confidence = rule.confidence
        override, evidence = hit
        if override is not None and _CONFIDENCE_RANK[override] > _CONFIDENCE_RANK[confidence]:
            confidence = override  # детектор может только понизить уверенность правила
        if not emit(reason, rule.signal, confidence, evidence):
            continue  # тот же текст уже подсказан более важным правилом — и его варианты тоже
        # Другие формулировки — только предложить: оператор выбирает одну из них.
        alternative_confidence = confidence if _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[HIGH] else MEDIUM
        for alternative in rule.alternatives:
            item = _catalogue_item(by_text, rule.signal, alternative)
            if item is not None:
                emit(item, rule.signal, alternative_confidence, evidence)
    return result


def _catalogue_item(by_text: Mapping[str, Mapping[str, Any]], signal: str, text: str) -> Mapping[str, Any] | None:
    reason = by_text.get(text)
    if reason is None and text not in _missing_logged:
        _missing_logged.add(text)
        logger.debug("return_suggestions.reason_missing signal=%s text=%s", signal, text)
    return reason


def attach_return_suggestions(
    dto: dict[str, Any], reasons: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    """Положить подсказки в ``dto["return_suggestions"]`` (справочник — из кэша, без разбора)."""
    try:
        catalogue = load_return_reasons() if reasons is None else reasons
        dto["return_suggestions"] = suggest_return_reasons(dto, catalogue)
    except Exception:  # noqa: BLE001 — подсказки не должны ломать DTO заказа
        logger.warning("return_suggestions.failed order_id=%s", dto.get("order_id"), exc_info=True)
        dto["return_suggestions"] = []
    return dto
