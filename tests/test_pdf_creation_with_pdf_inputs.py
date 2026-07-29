from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pymupdf

from core.inspector import ImageMetadata
from processing.batch_processor import BatchProcessor


def _points(mm: float) -> float:
    return mm * 72 / 25.4


def _make_pdf(path: Path, pages: int, width_mm: float = 94, height_mm: float = 54) -> None:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=_points(width_mm), height=_points(height_mm))
        page.insert_text((20, 30), f"source-page-{index + 1}")
    document.save(path)
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


def _create_orders(root: Path, processor: BatchProcessor):
    return processor.inspect_orders()


def test_complete_two_page_pdf_is_copied_as_two_pages(tmp_path: Path) -> None:
    source = tmp_path / "job_(90x50)_4-4_(1-200)_input.pdf"
    _make_pdf(source, 2)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    processor = _processor(tmp_path)

    orders = _create_orders(tmp_path, processor)
    results = processor.create_pdfs(orders)

    assert results[0][2] is None, results[0][2]
    output = results[0][1]
    with pymupdf.open(output) as document:
        assert document.page_count == 2
        assert [page.get_text().strip() for page in document] == [
            "source-page-1",
            "source-page-2",
        ]
        assert all(abs(page.rect.width - _points(94)) < 0.1 for page in document)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def test_one_page_4_0_pdf_without_side_is_created(tmp_path: Path) -> None:
    source = tmp_path / "job_(90x50)_4-0_(1-201)_input.pdf"
    _make_pdf(source, 1)
    processor = _processor(tmp_path)

    results = processor.create_pdfs(_create_orders(tmp_path, processor))

    assert results[0][2] is None, results[0][2]
    with pymupdf.open(results[0][1]) as document:
        assert document.page_count == 1


def test_mixed_face_image_and_back_pdf_merges_in_order(tmp_path: Path) -> None:
    face = tmp_path / "job_(90x50)_4-4_(1-202)_face.jpg"
    face.write_bytes(b"test image")
    back = tmp_path / "job_(90x50)_4-4_(1-202)_back.pdf"
    _make_pdf(back, 1)
    processor = _processor(tmp_path)

    with patch("processing.batch_processor.count_frames", return_value=1), patch(
        "processing.batch_processor.inspect_file", side_effect=_image_metadata
    ):
        orders = processor.inspect_orders()

    def fake_convert(_input: str, output: str, **_kwargs: object) -> str:
        _make_pdf(Path(output), 1)
        return output

    with patch("processing.batch_processor.convert_image_to_pdf", side_effect=fake_convert) as convert:
        results = processor.create_pdfs(orders)

    assert results[0][2] is None, results[0][2]
    assert convert.call_count == 1
    with pymupdf.open(results[0][1]) as document:
        assert document.page_count == 2
        assert document[1].get_text().strip() == "source-page-1"


def test_pdf_only_input_never_calls_image_conversion(tmp_path: Path) -> None:
    source = tmp_path / "job_(90x50)_4-0_(1-203)_input.pdf"
    _make_pdf(source, 1)
    processor = _processor(tmp_path)

    with patch("processing.batch_processor.convert_image_to_pdf") as convert:
        results = processor.create_pdfs(_create_orders(tmp_path, processor))

    assert results[0][2] is None, results[0][2]
    convert.assert_not_called()


def test_two_page_pdf_generates_one_preview_per_page(tmp_path: Path) -> None:
    source = tmp_path / "job_(90x50)_4-4_(1-204)_input.pdf"
    _make_pdf(source, 2)
    processor = _processor(tmp_path)
    results = processor.create_pdfs(_create_orders(tmp_path, processor))

    assert results[0][2] is None, results[0][2]
    previews = processor.generate_pdf_previews(
        results[0][1],
        tmp_path / "Previews",
        page_names=["face", "back"],
    )

    assert len(previews) == 2
    assert [path.name for path in previews] == [
        "face_preview.png",
        "back_preview.png",
    ]
    assert all(path.is_file() and path.stat().st_size > 0 for path in previews)
