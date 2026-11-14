"""Предел автоматической коррекции и режимы «Запрашивать / Автоматически / Не применять»."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config.profiles import DEFAULT_PROFILE, PROFILES, get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck, ParsedFilename
from processing.resample_policy import ResampleDecision, analyze_resample
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions

TARGET = (94.0, 54.0)
DPI = (300.0, 300.0)


def _file_check(actual: tuple[float, float]) -> FileCheck:
    check = FileCheck(path=Path("1_face.tif"))
    check.parsed = SimpleNamespace(
        width_mm=90.0, height_mm=50.0, front_colors=4, back_colors=4, side="face"
    )
    check.actual_width_mm, check.actual_height_mm = actual
    check.dpi = check.dpi_x = check.dpi_y = 300.0
    check.colorspace = "CMYK"
    return check


def _validate(actual: tuple[float, float], policy: str = "auto", profile=DEFAULT_PROFILE) -> FileCheck:
    processor = BatchProcessor(
        Path("in"), Path("out"), profile=profile, correction_policy=policy
    )
    check = _file_check(actual)
    processor._validate_file(check)
    return check


# --- политика и профиль -------------------------------------------------


def test_default_limit_is_four_mm_for_every_profile():
    for profile in PROFILES.values():
        assert profile.auto_crop_mm == 4.0
        assert profile.confirm_crop_mm >= profile.auto_crop_mm


def test_crop_up_to_limit_is_auto_corrected_and_above_is_rejected():
    inside = analyze_resample((94.0, 57.5), TARGET, DPI)
    outside = analyze_resample((94.0, 58.5), TARGET, DPI)

    assert inside.decision == ResampleDecision.AUTO_CORRECT
    assert inside.crop_mm[1] == pytest.approx(3.5)
    assert outside.decision == ResampleDecision.REJECT
    assert "предел коррекции 4.0 мм" in outside.reason


def test_with_correction_limit_sets_both_thresholds():
    profile = get_profile("digital").with_correction_limit(1.5)

    assert profile.auto_crop_mm == 1.5
    assert profile.confirm_crop_mm == 1.5
    assert get_profile("digital").auto_crop_mm == 4.0  # исходный профиль не изменён
    plan = analyze_resample(
        (94.0, 56.0), TARGET, DPI,
        auto_crop_mm=profile.auto_crop_mm, confirm_crop_mm=profile.confirm_crop_mm,
    )
    assert plan.decision == ResampleDecision.REJECT


def test_correction_never_stretches_axes_independently():
    plan = analyze_resample((94.0, 57.5), TARGET, DPI)

    # одинаковый масштаб по обеим осям, лишнее убирается обрезкой
    scaled = (94.0 * plan.scale, 57.5 * plan.scale)
    assert scaled[0] == pytest.approx(94.0)
    assert scaled[1] - plan.crop_mm[1] == pytest.approx(54.0)


# --- настройки ----------------------------------------------------------


def test_settings_default_limit_is_four_mm():
    with patch.dict(os.environ, {"IMAGE_MAGIC_AUTO_CORRECT_MAX_MM": ""}):
        assert Settings.from_env().auto_correct_max_mm == 4.0


def test_settings_read_limit_from_environment():
    with patch.dict(os.environ, {"IMAGE_MAGIC_AUTO_CORRECT_MAX_MM": "2,5"}):
        assert Settings.from_env().auto_correct_max_mm == 2.5


@pytest.mark.parametrize("value", ["abc", "-1", "nan"])
def test_settings_reject_invalid_limit(value):
    with patch.dict(os.environ, {"IMAGE_MAGIC_AUTO_CORRECT_MAX_MM": value}):
        with pytest.raises(ValueError, match="IMAGE_MAGIC_AUTO_CORRECT_MAX_MM"):
            Settings.from_env()


# --- режимы выбора ------------------------------------------------------


def test_auto_mode_applies_small_correction_without_asking():
    check = _validate((94.0, 57.5), "auto")

    assert check.resample_decision == "auto_correct"
    assert check.needs_resample is True
    assert check.passed


@pytest.mark.parametrize("policy", ["ask", "reject"])
def test_ask_and_reject_modes_put_even_small_correction_into_confirmation(policy):
    check = _validate((94.0, 54.3), policy)

    assert check.resample_decision == "ask_confirmation"
    assert check.needs_resample is False
    assert check.resample_target_mm is not None
    assert not check.passed
    assert any("требуется подтверждение" in warning for warning in check.warnings)


def test_ask_mode_confirmation_then_applies_correction():
    check = _validate((94.0, 57.5), "ask")

    BatchProcessor.confirm_resample(check, True)

    assert check.resample_decision == "auto_correct"
    assert check.needs_resample is True


def test_ask_mode_still_accepts_metadata_noise_and_rejects_above_limit():
    assert _validate((94.0, 54.05), "ask").resample_decision == "accept"
    rejected = _validate((94.0, 59.0), "ask")
    assert rejected.resample_decision == "reject"
    assert any("предел коррекции" in error for error in rejected.errors)


def test_custom_profile_limit_is_used_by_processor():
    profile = get_profile("digital").with_correction_limit(1.0)

    assert _validate((94.0, 57.5), "auto", profile).resample_decision == "reject"
    assert _validate((94.0, 54.8), "auto", profile).resample_decision == "auto_correct"


# --- адаптер ------------------------------------------------------------


class _CapturingProcessor:
    def __init__(self, *_args, **kwargs):
        self.kwargs = kwargs
        self.on_orders_grouped = None


def _adapter(tmp_path: Path, **options) -> BatchProcessorAdapter:
    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital", **options),
        processor_factory=_CapturingProcessor,
    )


def test_adapter_passes_policy_and_limit_to_processor(tmp_path: Path):
    adapter = BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital", correction_policy="ask"),
        processor_factory=_CapturingProcessor,
        correction_limit_mm=2.5,
    )

    assert adapter.processor.kwargs["correction_policy"] == "ask"
    assert adapter.processor.kwargs["profile"].auto_crop_mm == 2.5
    assert adapter.processor.kwargs["profile"].confirm_crop_mm == 2.5


def test_adapter_approve_corrections_forces_auto_policy(tmp_path: Path):
    adapter = _adapter(tmp_path, approve_corrections=True, correction_policy="ask")

    assert adapter.processor.kwargs["correction_policy"] == "auto"


def test_adapter_without_limit_uses_profile_default(tmp_path: Path):
    adapter = _adapter(tmp_path)

    assert adapter.processor.kwargs["profile"].auto_crop_mm == 4.0


# --- PDF ----------------------------------------------------------------


def _inspect_pdf(width_mm: float, height_mm: float) -> FileCheck:
    path = Path("job_(90x50)_4-0_(1-1).pdf")
    page = SimpleNamespace(
        page_number=1,
        width_mm=width_mm,
        height_mm=height_mm,
        rotation=0,
        images=(),
        content_type="vector",
    )
    inspection = SimpleNamespace(page_count=1, pages=(page,), errors=(), warnings=())
    check = FileCheck(path=path, parsed=ParsedFilename("1", "1", 90.0, 50.0, 4, 0, ""))
    processor = BatchProcessor(Path("in"), Path("out"))
    with patch("processing.batch_processor.inspect_pdf", return_value=inspection), patch.object(
        Path, "stat", return_value=SimpleNamespace(st_size=1024)
    ):
        processor._inspect_pdf_file(check)
    return check


def test_pdf_with_wrong_size_is_rejected_with_actionable_russian_message():
    check = _inspect_pdf(98.0, 54.0)  # ожидается 94x54, отклонение 4 мм

    assert check.resample_decision == "accept"  # PDF не масштабируется и не обрезается
    assert check.needs_resample is False
    message = next(error for error in check.errors if "PDF страница 1" in error)
    assert "Автоматическая коррекция размера для PDF не выполняется" in message
    assert "Коррекция размера макета" in message
    assert "94.0x54.0 мм" in message


def test_pdf_within_tolerance_passes_with_visible_warning():
    check = _inspect_pdf(94.3, 54.0)

    assert not check.errors
    assert any("принято без коррекции" in warning for warning in check.warnings)


def test_pdf_exact_size_has_no_size_warning():
    check = _inspect_pdf(94.0, 54.0)

    assert not check.errors
    assert not any("принято без коррекции" in warning for warning in check.warnings)
