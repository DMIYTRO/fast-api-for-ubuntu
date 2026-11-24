"""Печать заказа с готовым итоговым PDF не пересобирает его из слоёв TIFF (заказ 26261792)."""

from services.order_workflow import _has_production_pdf


def test_production_pdf_skips_layered_rebuild(tmp_path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert _has_production_pdf({"pdf_path": str(pdf), "pdf_kind": "production"})
    # Старые заказы без pdf_kind: готовый PDF тоже не пересобирается.
    assert _has_production_pdf({"pdf_path": str(pdf)})


def test_diagnostic_or_missing_pdf_still_builds_from_layers(tmp_path):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert not _has_production_pdf({"pdf_path": str(pdf), "pdf_kind": "diagnostic"})
    assert not _has_production_pdf({"pdf_path": str(tmp_path / "missing.pdf")})
    assert not _has_production_pdf({})
