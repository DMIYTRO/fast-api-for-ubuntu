"""Снятие ICC-профилей CMYK/Gray и OutputIntent у PDF без RGB (фейковый конвертер, без сети)."""
from __future__ import annotations

from pathlib import Path

import pymupdf

from core.pdf_inspector import find_icc_pages, find_lab_pages, find_rgb_pages
from processing.batch_processor import BatchProcessor
from processing.models import OrderCheck
from services.callas.color_conversion import (
    CallasRgbToCmykConverter,
    color_failure_warning,
    convert_rgb_to_cmyk,
    has_output_intent,
)
from tests.test_rgb_to_cmyk import Converter, make_pdf


LAB = "[/Lab << /WhitePoint [0.95 1 1.09] /Range [-128 127 -128 127] >>]"


def new_icc(document: pymupdf.Document, components: int) -> int:
    xref = document.get_new_xref()
    document.update_object(xref, f"<< /N {components} /Length 4 >>")
    document.update_stream(xref, b"abcd", new=True)
    return xref


def set_colorspaces(document: pymupdf.Document, page_index: int, entries: dict[str, str]) -> None:
    """Пишет /Resources/ColorSpace страницы (Resources у pymupdf-страницы — косвенный объект)."""
    kind, value = document.xref_get_key(document[page_index].xref, "Resources")
    assert kind == "xref"
    resources = int(value.split()[0])
    body = " ".join(f"/{name} {definition}" for name, definition in entries.items())
    document.xref_set_key(resources, "ColorSpace", f"<< {body} >>")


def add_colorspace(path: Path, page_index: int, definition: str, name: str = "CS0") -> Path:
    with pymupdf.open(str(path)) as document:
        set_colorspaces(document, page_index, {name: definition})
        document.saveIncr()
    return path


def add_icc(path: Path, components: int, page_index: int = 0, extra: dict[str, str] | None = None) -> Path:
    with pymupdf.open(str(path)) as document:
        xref = new_icc(document, components)
        set_colorspaces(document, page_index, {"CS0": f"[/ICCBased {xref} 0 R]", **(extra or {})})
        document.saveIncr()
    return path


def add_output_intent(path: Path) -> Path:
    with pymupdf.open(str(path)) as document:
        document.xref_set_key(
            document.pdf_catalog(),
            "OutputIntents",
            "[<< /Type /OutputIntent /S /GTS_PDFX /OutputConditionIdentifier (ISO Coated) >>]",
        )
        document.saveIncr()
    return path


def cmyk_with_icc(tmp_path: Path, name: str = "a.pdf") -> Path:
    return add_icc(make_pdf(tmp_path / name, (0, 0, 0, 1)), 4)


class StripConverter(Converter):
    """Результат: тот же CMYK-PDF без ICC (как после профиля callas)."""

    def __init__(self, keep_icc=False, **kwargs):
        super().__init__(**kwargs)
        self.keep_icc = keep_icc

    def __call__(self, source, target, label):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        if self.keep_icc:
            Path(target).write_bytes(Path(source).read_bytes())
        else:
            make_pdf(target, self.result_fill)
        return {"fixups_applied": 3}


# ------------------------------------------------------------------ detection
def test_find_icc_pages_detects_cmyk_and_gray_only(tmp_path):
    assert find_icc_pages(cmyk_with_icc(tmp_path)) == [1]
    gray = add_icc(make_pdf(tmp_path / "g.pdf", (0, 0, 0, 1)), 1)
    assert find_icc_pages(gray) == [1]
    rgb_icc = add_icc(make_pdf(tmp_path / "r.pdf", (0, 0, 0, 1)), 3)
    assert find_icc_pages(rgb_icc) == []
    assert find_icc_pages(make_pdf(tmp_path / "plain.pdf", (0, 0, 0, 1))) == []


