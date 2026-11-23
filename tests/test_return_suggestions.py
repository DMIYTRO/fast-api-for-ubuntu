"""Подсказки причин возврата: тексты справочника, сигналы, исключения и DTO API.

Без сети и без production: настоящий справочник «Цифра - Загальне.html», DTO PitStop —
тем же кодом, что в проде (``_pitstop_result_to_dto``), сообщения — реальные форматы.
"""
from __future__ import annotations

import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from config.return_reason_rules import RULES, RULES_BY_SIGNAL
from core import return_reasons as return_reasons_module
from core.return_reasons import load_return_reasons
from processing.models import FileCheck, OrderCheck, ParsedFilename
from services.batch_adapter import _pitstop_result_to_dto
from services.check_policy import apply_check_policy
from services.dto import order_check_to_dto
from services.issue_groups import attach_issue_groups
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopReport,
    PitStopSeverity,
)
from services.pitstop.baseline import FixBaselinePolicy, apply_fix_baseline
from services.return_suggestions import (
    _CATALOGUE_SIZE_RE,
    attach_return_suggestions,
    suggest_return_reasons,
)

TEMPLATE = Path(__file__).resolve().parents[1] / "Цифра - Загальне.html"
ERR, WARN, INFO = PitStopSeverity.ERROR, PitStopSeverity.WARNING, PitStopSeverity.INFORMATION

requires_catalogue = pytest.mark.skipif(
    not TEMPLATE.is_file(), reason="справочник «Цифра - Загальне.html» отсутствует в рабочей копии"
)


@pytest.fixture(scope="module")
def reasons():
    if not TEMPLATE.is_file():
        pytest.skip("справочник «Цифра - Загальне.html» отсутствует в рабочей копии")
    return load_return_reasons(TEMPLATE)


def text_of(signal: str) -> str:
    return RULES_BY_SIGNAL[signal].text


def pitstop(*items, status=PitStopExecutionStatus.COMPLETED, revision=1):
    """DTO PitStop из ``(action_id, severity, message)`` — как в проде."""
    issues = tuple(
        PitStopIssue(severity=severity, action_id=action, message=message)
        for action, severity, message in items
    )
    counts = PitStopCounts(
        errors=sum(issue.severity is ERR for issue in issues),
        warnings=sum(issue.severity is WARN for issue in issues),
    )
    completed = status is PitStopExecutionStatus.COMPLETED
    result = PitStopCheckResult(
        status=status,
        profile_id="digital",
        input_pdf=Path("/tmp/sample.pdf"),
        checked_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
        input_sha256="0" * 64,
        report=PitStopReport(counts=counts, issues=issues, pages=2) if completed else None,
        technical_error=None if completed else "SSH недоступен",
        job_id=f"check-r{revision}",
    )
    return _pitstop_result_to_dto(result, checked_revision=revision)


def file_check(name="12345(678-9001)_90x50_4+0_face.pdf", *, errors=(), warnings=(), expected=(94.0, 54.0)):
    parsed = ParsedFilename(
        customer_id="678", order_id="9001", width_mm=90, height_mm=50,
        front_colors=4, back_colors=0, side="face",
    )
    return FileCheck(
        path=Path("/input") / name, parsed=parsed, errors=list(errors), warnings=list(warnings),
        expected_mm=expected,
    )


def order_dto(*, errors=(), file_errors=(), file_warnings=(), name=None, expected=(94.0, 54.0),
              pitstop_dto=None, status="error", **extra):
    kwargs = {"name": name} if name else {}
    order = OrderCheck(
        order_id="9001", customer_id="678", errors=list(errors),
        files=[file_check(errors=file_errors, warnings=file_warnings, expected=expected, **kwargs)],
    )
    dto = order_check_to_dto(order, status=status, pitstop=pitstop_dto)
    dto.update(extra)
    return dto


def signals(items):
    return [item["signal"] for item in items]


# --- (a), (b) справочник -------------------------------------------------------


