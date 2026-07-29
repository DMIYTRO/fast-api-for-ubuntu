from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pymupdf

from core.inspector import ImageMetadata
from processing.batch_processor import BatchProcessor


def _make_pdf(path: Path, page_count: int, width_mm: float = 94, height_mm: float = 54) -> None:
    points = lambda mm: mm * 72 / 25.4
    document = pymupdf.open()
    for _ in range(page_count):
        document.new_page(width=points(width_mm), height=points(height_mm))
    document.save(str(path))
    document.close()


def _image_metadata(path: str) -> ImageMetadata:
    return ImageMetadata(
        file_path=path,
        file_name=Path(path).name,
        format="JPEG",
        width_px=1110,
        height_px=638,
        dpi=300,
        dpi_x=300,
        dpi_y=300,
        width_mm=94,
        height_mm=54,
        colorspace="CMYK",
        icc_profile="profile",
        image_type="TrueColor",
        depth_bits="8",
        size_mb=1,
    )


def _processor(root: Path) -> BatchProcessor:
    return BatchProcessor(root, root / "output")


def test_4_0_one_page_pdf_without_side_suffix_passes(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-100)_input.pdf"
    _make_pdf(pdf, 1)

    order = _processor(tmp_path).inspect_orders()[0]

    assert order.passed
    assert order.files[0].page_count == 1
    assert order.files[0].parsed.side == ""


def test_4_4_one_page_pdf_without_second_side_fails(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-4_(1-101)_input.pdf"
    _make_pdf(pdf, 1)

    order = _processor(tmp_path).inspect_orders()[0]

    assert not order.passed
    assert any("face" in error or "двусторон" in error for error in order.errors)


def test_mixed_face_image_and_back_pdf_passes(tmp_path: Path) -> None:
    face = tmp_path / "job_(90x50)_4-4_(1-102)_face.jpg"
    face.write_bytes(b"image fixture")
    back = tmp_path / "job_(90x50)_4-4_(1-102)_back.pdf"
    _make_pdf(back, 1)

    with patch("processing.batch_processor.count_frames", return_value=1), patch(
        "processing.batch_processor.inspect_file", side_effect=_image_metadata
    ):
        order = _processor(tmp_path).inspect_orders()[0]

    assert order.passed
    assert {item.parsed.side for item in order.files} == {"face", "back"}


def test_two_page_4_4_pdf_is_complete_order(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-4_(1-103)_input.pdf"
    _make_pdf(pdf, 2)

    order = _processor(tmp_path).inspect_orders()[0]

    assert order.passed
    assert order.files[0].page_count == 2


def test_two_page_pdf_with_extra_side_fails(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-4_(1-104)_input.pdf"
    _make_pdf(pdf, 2)
    back = tmp_path / "job_(90x50)_4-4_(1-104)_back.pdf"
    _make_pdf(back, 1)

    order = _processor(tmp_path).inspect_orders()[0]

    assert not order.passed
    assert any("дополнительные стороны" in error for error in order.errors)


def test_two_page_pdf_is_rejected_for_4_0(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-105)_input.pdf"
    _make_pdf(pdf, 2)

    order = _processor(tmp_path).inspect_orders()[0]

    assert not order.passed
    assert any("односторон" in error for error in order.errors)


def test_three_page_pdf_fails(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-4_(1-106)_input.pdf"
    _make_pdf(pdf, 3)

    order = _processor(tmp_path).inspect_orders()[0]

    assert not order.passed
    assert any("maximum supported is 2" in error for error in order.files[0].errors)


def test_real_pdf_fixture_has_expected_one_and_two_page_documents() -> None:
    fixture_dir = Path(__file__).parents[1] / "input_files" / "PDF" / "Print"
    processor = BatchProcessor(fixture_dir, fixture_dir / "_unused_output")
    orders = processor.inspect_orders()

    one_page = [
        order for order in orders
        if order.files and order.files[0].parsed.back_colors == 0
    ]
    two_page = [
        order for order in orders
        if order.files and order.files[0].parsed.back_colors > 0
    ]
    assert len(one_page) == 9
    assert len(two_page) == 49
    assert all(order.files[0].page_count == 1 for order in one_page)
    assert all(order.files[0].page_count == 2 for order in two_page)
    assert all(order.passed for order in orders)
    assert all(order.files[0].dpi_x is not None for order in orders)
    assert all(order.files[0].dpi_y is not None for order in orders)
