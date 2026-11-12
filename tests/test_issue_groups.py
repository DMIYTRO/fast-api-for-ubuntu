from __future__ import annotations

import json
from pathlib import Path

from processing.models import FileCheck, OrderCheck, ParsedFilename
from services.dto import order_check_to_dto
from services.issue_groups import (
    build_issue_groups,
    format_group_message,
    issue_groups_for_order_dto,
    normalize_local_message,
)
from services.pitstop.parser import parse_pitstop_payload

FIXTURES = Path(__file__).parent / "fixtures" / "pitstop"


def _pitstop_issue(message: str, *, page: int, bbox=(0, 0, 10, 10), action_id=2078, severity="error"):
    return {
        "severity": severity,
        "action_id": action_id,
        "message": message,
        "locations": [
            {
                "page": page,
                "bbox": {"min_x": bbox[0], "min_y": bbox[1], "max_x": bbox[2], "max_y": bbox[3]},
                "units": "pt",
            }
        ],
    }


def test_hundred_identical_pitstop_entries_become_one_group():
    issues = [_pitstop_issue("Low resolution image", page=1) for _ in range(100)]
    groups = build_issue_groups(pitstop_issues=issues)
    assert len(groups) == 1
    group = groups[0]
    assert group["count"] == 100
    assert group["source"] == "pitstop"
    assert group["severity"] == "error"
    assert group["code"] == "action:2078"
    # Все записи указывают на один и тот же bbox — это один объект.
    assert group["objects"] == 1
    assert group["pages"] == [1]
    assert group["samples"] == ["Low resolution image"]


def test_distinct_bboxes_on_two_pages_count_objects_and_pages():
    issues = [
        _pitstop_issue("Image resolution too low", page=1 + (i % 2), bbox=(i, i, i + 5, i + 5))
        for i in range(20)
    ]
    groups = build_issue_groups(pitstop_issues=issues)
    assert len(groups) == 1
    assert groups[0]["objects"] == 20
    assert groups[0]["pages"] == [1, 2]
    assert groups[0]["message"] == "Image resolution too low — 20 объектов на страницах 1, 2"


def test_single_pitstop_entry_with_many_locations_is_how_real_reports_look():
    payload = json.loads((FIXTURES / "error_report.json").read_text(encoding="utf-8-sig"))
    report = parse_pitstop_payload(payload)
    # PitStop Server агрегирует отчёт сам: одна запись на правило, N location.
    assert len(report.issues) == 1
    assert len(report.issues[0].locations) == 2
    issues = [
        {
            "severity": issue.severity.value,
            "action_id": issue.action_id,
            "message": issue.message,
            "locations": [
                {"page": loc.page, "bbox": {"min_x": loc.min_x, "min_y": loc.min_y, "max_x": loc.max_x, "max_y": loc.max_y}}
                for loc in issue.locations
            ],
        }
        for issue in report.issues
    ]
    groups = build_issue_groups(pitstop_issues=issues)
    assert groups[0]["count"] == 1
    assert groups[0]["objects"] == 2
    assert groups[0]["pages"] == [1, 2]


def test_different_codes_and_severities_stay_separate():
    issues = [
        _pitstop_issue("RGB color is used", page=1, action_id=2078),
        _pitstop_issue("Font not embedded", page=1, action_id=1001),
        _pitstop_issue("RGB color is used", page=1, action_id=2078, severity="warning"),
        {"severity": "information", "action_id": None, "message": "Page 1 has 3 images", "locations": []},
    ]
    groups = build_issue_groups(pitstop_issues=issues)
    assert [(g["severity"], g["code"]) for g in groups] == [
        ("error", "action:2078"),
        ("error", "action:1001"),
        ("warning", "action:2078"),
        ("info", "Page # has # images"),
    ]
    assert groups[3]["objects"] is None


def test_local_messages_with_different_numbers_collapse():
    groups = build_issue_groups(
        errors=[
            "размер 90.2x50.1 мм; ожидается 94.0x54.0 мм: отклонение слишком велико",
            "размер 88.0x48.0 мм; ожидается 94.0x54.0 мм: отклонение слишком велико",
            "не найден обязательный файл face",
        ],
        warnings=["Неизвестное сообщение №17 про 300 DPI", "Неизвестное сообщение №18 про 72 DPI"],
    )
    by_code = {g["code"]: g for g in groups}
    assert by_code["size_mismatch"]["count"] == 2
    assert by_code["size_mismatch"]["objects"] is None
    assert by_code["size_mismatch"]["message"].startswith("Размер не соответствует ожидаемому — 2 повтора")
    assert len(by_code["size_mismatch"]["samples"]) == 2
    assert by_code["missing_face"]["count"] == 1
    assert by_code["missing_face"]["message"] == "не найден обязательный файл face"
    unknown = by_code["Неизвестное сообщение №# про # DPI"]
    assert unknown["count"] == 2
    assert unknown["severity"] == "warning"


