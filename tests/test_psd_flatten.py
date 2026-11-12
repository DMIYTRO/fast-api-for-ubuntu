"""Regression tests for the standalone PSD inspection/flatten prototype.

Fixtures are generated at runtime with ImageMagick.  ImageMagick cannot write
the Photoshop "hidden" flag itself, so one fixture flips that flag byte in the
layer record it wrote (``8BIM`` + blend key, opacity, clipping, flags).
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from core.psd_flatten import (
    STRATEGY_COMPOSITE,
    STRATEGY_MERGE_VISIBLE,
    PsdError,
    flatten_psd,
    inspect_psd,
)

MAGICK = shutil.which("magick")
pytestmark = pytest.mark.skipif(MAGICK is None, reason="requires ImageMagick `magick`")


def _magick(*args: str) -> str:
    return subprocess.run([MAGICK, *args], check=True, capture_output=True, text=True).stdout


def _pixel(path, x: int, y: int) -> str:
    return _magick(str(path), "-format", f"%[pixel:p{{{x},{y}}}]", "info:").strip()


def _write_cmyk_psd(path, *, hidden_layer_index: int | None = None) -> None:
    """200x100 CMYK canvas, cyan 100x50 at +20+20, magenta 60x60 at +120+20."""
    _magick(
        "-size", "200x100", "xc:cmyk(0,0,0,0)", "-colorspace", "CMYK", "-set", "label", "bg",
        "(", "-size", "100x50", "xc:cmyk(255,0,0,0)", "-colorspace", "CMYK",
        "-set", "label", "cyan", "-repage", "+20+20", ")",
        "(", "-size", "60x60", "xc:cmyk(0,255,0,0)", "-colorspace", "CMYK",
        "-set", "label", "magenta", "-repage", "+120+20", ")",
        "-units", "PixelsPerInch", "-density", "300", "-depth", "8", str(path),
    )
    if hidden_layer_index is not None:
        data = bytearray(path.read_bytes())
        records = [m.start() for m in re.finditer(rb"8BIM(norm|mron)", bytes(data))]
        flags_offset = records[hidden_layer_index] + 4 + 4 + 1 + 1
        data[flags_offset] |= 0x02
        path.write_bytes(data)


def test_inspect_reports_layers_colorspace_dpi_and_no_alpha(tmp_path):
    psd = tmp_path / "1234_1_90x50_face.psd"
    _write_cmyk_psd(psd)

    structure = inspect_psd(psd)

    assert (structure.width_px, structure.height_px) == (200, 100)
    assert structure.colorspace == "CMYK"
    assert structure.dpi == 300.0
    assert structure.depth_bits == 8
    assert structure.has_alpha is False
    assert structure.layer_count == 2
    assert structure.visible_layer_count == 2
    assert structure.hidden_layer_count == 0
    assert [layer.name for layer in structure.layers] == ["cyan", "magenta"]
    assert structure.has_merged_composite is None  # not written by Photoshop
    assert structure.warnings == ()


def test_hidden_layer_is_counted_and_skipped_by_flatten(tmp_path):
    psd = tmp_path / "hidden.psd"
    _write_cmyk_psd(psd, hidden_layer_index=1)

    structure = inspect_psd(psd)
    assert structure.hidden_layer_count == 1
    assert [layer.visible for layer in structure.layers] == [True, False]
    assert structure.layers[1].compose == "None"

    result = flatten_psd(psd, tmp_path / "flat.tif")

    assert result.strategy == STRATEGY_MERGE_VISIBLE
    assert result.hidden_layers_skipped == 1
    assert result.colorspace == "CMYK"
    assert result.dpi == 300.0
    assert result.has_alpha is False
    assert (result.width_px, result.height_px) == (200, 100)
    assert _pixel(result.target, 70, 45) == "cmyk(255,0,0,0)"  # visible cyan layer
    assert _pixel(result.target, 150, 50) == "cmyk(0,0,0,0)"  # hidden magenta skipped


def test_flatten_keeps_visible_layers_and_colorspace(tmp_path):
    psd = tmp_path / "visible.psd"
    _write_cmyk_psd(psd)

    result = flatten_psd(psd, tmp_path / "out" / "visible.tif")

    assert _pixel(result.target, 70, 45) == "cmyk(255,0,0,0)"
    assert _pixel(result.target, 150, 50) == "cmyk(0,255,0,0)"
    assert _pixel(result.target, 5, 5) == "cmyk(0,0,0,0)"
    info = _magick(str(result.target), "-format", "%m %[colorspace] %[resolution.x] %[units]", "info:")
    assert info == "TIFF CMYK 300 PixelsPerInch"


def test_composite_strategy_copies_scene_zero_only(tmp_path):
    psd = tmp_path / "composite.psd"
    _write_cmyk_psd(psd)

    result = flatten_psd(psd, tmp_path / "comp.tif", strategy=STRATEGY_COMPOSITE)

    # ImageMagick stores the first image as the merged composite: layers are not re-rendered.
    assert result.strategy == STRATEGY_COMPOSITE
    assert _pixel(result.target, 70, 45) == "cmyk(0,0,0,0)"


def test_rgb_psd_with_alpha_layer_reports_alpha_without_conversion(tmp_path):
    psd = tmp_path / "rgba.psd"
    _magick(
        "-size", "120x80", "xc:#1040c0", "-set", "label", "bg",
        "(", "-size", "40x40", "xc:#ff0000", "-alpha", "set", "-channel", "A",
        "-evaluate", "set", "50%", "+channel", "-set", "label", "half", "-repage", "+10+10", ")",
        "-units", "PixelsPerInch", "-density", "150", "-depth", "8", str(psd),
    )

    structure = inspect_psd(psd)
    assert structure.colorspace == "sRGB"
    assert structure.dpi == 150.0
    assert structure.has_alpha is False
    assert structure.layers[0].has_alpha is True

    result = flatten_psd(psd, tmp_path / "rgba.tif")
    assert result.colorspace == "sRGB"
    assert result.has_alpha is False
    assert result.dpi == 150.0


def test_non_psd_and_missing_files_are_rejected(tmp_path):
    bogus = tmp_path / "bogus.psd"
    bogus.write_bytes(b"not a photoshop file at all" * 4)
    with pytest.raises(PsdError):
        inspect_psd(bogus)
    with pytest.raises(FileNotFoundError):
        inspect_psd(tmp_path / "missing.psd")
    _write_cmyk_psd(tmp_path / "ok.psd")
    with pytest.raises(ValueError):
        flatten_psd(tmp_path / "ok.psd", tmp_path / "out.png")


def test_16bit_rgb_and_embedded_icc_are_preserved(tmp_path):
    from pathlib import Path
    profile = Path('/usr/share/color/icc/colord/ECI-RGBv2.icc')
    if not profile.is_file():
        pytest.skip('optional system RGB ICC profile unavailable')
    psd = tmp_path / '16bit.psd'
    _magick('-size', '20x20', 'gradient:red-blue', '-depth', '16',
            '-profile', str(profile), '-units', 'PixelsPerInch', '-density', '300', str(psd))
    structure = inspect_psd(psd)
    assert structure.depth_bits == 16 and structure.has_icc_profile
    result = flatten_psd(psd, tmp_path / '16bit.tif')
    assert _magick(str(result.target), '-format', '%z', 'info:') == '16'
    before = subprocess.run([MAGICK, f'{psd}[0]', 'icc:-'], check=True, capture_output=True).stdout
    after = subprocess.run([MAGICK, result.target, 'icc:-'], check=True, capture_output=True).stdout
    assert before and before == after
    assert any('Экспериментальное' in message for message in result.warnings)
