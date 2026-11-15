"""Cheap (-ping) inspection must agree with the old full-decode implementation.

The reference below is the previous implementation of ``core.inspector``
(full-decode ``magick identify``, one call per question).  Every fixture is a
real file generated with the local ``magick`` binary.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core import inspector
from core.inspector import (
    TiffStructure,
    count_frames,
    inspect_file,
    inspect_file_full,
    inspect_raster,
    inspect_tiff_structure,
)

MAGICK = shutil.which("magick")
pytestmark = pytest.mark.skipif(not MAGICK, reason="requires ImageMagick")

METADATA_FIELDS = (
    "file_name", "format", "width_px", "height_px", "dpi", "dpi_x", "dpi_y",
    "width_mm", "height_mm", "colorspace", "icc_profile", "image_type",
    "depth_bits", "size_mb",
)


def magick(*args) -> None:
    subprocess.run([MAGICK, *map(str, args)], check=True, capture_output=True)


def identify(*args) -> str:
    return subprocess.run(
        [MAGICK, "identify", *map(str, args)], check=True, capture_output=True, text=True
    ).stdout


# --- reference: the pre-optimisation behaviour -----------------------------

def reference_metadata(path: Path) -> dict:
    lines = [
        line.strip()
        for line in identify(
            "-format",
            "%f\n%m\n%w\n%h\n%x\n%y\n%[units]\n%[colorspace]\n%[type]\n%[depth]\n%[icc:description]",
            path,
        ).strip().split("\n")
    ]
    res_x, res_y = float(lines[4]), float(lines[5])
    if "Centimeter" in lines[6]:
        dpi_x, dpi_y = res_x * 2.54, res_y * 2.54
    else:
        dpi_x = res_x if res_x > 0 else 72.0
        dpi_y = res_y if res_y > 0 else 72.0
    w, h = float(lines[2]), float(lines[3])
    return {
        "file_name": lines[0],
        "format": lines[1],
        "width_px": int(w),
        "height_px": int(h),
        "dpi": round(min(dpi_x, dpi_y), 1),
        "dpi_x": round(dpi_x, 1),
        "dpi_y": round(dpi_y, 1),
        "width_mm": round((w / dpi_x) * 25.4, 1),
        "height_mm": round((h / dpi_y) * 25.4, 1),
        "colorspace": lines[7],
        "icc_profile": (lines[10] if len(lines) > 10 and lines[10] else "") or "Не внедрен",
        "image_type": lines[8],
        "depth_bits": lines[9],
        "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2),
    }


def reference_structure(path: Path) -> TiffStructure:
    composite = [
        line.strip()
        for line in identify("-define", "tiff:ignore-layers=true", "-format", "%p\t%[channels]\n", path).splitlines()
        if line.strip()
    ]
    layer_lines = [
        line for line in identify("-format", "%p\t%[tiff:has-layers]\n", path).splitlines() if line.strip()
    ]
    first = composite[0].split("\t", maxsplit=1)
    channels = first[1].strip() if len(first) == 2 else ""
    mnemonic = channels.split(maxsplit=1)[0].lower() if channels else ""
    has_layers = any(
        len(f := line.split("\t", maxsplit=1)) == 2 and f[1].strip().lower() in {"true", "1", "yes"}
        for line in layer_lines
    )
    layer_count = 0
    if has_layers:
        decoded = len(layer_lines) - len(composite)
        layer_count = decoded if decoded > 0 else None
    return TiffStructure(len(composite), has_layers, mnemonic.endswith("a"), channels, layer_count)


# --- fixtures ----------------------------------------------------------------

def _make_fixtures(root: Path) -> dict[str, Path]:
    make = {
        "rgb.tif": ["-size", "200x100", "xc:red"],
        "rgba.tif": ["-size", "200x100", "xc:red", "-alpha", "set", "-channel", "A", "-evaluate", "set", "50%", "+channel"],
        "cmyk.tif": ["-size", "200x100", "xc:red", "-colorspace", "CMYK"],
        "cmyka.tif": ["-size", "200x100", "xc:red", "-colorspace", "CMYK", "-alpha", "set",
                      "-channel", "A", "-evaluate", "set", "50%", "+channel"],
        "gray.tif": ["-size", "200x100", "xc:gray50", "-colorspace", "Gray"],
        "graya.tif": ["-size", "200x100", "xc:gray50", "-colorspace", "Gray", "-alpha", "set"],
        "multi.tif": ["-size", "20x20", "xc:cyan", "xc:magenta"],
        "cm.tif": ["-size", "200x100", "xc:red", "-units", "PixelsPerCentimeter", "-density", "118x236"],
        "undefined_units.tif": ["-size", "200x100", "xc:red", "-units", "Undefined", "-density", "150"],
        "dpi300.tif": ["-size", "200x100", "xc:red", "-units", "PixelsPerInch", "-density", "300x150"],
        "depth16.tif": ["-size", "200x100", "gradient:red-blue", "-depth", "16"],
        "lzw.tif": ["-size", "200x100", "plasma:red-blue", "-compress", "LZW"],
        "msb.tif": ["-size", "200x100", "xc:red", "-endian", "MSB"],
        "big.tif": ["-size", "200x100", "xc:red"],  # rewritten below as BigTIFF
        "bigcmyka.tif": ["-size", "200x100", "xc:red", "-colorspace", "CMYK", "-alpha", "set"],
        "layered.tif": [
            "-size", "100x80", "xc:cyan", "(", "-size", "100x80", "xc:cyan", ")",
            "(", "-size", "100x80", "xc:magenta", ")", "-colorspace", "sRGB", "-type", "TrueColor",
            "-depth", "8", "-units", "PixelsPerInch", "-density", "300", "-define", "tiff:write-layers=true",
        ],
        "photo.jpg": ["-size", "200x100", "xc:red", "-density", "300"],
        "cmyk.jpg": ["-size", "200x100", "xc:red", "-colorspace", "CMYK"],
        "gray.jpg": ["-size", "200x100", "xc:gray50", "-colorspace", "Gray"],
        "opaque.png": ["-size", "200x100", "xc:red", "-alpha", "off"],
        "alpha.png": ["-size", "200x100", "xc:red", "-alpha", "set", "-channel", "A", "-evaluate", "set", "50%", "+channel"],
        "cm.png": ["-size", "200x100", "xc:red", "-units", "PixelsPerCentimeter", "-density", "118x236"],
    }
    paths = {}
    for name, args in make.items():
        target = root / name
        if name.startswith("big"):
            magick(*args, f"TIFF64:{target}")
        else:
            magick(*args, target)
        paths[name] = target
    return paths


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    return _make_fixtures(tmp_path_factory.mktemp("inspector"))


ALL_NAMES = [
    "rgb.tif", "rgba.tif", "cmyk.tif", "cmyka.tif", "gray.tif", "graya.tif", "multi.tif",
    "cm.tif", "undefined_units.tif", "dpi300.tif", "depth16.tif", "lzw.tif", "msb.tif",
    "big.tif", "bigcmyka.tif", "layered.tif", "photo.jpg", "cmyk.jpg", "gray.jpg",
    "opaque.png", "alpha.png", "cm.png",
]
TIFF_NAMES = [name for name in ALL_NAMES if name.endswith(".tif")]


@pytest.mark.parametrize("name", ALL_NAMES)
def test_metadata_matches_the_full_decode_reference(fixtures, name):
    path = fixtures[name]
    reference = reference_metadata(path)

    metadata = inspect_raster(str(path)).metadata
    assert {field: getattr(metadata, field) for field in METADATA_FIELDS} == reference
    # inspect_file() alone is cheap too and agrees.
    assert {field: getattr(inspect_file(str(path)), field) for field in METADATA_FIELDS} == reference
    assert {field: getattr(inspect_file_full(str(path)), field) for field in METADATA_FIELDS} == reference


@pytest.mark.parametrize("name", TIFF_NAMES)
def test_tiff_structure_matches_the_full_decode_reference(fixtures, name):
    path = fixtures[name]
    expected = reference_structure(path)
    assert inspect_tiff_structure(str(path)) == expected
    raster = inspect_raster(str(path))
    assert raster.tiff == expected
    assert raster.frame_count == expected.page_count


def test_known_properties_of_the_fixtures(fixtures):
    """Absolute expectations, independent of the reference implementation."""
    structure = {name: inspect_tiff_structure(str(fixtures[name])) for name in TIFF_NAMES}
    assert [name for name, s in structure.items() if s.has_alpha] == [
        "rgba.tif", "cmyka.tif", "graya.tif", "bigcmyka.tif",
    ]
    assert structure["rgb.tif"].channels == "srgb  3.0"
    assert structure["rgba.tif"].channels == "srgba 4.0"
    assert structure["cmyk.tif"].channels == "cmyk  4.0"
    assert structure["cmyka.tif"].channels == "cmyka 5.0"
    assert structure["multi.tif"].page_count == 2
    layered = structure["layered.tif"]
    assert (layered.page_count, layered.has_unflattened_layers, layered.layer_count) == (1, True, 2)
    assert not any(s.has_unflattened_layers for n, s in structure.items() if n != "layered.tif")

    cm = inspect_raster(str(fixtures["cm.tif"])).metadata
    assert (cm.dpi_x, cm.dpi_y) == (round(118 * 2.54, 1), round(236 * 2.54, 1))
    metadata = inspect_raster(str(fixtures["dpi300.tif"])).metadata
    assert (metadata.dpi_x, metadata.dpi_y, metadata.dpi) == (300.0, 150.0, 150.0)
    assert inspect_raster(str(fixtures["cmyk.tif"])).metadata.colorspace == "CMYK"
    assert inspect_raster(str(fixtures["gray.tif"])).metadata.colorspace == "Gray"
    assert inspect_raster(str(fixtures["photo.jpg"])).metadata.format == "JPEG"


@pytest.mark.parametrize("name", ["photo.jpg", "cmyk.jpg", "gray.jpg", "opaque.png", "alpha.png", "cm.png"])
def test_non_tiff_frame_count_matches(fixtures, name):
    path = fixtures[name]
    assert count_frames(str(path)) == len(identify("-format", "%p\n", path).split())
    raster = inspect_raster(str(path))
    assert raster.frame_count == 1 and raster.tiff is None


def test_count_frames_of_multipage_and_layered_tiff_keeps_counting_every_scene(fixtures):
    for name in ("multi.tif", "layered.tif"):
        assert count_frames(str(fixtures[name])) == len(identify("-format", "%p\n", fixtures[name]).split())


# --- launches ------------------------------------------------------------------

class _Counter:
    def __init__(self):
        self.commands: list[list[str]] = []


@pytest.fixture
def counted(monkeypatch):
    real = inspector.run_command
    counter = _Counter()

    def counting(command, **kwargs):
        counter.commands.append(list(command))
        return real(command, **kwargs)

    monkeypatch.setattr(inspector, "run_command", counting)
    return counter


def _old_launches(path: Path) -> int:
    """Launches of the previous implementation: structure x2 + metadata, or frames + metadata."""
    return 3 if path.suffix == ".tif" else 2


@pytest.mark.parametrize("name", ["rgb.tif", "cmyk.tif", "gray.tif", "multi.tif", "cm.tif", "msb.tif", "big.tif"])
def test_plain_tiff_needs_a_single_ping_launch(fixtures, counted, name):
    inspect_raster(str(fixtures[name]))
    assert len(counted.commands) == 1 < _old_launches(fixtures[name])
    assert "-ping" in counted.commands[0]


@pytest.mark.parametrize("name", ["photo.jpg", "cmyk.jpg", "opaque.png", "alpha.png"])
def test_jpeg_and_png_need_a_single_ping_launch(fixtures, counted, name):
    inspect_raster(str(fixtures[name]))
    assert len(counted.commands) == 1 < _old_launches(fixtures[name])
    assert "-ping" in counted.commands[0]


@pytest.mark.parametrize("name", ["rgba.tif", "cmyka.tif", "graya.tif", "bigcmyka.tif"])
def test_alpha_tiff_falls_back_to_one_full_decode_and_one_ping(fixtures, counted, name):
    raster = inspect_raster(str(fixtures[name]))
    assert raster.tiff.has_alpha
    full = [c for c in counted.commands if "-ping" not in c]
    assert len(full) == 1 and len(counted.commands) == 2


def test_layered_tiff_needs_two_ping_launches(fixtures, counted):
    raster = inspect_raster(str(fixtures["layered.tif"]))
    assert raster.tiff.layer_count == 2
    assert len(counted.commands) == 2 and all("-ping" in c for c in counted.commands)


def test_inspect_file_is_one_ping_launch_and_decode_only_fields_are_lazy(fixtures, counted):
    metadata = inspect_file(str(fixtures["photo.jpg"]))
    assert len(counted.commands) == 1
    assert metadata.width_px == 200 and metadata.dpi == 300.0
    assert len(counted.commands) == 1  # batch fields cost nothing more
    assert metadata.icc_profile == "Не внедрен"
    assert metadata.image_type and metadata.depth_bits
    assert len(counted.commands) == 2  # one full decode on first use, then cached
    assert metadata.depth_bits == reference_metadata(fixtures["photo.jpg"])["depth_bits"]
    assert len(counted.commands) == 2


# --- the TIFF header parser must fall back instead of guessing ------------------

def test_header_parser_reads_ifd0_tags(fixtures):
    rgb = inspector._read_tiff_ifd0(str(fixtures["rgb.tif"]))
    assert rgb is not None and rgb.alpha_is_excluded() and not rgb.has_image_source_data
    assert inspector._read_tiff_ifd0(str(fixtures["rgba.tif"])).extra_samples
    assert not inspector._read_tiff_ifd0(str(fixtures["cmyka.tif"])).alpha_is_excluded()
    assert inspector._read_tiff_ifd0(str(fixtures["layered.tif"])).has_image_source_data
    # Big-endian and BigTIFF files are really of those kinds and still parse.
    assert fixtures["msb.tif"].read_bytes()[:4] == b"MM\x00*"
    assert fixtures["big.tif"].read_bytes()[2:4] == b"+\x00"
    assert fixtures["bigcmyka.tif"].read_bytes()[2:4] == b"+\x00"
    for name in ("msb.tif", "big.tif"):
        assert inspector._read_tiff_ifd0(str(fixtures[name])).alpha_is_excluded()
    assert not inspector._read_tiff_ifd0(str(fixtures["bigcmyka.tif"])).alpha_is_excluded()


@pytest.mark.parametrize(
    "content",
    [b"", b"II", b"not a tiff at all", b"II*\x00\x08\x00\x00\x00", b"II*\x00\xff\xff\xff\x7f\x00\x00",
     b"II+\x00\x08\x00\x00\x00\x10\x00\x00\x00\x00\x00\x00\x00"],
)
def test_header_parser_gives_up_on_malformed_files(tmp_path, content):
    path = tmp_path / "broken.tif"
    path.write_bytes(content)
    assert inspector._read_tiff_ifd0(str(path)) is None
    assert inspector._read_tiff_ifd0(str(tmp_path / "missing.tif")) is None


def test_truncated_ifd_falls_back_to_full_identify(fixtures, tmp_path):
    # Header intact but the directory is cut: the parser must not decide alpha.
    data = fixtures["rgba.tif"].read_bytes()
    broken = tmp_path / "cut.tif"
    broken.write_bytes(data[:10])
    assert inspector._read_tiff_ifd0(str(broken)) is None
    with pytest.raises(subprocess.CalledProcessError):
        inspect_tiff_structure(str(broken))


def test_rgba_is_detected_even_though_ping_reports_three_channels(fixtures):
    """The trap: ``-ping`` says ``srgb 3.0`` for an RGBA TIFF."""
    path = fixtures["rgba.tif"]
    assert identify("-ping", "-format", "%[channels]", path).startswith("srgb ")
    assert inspect_tiff_structure(str(path)).has_alpha is True
    assert inspect_raster(str(path)).tiff.channels == "srgba 4.0"
