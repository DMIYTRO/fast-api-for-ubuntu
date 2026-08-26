from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import call, patch

import pytest

from core.inspector import ImageMetadata, TiffStructure, inspect_tiff_structure
from processing.batch_processor import BatchProcessor
from services.dto import file_check_to_dto


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["identify"], 0, stdout, "")


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
    with patch("core.inspector.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.inspector.run_command",
        side_effect=[_completed(f"0\t{channels}\n"), _completed("")],
    ) as run:
        structure = inspect_tiff_structure("artwork.tif")

    assert structure == TiffStructure(
        page_count=1,
        has_unflattened_layers=False,
        has_alpha=expected_alpha,
        channels=channels,
    )
    assert run.call_args_list == [
        call(
            [
                "/usr/bin/magick",
                "identify",
                "-define",
                "tiff:ignore-layers=true",
                "-format",
                "%p\t%[channels]\n",
                "artwork.tif",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        ),
        call(
            [
                "/usr/bin/magick",
                "identify",
                "-format",
                "%[tiff:has-layers]\n",
                "artwork.tif",
            ],
            capture_output=True,
            text=True,
            errors="replace",
            check=True,
        ),
    ]


def test_tiff_structure_distinguishes_layers_from_real_pages() -> None:
    with patch("core.inspector.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.inspector.run_command",
        side_effect=[_completed("0\tcmyka  5.0\n"), _completed("true\ntrue\n")],
    ):
        layered = inspect_tiff_structure("layered.tiff")

    with patch("core.inspector.shutil.which", return_value="/usr/bin/magick"), patch(
        "core.inspector.run_command",
        side_effect=[
            _completed("0\tcmyk  4.0\n1\tcmyk  4.0\n"),
            _completed("\n\n"),
        ],
    ):
        multipage = inspect_tiff_structure("multipage.tiff")

    assert layered == TiffStructure(1, True, True, "cmyka  5.0")
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
        "processing.batch_processor.inspect_tiff_structure", return_value=structure
    ), patch("processing.batch_processor.inspect_file", side_effect=_metadata), patch(
        "processing.batch_processor.count_frames"
    ) as legacy_count:
        order = processor.inspect_orders()[0]
    legacy_count.assert_not_called()
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


@pytest.mark.parametrize(
    ("structure", "expected_fragments", "unexpected_fragment"),
    [
        (TiffStructure(1, False, True, "cmyka  5.0"), ("альфа-канал",), "страниц"),
        (TiffStructure(1, True, False, "cmyk  4.0"), ("несведённые слои",), "страниц"),
        (TiffStructure(2, False, False, "cmyk  4.0"), ("2 страниц",), "несведённые слои"),
        (
            TiffStructure(1, True, True, "cmyka  5.0"),
            ("альфа-канал", "несведённые слои"),
            "страниц",
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