@requires_catalogue
def test_every_static_rule_text_is_in_the_catalogue_verbatim(reasons):
    texts = {item["text"] for item in reasons}
    missing = [rule.text for rule in RULES if not rule.dynamic and rule.text not in texts]
    missing += [text for rule in RULES for text in rule.alternatives if text not in texts]
    assert missing == []
    assert all(text != rule.text for rule in RULES for text in rule.alternatives)


@requires_catalogue
def test_catalogue_has_parseable_exact_size_items(reasons):
    parsed = [
        (int(match.group(1)), int(match.group(2)))
        for match in (_CATALOGUE_SIZE_RE.match(item["text"]) for item in reasons)
        if match
    ]
    assert (94, 54) in parsed
    assert len(parsed) >= 5


def test_catalogue_size_regex_accepts_cyrillic_and_latin_x():
    assert _CATALOGUE_SIZE_RE.match("Розмір макета з урахуванням доливок повинен бути 94х54мм")
    assert _CATALOGUE_SIZE_RE.match("Розмір макета з урахуванням доливок повинен бути 94x54мм")
    assert not _CATALOGUE_SIZE_RE.match("Розмір макета з урахуванням доливок повинен бути мм")


def test_load_return_reasons_is_cached_and_returns_copies(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "reasons.html"
        path.write_text("<h1>A</h1><label>Перший.</label>", encoding="utf-8")
        calls = []
        original = return_reasons_module._parse_reasons
        monkeypatch.setattr(
            return_reasons_module, "_parse_reasons", lambda value: calls.append(value) or original(value)
        )
        first = load_return_reasons(path)
        first[0]["text"] = "испорчено"
        assert load_return_reasons(path) == [{"category": "A", "text": "Перший"}]
        assert len(calls) == 1
        # Изменённый файл разбирается заново.
        path.write_text("<h1>A</h1><label>Другий;</label><label>Третій</label>", encoding="utf-8")
        stamp = time.time() + 5
        os.utime(path, (stamp, stamp))
        assert [item["text"] for item in load_return_reasons(path)] == ["Другий", "Третій"]
        assert len(calls) == 2
    assert load_return_reasons(Path(tmp) / "missing.html") == []


# --- (c) сигналы ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, signal",
    [
        ({"file_errors": ["PDF страница 1: effective DPI 132.4; требуется не менее 300"]}, "low_dpi"),
        ({"file_errors": ["разрешение 150.0x150.0 DPI; по обеим осям требуется не меньше 250 DPI"]}, "low_dpi"),
        ({"file_errors": [
            "размер 92.0x52.0 мм; ожидается 94.0x54.0 мм: после коррекции эффективное разрешение "
            "180 DPI ниже 250 DPI"
        ]}, "low_dpi"),
        ({"errors": ["двухстраничный PDF недопустим для односторонней печати"]}, "page_count"),
        ({"file_errors": ["PDF contains 3 pages; maximum supported is 2"]}, "page_count"),
        ({"errors": ["найдено несколько двухстраничных PDF"]}, "page_count"),
        ({"errors": ["двухстраничный PDF является полным заказом: лишние файлы сторон"]}, "page_count"),
        ({"file_errors": ["файл содержит 3 страниц/изображений; разрешён только одностраничный файл"]}, "page_count"),
        ({"errors": ["для цветности с оборотом требуется 2 логические страницы"]}, "page_count"),
        ({"errors": [
            "в заказе 3 страницы, а для двусторонней печати допускается 2 (лицо и оборот)"
        ]}, "page_count"),
        ({"errors": ["для односторонней печати найден лишний файл back"]}, "extra_back_one_sided"),
        ({"errors": ["для односторонней печати разрешён только один входной файл"]}, "extra_back_one_sided"),
        ({"errors": ["для двусторонней печати не найден файл back"]}, "missing_back"),
        ({"errors": ["не найден обязательный файл face"]}, "missing_face"),
        ({"file_errors": ["TIFF содержит несведённые слои; перед отправкой сведите изображение"]}, "tiff_layers"),
        ({"file_errors": ["TIFF содержит альфа-канал; перед отправкой удалите прозрачность"]}, "tiff_alpha"),
        ({"file_errors": ["не удалось прочитать файл: unexpected end of file"]}, "file_unreadable"),
        ({"file_errors": ["PDF cannot be opened: cannot open broken document"]}, "file_unreadable"),
    ],
)
def test_local_signals(reasons, kwargs, signal):
    items = suggest_return_reasons(order_dto(**kwargs), reasons)
    assert signals(items) == [signal]
    assert items[0]["text"] == text_of(signal)


