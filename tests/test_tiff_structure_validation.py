from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core.inspector import (
    ImageMetadata,
    RasterInspection,
    TiffStructure,
    inspect_tiff_structure,
)
from processing.batch_processor import BatchProcessor
from services.dto import file_check_to_dto


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["identify"], 0, stdout, "")


def _scene_line(index: int, channels: str, has_layers: str = "") -> str:
    # %p, %w, %h, %x, %y, %[units], %[colorspace], %[channels], layers, %m, %f
    return f"{index}\t10\t20\t300\t300\tPixelsPerInch\tCMYK\t{channels}\t{has_layers}\tTIFF\ta.tif\n"


@pytest.mark.parametrize(
    ("channels", "expected_alpha"),
    [
        ("cmyk  4.0", False),
        ("srgb  3.0", False),
        ("cmyka  5.0", True),
        ("srgba  4.0", True),
    ],
)
def test_tiff_structure_reads_composite_channels_and_ignores_layers_for_page_count(
    channels: str,
    expected_alpha: bool,
) -> None:
    # A file that cannot be parsed cheaply (here: it does not exist) takes the
    # full-decode path: composite channels with ignore-layers, then a -ping
    # launch for the layer flags.
    with patch("core.inspector.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.inspector.run_command",
        side_effect=[_completed(_scene_line(0, channels)), _completed(_scene_line(0, channels))],
    ) as run:
        structure = inspect_tiff_structure("artwork.tif")

    assert structure == TiffStructure(
        page_count=1,
        has_unflattened_layers=False,
        has_alpha=expected_alpha,
        channels=channels,
    )
    commands = [c.args[0] for c in run.call_args_list]
    assert commands[0][:5] == [
        "/usr/bin/magick", "identify", "-define", "tiff:ignore-layers=true", "-format"
    ]
    assert "-ping" not in commands[0]  # only the full decode can see alpha
    assert commands[1][:3] == ["/usr/bin/magick", "identify", "-ping"]
    assert "tiff:ignore-layers=true" not in commands[1]
    assert all(
        c.kwargs == {"capture_output": True, "text": True, "errors": "replace", "check": True}
        for c in run.call_args_list
    )


def test_tiff_structure_distinguishes_layers_from_real_pages() -> None:
    with patch("core.inspector.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.inspector.run_command",
        side_effect=[
            _completed(_scene_line(0, "cmyka  5.0")),
            _completed(_scene_line(0, "cmyka  5.0", "true") + _scene_line(1, "cmyka  5.0", "true")),
        ],
    ):
        layered = inspect_tiff_structure("layered.tiff")

    with patch("core.inspector.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.inspector.run_command",
        side_effect=[
            _completed(_scene_line(0, "cmyk  4.0") + _scene_line(1, "cmyk  4.0")),
            _completed(_scene_line(0, "cmyk  4.0") + _scene_line(1, "cmyk  4.0")),
        ],
    ):
        multipage = inspect_tiff_structure("multipage.tiff")

    assert layered == TiffStructure(1, True, True, "cmyka  5.0", layer_count=1)
    assert multipage == TiffStructure(2, False, False, "cmyk  4.0")


def _metadata(path: str) -> ImageMetadata:
    return ImageMetadata(
        file_path=path,
        file_name=Path(path).name,
        format="TIFF",
        width_px=1110,
        height_px=638,
        dpi=300,
        dpi_x=300,
        dpi_y=300,
        width_mm=94,
        height_mm=54,
        colorspace="CMYK",
        icc_profile="ISO Coated v2",
        image_type="ColorSeparation",
        depth_bits="8",
        size_mb=1,
    )


def _inspect_batch(tmp_path: Path, structure: TiffStructure):
    source = tmp_path / "job_(90x50)_4-0_(1-100)_face.tif"
    source.write_bytes(b"deterministic mocked TIFF")
    processor = BatchProcessor(tmp_path, tmp_path / "PDF")
    with patch(
        "processing.batch_processor.inspect_raster",
        side_effect=lambda path: RasterInspection(
            structure.page_count, _metadata(path), structure
        ),
    ):
        order = processor.inspect_orders()[0]
    return order.files[0]


def test_flattened_single_page_tiff_has_no_structural_errors(tmp_path: Path) -> None:
    item = _inspect_batch(tmp_path, TiffStructure(1, False, False, "cmyk  4.0"))

    assert item.has_alpha is False
    assert item.has_unflattened_layers is False
    assert item.tiff_page_count == 1
    assert item.page_count is None
    assert not any("альфа-канал" in error for error in item.errors)
    assert not any("несведённые слои" in error for error in item.errors)
    assert not any("страниц" in error for error in item.errors)

    dto = file_check_to_dto(item)
    assert dto["has_alpha"] is False
    assert dto["has_unflattened_layers"] is False
    assert dto["tiff_page_count"] == 1
    assert dto["page_count"] is None


def test_single_layer_tiff_can_be_flattened_during_pdf_creation(tmp_path: Path) -> None:
    item = _inspect_batch(
        tmp_path, TiffStructure(1, True, False, "cmyk  4.0", layer_count=1)
    )

    assert not any("несведённые слои" in error for error in item.errors)
    assert not item.warnings
    assert not item.tiff_flattened  # inspection never claims a conversion occurred
    assert item.tiff_layer_count == 1
    assert file_check_to_dto(item)["tiff_layer_count"] == 1


@pytest.mark.parametrize(
    ("structure", "expected_fragments", "unexpected_fragment"),
    [
        (TiffStructure(1, False, True, "cmyka  5.0"), (), "альфа-канал"),
        (TiffStructure(1, True, False, "cmyk  4.0", layer_count=2), (), "страниц"),
        (TiffStructure(2, False, False, "cmyk  4.0"), ("2 страниц",), "несведённые слои"),
        (
            TiffStructure(1, True, True, "cmyka  5.0", layer_count=2),
            (),
            "альфа-канал",
        ),
    ],
)
def test_batch_processor_reports_tiff_structure_without_false_multipage_error(
    tmp_path: Path,
    structure: TiffStructure,
    expected_fragments: tuple[str, ...],
    unexpected_fragment: str,
) -> None:
    item = _inspect_batch(tmp_path, structure)

    for fragment in expected_fragments:
        assert any(fragment in error for error in item.errors)
    assert not any(unexpected_fragment in error for error in item.errors)


def test_tiff_transparency_is_a_warning_not_an_error(tmp_path: Path) -> None:
    """Несведённая прозрачность сводится при сборке: предупреждение (заказ 26264275)."""
    item = _inspect_batch(tmp_path, TiffStructure(1, False, True, "cmyka  5.0"))

    assert item.errors == []
    assert any(w.startswith("TIFF с несведённой прозрачностью") for w in item.warnings)
    assert item.has_alpha and not item.transparency_flattened
