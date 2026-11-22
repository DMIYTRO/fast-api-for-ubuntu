"""Градиенты не изображения: MuPDF отдаёт их в get_image_info() как RGB-«картинки» на 72 dpi.

Регрессия 2026-11-22: CMYK-градиенты (оператор ``sh`` и заливка узором-градиентом) давали
ложные «effective DPI 72; требуется не менее 250» и «растровое изображение имеет RGB
colorspace» — 312 из 312 проверенных файлов прода с одной такой ошибкой (заказ 26251765:
градиенты DeviceN Cyan/Yellow). Настоящие изображения, в том числе inline, проверяются как раньше.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from core.pdf_inspector import find_rgb_pages, inspect_pdf

COLORSPACES = {
    "cmyk": ("/DeviceCMYK", "0 0 0 0", "1 0 1 0"),
    "rgb": ("/DeviceRGB", "0 0 0", "1 0 0"),
    # Как в заказе 26251765: плашечные имена процессных красок с альтернативой CMYK.
    "devicen": ("[/DeviceN[/Cyan/Yellow]/DeviceCMYK {tint}]", "0 0", "1 1"),
}


def gradient_pdf(path: Path, colorspace: str, *, pattern: bool) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=255, height=142)
    cs, c0, c1 = COLORSPACES[colorspace]
    if "{tint}" in cs:
        tint = document.get_new_xref()
        document.update_object(tint, "<</FunctionType 2/Domain[0 1]/C0[0 0 0 0]/C1[1 0 1 0]/N 1>>")
        tint_function = document.get_new_xref()
        document.update_object(
            tint_function,
            "<</FunctionType 4/Domain[0 1 0 1]/Range[0 1 0 1 0 1 0 1]/Length 20>>",
        )
        document.update_stream(tint_function, b"{ 0 exch 0 exch }")
        cs = cs.format(tint=f"{tint_function} 0 R")
    function = document.get_new_xref()
    document.update_object(function, f"<</FunctionType 2/Domain[0 1]/C0[{c0}]/C1[{c1}]/N 1>>")
    shading = document.get_new_xref()
    document.update_object(
        shading,
        f"<</ShadingType 2/ColorSpace {cs}/Coords[0 0 200 0]/Function {function} 0 R/Extend[true true]>>",
    )
    if pattern:
        pattern_xref = document.get_new_xref()
        document.update_object(pattern_xref, f"<</Type/Pattern/PatternType 2/Shading {shading} 0 R>>")
        resources = f"<</Pattern<</P0 {pattern_xref} 0 R>>>>"
        content = b"/Pattern cs /P0 scn 20 20 200 100 re f"
    else:
        resources = f"<</Shading<</Sh0 {shading} 0 R>>>>"
        content = b"q 20 20 200 100 re W n /Sh0 sh Q"
    stream = document.get_new_xref()
    document.update_object(stream, "<<>>")
    document.update_stream(stream, content)
    document.xref_set_key(page.xref, "Contents", f"{stream} 0 R")
    document.xref_set_key(page.xref, "Resources", resources)
    document.save(str(path))
    return path


def inline_rgb_image_pdf(path: Path) -> Path:
    """Inline-изображение (BI … EI, без xref) 4×2 px на 40×20 pt = 7.2 dpi."""
    document = pymupdf.open()
    page = document.new_page(width=200, height=100)
    stream = document.get_new_xref()
    document.update_object(stream, "<<>>")
    pixels = b"\xff\x00\x00" * 8
    document.update_stream(
        stream, b"q 40 0 0 20 10 10 cm BI /W 4 /H 2 /CS /RGB /BPC 8 ID " + pixels + b" EI Q"
    )
    document.xref_set_key(page.xref, "Contents", f"{stream} 0 R")
    document.save(str(path))
    return path


@pytest.mark.parametrize("pattern", [False, True], ids=["sh", "pattern"])
@pytest.mark.parametrize("colorspace", ["cmyk", "devicen"])
def test_cmyk_gradient_is_neither_an_image_nor_rgb(tmp_path, colorspace, pattern):
    pdf = gradient_pdf(tmp_path / "gradient.pdf", colorspace, pattern=pattern)
    page = inspect_pdf(pdf).pages[0]
    assert page.images == ()  # раньше: DeviceRGB 200×100 px, 72 dpi
    assert page.content_type == "vector"
    assert find_rgb_pages(pdf) == ()


@pytest.mark.parametrize("pattern", [False, True], ids=["sh", "pattern"])
def test_rgb_gradient_is_still_reported_as_rgb(tmp_path, pattern):
    pdf = gradient_pdf(tmp_path / "gradient.pdf", "rgb", pattern=pattern)
    assert inspect_pdf(pdf).pages[0].images == ()
    assert find_rgb_pages(pdf) == (1,)


def test_inline_image_keeps_its_dpi_and_rgb(tmp_path):
    pdf = inline_rgb_image_pdf(tmp_path / "inline.pdf")
    [image] = inspect_pdf(pdf).pages[0].images
    assert (image.width_px, image.height_px) == (4, 2)
    assert image.effective_dpi_x == pytest.approx(7.2)
    assert find_rgb_pages(pdf) == (1,)