def test_low_dpi_evidence_is_short(reasons):
    items = suggest_return_reasons(
        order_dto(file_errors=["PDF страница 1: effective DPI 132.4; требуется не менее 300"]), reasons
    )
    assert items[0]["evidence"] == "12345(678-9001)_90x50_4+0_face.pdf: Стр. 1: effective DPI 132"


SIZE_ERROR = (
    "PDF: размер 69.0x79.0 мм; ожидается 54.0x94.0 мм: для приведения к размеру потребуется "
    "обрезка до 15 мм; допускается не более 5 мм"
)


def test_size_with_expected_catalogue_size_picks_exact_item(reasons):
    items = suggest_return_reasons(order_dto(file_errors=[SIZE_ERROR]), reasons)
    assert signals(items) == ["size_exact_catalogue"]
    assert items[0]["text"] == "Розмір макета з урахуванням доливок повинен бути 94х54мм"
    assert items[0]["preselect"] is True
    assert "ожидается 54x94 мм" in items[0]["evidence"]


def test_size_without_catalogue_size_is_generic_and_not_preselected(reasons):
    items = suggest_return_reasons(
        order_dto(file_errors=[SIZE_ERROR.replace("54.0x94.0", "60.0x100.0")], expected=(100.0, 60.0)),
        reasons,
    )
    assert signals(items) == ["size_mismatch"]
    assert items[0]["confidence"] == "medium" and items[0]["preselect"] is False


@pytest.mark.parametrize(
    "message",
    [
        "PDF страница 1 имеет размер 70.0x80.0 мм; ожидается 100.0x60.0 мм. Автоматическая коррекция",
        "размер 70.0x80.0 мм; ожидается 100.0x60.0 мм: обрезка больше допустимого",
        "пользователь отказался от предложенной коррекции размера",
    ],
)
def test_other_size_messages_are_size_mismatch(reasons, message):
    items = suggest_return_reasons(order_dto(file_errors=[message], expected=(100.0, 60.0)), reasons)
    assert signals(items) == ["size_mismatch"]


def test_pitstop_page_size_uses_file_expected_size(reasons):
    dto = order_dto(pitstop_dto=pitstop(
        (1164, ERR, "Not all pages in the document have the same size (based on Media box) (1x on page 3)")
    ))
    assert signals(suggest_return_reasons(dto, reasons)) == ["size_exact_catalogue"]
    dto = order_dto(expected=(100.0, 60.0), pitstop_dto=pitstop(
        (1164, ERR, "Not all pages in the document have the same size (based on Media box) (1x on page 3)")
    ))
    assert signals(suggest_return_reasons(dto, reasons)) == ["size_mismatch"]


@pytest.mark.parametrize(
    "name, signal",
    [
        ("12345(678-9001)_305x430_4+0_(SRA3)_face.pdf", "size_reference_sra3"),
        ("12345(678-9001)_320x450_4+0_(sra3p)_face.pdf", "size_reference_sra3p"),
        ("12345(678-9001)_700x320_4+0_(BAN)_face.pdf", "size_reference_ban"),
    ],
)
def test_size_reference_markers(reasons, name, signal):
    items = suggest_return_reasons(
        order_dto(name=name, expected=(314.0, 434.0),
                  file_errors=["размер 330.0x487.0 мм; ожидается 314.0x434.0 мм: обрезка больше допустимого"]),
        reasons,
    )
    assert signals(items) == [signal]
    assert items[0]["preselect"] is True


def test_marker_without_size_problem_gives_nothing(reasons):
    dto = order_dto(name="12345(678-9001)_305x430_4+0_(SRA3)_face.pdf", status="passed")
    assert suggest_return_reasons(dto, reasons) == []


