"""Сборка reviewed layered PDF: части — через extractor (callas split), склейка — merger."""
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


def _fake_extractor(calls=None):
    """Фейк callas split: в тесте страницу копирует PyMuPDF, в коде — служба."""

    def extractor(source, page_number, output, label):
        if calls is not None:
            calls.append((Path(source).name, page_number, label))
        doc = pymupdf.open()
        with pymupdf.open(source) as src:
            doc.insert_pdf(src, from_page=page_number - 1, to_page=page_number - 1)
        doc.save(output)
        doc.close()

    return extractor


def test_assembly_uses_callas_extractor_and_merger(tmp_path):
    from tests.fake_callas import FakeCallasMerger

    calls = []
    out = build_reviewed_layered_print_pdf(
        _order(tmp_path), tmp_path,
        pdf_merger=FakeCallasMerger(), pdf_page_extractor=_fake_extractor(calls),
    )
    assert pymupdf.open(out).page_count == 2
    assert calls == [
        ("A_(5-1)_face_layered-composite.pdf", 1, "5"),
        ("A_(5-1)_face.pdf", 2, "5"),
    ]


def test_missing_extractor_fails_without_pymupdf_fallback(tmp_path):
    import pytest
    from services.layered_tiff_print import ReviewedLayeredPdfError

    with pytest.raises(ReviewedLayeredPdfError, match="callas"):
        build_reviewed_layered_print_pdf(_order(tmp_path), tmp_path)
    assert not list((tmp_path / "PDF" / "Print" / "Parts").glob("*.pdf"))


def test_extractor_returning_wrong_page_count_is_rejected(tmp_path):
    import pytest
    from services.layered_tiff_print import ReviewedLayeredPdfError

    def bad(source, page_number, output, label):
        _pdf(output, 2)

    with pytest.raises(ReviewedLayeredPdfError):
        build_reviewed_layered_print_pdf(_order(tmp_path), tmp_path, pdf_page_extractor=bad)
    assert not list((tmp_path / "PDF" / "Print" / "Parts").glob("*.pdf"))


def test_parts_are_not_written_with_pymupdf(tmp_path, monkeypatch):
    order = _order(tmp_path)
    extractor = _fake_extractor()

    def merger(parts, output, label):
        _pdf(output, len(parts))

    # Фейки готовят файлы до запрета; сам код сборки PDF не пишет.
    real_save = pymupdf.Document.save

    def guarded(name):
        def boom(*args, **kwargs):
            raise AssertionError(f"PyMuPDF {name} вызван в сборке reviewed PDF")
        return boom

    def extractor_unguarded(source, page_number, output, label):
        monkeypatch.setattr(pymupdf.Document, "save", real_save)
        monkeypatch.setattr(pymupdf.Document, "insert_pdf", real_insert)
        try:
            extractor(source, page_number, output, label)
        finally:
            monkeypatch.setattr(pymupdf.Document, "save", guarded("save"))
            monkeypatch.setattr(pymupdf.Document, "insert_pdf", guarded("insert_pdf"))

    def merger_unguarded(parts, output, label):
        monkeypatch.setattr(pymupdf.Document, "save", real_save)
        try:
            merger(parts, output, label)
        finally:
            monkeypatch.setattr(pymupdf.Document, "save", guarded("save"))

    real_insert = pymupdf.Document.insert_pdf
    monkeypatch.setattr(pymupdf.Document, "save", guarded("save"))
    monkeypatch.setattr(pymupdf.Document, "insert_pdf", guarded("insert_pdf"))
    out = build_reviewed_layered_print_pdf(
        order, tmp_path, pdf_merger=merger_unguarded, pdf_page_extractor=extractor_unguarded
    )
    monkeypatch.undo()
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

    out = build_reviewed_layered_print_pdf(
        _order(tmp_path), tmp_path, pdf_merger=merger, pdf_page_extractor=_fake_extractor()
    )
    assert pymupdf.open(out).page_count == 2
    assert calls and calls[0][1] == "5"
    assert calls[0][0][0].startswith("01_face") and calls[0][0][1].startswith("02_back")


def test_wrong_page_count_from_merger_is_rejected(tmp_path):
    import pytest
    from services.layered_tiff_print import ReviewedLayeredPdfError

    def bad_merger(parts, output, label):
        _pdf(output, 1)

    with pytest.raises(ReviewedLayeredPdfError):
        build_reviewed_layered_print_pdf(
            _order(tmp_path), tmp_path, pdf_merger=bad_merger, pdf_page_extractor=_fake_extractor()
        )
    assert not list((tmp_path / "PDF").glob("*_reviewed-layered.pdf"))
