"""Контрольная проверка размера итогового PDF меряет боксы страниц, а не пиксели рендера.

Заказы 2026-11-20:
* 26242148 (49.0×74.0 мм ровно) — рендер callas 72 DPI дал 138×208 px = 48.7×73.4 мм,
  отклонение 0.62 мм > 0.6, заказ стал красным, хотя файл верный;
* 26241885 (страница 88.9×59.0 мм с ``/Rotate 90``) — рендер повёрнут (59×88.9), а
  ожидался размер без поворота, как его мерит осмотр исходника.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from processing.batch_processor import BatchProcessor
from tests.fake_callas import FakeCallasMerger

MM = 72 / 25.4


def _make_pdf(path: Path, width_mm: float, height_mm: float, *, rotate: int = 0) -> None:
    document = pymupdf.open()
    page = document.new_page(width=width_mm * MM, height=height_mm * MM)
    page.insert_text((10, 20), "artwork")
    if rotate:
        page.set_rotation(rotate)
    document.save(path)
    document.close()


def _png(path: Path, width_px: int, height_px: int) -> Path:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width_px, height_px), False)
    pixmap.set_rect(pixmap.irect, (255, 255, 255))
    pixmap.save(str(path))
    return path


def _run(tmp_path: Path, monkeypatch, name: str, render_px: tuple[int, int], **pdf):
    source = tmp_path / name
    _make_pdf(source, **pdf)

    def render(self, pdf_path, dpi, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        return [_png(out_dir / "page-1.png", *render_px)]

    monkeypatch.setattr(BatchProcessor, "_render_pdf_with_callas", render)
    processor = BatchProcessor(tmp_path, tmp_path / "output", pdf_merger=FakeCallasMerger())
    orders = processor.inspect_orders()
    assert orders and orders[0].passed, [item.errors for item in orders[0].files]
    return processor, processor.create_pdfs(orders)


def test_exact_size_pdf_is_not_rejected_by_render_rounding(tmp_path, monkeypatch):
    _processor, results = _run(
        tmp_path, monkeypatch,
        "23_NP(P)_(70x45)_4-0_(31696-26242148)_drill1.pdf",
        render_px=(138, 208),  # то, что вернул callas: 48.7×73.4 мм
        width_mm=49.0, height_mm=74.0,
    )
    assert results[0][2] is None, results[0][2]


def test_rotated_page_is_not_rejected_for_orientation(tmp_path, monkeypatch):
    _processor, results = _run(
        tmp_path, monkeypatch,
        "23_KS(K)_(85x55)_4-0_(39691-26241885)_1.pdf",
        render_px=(167, 252),  # рендер учитывает /Rotate 90
        width_mm=88.9, height_mm=59.0, rotate=90,
    )
    assert results[0][2] is None, results[0][2]


def test_really_wrong_page_size_is_still_rejected(tmp_path, monkeypatch):
    processor, _ = _run(
        tmp_path, monkeypatch,
        "23_NP(P)_(70x45)_4-0_(31696-26242149)_drill1.pdf",
        render_px=(138, 209),
        width_mm=49.0, height_mm=74.0,
    )
    order = processor.inspect_orders()[0]
    # Итоговый PDF «съёжился» при сборке: боксы 47×74 вместо 49×74.
    def shrink(page_pdfs, output, _order=None, *a, **k):
        _make_pdf(Path(output), 47.0, 74.0)

    processor._merge_page_pdfs = shrink  # type: ignore[method-assign]
    results = processor.create_pdfs([order])
    assert results[0][2] is not None and "имеет размер 47.0x74.0 мм" in results[0][2]


def test_render_fallback_tolerates_two_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr("processing.batch_processor._pdf_page_sizes_mm", lambda *_a: None)
    _processor, results = _run(
        tmp_path, monkeypatch,
        "23_NP(P)_(70x45)_4-0_(31696-26242150)_drill1.pdf",
        render_px=(138, 208),
        width_mm=49.0, height_mm=74.0,
    )
    assert results[0][2] is None, results[0][2]


@pytest.mark.parametrize("render_px", [(132, 209)])
def test_render_fallback_still_catches_a_real_mismatch(tmp_path, monkeypatch, render_px):
    monkeypatch.setattr("processing.batch_processor._pdf_page_sizes_mm", lambda *_a: None)
    _processor, results = _run(
        tmp_path, monkeypatch,
        "23_NP(P)_(70x45)_4-0_(31696-26242151)_drill1.pdf",
        render_px=render_px,  # 46.6 мм вместо 49
        width_mm=49.0, height_mm=74.0,
    )
    assert results[0][2] is not None and "имеет размер" in results[0][2]