@pytest.mark.parametrize(
    "issue, signal",
    [
        ((1150, ERR, "Image resolution is 197 x 197 ppi - color or grayscale image is less than 275 ppi (8x on pages 1-2)"), "low_dpi"),
        ((1148, ERR, "Image resolution is 321 x 332 ppi - 1-bit image is less than 600 ppi (1x on page 1)"), "low_dpi"),
        ((2078, WARN, "ICC based is used (10x on pages 1-2)"), "icc"),
        ((3202, ERR, "Page contains a default color space (10x on pages 1-2)"), "icc"),
        ((2078, ERR, "RGB color is used (2x on pages 1-2)"), "rgb_left"),
        ((3156, ERR, "Spot color All is used for an element within the trim box (16x on page 1)"), "spot_colors"),
        ((3159, WARN, "Document uses 2 separations, should be equal to 0. Those 2 separations are: PANTONE 485 C, PANTONE Black C"), "spot_colors"),
        ((3004, ERR, "Transparent objects have been found (10x on pages 1-2)"), "transparency"),
        ((1185, ERR, "Embedded font Bahnschrift-Bold is not complete (279x on pages 1-2)"), "fonts"),
        ((1180, ERR, "Font ArialMT is not embedded (2x on page 1)"), "fonts"),
        ((1128, ERR, "Font Bahnschrift-Bold is a composite font (279x on pages 1-2)"), "fonts"),
        ((1153, WARN, "Total ink coverage of an object is more than 310% (1x on page 1)"), "ink_max"),
        ((3136, ERR, "White line art does not knock out (1x on page 1)"), "white_overprint"),
        ((3140, WARN, "Line weight is less than 0.2 pt (1x on page 1)"), "thin_lines"),
    ],
)
def test_pitstop_signals(reasons, issue, signal):
    items = suggest_return_reasons(order_dto(pitstop_dto=pitstop(issue)), reasons)
    rule = RULES_BY_SIGNAL[signal]
    assert signals(items) == [signal] * (1 + len(rule.alternatives))
    assert items[0]["text"] == text_of(signal)
    assert items[0]["confidence"] == rule.confidence
    assert [item["text"] for item in items[1:]] == list(rule.alternatives)


def test_alternatives_follow_main_text_unselected(reasons):
    dto = order_dto(pitstop_dto=pitstop(
        (3156, ERR, "Spot color All is used for an element within the trim box (16x on page 1)"),
        (3004, ERR, "Transparent objects have been found (10x on pages 1-2)"),
    ))
    items = suggest_return_reasons(dto, reasons)
    assert [(item["signal"], item["confidence"], item["preselect"]) for item in items] == [
        ("spot_colors", "high", True),
        ("spot_colors", "medium", False),
        ("transparency", "medium", False),
        ("transparency", "medium", False),
    ]
    assert items[1]["text"] == (
        "Всі елементи повинні бути в CMYK, Panton'и неприпустимі. При друці може вийти абсолютно інший колір"
    )
    assert items[1]["category"] == "Брошури на скобі"
    assert items[3]["text"].startswith("Складні спецефекти (тіні, лінзи, текстури")
    assert items[3]["category"] == "Основні вимоги"
    assert items[1]["evidence"] == items[0]["evidence"]
    # Детектор понизил уверенность (3157 — medium): вариант не выше неё и тоже не отмечен.
    lowered = suggest_return_reasons(order_dto(pitstop_dto=pitstop(
        (3157, WARN, "Alternate color space is not Device CMYK or Device gray (2x on page 1)"),
    )), reasons)
    assert [(item["confidence"], item["preselect"]) for item in lowered] == [
        ("medium", False), ("medium", False),
    ]


def test_alternative_missing_from_catalogue_is_skipped():
    reasons = [{"category": "Основні вимоги", "text": text_of("transparency")}]
    dto = order_dto(pitstop_dto=pitstop((3004, ERR, "Transparent objects have been found (1x on page 1)")))
    assert [item["text"] for item in suggest_return_reasons(dto, reasons)] == [text_of("transparency")]