def test_find_icc_pages_looks_into_form_xobjects(tmp_path):
    pdf = make_pdf(tmp_path / "f.pdf", (0, 0, 0, 1))
    with pymupdf.open(str(pdf)) as document:
        icc = new_icc(document, 4)
        form = document.get_new_xref()
        document.update_object(
            form,
            f"<< /Type /XObject /Subtype /Form /BBox [0 0 10 10] "
            f"/Resources << /ColorSpace << /C [/ICCBased {icc} 0 R] >> >> /Length 0 >>",
        )
        document.update_stream(form, b"", new=True)
        resources = int(document.xref_get_key(document[0].xref, "Resources")[1].split()[0])
        document.xref_set_key(resources, "XObject", f"<< /Fm0 {form} 0 R >>")
        document.saveIncr()
    assert find_icc_pages(pdf) == [1]


def test_find_lab_pages_and_per_page_split(tmp_path):
    pdf = make_pdf(tmp_path / "lab.pdf", (0, 0, 0, 1))
    add_colorspace(pdf, 0, LAB)
    assert find_lab_pages(pdf) == [1] and find_icc_pages(pdf) == []
    # слово «Label» в ресурсах Lab не означает
    plain = make_pdf(tmp_path / "p.pdf", (0, 0, 0, 1))
    add_colorspace(plain, 0, "[/DeviceN [/Label] /DeviceCMYK null]")
    assert find_lab_pages(plain) == []


# ------------------------------------------------------------------ strip
def test_icc_cmyk_is_stripped_and_recorded(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    converter = StripConverter()
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert converter.calls == 1
    assert record["status"] == "converted" and record["reason"] == "icc"
    assert record["from"] == "ICC" and record["to"] == "DeviceCMYK" and record["pages"] == [1]
    assert record["icc_removed"] is True and record["output_intent_removed"] is False
    assert record["error"] is None
    assert find_icc_pages(pdf) == [] and find_rgb_pages(pdf) == ()
    assert not list(tmp_path.glob(".*tmp*"))


def test_output_intent_is_stripped_without_icc_objects(tmp_path):
    pdf = add_output_intent(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)))
    record = convert_rgb_to_cmyk(pdf, StripConverter(), "a")
    assert record["status"] == "converted" and record["reason"] == "icc"
    assert record["output_intent_removed"] is True and record["icc_removed"] is True
    assert has_output_intent(pdf) is False


def test_output_intent_that_callas_keeps_leaves_pdf_untouched(tmp_path):
    pdf = add_output_intent(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)))
    before = pdf.read_bytes()
    record = convert_rgb_to_cmyk(pdf, StripConverter(keep_icc=True), "a")
    assert record["status"] == "skipped" and record["skip_reason"] == "output_intent"
    assert pdf.read_bytes() == before
    assert color_failure_warning(record) is None


def test_lab_present_skips_without_touching_the_pdf(tmp_path):
    pdf = add_icc(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)), 4, extra={"CS1": LAB})
    before = pdf.read_bytes()
    converter = StripConverter()
    record = convert_rgb_to_cmyk(pdf, converter, "a")
    assert converter.calls == 0
    assert record["status"] == "skipped" and record["skip_reason"] == "lab"
    assert record["reason"] == "icc" and record["pages"] == [1] and record["lab_pages"] == [1]
    assert pdf.read_bytes() == before
    assert color_failure_warning(record) is None


def test_flag_off_does_nothing(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    converter = CallasRgbToCmykConverter(client=None, profile="p", strip_icc=False)
    assert convert_rgb_to_cmyk(pdf, converter, "a") is None
    assert find_icc_pages(pdf) == [1]


def test_flag_default_is_on_and_in_settings():
    from server.settings import Settings

    assert CallasRgbToCmykConverter(client=None).strip_icc is True
    assert Settings.__dataclass_fields__["callas_strip_icc_enabled"].default is True


def test_no_icc_means_no_call(tmp_path):
    converter = StripConverter()
    assert convert_rgb_to_cmyk(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)), converter, "a") is None
    assert converter.calls == 0


