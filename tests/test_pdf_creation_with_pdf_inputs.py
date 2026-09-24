from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
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


def test_complete_duplex_pdf_uses_face_and_back_preview_names(tmp_path: Path) -> None:
    source = tmp_path / "job_(90x50)_4-4_(1-205)_input.pdf"
    _make_pdf(source, 2)
    processor = _processor(tmp_path)
    order = processor.inspect_orders()[0]

    with patch.object(processor, "generate_pdf_previews", return_value=[]) as generate:
        processor.generate_previews_for_files(order.files, tmp_path / "Previews")

    assert generate.call_args.kwargs["page_names"] == [
        "job_(90x50)_4-4_(1-205)_input_face",
        "job_(90x50)_4-4_(1-205)_input_back",
    ]


def test_ghostscript_uses_external_temporary_directory(tmp_path: Path, monkeypatch) -> None:
    share = tmp_path / "share"
    preview_dir = share / "Previews"
    pdf = share / "PDF" / "source.pdf"
    pdf.parent.mkdir(parents=True)
    preview_dir.mkdir(parents=True)
    pdf.write_bytes(b"pdf")
    cache_dir = share / ".preview-cache"
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(share))
    monkeypatch.setenv("IMAGE_MAGIC_PREVIEW_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr("core.preview_cache._is_external_mount", lambda _path: True)

    def render(command, **kwargs):
        assert kwargs["env"]["TMPDIR"].startswith(str(cache_dir / ".work"))
        assert kwargs["env"]["TEMP"] == kwargs["env"]["TMPDIR"]
        assert kwargs["env"]["TMP"] == kwargs["env"]["TMPDIR"]
        output_pattern = next(value.split("=", 1)[1] for value in command if value.startswith("-sOutputFile="))
        Path(output_pattern.replace("%03d", "001")).write_bytes(b"page")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    with patch("processing.batch_processor.shutil.which", return_value="/usr/bin/gs"), patch(
        "processing.batch_processor.run_command", side_effect=render
    ) as run_command, patch(
        "processing.batch_processor.inspect_file", return_value=_image_metadata("page.png")
    ), patch("processing.batch_processor.generate_preview"):
        processor = BatchProcessor(share, share)
        processor.generate_pdf_previews(pdf, preview_dir)

    scratch_path = Path(run_command.call_args.kwargs["env"]["TMPDIR"])
    assert scratch_path.is_relative_to(cache_dir / ".work")
    assert not scratch_path.exists()