def test_pitstop_resolution_evidence(reasons):
    items = suggest_return_reasons(order_dto(pitstop_dto=pitstop(
        (1150, ERR, "Image resolution is 197 x 197 ppi - color or grayscale image is less than 275 ppi (8x on pages 1-2)")
    )), reasons)
    assert items[0]["evidence"] == "PitStop 1150: 197 ppi"
    ranged = suggest_return_reasons(order_dto(pitstop_dto=pitstop(
        (1150, ERR, "Image resolution is 114 x 128...59 x 63 ppi - color or grayscale image is less than 275 ppi (3x on page 1)")
    )), reasons)
    assert ranged[0]["evidence"] == "PitStop 1150: 59 ppi"


def test_icc_for_transparency_group_is_not_icc(reasons):
    dto = order_dto(pitstop_dto=pitstop((2078, ERR, "ICC based is used for transparency group (1x on page 1)")))
    assert suggest_return_reasons(dto, reasons) == []


def test_technical_separations_are_not_spot_colors(reasons):
    for names in ("White", "LAK", "Clear"):
        dto = order_dto(pitstop_dto=pitstop((
            3159, WARN, f"Document uses 1 separations, should be equal to 0. Those 1 separations are: {names}"
        )))
        assert suggest_return_reasons(dto, reasons) == [], names


def test_ambiguous_findings_are_offered_without_preselect(reasons):
    dto = order_dto(pitstop_dto=pitstop(
        (3136, WARN, "Object does not knock out (1x on page 1)"),
        (3157, WARN, "Alternate color space is not Device CMYK or Device gray (2x on page 1)"),
    ))
    items = {item["signal"]: item for item in suggest_return_reasons(dto, reasons)}
    assert items["white_overprint"]["confidence"] == "medium"
    assert items["spot_colors"]["preselect"] is False


RGB_TEXT = "Всі елементи повинні бути в CMYK, присутні RGB об'єкти"


def test_rgb_left_only_when_conversion_failed(reasons):
    rgb_warning = "PDF страница 1: растровое изображение имеет RGB colorspace"
    failed = order_dto(file_warnings=[rgb_warning], status="warning",
                       color_conversion={"status": "failed", "reason": "rgb", "error": "callas timeout"})
    items = suggest_return_reasons(failed, reasons)
    assert signals(items) == ["rgb_left"] and items[0]["preselect"] is True
    assert items[0]["text"] == RGB_TEXT and items[0]["category"] == "Брошури на скобі"
    icc_failed = order_dto(status="warning", color_conversion={"status": "failed", "reason": "icc"})
    assert suggest_return_reasons(icc_failed, reasons) == []
    pitstop_rgb = order_dto(pitstop_dto=pitstop((2078, ERR, "RGB color is used (2x on pages 1-2)")),
                            color_conversion={"status": "converted", "reason": "rgb"})
    assert signals(suggest_return_reasons(pitstop_rgb, reasons)) == ["rgb_left"]  # без дубля medium


def test_converted_rgb_is_offered_without_preselect(reasons):
    rgb_warning = "PDF страница 1: растровое изображение имеет RGB colorspace"
    converted = order_dto(status="warning", color_conversion={"status": "converted", "pages": [1, 2]})
    items = suggest_return_reasons(converted, reasons)
    assert [(item["signal"], item["confidence"], item["preselect"]) for item in items] == [
        ("rgb_converted", "medium", False),
    ]
    assert items[0]["text"] == RGB_TEXT and items[0]["evidence"] == "RGB переведён в CMYK (стр. 1, 2)"
    warned = suggest_return_reasons(order_dto(file_warnings=[rgb_warning], status="warning"), reasons)
    assert signals(warned) == ["rgb_converted"]
    assert warned[0]["evidence"] == "12345(678-9001)_90x50_4+0_face.pdf: PDF страница 1: растровое изображение имеет RGB colorspace"
    icc_only = order_dto(status="warning", color_conversion={"status": "converted", "reason": "icc"})
    assert suggest_return_reasons(icc_only, reasons) == []
    colour_rgb = order_dto(file_errors=["цветовая модель sRGB не входит в профиль digital (CMYK, Gray)"],
                           color_conversion={"status": "converted", "reason": "rgb"})
    assert signals(suggest_return_reasons(colour_rgb, reasons)) == ["rgb_converted"]


