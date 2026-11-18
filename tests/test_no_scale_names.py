"""Метки «без масштабирования» в имени ((SRA3P), (SRA3), (BAN)): растровый путь.

Такой файл никогда не масштабируется: размер приводится только белыми полями (холст
больше) или центральной обрезкой (холст меньше) в исходном разрешении. PDF-часть —
в ``tests/test_no_scale_pdf.py``.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest

from config.profiles import DEFAULT_NO_SCALE_NAME_MARKERS, DEFAULT_PROFILE, get_profile
from core.resampler import canvas_image
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, ParsedFilename
from processing.resample_policy import (
    ResampleDecision,
    analyze_canvas,
    canvas_margins_mm,
)
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions

MAGICK = shutil.which("magick")
needs_magick = pytest.mark.skipif(not MAGICK, reason="requires ImageMagick")

TARGET = (438.0, 318.0)  # 434x314 + вылеты 2 мм ((BAN): эталона нет, формат из имени)
DPI = (300.0, 300.0)
LIMITS = dict(min_dpi=250.0, metadata_tolerance_mm=0.1, auto_crop_mm=4.0, confirm_crop_mm=10.0)
MARKED = "28_NP(K)_(BAN)_Glam10_450_mel_(434x314)_4-0_L5_T1_(3773-26162382)_1-face.tif"
UNMARKED = "06_NP(K)_Glam10_450_mel_(434x314)_4-0_L5_T1_(3773-26162382)_1-face.tif"


def _canvas(actual, target=TARGET, dpi=DPI, **limits):
    return analyze_canvas(actual, target, dpi, **{**LIMITS, **limits})


# --- профиль ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        MARKED,
        "06_NP(SRA3P)_(PPP)_Glam10_(471x324)_4-0_1-face.tif",
        "28_NP(K)_(BAN)_Bezlam_250_mel_Banner_(666x302)_4-4_1-back.pdf",
        "28_np(k)_(ban)_banner_(666x302)_4-4_1-face.tif",
    ],
)
def test_profile_detects_marker_case_insensitively(name):
    assert DEFAULT_PROFILE.is_no_scale_name(name)


@pytest.mark.parametrize("name", [UNMARKED, "06_SRA3_(434x314)_4-0_1-face.tif", "BANNER_(666x302)_4-4.tif"])
def test_profile_ignores_names_without_bracketed_marker(name):
    assert not DEFAULT_PROFILE.is_no_scale_name(name)


def test_with_no_scale_markers_replaces_and_disables():
    profile = DEFAULT_PROFILE.with_no_scale_markers([" (XL) ", "", "(xl)"])
    assert profile.no_scale_name_markers == ("(XL)",)
    assert profile.is_no_scale_name("a_(xl)_b.tif")
    assert not profile.is_no_scale_name(MARKED)
    assert not DEFAULT_PROFILE.with_no_scale_markers(()).is_no_scale_name(MARKED)


# --- политика analyze_canvas ---------------------------------------------


def test_canvas_pad_is_automatic_without_scaling():
    plan = _canvas((434.0, 314.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.scale == 1.0
    assert plan.rotation_degrees == 0
    assert plan.crop_mm == (0.0, 0.0)
    assert plan.effective_dpi == DPI
    assert "без масштабирования: белые поля 4.00x4.00 мм" in plan.reason
    assert canvas_margins_mm((434.0, 314.0), TARGET) == (4.0, 4.0)


def test_canvas_crop_is_total_per_dimension():
    plan = _canvas((441.0, 318.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.crop_mm == pytest.approx((3.0, 0.0))
    assert "обрезка 3.00x0.00 мм" in plan.reason
    assert "белые поля" not in plan.reason


def test_canvas_mixed_pad_and_crop():
    plan = _canvas((440.0, 315.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.crop_mm == pytest.approx((2.0, 0.0))
    assert canvas_margins_mm((440.0, 315.0), TARGET) == pytest.approx((0.0, 3.0))
    assert "белые поля 0.00x3.00 мм" in plan.reason
    assert "обрезка 2.00x0.00 мм" in plan.reason


def test_canvas_accepts_metadata_noise():
    plan = _canvas((438.05, 317.95))
    assert plan.decision == ResampleDecision.ACCEPT


def test_canvas_between_limits_needs_confirmation_and_above_is_rejected():
    ask = _canvas((430.0, 318.0))  # 8 мм полей
    assert ask.decision == ResampleDecision.ASK_CONFIRMATION
    assert "подтверждение" in ask.reason
    rejected = _canvas((426.0, 318.0))  # 12 мм полей
    assert rejected.decision == ResampleDecision.REJECT
    assert "пришлите макет нужного размера" in rejected.reason
    assert "максимум коррекции 10.0 мм" in rejected.reason


def test_canvas_has_no_shrink_rule_and_ignores_low_dpi():
    # Макет больше формата на 3 мм: пропорциональное уменьшение потребовало бы
    # подтверждения (> shrink_auto_mm), холст просто обрезает.
    assert _canvas((441.0, 321.0)).decision == ResampleDecision.AUTO_CORRECT
    # Низкое DPI решения по размеру не меняет (о нём сообщает правило метаданных).
    low = _canvas((434.0, 314.0), dpi=(150.0, 150.0))
    assert low.decision == ResampleDecision.AUTO_CORRECT
    assert low.effective_dpi == (150.0, 150.0)


def test_canvas_accepts_vector_dpi():
    plan = _canvas((436.0, 316.0), dpi=(math.inf, math.inf))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.effective_dpi == (math.inf, math.inf)


# --- BatchProcessor: проверка растра -------------------------------------


def _file_check(name: str, actual, dpi=300.0) -> FileCheck:
    check = FileCheck(path=Path(name))
    check.parsed = SimpleNamespace(
        width_mm=434.0, height_mm=314.0, front_colors=4, back_colors=0, side="face"
    )
    check.actual_width_mm, check.actual_height_mm = actual
    check.dpi = check.dpi_x = check.dpi_y = dpi
    check.colorspace = "CMYK"
    return check


def _validate(name: str, actual, policy: str = "auto", profile=DEFAULT_PROFILE, dpi=300.0) -> FileCheck:
    processor = BatchProcessor(Path("in"), Path("out"), profile=profile, correction_policy=policy)
    check = _file_check(name, actual, dpi)
    processor._validate_file(check)
    return check


def test_marked_raster_gets_canvas_size_fix_without_scaling():
    check = _validate(MARKED, (436.0, 314.0))

    assert check.no_scale is True
    assert check.resample_decision == "auto_correct"
    assert check.needs_resample is True
    assert check.resample_scale == 1.0
    assert check.rotation_degrees == 0
    assert check.resample_target_mm == TARGET
    assert check.resample_effective_dpi == DPI
    assert check.size_fix["method"] == "canvas"
    assert check.size_fix["auto"] is True
    assert check.size_fix["detail"] == (
        "436.0x314.0 → 438.0x318.0 мм: без масштабирования, белые поля 2.00x4.00 мм"
    )
    assert not check.warnings
    assert check.passed


def test_marked_raster_with_8mm_gap_needs_confirmation_like_crop_limits():
    # 8 мм полей по высоте — выше предела авто 4 мм.
    check = _validate(MARKED, (438.0, 310.0))
    assert check.resample_decision == "ask_confirmation"
    assert any("белые поля 0.00x8.00 мм" in warning for warning in check.warnings)


def test_unmarked_raster_with_same_geometry_still_uses_cover_scaling():
    marked = _validate(MARKED, (436.0, 316.0))
    unmarked = _validate(UNMARKED, (436.0, 316.0))

    assert marked.size_fix["method"] == "canvas"
    assert marked.resample_scale == 1.0
    assert unmarked.no_scale is False
    assert unmarked.size_fix["method"] == "fill"
    assert unmarked.resample_scale > 1.0


def test_marked_raster_is_cropped_instead_of_shrunk():
    marked = _validate(MARKED, (441.0, 321.0))
    unmarked = _validate(UNMARKED, (441.0, 321.0))

    # Без метки — уменьшение на 3 мм (> 2 мм) требует подтверждения.
    assert unmarked.resample_decision == "ask_confirmation"
    assert marked.resample_decision == "auto_correct"
    assert marked.resample_crop_mm == pytest.approx((3.0, 3.0))
    assert marked.size_fix["method"] == "canvas"
    assert "обрезка 3.00x3.00 мм" in marked.size_fix["detail"]


def test_marked_raster_picks_the_orientation_with_the_smaller_change():
    check = _validate(MARKED, (316.0, 436.0))
    assert check.resample_target_mm == (318.0, 438.0)
    assert check.rotation_degrees == 0
    assert "белые поля 2.00x2.00 мм" in check.size_fix["detail"]


def test_marked_raster_low_dpi_is_reported_by_metadata_rule_not_size():
    check = _validate(MARKED, (434.0, 314.0), dpi=150.0)
    assert check.resample_decision == "auto_correct"
    assert check.size_fix["method"] == "canvas"
    assert any("DPI" in error for error in check.errors)


def test_disabled_markers_fall_back_to_cover_scaling():
    profile = get_profile("digital").with_no_scale_markers(())
    check = _validate(MARKED, (436.0, 316.0), profile=profile)
    assert check.no_scale is False
    assert check.size_fix["method"] == "fill"


@pytest.mark.parametrize("policy", ["ask", "reject"])
def test_ask_modes_turn_canvas_auto_into_confirmation(policy):
    check = _validate(MARKED, (434.0, 314.0), policy)

    assert check.resample_decision == "ask_confirmation"
    assert check.needs_resample is False
    assert check.size_fix is None
    assert "без масштабирования" in check.resample_reason
    assert any(
        "требуется подтверждение коррекции" in warning and "белые поля 4.00x4.00 мм" in warning
        for warning in check.warnings
    )


def test_confirm_resample_produces_confirmed_canvas_size_fix():
    check = _validate(MARKED, (434.0, 310.0), "auto")  # 8 мм полей: выше авто 4 мм
    assert check.resample_decision == "ask_confirmation"

    BatchProcessor.confirm_resample(check, True)

    assert check.size_fix["detail"] == (
        "434.0x310.0 → 438.0x318.0 мм: без масштабирования, белые поля 4.00x8.00 мм; "
        "подтверждено оператором"
    )

    assert check.resample_decision == "auto_correct"
    assert check.needs_resample is True
    assert check.size_fix["method"] == "canvas"
    assert check.size_fix["auto"] is False
    assert check.size_fix["detail"].endswith("; подтверждено оператором")
    assert "без масштабирования" in check.size_fix["detail"]


def test_marked_raster_accepted_size_keeps_high_dpi_downsample():
    check = _validate(MARKED, (438.0, 318.0), dpi=600.0)
    assert check.resample_decision == "accept"
    assert check.size_fix is None
    assert check.downsampled_from_dpi == 600.0


# --- настройки и адаптер -------------------------------------------------


def test_settings_default_markers_when_unset():
    env = {key: value for key, value in os.environ.items() if key != "IMAGE_MAGIC_NO_SCALE_NAME_MARKERS"}
    with patch.dict(os.environ, env, clear=True):
        assert Settings.from_env().no_scale_name_markers == DEFAULT_NO_SCALE_NAME_MARKERS


def test_settings_parse_markers_and_empty_disables():
    with patch.dict(os.environ, {"IMAGE_MAGIC_NO_SCALE_NAME_MARKERS": " (BAN), ,(XL),(ban)"}):
        assert Settings.from_env().no_scale_name_markers == ("(BAN)", "(XL)")
    with patch.dict(os.environ, {"IMAGE_MAGIC_NO_SCALE_NAME_MARKERS": ""}):
        assert Settings.from_env().no_scale_name_markers == ()


class _CapturingProcessor:
    def __init__(self, *_args, **kwargs):
        self.kwargs = kwargs
        self.on_orders_grouped = None


def test_adapter_applies_markers_to_profile(tmp_path: Path):
    def adapter(**kwargs):
        return BatchProcessorAdapter(
            ProcessingOptions(str(tmp_path), "digital"),
            processor_factory=_CapturingProcessor,
            **kwargs,
        ).processor.kwargs["profile"]

    assert adapter().no_scale_name_markers == DEFAULT_NO_SCALE_NAME_MARKERS
    assert adapter(no_scale_name_markers=("(XL)",)).no_scale_name_markers == ("(XL)",)
    assert adapter(no_scale_name_markers=()).no_scale_name_markers == ()


# --- ImageMagick: canvas_image -------------------------------------------


def _magick(*args) -> str:
    return subprocess.run(
        [MAGICK, *map(str, args)], check=True, capture_output=True, text=True
    ).stdout.strip()


def _make(path: Path, color: str, colorspace: str, size: str = "120x60", dpi: int = 300) -> Path:
    _magick(
        "-size", size, f"xc:{color}", "-colorspace", colorspace,
        "-units", "PixelsPerInch", "-density", dpi, path,
    )
    return path


def _mm(px: int, dpi: float) -> float:
    return px * 25.4 / dpi


@needs_magick
@pytest.mark.parametrize(
    ("colorspace", "color", "white", "colored"),
    [
        ("CMYK", "red", "cmyk(0,0,0,0)", "cmyk(0,255,255,0)"),
        ("sRGB", "red", "srgb(255,255,255)", "srgb(255,0,0)"),
        ("Gray", "black", "gray(255)", "gray(0)"),
    ],
)
def test_canvas_image_pads_with_white_in_source_pixels(tmp_path, colorspace, color, white, colored):
    source = _make(tmp_path / "src.tif", color, colorspace)
    output = tmp_path / "out.tif"

    canvas_image(str(source), str(output), _mm(140, 300), _mm(80, 300), (300.0, 300.0))

    info = _magick(output, "-format", "%[colorspace] %wx%h %x %y %[units]", "info:")
    assert info == f"{colorspace} 140x80 300 300 PixelsPerInch"
    assert _magick(output, "-format", "%[pixel:p{0,0}]", "info:") == white
    assert _magick(output, "-format", "%[pixel:p{139,79}]", "info:") == white
    # Макет по центру, не растянут: 120x60 начинается с (10,10).
    assert _magick(output, "-format", "%[pixel:p{10,10}]", "info:") == colored
    assert _magick(output, "-format", "%[pixel:p{9,10}]", "info:") == white
    assert _magick(output, "-format", "%[pixel:p{129,69}]", "info:") == colored


@needs_magick
def test_canvas_image_crop_keeps_center(tmp_path):
    source = tmp_path / "src.tif"
    # Синие края 10 px вокруг красного центра 100x40.
    _magick(
        "-size", "120x60", "xc:blue", "-fill", "red", "-draw", "rectangle 10,10 109,49",
        "-alpha", "off", "-colorspace", "CMYK", "-units", "PixelsPerInch", "-density", 300, source,
    )
    output = tmp_path / "out.tif"

    canvas_image(str(source), str(output), _mm(100, 300), _mm(40, 300), (300.0, 300.0))

    assert _magick(output, "-format", "%wx%h", "info:") == "100x40"
    assert _magick(output, "-format", "%[pixel:p{0,0}]", "info:") == "cmyk(0,255,255,0)"
    assert _magick(output, "-format", "%[pixel:p{99,39}]", "info:") == "cmyk(0,255,255,0)"


@needs_magick
def test_canvas_image_keeps_anisotropic_source_dpi(tmp_path):
    source = tmp_path / "src.tif"
    _magick(
        "-size", "120x60", "xc:red", "-colorspace", "CMYK",
        "-units", "PixelsPerInch", "-density", "300x150", source,
    )
    output = tmp_path / "out.tif"

    canvas_image(str(source), str(output), _mm(130, 300), _mm(70, 150), (300.0, 150.0))

    assert _magick(output, "-format", "%wx%h %x %y", "info:") == "130x70 300 150"


@needs_magick
def test_canvas_image_requires_known_dpi(tmp_path):
    source = _make(tmp_path / "src.tif", "red", "CMYK")
    with pytest.raises(ValueError, match="DPI"):
        canvas_image(str(source), str(tmp_path / "out.tif"), 10.0, 10.0, (0.0, 300.0))


# --- сборка PDF заказа ---------------------------------------------------


@needs_magick
def test_prepare_page_pdfs_uses_canvas_in_source_dpi(tmp_path):
    name = "06_NP(K)_(BAN)_Glam10_(23.4x10)_4-0_(1-1)_1-face.tif"
    source = _make(tmp_path / name, "red", "CMYK", size="300x180")  # 25.4x15.24 мм
    item = FileCheck(
        path=source,
        parsed=ParsedFilename("1", "1", 23.4, 10.0, 4, 0, "face"),
        actual_width_mm=25.4,
        actual_height_mm=15.24,
        dpi=300.0,
        dpi_x=300.0,
        dpi_y=300.0,
        colorspace="CMYK",
    )
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", correction_policy="auto")
    processor._validate_raster_size(item)
    assert item.size_fix["method"] == "canvas"
    assert item.resample_target_mm == pytest.approx((27.4, 14.0))

    work = tmp_path / "work"
    work.mkdir()
    with patch(
        "processing.batch_processor.resample_image",
        side_effect=AssertionError("файл с меткой не масштабируется"),
    ):
        outputs = processor._prepare_page_pdfs(processor._build_page_refs([item]), work)

    canvas = next(work.glob("canvas_1_*"))
    # 27.4 мм и 14 мм при 300 DPI: 324x165 px, разрешение исходника.
    assert _magick(canvas, "-format", "%wx%h %x", "info:") == "324x165 300"
    assert _magick(canvas, "-format", "%[pixel:p{0,82}]", "info:") == "cmyk(0,0,0,0)"
    assert _magick(canvas, "-format", "%[pixel:p{162,82}]", "info:") == "cmyk(0,255,255,0)"
    with pymupdf.open(outputs[0]) as document:
        rect = document[0].rect
    assert rect.width * 25.4 / 72 == pytest.approx(27.4, abs=0.1)
    assert rect.height * 25.4 / 72 == pytest.approx(14.0, abs=0.1)
    assert {
        "kind": "size_correction", "method": "canvas",
        "target_mm": [27.4, 14.0], "dpi": [300.0, 300.0],
    } in item.processing_actions