def test_converter_failure_keeps_pdf_and_records_error(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    before = pdf.read_bytes()
    record = convert_rgb_to_cmyk(pdf, StripConverter(fail="callas недоступен"), "a")
    assert record["status"] == "failed" and record["reason"] == "icc"
    assert record["error"] == "callas недоступен"
    assert pdf.read_bytes() == before
    assert color_failure_warning(record) == (
        "Не удалось снять ICC-профиль: callas недоступен. PDF оставлен как есть."
    )
    assert not list(tmp_path.glob(".*tmp*"))


def test_icc_left_in_result_is_a_failure(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    before = pdf.read_bytes()
    record = convert_rgb_to_cmyk(pdf, StripConverter(keep_icc=True), "a")
    assert record["status"] == "failed" and "ICC-профиль остался" in record["error"]
    assert pdf.read_bytes() == before


def test_rgb_appearing_in_result_is_a_failure(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    record = convert_rgb_to_cmyk(pdf, StripConverter(result_fill=(1, 0, 0)), "a")
    assert record["status"] == "failed" and "RGB остался" in record["error"]
    assert find_icc_pages(pdf) == [1]


def test_page_count_change_is_a_failure(tmp_path):
    class TwoPages(StripConverter):
        def __call__(self, source, target, label):
            document = pymupdf.open()
            document.new_page()
            document.new_page()
            document.save(target)
            document.close()
            return {}

    pdf = cmyk_with_icc(tmp_path)
    record = convert_rgb_to_cmyk(pdf, TwoPages(), "a")
    assert record["status"] == "failed" and "число страниц" in record["error"]


def test_rgb_path_still_works_and_has_reason_rgb(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", (1, 0, 0))
    record = convert_rgb_to_cmyk(pdf, Converter(), "a")
    assert record["status"] == "converted" and record["reason"] == "rgb"
    assert record["from"] == "RGB" and record["to"] == "CMYK"
    failed = convert_rgb_to_cmyk(make_pdf(tmp_path / "b.pdf", (1, 0, 0)), Converter(fail="x"), "b")
    assert color_failure_warning(failed) == "Не удалось перевести RGB в CMYK: x. PDF оставлен в RGB."


# ------------------------------------------------------------------ BatchProcessor
def processor(converter, tmp_path):
    return BatchProcessor(tmp_path, tmp_path / "out", pdf_color_converter=converter)


def test_batch_processor_records_icc_strip_and_keeps_rgb_warnings_off(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    order = OrderCheck("1", "2", files=[])
    order.warnings.append("Цвет: что-то другое")
    processor(StripConverter(), tmp_path)._convert_rgb_to_cmyk(pdf, order)
    assert order.color_conversion["reason"] == "icc"
    assert order.color_conversion["status"] == "converted"
    assert order.warnings == ["Цвет: что-то другое"]


def test_batch_processor_adds_warning_when_strip_fails(tmp_path):
    pdf = cmyk_with_icc(tmp_path)
    order = OrderCheck("1", "2", files=[])
    processor(StripConverter(fail="boom"), tmp_path)._convert_rgb_to_cmyk(pdf, order)
    assert order.color_conversion["status"] == "failed"
    assert order.warnings == ["Не удалось снять ICC-профиль: boom. PDF оставлен как есть."]


def test_batch_processor_lab_skip_adds_no_warning(tmp_path):
    pdf = add_icc(make_pdf(tmp_path / "a.pdf", (0, 0, 0, 1)), 4, extra={"CS1": LAB})
    order = OrderCheck("1", "2", files=[])
    processor(StripConverter(), tmp_path)._convert_rgb_to_cmyk(pdf, order)
    assert order.color_conversion["status"] == "skipped" and order.warnings == []


def test_strip_icc_setting_from_env(monkeypatch):
    from server.settings import Settings

    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_STRIP_ICC_ENABLED", "0")
    assert Settings.from_env().callas_strip_icc_enabled is False
    monkeypatch.delenv("IMAGE_MAGIC_CALLAS_STRIP_ICC_ENABLED")
    assert Settings.from_env().callas_strip_icc_enabled is True
