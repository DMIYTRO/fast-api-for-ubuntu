"""Лестница коррекции размера: правило уменьшения, порядок способов PDF, пометка size_fix."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from config.profiles import DEFAULT_SHRINK_AUTO_MAX_MM, get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, make_size_fix
from processing.pdf_resize_policy import (
    METHOD_FILL,
    METHOD_REVEAL,
    METHOD_SHRINK,
    METHOD_STRETCH,
    MAX_VARIABLE_LENGTH,
    PdfResizeLimits,
    build_resize_quickfix,
    plan_pdf_page_resize,
    quickfix_variable,
)
from processing.resample_policy import ResampleDecision, analyze_resample
from services.dto import file_check_to_dto

PT = 72 / 25.4
TARGET = (94.0, 54.0)
LIMITS = PdfResizeLimits(
    min_dpi=250, metadata_tolerance_mm=0.1, auto_mm=4.0, confirm_mm=10.0,
    stretch_auto_pct=1.0, stretch_confirm_pct=3.0, shrink_auto_mm=2.0,
)


def _pdf_plan(actual, expected=TARGET, dpi=math.inf, hidden=(), limits=LIMITS):
    return plan_pdf_page_resize(actual, expected, dpi, limits, hidden)


# --- правило уменьшения: растр ------------------------------------------------

def test_profile_default_and_correction_limit_keep_shrink_limit() -> None:
    profile = get_profile("digital")
    assert profile.shrink_auto_mm == DEFAULT_SHRINK_AUTO_MAX_MM == 2.0
    assert profile.with_correction_limit(1.5, 3.0).shrink_auto_mm == 2.0
    assert replace(profile, shrink_auto_mm=5.0).with_correction_limit(1.0).shrink_auto_mm == 5.0


def test_shrink_up_to_two_mm_is_automatic() -> None:
    plan = analyze_resample((96.0, 56.0), TARGET, (300.0, 300.0))
    assert plan.scale < 1
    assert plan.decision == ResampleDecision.AUTO_CORRECT


def test_shrink_over_two_mm_needs_confirmation() -> None:
    plan = analyze_resample((96.5, 56.5), TARGET, (300.0, 300.0))
    assert plan.decision == ResampleDecision.ASK_CONFIRMATION
    assert "уменьшение на 2.5 мм больше предела авто 2.0 мм" in plan.reason
    assert "подтверждение" in plan.reason


def test_low_dpi_source_rescued_by_shrink_stays_automatic() -> None:
    # 188x108 мм при 150 DPI — тот же макет 94x54 при 300 DPI с неверными метаданными.
    plan = analyze_resample((188.0, 108.0), TARGET, (150.0, 150.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert min(plan.effective_dpi) == pytest.approx(300.0)
    assert "поднимает разрешение" in plan.reason


def test_low_dpi_source_not_rescued_is_still_rejected() -> None:
    plan = analyze_resample((188.0, 108.0), TARGET, (100.0, 100.0))
    assert plan.decision == ResampleDecision.REJECT


def test_large_high_dpi_shrink_needs_confirmation_and_rule_can_be_disabled() -> None:
    assert analyze_resample((188.0, 108.0), TARGET, (300.0, 300.0)).decision == (
        ResampleDecision.ASK_CONFIRMATION
    )
    assert analyze_resample(
        (188.0, 108.0), TARGET, (300.0, 300.0), shrink_auto_mm=None
    ).decision == ResampleDecision.AUTO_CORRECT


def test_enlargement_and_crop_limits_are_not_affected() -> None:
    assert analyze_resample((92.0, 52.0), TARGET, (300.0, 300.0)).decision == (
        ResampleDecision.AUTO_CORRECT
    )
    # Обрезка 5 мм — подтверждение по пределу обрезки, как и раньше.
    plan = analyze_resample((94.0, 59.0), TARGET, (300.0, 300.0))
    assert plan.decision == ResampleDecision.ASK_CONFIRMATION
    assert "обрезка" in plan.reason


# --- правило уменьшения и порядок способов: PDF --------------------------------

def test_vector_pdf_small_shrink_is_automatic_shrink() -> None:
    plan = _pdf_plan((96.0, 56.0))
    assert (plan.method, plan.decision) == (METHOD_SHRINK, ResampleDecision.AUTO_CORRECT)


def test_vector_pdf_uses_only_the_size_rule() -> None:
    # Ровно вдвое больше: вытягивание с искажением 0 отброшено, уменьшение на 94 мм — подтверждение.
    plan = _pdf_plan((188.0, 108.0))
    assert plan.method == METHOD_SHRINK
    assert plan.decision == ResampleDecision.ASK_CONFIRMATION
    assert "предела авто" in plan.reason


def test_pdf_low_dpi_page_rescued_by_shrink_is_automatic() -> None:
    plan = _pdf_plan((188.0, 108.0), dpi=150.0)
    assert (plan.method, plan.decision) == (METHOD_SHRINK, ResampleDecision.AUTO_CORRECT)


def test_stretch_is_evaluated_even_when_fill_is_automatic() -> None:
    # 89x51: заполнение — авто (обрезка 0.24 мм), вытягивание 0.25% — тоже авто и раньше.
    plan = _pdf_plan((89.0, 51.0))
    assert (plan.method, plan.decision) == (METHOD_STRETCH, ResampleDecision.AUTO_CORRECT)


def test_stretch_comes_before_shrink_at_equal_decision() -> None:
    # 95x55 → 94x54: и уменьшение (1 мм), и вытягивание (0.78%) — авто; вытягивание раньше.
    plan = _pdf_plan((95.0, 55.0))
    assert (plan.method, plan.decision) == (METHOD_STRETCH, ResampleDecision.AUTO_CORRECT)


def test_auto_shrink_beats_confirmed_stretch() -> None:
    # 96x56: вытягивание 1.54% (подтверждение), уменьшение 2 мм — авто.
    assert _pdf_plan((96.0, 56.0)).method == METHOD_SHRINK


def test_reveal_comes_first() -> None:
    plan = _pdf_plan((90.0, 50.0), hidden=[(2.0, 2.0)])
    assert (plan.method, plan.decision) == (METHOD_REVEAL, ResampleDecision.AUTO_CORRECT)


def test_enlarging_fill_is_labelled_fill() -> None:
    plan = _pdf_plan((89.0, 52.0))
    assert plan.method == METHOD_FILL and plan.scale[0] > 1


def test_large_shrinking_stretch_needs_confirmation() -> None:
    # Почти вдвое больше: вытягивание 0.2% уменьшило бы страницу на 94 мм без подтверждения.
    plan = _pdf_plan((188.5, 108.5))
    assert plan.decision == ResampleDecision.ASK_CONFIRMATION
    assert plan.method == METHOD_STRETCH


def test_side_equal_to_format_within_noise_is_not_a_shrink() -> None:
    # 605 × 400.00001 → 600 × 400: обрезка/вытягивание, а не уменьшение на 5 мм.
    assert analyze_resample((94.00001, 56.0), TARGET, (300.0, 300.0)).decision == (
        ResampleDecision.AUTO_CORRECT
    )
    plan = _pdf_plan((605.0, 400.00001), expected=(600.0, 400.0))
    assert (plan.method, plan.decision) == (METHOD_STRETCH, ResampleDecision.AUTO_CORRECT)


def test_shrink_quickfix_uses_fit_mode_and_fits_variable_limit() -> None:
    shrink = _pdf_plan((96.0, 56.0))
    config = build_resize_quickfix({1: shrink, 2: _pdf_plan((89.0, 51.0))}, 2.0)
    modes = [i["page_scale_mode"] for i in config["quickfixes"][0]["instructions"]]
    assert modes == ["fit_from_outside_cut_page", "stretch_to_fill"]
    assert len(quickfix_variable(config)) <= MAX_VARIABLE_LENGTH


# --- пометка size_fix: растр ---------------------------------------------------

def _raster(actual, dpi=300.0, product=(90.0, 50.0)) -> FileCheck:
    check = FileCheck(path=Path("1_2_90x50_4-0_face.tif"))
    check.parsed = SimpleNamespace(
        width_mm=product[0], height_mm=product[1], front_colors=4, back_colors=0, side="face",
    )
    check.actual_width_mm, check.actual_height_mm = actual
    check.width_px = round(actual[0] / 25.4 * dpi)
    check.height_px = round(actual[1] / 25.4 * dpi)
    check.dpi = check.dpi_x = check.dpi_y = dpi
    check.colorspace = "CMYK"
    return check


def _processor(**kwargs) -> BatchProcessor:
    return BatchProcessor(Path("input"), Path("output"), **kwargs)


def test_raster_auto_shrink_sets_size_fix_instead_of_warning() -> None:
    check = _raster((96.0, 56.0))
    _processor()._validate_file(check)

    assert check.needs_resample and check.resample_decision == "auto_correct"
    assert check.size_fix == {
        "method": "shrink", "auto": True, "from_mm": [96.0, 56.0], "to_mm": [94.0, 54.0],
        "detail": check.size_fix["detail"],
    }
    assert "уменьшение" in check.size_fix["detail"] and "обрезка" in check.size_fix["detail"]
    assert not any("скорректирован" in warning for warning in check.warnings)


def test_raster_auto_enlarge_is_marked_fill() -> None:
    check = _raster((92.0, 52.0))
    _processor()._validate_file(check)
    assert check.size_fix["method"] == "fill" and check.size_fix["auto"] is True
    assert "увеличение" in check.size_fix["detail"]


def test_raster_big_shrink_asks_and_confirmation_marks_size_fix() -> None:
    check = _raster((98.0, 58.0))
    _processor()._validate_file(check)

    assert check.resample_decision == "ask_confirmation"
    assert check.size_fix is None
    assert any("требуется подтверждение коррекции" in warning for warning in check.warnings)

    BatchProcessor.confirm_resample(check, True)
    assert check.needs_resample
    assert check.size_fix["method"] == "shrink" and check.size_fix["auto"] is False
    assert "подтверждено оператором" in check.size_fix["detail"]
    assert not any("подтверждена" in warning for warning in check.warnings)


def test_raster_rejected_confirmation_leaves_no_mark() -> None:
    check = _raster((98.0, 58.0))
    _processor()._validate_file(check)
    BatchProcessor.confirm_resample(check, False)
    assert check.size_fix is None
    assert check.resample_decision == "reject" and check.errors


def test_profile_shrink_limit_is_used_by_processor() -> None:
    profile = replace(get_profile("digital"), shrink_auto_mm=5.0)
    check = _raster((98.0, 58.0))
    _processor(profile=profile)._validate_file(check)
    assert check.resample_decision == "auto_correct" and check.size_fix["auto"] is True


def test_ask_policy_demotes_auto_correction_without_mark() -> None:
    check = _raster((96.0, 56.0))
    _processor(correction_policy="ask")._validate_file(check)
    assert check.resample_decision == "ask_confirmation" and check.size_fix is None


def test_size_fix_survives_dto_as_json() -> None:
    check = _raster((96.0, 56.0))
    _processor()._validate_file(check)
    check.parsed = None  # DTO ждёт ParsedFilename; для пометки размер имени не нужен
    dto = file_check_to_dto(check)
    assert json.loads(json.dumps(dto))["size_fix"] == check.size_fix
    plain = _raster((94.0, 54.0))
    plain.parsed = None
    assert file_check_to_dto(plain)["size_fix"] is None


def test_make_size_fix_validates_method() -> None:
    fix = make_size_fix("bleed", True, (90.0, 50.0), (94.0, 54.0), "дорисованы вылеты")
    assert fix == {
        "method": "bleed", "auto": True, "from_mm": [90.0, 50.0], "to_mm": [94.0, 54.0],
        "detail": "дорисованы вылеты",
    }
    with pytest.raises(ValueError):
        make_size_fix("squash", True, (1, 1), (1, 1), "")


# --- пометка size_fix: PDF -----------------------------------------------------

def _make_pdf(path: Path, sizes) -> None:
    document = pymupdf.open()
    for width, height in sizes:
        document.new_page(width=width * PT, height=height * PT)
    document.save(str(path))
    document.close()


def _fake_resizer(source, target, page_plans, bleed_mm, label):  # pragma: no cover - not called
    raise AssertionError("resize is not expected during inspection")


def test_pdf_stretch_size_fix_on_large_format(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(596x396)_4-0_(1-100)_input.pdf"
    _make_pdf(pdf, [(605.0, 400.0)])

    item = BatchProcessor(tmp_path, tmp_path / "out", pdf_resizer=_fake_resizer).inspect_orders()[0].files[0]

    assert item.resample_decision == "auto_correct", (item.errors, item.warnings, item.resample_reason)
    assert item.size_fix["method"] == "stretch" and item.size_fix["auto"] is True
    assert item.size_fix["from_mm"] == [605.0, 400.0] and item.size_fix["to_mm"] == [600.0, 400.0]
    assert "вытягивание" in item.size_fix["detail"]
    assert not any("скорректирован" in warning for warning in item.warnings)


def test_pdf_big_shrink_asks_then_confirmation_marks_size_fix(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-4_(1-100).pdf"
    _make_pdf(pdf, [(188.0, 108.0), (188.0, 108.0)])

    item = BatchProcessor(tmp_path, tmp_path / "out", pdf_resizer=_fake_resizer).inspect_orders()[0].files[0]

    assert item.resample_decision == "ask_confirmation"
    assert item.size_fix is None
    assert any(w.startswith("PDF: требуется подтверждение коррекции") for w in item.warnings)

    BatchProcessor.confirm_resample(item, True)
    assert item.size_fix["method"] == "shrink" and item.size_fix["auto"] is False
    assert item.size_fix["from_mm"] == [188.0, 108.0] and item.size_fix["to_mm"] == [94.0, 54.0]
    assert "стр. 1" in item.size_fix["detail"] and "стр. 2" in item.size_fix["detail"]
