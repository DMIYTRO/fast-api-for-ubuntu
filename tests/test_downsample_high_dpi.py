"""Автоматическое уменьшение растров с верным размером, но DPI выше IMAGE_MAGIC_MAX_DPI."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymupdf
import pytest

from config.profiles import DEFAULT_PROFILE, get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from tests.test_callas_batch_integration import FakeCallas

needs_magick = pytest.mark.skipif(not shutil.which("magick"), reason="нужен ImageMagick")


def _check(actual: tuple[float, float], dpi: float, product=(90.0, 50.0), suffix=".tif") -> FileCheck:
    check = FileCheck(path=Path(f"1_face{suffix}"))
    check.parsed = SimpleNamespace(
        width_mm=product[0], height_mm=product[1], front_colors=4, back_colors=4, side="face"
    )
    check.actual_width_mm, check.actual_height_mm = actual
    check.dpi = check.dpi_x = check.dpi_y = dpi
    check.colorspace = "CMYK"
    return check


def _validate(check: FileCheck, policy: str = "auto", profile=DEFAULT_PROFILE) -> FileCheck:
    BatchProcessor(Path("in"), Path("out"), profile=profile, correction_policy=policy)._validate_file(check)
    return check


# --- решение при валидации ------------------------------------------------


@pytest.mark.parametrize("policy", ["auto", "ask", "reject"])
def test_correct_size_with_1200_dpi_is_downsampled_regardless_of_policy(policy):
    check = _validate(_check((94.0, 54.0), 1200.0), policy)

    assert check.resample_decision == "accept"
    assert check.needs_resample is True
    assert check.resample_target_mm == (94.0, 54.0)
    assert check.downsampled_from_dpi == 1200.0
    assert check.errors == []
    assert check.passed
    assert "разрешение 1200 DPI уменьшено до 300 DPI (выше 500 DPI)" in check.warnings


def test_dpi_at_or_below_limit_is_kept_as_is():
    for dpi in (300.0, 450.0, 500.0):
        check = _validate(_check((94.0, 54.0), dpi))
        assert check.needs_resample is False
        assert check.downsampled_from_dpi is None
        assert not any("уменьшено" in w for w in check.warnings)


def test_either_axis_above_limit_triggers_downsample():
    check = _check((94.0, 54.0), 300.0)
    check.dpi_y = 800.0
    _validate(check)
    assert check.needs_resample and check.downsampled_from_dpi == 800.0


def test_disabled_profile_keeps_high_dpi():
    profile = DEFAULT_PROFILE.with_downsample(enabled=False)
    check = _validate(_check((94.0, 54.0), 1200.0), profile=profile)
    assert not check.needs_resample and check.downsampled_from_dpi is None


def test_custom_max_dpi_is_respected_and_never_below_target():
    profile = DEFAULT_PROFILE.with_downsample(max_dpi=1500.0)
    assert not _validate(_check((94.0, 54.0), 1200.0), profile=profile).needs_resample
    assert DEFAULT_PROFILE.with_downsample(max_dpi=100.0).max_dpi == DEFAULT_PROFILE.target_dpi
    assert get_profile("digital").max_dpi == 500.0  # исходный профиль не изменён


def test_pdf_inputs_are_out_of_scope():
    check = _validate(_check((94.0, 54.0), 1200.0, suffix=".pdf"))
    assert not check.needs_resample


def test_wrong_size_files_keep_their_own_correction_not_downsample_marker():
    # размер не совпадает (обрезка 2 мм) -> обычная автокоррекция, она и так идёт до 300 DPI
    check = _validate(_check((94.0, 56.0), 1200.0))
    assert check.resample_decision == "auto_correct"
    assert check.needs_resample and check.downsampled_from_dpi is None


# --- крупный размер / низкий DPI уже работает через AUTO_CORRECT ----------


def test_oversize_150_dpi_file_is_auto_corrected_to_300_dpi_effective():
    # 178x178 мм при 150 DPI, заказ 85x85 (+4 мм вылет = 89x89)
    check = _validate(_check((178.0, 178.0), 150.0, product=(85.0, 85.0)))

    assert check.resample_decision == "auto_correct"
    assert check.needs_resample
    assert check.resample_target_mm == (89.0, 89.0)
    assert check.resample_effective_dpi == pytest.approx((300.0, 300.0))
    assert check.errors == []
    assert check.downsampled_from_dpi is None


# --- настройки и адаптер --------------------------------------------------


def test_settings_defaults_and_overrides():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("IMAGE_MAGIC_MAX_DPI", None)
        os.environ.pop("IMAGE_MAGIC_DOWNSAMPLE_HIGH_DPI_ENABLED", None)
        settings = Settings.from_env()
    assert settings.max_dpi == 500.0 and settings.downsample_high_dpi_enabled is True

    with patch.dict(
        os.environ,
        {"IMAGE_MAGIC_MAX_DPI": "600,5", "IMAGE_MAGIC_DOWNSAMPLE_HIGH_DPI_ENABLED": "false"},
    ):
        settings = Settings.from_env()
    assert settings.max_dpi == 600.5 and settings.downsample_high_dpi_enabled is False


@pytest.mark.parametrize("bad", ["abc", "100", "-1", "nan", "inf"])
def test_settings_reject_invalid_max_dpi(bad):
    with patch.dict(os.environ, {"IMAGE_MAGIC_MAX_DPI": bad}):
        with pytest.raises(ValueError, match="IMAGE_MAGIC_MAX_DPI"):
            Settings.from_env()


def test_adapter_plumbs_options_into_processor_profile(tmp_path):
    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        max_dpi=700.0,
        downsample_high_dpi=False,
    )
    assert adapter.processor.profile.max_dpi == 700.0
    assert adapter.processor.profile.downsample_high_dpi is False


# --- реальная сборка PDF --------------------------------------------------


def _make_tiff(path: Path, size_px: str, dpi: int) -> None:
    subprocess.run(
        ["magick", "-size", size_px, "gradient:#b7c5d8-#203040", "-colorspace", "CMYK",
         "-units", "PixelsPerInch", "-density", str(dpi), str(path)],
        check=True, capture_output=True,
    )


def _page_image_pixels(pdf: Path) -> tuple[int, int]:
    with pymupdf.open(pdf) as document:
        info = document[0].get_image_info()
    return info[0]["width"], info[0]["height"]


@needs_magick
def test_pdf_is_built_from_downsampled_file_and_source_is_untouched(tmp_path):
    source = tmp_path / "T_(90x50)_4-0_(7-1)_face.tif"
    _make_tiff(source, "4441x2551", 1200)  # 94x54 мм при 1200 DPI
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    processor = BatchProcessor(
        tmp_path, tmp_path / "PDF", callas_client=FakeCallas(tmp_path / "share")
    )

    orders = processor.inspect_orders()
    item = orders[0].files[0]
    assert item.needs_resample and item.downsampled_from_dpi == pytest.approx(1200.0, abs=1)
    assert any("уменьшено до 300 DPI" in w for w in item.warnings)

    results = processor.create_pdfs(orders)

    assert results[0][2] is None, results[0][2]
    width_px, height_px = _page_image_pixels(results[0][1])
    assert (width_px, height_px) == (1110, 638)  # 94x54 мм при 300 DPI
    with pymupdf.open(results[0][1]) as document:
        assert document[0].rect.width == pytest.approx(94 * 72 / 25.4, abs=0.6)
    assert item.processing_actions[0]["kind"] == "dpi_downsample"
    assert item.processing_actions[0]["from_dpi"] == pytest.approx(1200.0, abs=1)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert subprocess.run(
        ["magick", "identify", "-format", "%w", str(source)], capture_output=True, text=True
    ).stdout == "4441"


@needs_magick
def test_oversize_low_dpi_file_builds_pdf_at_300_dpi(tmp_path):
    source = tmp_path / "T_(85x85)_4-0_(7-2)_face.tif"
    _make_tiff(source, "1051x1051", 150)  # 178x178 мм при 150 DPI
    processor = BatchProcessor(
        tmp_path, tmp_path / "PDF", callas_client=FakeCallas(tmp_path / "share")
    )

    orders = processor.inspect_orders()
    assert orders[0].files[0].resample_decision == "auto_correct"
    results = processor.create_pdfs(orders)

    assert results[0][2] is None, results[0][2]
    with pymupdf.open(results[0][1]) as document:
        assert document[0].rect.width == pytest.approx(89 * 72 / 25.4, abs=0.6)
        assert document[0].rect.height == pytest.approx(89 * 72 / 25.4, abs=0.6)
    assert _page_image_pixels(results[0][1]) == (1051, 1051)  # 89 мм при 300 DPI