def test_colour_model_error(reasons):
    rgb = order_dto(file_errors=["цветовая модель sRGB не входит в профиль digital (CMYK, Gray)"])
    items = suggest_return_reasons(rgb, reasons)
    assert signals(items) == ["rgb_left"] and items[0]["text"] == RGB_TEXT
    lab = order_dto(file_errors=["цветовая модель Lab не входит в профиль digital (CMYK, Gray)"])
    items = suggest_return_reasons(lab, reasons)
    assert signals(items) == ["color_model"] and items[0]["preselect"] is True
    assert items[0]["text"] == "Макет має бути виконаний в кольоровій моделі CMYK"


def test_ink_overflow_flag_above_300(reasons):
    dto = order_dto(status="warning", ink_coverage={"status": "completed", "flags": [
        {"kind": "overflow", "page": 2, "percent": 335.0, "threshold": 300.0},
    ]})
    items = suggest_return_reasons(dto, reasons)
    assert signals(items) == ["ink_max"] and items[0]["evidence"] == "Красочность 335% на стр. 2"
    low = order_dto(status="warning", ink_coverage={"status": "completed", "flags": [
        {"kind": "overflow", "page": 1, "percent": 262.0, "threshold": 250.0},
    ]})
    assert suggest_return_reasons(low, reasons) == []


def test_ink_min_only_from_light_areas_not_old_underflow(reasons):
    light = order_dto(status="warning", ink_coverage={"status": "completed", "flags": [
        {"kind": "light_areas", "page": 1, "percent": 74.48, "threshold": 20.0},
    ]})
    items = suggest_return_reasons(light, reasons)
    assert [(item["signal"], item["confidence"], item["preselect"]) for item in items] == [
        ("ink_min", "medium", False),
    ]
    assert items[0]["text"] == "Мінімальна барвистість - 20%" and items[0]["category"] == "Кольори"
    assert items[0]["evidence"] == "Светлые участки светлее 20%: 74.48% площади на стр. 1"
    underflow = order_dto(status="warning", ink_coverage={"status": "completed", "flags": [
        {"kind": "underflow", "page": 2, "percent": 12.5, "threshold": 20.0},
    ]})
    # Старый underflow (среднее ниже 20 %) срабатывал на белых макетах — не сигнал.
    assert suggest_return_reasons(underflow, reasons) == []
    # Пустая страница (0 % краски, старый underflow 26242124) — не светлый макет.
    blank = order_dto(status="warning", ink_coverage={"status": "completed", "flags": [
        {"kind": "underflow", "page": 1, "percent": 0.0, "threshold": 20.0},
        {"kind": "light_areas", "page": 1, "percent": 0.0, "threshold": 20.0},
    ]})
    assert suggest_return_reasons(blank, reasons) == []


def test_blocking_overprint_check(reasons):
    def prepress(white):
        return {"overprint": {
            "status": "ok", "blocking_count": 2, "technical_count": 0,
            "message": "Overprint на 2 объектах", "by_kind": {"white": white, "process": 2 - white},
        }}
    white = suggest_return_reasons(order_dto(prepress=prepress(1)), reasons)
    assert signals(white) == ["white_overprint"] and white[0]["preselect"] is True
    process = suggest_return_reasons(order_dto(prepress=prepress(0)), reasons)
    assert process[0]["confidence"] == "medium"
    technical_only = {"overprint": {"status": "ok", "blocking_count": 0, "technical_count": 3}}
    assert suggest_return_reasons(order_dto(prepress=technical_only, status="passed"), reasons) == []


def test_revision_check_replaces_stale_file_messages(reasons):
    dto = order_dto(file_errors=["TIFF содержит альфа-канал; перед отправкой удалите прозрачность"])
    dto["revision_check"] = {"errors": ["PDF страница 1: effective DPI 120.0; требуется не менее 250"], "warnings": []}
    assert signals(suggest_return_reasons(dto, reasons)) == ["low_dpi"]


