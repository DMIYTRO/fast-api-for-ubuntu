"""Склейка reviewed layered PDF: PyMuPDF по умолчанию и внешний merger при передаче."""
from pathlib import Path

import pymupdf

from services.layered_tiff_print import build_reviewed_layered_print_pdf


def _pdf(path: Path, pages: int, size=(200, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=size[0], height=size[1])
    doc.save(path)
    doc.close()
    return path


def _order(root: Path) -> dict:
    face = root / "A_(5-1)_face.tif"
    back = root / "A_(5-1)_back.tif"
    face.write_bytes(b"x")
    back.write_bytes(b"x")
    _pdf(root / "PDF" / "A_(5-1)_face_layered-composite.pdf", 1)
    production = _pdf(root / "PDF" / "A_(5-1)_face.pdf", 2)
    return {
        "order_id": "5",
        "pdf_path": str(production),
        "files": [
            {"path": str(face), "side": "face", "has_unflattened_layers": True},
            {"path": str(back), "side": "back", "has_unflattened_layers": False},
        ],
    }


def test_default_assembly_uses_pymupdf(tmp_path):
    out = build_reviewed_layered_print_pdf(_order(tmp_path), tmp_path)
    assert pymupdf.open(out).page_count == 2


def test_external_merger_is_used_and_order_kept(tmp_path):
    calls = []

    def merger(parts, output, label):
        calls.append(([Path(p).name for p in parts], label))
        doc = pymupdf.open()
        for part in parts:
            with pymupdf.open(part) as src:
                doc.insert_pdf(src)
        doc.save(output)
        doc.close()

    out = build_reviewed_layered_print_pdf(_order(tmp_path), tmp_path, pdf_merger=merger)
    assert pymupdf.open(out).page_count == 2
    assert calls and calls[0][1] == "5"
    assert calls[0][0][0].startswith("01_face") and calls[0][0][1].startswith("02_back")


def test_wrong_page_count_from_merger_is_rejected(tmp_path):
    import pytest
    from services.layered_tiff_print import ReviewedLayeredPdfError

    def bad_merger(parts, output, label):
        _pdf(output, 1)

    with pytest.raises(ReviewedLayeredPdfError):
        build_reviewed_layered_print_pdf(_order(tmp_path), tmp_path, pdf_merger=bad_merger)
    assert not list((tmp_path / "PDF").glob("*_reviewed-layered.pdf"))