def test_file_level_messages_track_files():
    groups = build_issue_groups(
        files=[
            {"name": "1_2_90x50_4-0_face.tif", "errors": ["разрешение 150 DPI; по обеим осям требуется не меньше 300 DPI"], "warnings": []},
            {"name": "1_2_90x50_4-0_back.tif", "errors": ["разрешение 200 DPI; по обеим осям требуется не меньше 300 DPI"], "warnings": []},
        ]
    )
    assert len(groups) == 1
    assert groups[0]["code"] == "low_dpi"
    assert groups[0]["files"] == ["1_2_90x50_4-0_face.tif", "1_2_90x50_4-0_back.tif"]
    assert groups[0]["message"] == "Низкое разрешение — 2 повтора в 2 файлах"


def test_normalizer_known_templates():
    assert normalize_local_message("PDF страница 2: effective DPI 120.0; требуется не менее 300")[0] == "pdf_low_dpi"
    assert normalize_local_message("TIFF содержит альфа-канал; перед отправкой удалите прозрачность")[0] == "tiff_alpha"
    assert normalize_local_message("В TIFF найдено несведённых слоёв: 3; требуется ручное решение")[0] == "tiff_layers"
    assert normalize_local_message("формат PSD исключён из обработки")[0] == "unsupported_format"
    assert normalize_local_message("не удалось прочитать файл: boom")[0] == "file_unreadable"
    assert normalize_local_message("цветовая модель sRGB не входит в профиль digital (CMYK)")[0] == "colorspace_not_allowed"
    assert normalize_local_message("Encrypted PDF files are not supported")[0] == "pdf_encrypted"
    assert normalize_local_message("Превышено время обработки заказа (60 с); результат не принят.")[0] == "processing_timeout"


def test_format_group_message_plurals():
    assert format_group_message("X", count=1, objects=None, pages=[], files=[]) == "X"
    assert format_group_message("X", count=1, objects=1, pages=[3], files=[]) == "X — 1 объект на странице 3"
    assert format_group_message("X", count=5, objects=None, pages=[], files=[]) == "X — 5 повторов"
    assert format_group_message("X", count=3, objects=None, pages=[], files=["a.tif"]) == "X — 3 повтора в файле a.tif"


def test_order_dto_carries_issue_groups_with_pitstop_and_processing_errors(tmp_path):
    parsed = ParsedFilename(customer_id="1", order_id="2", width_mm=90, height_mm=50, front_colors=4, back_colors=0, side="face")
    file_check = FileCheck(path=tmp_path / "1_2_90x50_4-0_face.tif", parsed=parsed)
    file_check.errors.append("TIFF содержит альфа-канал; перед отправкой удалите прозрачность")
    order = OrderCheck(order_id="2", customer_id="1", files=[file_check])
    order.warnings.append("стороны заказа имеют разные форматы: PDF, TIFF")
    pitstop = {"verdict": "error", "execution_status": "completed", "issues": [_pitstop_issue("RGB color is used", page=1)]}
    dto = order_check_to_dto(order, processing_errors=["PDF: Ghostscript не смог создать файл"], pitstop=pitstop)
    assert dto["errors"] == [] and dto["processing_errors"] == ["PDF: Ghostscript не смог создать файл"]
    codes = {(g["source"], g["severity"], g["code"]) for g in dto["issue_groups"]}
    assert codes == {
        ("local", "error", "pdf_error"),
        ("local", "error", "tiff_alpha"),
        ("local", "warning", "mixed_formats"),
        ("pitstop", "error", "action:2078"),
    }
    assert issue_groups_for_order_dto(dto) == dto["issue_groups"]


def test_page_only_locations_do_not_claim_unique_objects():
    groups = build_issue_groups(pitstop_issues=[{
        "severity": "error", "action_id": 1, "message": "Low DPI",
        "locations": [{"page": 2}, {"page": 2}],
    }])
    assert groups[0]["objects"] is None
    assert groups[0]["pages"] == [2]
    assert groups[0]["count"] == 1


def test_partial_locations_do_not_undercount_objects():
    issues = [_pitstop_issue("Low DPI", page=1), {
        "severity": "error", "action_id": 2078, "message": "Low DPI", "locations": [],
    }]
    assert build_issue_groups(pitstop_issues=issues)[0]["objects"] is None


def test_current_revision_groups_replace_historical_source_findings():
    from services.issue_groups import attach_issue_groups
    dto = {"errors": ["old source error"], "files": [{"name": "face.tif", "errors": ["old DPI"]}], "processing_errors": ["new size error"], "revision_check": {"errors": ["new size error"], "warnings": []}}
    attach_issue_groups(dto)
    assert len(dto["issue_groups"]) == 1
    assert dto["issue_groups"][0]["samples"] == ["new size error"]
    assert dto["issue_groups"][0]["count"] == 1
    assert dto["files"][0]["errors"] == ["old DPI"]
    dto["revision_check"] = {"errors": [], "warnings": []}
    dto["processing_errors"] = []
    assert attach_issue_groups(dto)["issue_groups"] == []
