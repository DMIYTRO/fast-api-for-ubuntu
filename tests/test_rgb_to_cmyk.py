from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from core.pdf_inspector import find_rgb_pages
from processing.batch_processor import BatchProcessor
from processing.models import OrderCheck
from services.callas.color_conversion import convert_rgb_to_cmyk, rgb_warning
from services.dto import order_check_to_dto


def make_pdf(path: Path, fill) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=200, height=100)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(10, 10, 100, 80))
    shape.finish(fill=fill)
    shape.commit()
    document.save(path)
    document.close()
    return path


class Converter:
    profile = "test-profile"

    def __init__(self, result_fill=(0, 0, 0, 1), fail=None):
        self.calls = 0
        self.result_fill = result_fill
        self.fail = fail

    def __call__(self, source, target, label):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        make_pdf(target, self.result_fill)
        return {"fixups_applied": 1}


def test_find_rgb_pages_detects_vector_rgb_only(tmp_path):
    assert find_rgb_pages(make_pdf(tmp_path / "rgb.pdf", (1, 0, 0))) == (1,)
    assert find_rgb_pages(make_pdf(tmp_path / "cmyk.pdf", (0, 0, 0, 1))) == ()


def test_converts_in_place_and_verifies(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", (1, 0, 0))
    converter = Converter()
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert record["status"] == "converted" and record["profile"] == "test-profile"
    assert record["pages"] == [1]
    assert find_rgb_pages(pdf) == ()
    assert not list(tmp_path.glob(".*tmp*"))


def test_cmyk_pdf_does_not_call_callas(tmp_path):
    converter = Converter()
    assert convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)), converter, "a") is None
    assert converter.calls == 0


@pytest.mark.parametrize("converter", [Converter(fail="boom"), Converter(result_fill=(0, 1, 0))])
def test_failure_keeps_original_pdf(tmp_path, converter):
    pdf = make_pdf(tmp_path / "a.pdf", (1, 0, 0))
    before = pdf.read_bytes()
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert record["status"] == "failed" and record["error"]
    assert pdf.read_bytes() == before
    assert not list(tmp_path.glob(".*tmp*"))


def test_rgb_warning_matches_only_rgb_messages():
    assert rgb_warning("цветовая модель sRGB не входит в профиль digital (CMYK); файл будет")
    assert rgb_warning("PDF страница 1: растровое изображение имеет RGB colorspace")
    assert not rgb_warning("цветовая модель Gray не входит в профиль digital (CMYK)")
    assert not rgb_warning("разрешение 100x100 DPI")


def test_processor_clears_rgb_warnings_on_success_and_warns_on_failure(tmp_path):
    def order():
        item = SimpleNamespace(warnings=["цветовая модель sRGB не входит в профиль x", "другое"])
        return OrderCheck("1", "2", files=[item], warnings=["PDF страница 1: растровое изображение имеет RGB colorspace"])

    ok = order()
    BatchProcessor(tmp_path, tmp_path, pdf_color_converter=Converter())._convert_rgb_to_cmyk(
        make_pdf(tmp_path / "a.pdf", (1, 0, 0)), ok
    )
    assert ok.warnings == [] and ok.files[0].warnings == ["другое"]
    assert ok.color_conversion["status"] == "converted"
    ok.files = []
    assert order_check_to_dto(ok)["color_conversion"]["status"] == "converted"

    bad = order()
    BatchProcessor(tmp_path, tmp_path, pdf_color_converter=Converter(fail="boom"))._convert_rgb_to_cmyk(
        make_pdf(tmp_path / "b.pdf", (1, 0, 0)), bad
    )
    assert bad.color_conversion["status"] == "failed"
    assert any("Не удалось перевести RGB" in text for text in bad.warnings)