# --- (d) исключения -------------------------------------------------------------


def test_finding_hidden_by_fix_baseline_is_skipped(reasons):
    current = pitstop((3004, ERR, "Transparent objects have been found (10x on pages 1-2)"), revision=2)
    hidden = apply_fix_baseline(current, pitstop(), FixBaselinePolicy())
    assert hidden["issues"][0]["hidden_by_baseline"] is True
    assert suggest_return_reasons(order_dto(pitstop_dto=hidden), reasons) == []
    # Даже если уровень почему-то остался прежним, метка всё равно исключает находку.
    hidden["issues"][0]["severity"] = "error"
    assert suggest_return_reasons(order_dto(pitstop_dto=hidden), reasons) == []


def test_finding_demoted_by_policy_is_skipped(reasons):
    raw = pitstop(
        (3004, ERR, "Transparent objects have been found (10x on pages 1-2)"),
        (3140, WARN, "Line weight is less than 0.2 pt (1x on page 1)"),
        (1131, ERR, "Font ArialMT is a TrueType font (2x on page 1)"),
    )
    basic = apply_check_policy(raw, "basic")
    assert all(issue["policy_demoted"] for issue in basic["issues"])
    assert suggest_return_reasons(order_dto(pitstop_dto=basic, status="passed"), reasons) == []
    full = apply_check_policy(raw, "full")
    assert signals(suggest_return_reasons(order_dto(pitstop_dto=full), reasons)) == [
        "transparency", "transparency", "fonts", "thin_lines",
    ]


def test_information_and_fix_severity_are_skipped(reasons):
    dto = order_dto(status="passed", pitstop_dto=pitstop(
        (1150, INFO, "Image resolution is 51 x 51 ppi - color or grayscale image is less than 275 ppi (3x on page 1)"),
        (2078, PitStopSeverity.FIX, "ICC based is used (1x on page 1)"),
    ))
    assert suggest_return_reasons(dto, reasons) == []


def test_technical_pitstop_failure_is_skipped(reasons):
    failed = pitstop(status=PitStopExecutionStatus.FAILED)
    failed["issues"] = [{"severity": "error", "action_id": 1150, "message": "Image resolution is 10 x 10 ppi"}]
    assert suggest_return_reasons(order_dto(pitstop_dto=failed), reasons) == []


def test_local_warnings_and_unknown_messages_are_skipped(reasons):
    dto = order_dto(
        status="warning",
        file_warnings=["PDF: требуется подтверждение коррекции 92.0x52.0 → 94.0x54.0 мм; страница 1"],
        errors=["у файлов заказа не совпадает номер клиента"],
    )
    assert suggest_return_reasons(dto, reasons) == []


def test_passed_order_has_no_suggestions(reasons):
    assert suggest_return_reasons(order_dto(status="passed", pitstop_dto=pitstop()), reasons) == []


def test_rule_text_missing_from_catalogue_is_skipped_silently():
    partial = [{"category": "X", "text": text_of("tiff_alpha")}]
    dto = order_dto(file_errors=[
        "TIFF содержит альфа-канал; перед отправкой удалите прозрачность",
        "PDF страница 1: effective DPI 132.4; требуется не менее 300",
    ])
    assert signals(suggest_return_reasons(dto, partial)) == ["tiff_alpha"]
    assert suggest_return_reasons(dto, []) == []


# --- (e) дедупликация, порядок, preselect ----------------------------------------


def test_dedup_order_and_preselect(reasons):
    dto = order_dto(
        errors=["для двусторонней печати не найден файл back"],
        file_errors=[
            "PDF страница 1: effective DPI 132.4; требуется не менее 300",
            "PDF страница 2: effective DPI 90.0; требуется не менее 300",
        ],
        pitstop_dto=pitstop(
            (3140, WARN, "Line weight is less than 0.2 pt (1x on page 1)"),
            (1150, ERR, "Image resolution is 197 x 197 ppi - color or grayscale image is less than 275 ppi (8x on pages 1-2)"),
            (2078, WARN, "ICC based is used (10x on pages 1-2)"),
            (3202, ERR, "Page contains a default color space (10x on pages 1-2)"),
        ),
    )
    items = suggest_return_reasons(dto, reasons)
    assert signals(items) == ["low_dpi", "missing_back", "icc", "thin_lines"]
    assert len({item["text"] for item in items}) == len(items)
    assert [item["preselect"] for item in items] == [True, True, True, False]
    assert items[0]["evidence"].endswith("Стр. 1: effective DPI 132")
    assert set(items[0]) == {"text", "category", "signal", "confidence", "preselect", "evidence"}
    assert items[0]["category"] == "Основні вимоги"


