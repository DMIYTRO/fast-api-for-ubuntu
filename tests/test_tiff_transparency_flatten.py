"""Несведённая прозрачность TIFF сводится на белый фон ImageMagick'ом (заказ 26264275)."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from core.inspector import inspect_tiff_structure
from core.tiff_flatten import flatten_tiff_transparency
from processing.batch_processor import build_raster_source_pdf

pytestmark = pytest.mark.skipif(not shutil.which("magick"), reason="ImageMagick is required")


def _cmyk_with_transparency(path: Path) -> None:
    """CMYK 300 ppi: левая половина — непрозрачная синяя, правая — прозрачная красная."""
    subprocess.run([
        "magick", "-size", "200x100", "xc:cmyk(100%,50%,0%,0%)",
        "(", "-size", "200x100", "xc:cmyk(0%,100%,100%,0%)", ")",
        "-colorspace", "CMYK", "-delete", "1",
        "(", "-size", "100x100", "xc:white", "-size", "100x100", "xc:black", "+append", ")",
        "-alpha", "off", "-compose", "CopyOpacity", "-composite",
        "-units", "PixelsPerInch", "-density", "300", "-define", "tiff:alpha=unassociated",
        str(path),
    ], check=True)


def _pixel(path: Path, x: int, y: int) -> str:
    return subprocess.run(
        ["magick", str(path), "-format", f"%[pixel:p{{{x},{y}}}]", "info:"],
        check=True, capture_output=True, text=True,
    ).stdout


def test_transparency_goes_to_cmyk_white_keeping_colour_and_resolution(tmp_path):
    source = tmp_path / "art-face.tif"
    _cmyk_with_transparency(source)
    assert inspect_tiff_structure(str(source)).has_alpha
    before = source.read_bytes()

    target = tmp_path / "flat.tif"
    flatten_tiff_transparency(source, target, "CMYK")

    structure = inspect_tiff_structure(str(target))
    assert not structure.has_alpha and structure.channels.split()[0] == "cmyk"
    assert source.read_bytes() == before  # исходник не трогается
    assert _pixel(target, 150, 50) == "cmyk(0,0,0,0)"  # прозрачное → белая бумага
    assert _pixel(target, 20, 50) == _pixel(source, 20, 50).replace("cmyka", "cmyk").rsplit(",", 1)[0] + ")"
    density = subprocess.run(
        ["magick", "identify", "-format", "%x %U", str(target)],
        check=True, capture_output=True, text=True,
    ).stdout
    assert density.startswith("300") and "PixelsPerInch" in density


def test_refuses_to_overwrite_the_source(tmp_path):
    source = tmp_path / "a.tif"
    _cmyk_with_transparency(source)
    with pytest.raises(ValueError):
        flatten_tiff_transparency(source, source, "CMYK")


def test_raster_pdf_is_built_from_the_flattened_copy(tmp_path):
    source = tmp_path / "24_NP_Mlam10_170_mel_(17x8)_4-4_L1_T1_(1-2)-face.tif"
    _cmyk_with_transparency(source)

    check = build_raster_source_pdf(source, tmp_path / "out.pdf")

    assert check.transparency_flattened
    assert (tmp_path / "out.pdf").is_file()
    assert inspect_tiff_structure(str(source)).has_alpha  # исходник с прозрачностью как был
