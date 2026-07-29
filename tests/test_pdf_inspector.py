from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from core.pdf_inspector import expected_page_count_error, inspect_pdf


def _rgb_png(path: Path, width: int = 300, height: int = 150) -> None:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, width, height), False)
    pixmap.clear_with(0x336699)
    pixmap.save(str(path))


def _make_pdf(path: Path, pages: list[tuple[float, float]], image: Path | None = None) -> None:
    document = pymupdf.open()
    for width, height in pages:
        page = document.new_page(width=width, height=height)
        if image is not None:
            page.insert_image(page.rect, filename=str(image))
    document.save(str(path))
    document.close()


def test_inspects_one_page_raster_and_effective_dpi_without_modifying_source(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    pdf = tmp_path / "one-page.pdf"
    _rgb_png(image, width=300, height=150)
    _make_pdf(pdf, [(72 * 2, 72)], image)
    before = hashlib.sha256(pdf.read_bytes()).digest()

    result = inspect_pdf(pdf)

    assert result.is_valid
    assert result.page_count == 1
    assert result.pages[0].content_type == "raster"
    assert result.pages[0].width_mm == 50.8
    assert result.pages[0].height_mm == 25.4
    raster = result.pages[0].images[0]
    assert (raster.width_px, raster.height_px) == (300, 150)
    assert raster.colorspace == 3
    assert "RGB" in (raster.colorspace_name or "")
    assert raster.effective_dpi_x == 150.0
    assert raster.effective_dpi_y == 150.0
    assert hashlib.sha256(pdf.read_bytes()).digest() == before


def test_inspects_two_pages_with_independent_geometry_and_rotation(tmp_path: Path) -> None:
    pdf = tmp_path / "two-page.pdf"
    document = pymupdf.open()
    first = document.new_page(width=200, height=100)
    first.set_rotation(90)
    second = document.new_page(width=300, height=150)
    second.set_rotation(180)
    document.save(str(pdf))
    document.close()

    result = inspect_pdf(pdf)

    assert result.is_valid
    assert result.page_count == 2
    assert [page.rotation for page in result.pages] == [90, 180]
    assert result.pages[0].mediabox == (0.0, 0.0, 200.0, 100.0)
    assert result.pages[1].mediabox == (0.0, 0.0, 300.0, 150.0)
    assert result.pages[0].width_mm == 200 * 25.4 / 72
    assert result.pages[1].height_mm == 150 * 25.4 / 72
    for page in result.pages:
        assert page.cropbox == page.mediabox
        assert page.trimbox == page.mediabox
        assert page.bleedbox == page.mediabox
        assert page.artbox == page.mediabox


def test_three_pages_is_diagnostic_error(tmp_path: Path) -> None:
    pdf = tmp_path / "three-page.pdf"
    _make_pdf(pdf, [(100, 100), (100, 100), (100, 100)])

    result = inspect_pdf(pdf)

    assert not result.is_valid
    assert result.page_count == 3
    assert any("maximum supported is 2" in error for error in result.errors)


def test_empty_corrupt_and_missing_files_are_diagnostics(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.touch()
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a PDF")

    assert "empty" in inspect_pdf(empty).errors[0].lower()
    assert "cannot be opened" in inspect_pdf(corrupt).errors[0]
    assert "does not exist" in inspect_pdf(tmp_path / "missing.pdf").errors[0]


def test_encrypted_pdf_is_rejected(tmp_path: Path) -> None:
    pdf = tmp_path / "encrypted.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(
        str(pdf),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-password",
        user_pw="user-password",
    )
    document.close()

    result = inspect_pdf(pdf)

    assert result.encrypted
    assert any("Encrypted PDF" in error for error in result.errors)


def test_vector_and_mixed_content_types(tmp_path: Path) -> None:
    image = tmp_path / "image.png"
    _rgb_png(image)

    vector_pdf = tmp_path / "vector.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(10, 10, 80, 80), color=(1, 0, 0), fill=(0, 1, 0))
    page.insert_text((20, 110), "vector text")
    document.save(str(vector_pdf))
    document.close()

    mixed_pdf = tmp_path / "mixed.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(pymupdf.Rect(0, 0, 100, 50), filename=str(image))
    page.draw_rect(pymupdf.Rect(110, 10, 180, 80), color=(1, 0, 0))
    document.save(str(mixed_pdf))
    document.close()

    assert inspect_pdf(vector_pdf).pages[0].content_type == "vector"
    assert inspect_pdf(mixed_pdf).pages[0].content_type == "mixed"


def test_page_count_rule_is_filename_independent() -> None:
    assert expected_page_count_error(1, 1) is None
    assert expected_page_count_error(1, 2) == "PDF contains 1 pages; expected 2"