# --- (f) DTO и API ----------------------------------------------------------------


def test_order_dto_and_issue_groups_carry_suggestions():
    dto = order_dto(file_errors=["TIFF содержит альфа-канал; перед отправкой удалите прозрачность"])
    assert "issue_groups" in dto and isinstance(dto["return_suggestions"], list)
    if TEMPLATE.is_file():
        assert signals(dto["return_suggestions"]) == ["tiff_alpha"]
    bare = {"errors": [], "files": []}
    assert attach_issue_groups(bare)["return_suggestions"] == []


def test_attach_never_raises(monkeypatch):
    import services.return_suggestions as module

    monkeypatch.setattr(module, "suggest_return_reasons", lambda *_: 1 / 0)
    assert attach_return_suggestions({"order_id": "1"})["return_suggestions"] == []


@requires_catalogue
def test_api_order_payload_contains_return_suggestions():
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient

    os.environ.setdefault("IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password"))
    import control_panel
    from server.settings import Settings
    from services.batch_adapter import OrderArtifacts

    class LowDpiAdapter:
        def __init__(self, options):
            self.input_dir = Path(options.input_path)
            source = self.input_dir / "12345(678-9001)_90x50_4+0_face.jpg"
            source.write_bytes(b"source-image")
            check = file_check(source.name, errors=[
                "разрешение 150.0x150.0 DPI; по обеим осям требуется не меньше 250 DPI"
            ])
            check.path = source
            self.order = OrderCheck(order_id="9001", customer_id="678", files=[check])

        def scan_and_inspect(self):
            return [self.order]

        def pending_files(self, _order):
            return []

        def decide(self, _order, _approved):
            return None

        def process_order(self, _order):
            return OrderArtifacts()

    with tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR) as tmp:
        root = Path(tmp)
        settings = Settings(
            database_url=f"sqlite:///{root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=root / "logs",
            log_heartbeat_seconds=10,
        )
        app = control_panel.create_app(
            settings=settings, allowed_roots=(root,), default_input_dir=root,
            adapter_factory=LowDpiAdapter, initialize_schema=True,
        )
        with TestClient(app) as client:
            assert client.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
            response = client.post("/api/checks", json={
                "input_path": str(root), "direction": "digital",
                "create_pdfs": False, "generate_previews": False, "copy_failures": False,
            })
            assert response.status_code == 202, response.text
            run_id = response.json()["id"]
            client.app.state.coordinator.wait_for(run_id, timeout=5)

            listed = client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
            paged = client.get(
                f"/api/checks/{run_id}/orders?page=1&page_size=10&status=all&active_only=false"
            ).json()["items"][0]
            single = client.get(f"/api/checks/{run_id}/orders/{listed['aggregate_id']}").json()
            whole = next(iter(client.get(f"/api/checks/{run_id}").json()["orders"].values()))
            for order in (listed, paged, single, whole):
                assert order["status"] == "error"
                assert "issue_groups" in order
                assert [item["signal"] for item in order["return_suggestions"]] == ["low_dpi"]
                assert order["return_suggestions"][0]["preselect"] is True

        # Заказ, сохранённый без подсказок (старый запуск), получает их при чтении.
        stored = {"errors": ["не найден обязательный файл face"], "files": []}
        assert [item["signal"] for item in control_panel._ensure_return_suggestions(stored)["return_suggestions"]] == [
            "missing_face"
        ]
        assert re.search("Лице", stored["return_suggestions"][0]["text"])
