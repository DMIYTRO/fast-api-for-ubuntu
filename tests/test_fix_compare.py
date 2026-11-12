"""Сравнение PDF до/после исправления (core/fix_compare.py)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import fitz
import pytest

from core.fix_compare import (
    FixCompareError,
    FixCompareFileError,
    FixComparePageError,
    FixComparison,
    build_fix_comparison,
)

MM = 72.0 / 25.4

SAMPLES = Path(__file__).resolve().parents[1] / "callas_checks_fixups" / "samples"
# В git PDF не попадают; в worktree (<main>/.claude/worktrees/<имя>) образцы лежат в основном чекауте.
_MAIN_SAMPLES = Path(__file__).resolve().parents[1].parents[2] / "callas_checks_fixups" / "samples"


def _sample(name: str) -> Path:
    for folder in (SAMPLES, _MAIN_SAMPLES):
        candidate = folder / f"{name}.pdf"
        if candidate.is_file():
            return candidate
    pytest.skip(
        f"Нет образца {name}.pdf в callas_checks_fixups/samples (PDF не хранятся в git); "
        "тест на реальных файлах пропущен."
    )


def _grid_rects(width_mm: float, height_mm: float):
    """Детерминированный набор прямоугольников разного размера и цвета."""
    rects = []
    for row in range(10):
        for col in range(6):
            x = 6 + col * (width_mm - 18) / 6
            y = 8 + row * (height_mm - 24) / 10
            w = 4 + (col * 3 + row) % 7
            h = 5 + (row * 2 + col) % 9
            fill = ((col * 40 % 255) / 255, (row * 25 % 255) / 255, 0.3)
            rects.append((x, y, w, h, fill))
    return rects


def make_pdf(
    path: Path,
    *,
    page_mm: tuple[float, float] = (100.0, 200.0),
    trim_mm: tuple[float, float, float, float] | None = None,
    scale: tuple[float, float] = (1.0, 1.0),
    shift_mm: tuple[float, float] = (0.0, 0.0),
    source_mm: tuple[float, float] = (100.0, 200.0),
    pages: int = 1,
) -> Path:
    """Векторный PDF: сетка прямоугольников, масштабированная вокруг центра страницы."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=page_mm[0] * MM, height=page_mm[1] * MM)
        if trim_mm is not None:
            x0, y0, x1, y1 = trim_mm
            page.set_trimbox(fitz.Rect(x0 * MM, y0 * MM, x1 * MM, y1 * MM))
        src_cx, src_cy = source_mm[0] / 2, source_mm[1] / 2
        dst_cx, dst_cy = page_mm[0] / 2 + shift_mm[0], page_mm[1] / 2 + shift_mm[1]
        for x, y, w, h, fill in _grid_rects(*source_mm):
            nx = dst_cx + (x - src_cx) * scale[0]
            ny = dst_cy + (y - src_cy) * scale[1]
            rect = fitz.Rect(nx * MM, ny * MM, (nx + w * scale[0]) * MM, (ny + h * scale[1]) * MM)
            page.draw_rect(rect, color=None, fill=fill)
    doc.save(path)
    doc.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Синтетические PDF -----------------------------------------------------------


def test_non_uniform_scale_is_reported_as_distortion(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", page_mm=(104, 204))
    fixed = make_pdf(
        tmp_path / "fixed.pdf",
        page_mm=(103, 214),
        trim_mm=(2, 2, 101, 212),
        scale=(0.99, 1.049),
        source_mm=(100, 200),
    )

    result = build_fix_comparison(original, fixed, tmp_path / "out")

    summary = result.summary
    assert summary["distortion"] is True
    assert summary["scale_x"] == pytest.approx(0.99, abs=0.005)
    assert summary["scale_y"] == pytest.approx(1.049, abs=0.005)
    assert summary["scale_y"] > summary["scale_x"]
    assert summary["changed_percent"] > 0
    assert summary["original"]["has_trimbox"] is False
    assert summary["result"]["has_trimbox"] is True
    assert summary["result"]["trimbox_width_mm"] == pytest.approx(99.0, abs=0.01)
    assert summary["result"]["trimbox_height_mm"] == pytest.approx(210.0, abs=0.01)


def test_uniform_scale_is_not_distortion(tmp_path):
    original = make_pdf(tmp_path / "original.pdf")
    fixed = make_pdf(tmp_path / "fixed.pdf", scale=(1.04, 1.04))

    summary = build_fix_comparison(original, fixed, tmp_path / "out").summary

    assert summary["scale_x"] == pytest.approx(1.04, abs=0.005)
    assert summary["scale_y"] == pytest.approx(1.04, abs=0.005)
    assert summary["distortion"] is False
    assert summary["changed_percent"] > 0


def test_same_file_has_no_changes(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", trim_mm=(3, 3, 97, 197))

    result = build_fix_comparison(original, original, tmp_path / "out")

    assert result.summary["changed_percent"] == pytest.approx(0.0)
    assert result.summary["changed_content_percent"] == pytest.approx(0.0)
    assert result.summary["distortion"] is False
    assert result.summary["scale_x"] == pytest.approx(1.0, abs=1e-3)
    assert result.summary["scale_y"] == pytest.approx(1.0, abs=1e-3)
    assert result.summary["center_shift_mm"]["distance"] == pytest.approx(0.0)
    assert result.summary["page_size_changed"] is False


def test_creates_three_images_only_in_out_dir(tmp_path):
    (tmp_path / "in").mkdir()
    original = make_pdf(tmp_path / "in" / "original.pdf")
    fixed = make_pdf(tmp_path / "in" / "fixed.pdf", scale=(1.0, 1.02))
    before = {p: _digest(p) for p in (original, fixed)}
    out_dir = tmp_path / "work" / "compare"

    result = build_fix_comparison(original, fixed, out_dir)

    assert isinstance(result, FixComparison)
    images = {result.original_image, result.result_image, result.overlay_image}
    assert len(images) == 3
    for image in images:
        assert image.parent == out_dir
        assert image.suffix in {".jpg", ".png"}
        assert image.stat().st_size > 0
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(i.name for i in images)
    assert sorted(p.name for p in (tmp_path / "work").iterdir()) == ["compare"]
    assert sorted(p.name for p in (tmp_path / "in").iterdir()) == ["fixed.pdf", "original.pdf"]
    assert {p: _digest(p) for p in (original, fixed)} == before


@pytest.mark.parametrize("preview_format, suffix", [("png", ".png"), ("jpeg", ".jpg")])
def test_image_format_follows_preview_settings(tmp_path, preview_format, suffix):
    original = make_pdf(tmp_path / "original.pdf")

    result = build_fix_comparison(original, original, tmp_path / "out", preview_format=preview_format)

    assert result.overlay_image.suffix == suffix
    pix = fitz.Pixmap(str(result.overlay_image))
    assert (pix.width, pix.height) == (
        result.summary["canvas_px"]["width"], result.summary["canvas_px"]["height"],
    )


def test_images_share_canvas_and_show_page_edges(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", page_mm=(100, 200))
    fixed = make_pdf(tmp_path / "fixed.pdf", page_mm=(120, 230), source_mm=(100, 200))

    result = build_fix_comparison(original, fixed, tmp_path / "out", preview_format="png")

    sizes = {
        (fitz.Pixmap(str(p)).width, fitz.Pixmap(str(p)).height)
        for p in (result.original_image, result.result_image, result.overlay_image)
    }
    assert len(sizes) == 1
    # Холст больше самой большой страницы: по краям светло-серый фон.
    width, height = sizes.pop()
    assert width > 120 * 100 / 25.4
    assert height > 230 * 100 / 25.4
    corner = fitz.Pixmap(str(result.original_image)).pixel(0, 0)
    assert corner[0] == corner[1] == corner[2]
    assert 200 <= corner[0] < 255


def test_different_page_sizes_and_trimbox_summary(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", page_mm=(104, 204))
    fixed = make_pdf(
        tmp_path / "fixed.pdf", page_mm=(103, 214), trim_mm=(2, 2, 101, 212), source_mm=(104, 204)
    )

    summary = build_fix_comparison(original, fixed, tmp_path / "out").summary

    assert summary["page_size_changed"] is True
    assert summary["original"]["page_width_mm"] == pytest.approx(104.0, abs=0.01)
    assert summary["original"]["page_height_mm"] == pytest.approx(204.0, abs=0.01)
    assert summary["original"]["trimbox_width_mm"] == pytest.approx(104.0, abs=0.01)
    assert summary["result"]["page_width_mm"] == pytest.approx(103.0, abs=0.01)
    assert summary["result"]["page_height_mm"] == pytest.approx(214.0, abs=0.01)
    assert summary["result"]["trimbox_width_mm"] == pytest.approx(99.0, abs=0.01)
    assert summary["result"]["trimbox_height_mm"] == pytest.approx(210.0, abs=0.01)


def test_center_shift_is_reported(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", trim_mm=(5, 5, 95, 195))
    # TrimBox результата смещён к правому краю страницы на 3 мм.
    fixed = make_pdf(tmp_path / "fixed.pdf", trim_mm=(8, 5, 98, 195))

    summary = build_fix_comparison(original, fixed, tmp_path / "out").summary

    assert summary["center_shift_mm"]["x"] == pytest.approx(3.0, abs=0.01)
    assert summary["center_shift_mm"]["y"] == pytest.approx(0.0, abs=0.01)
    assert summary["center_shift_mm"]["distance"] == pytest.approx(3.0, abs=0.01)


def test_raster_only_pdf_has_no_scale_estimate(tmp_path):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    pix.set_rect(pix.irect, (200, 30, 30))
    doc = fitz.open()
    page = doc.new_page(width=200, height=300)
    page.insert_image(fitz.Rect(20, 20, 180, 280), pixmap=pix)
    path = tmp_path / "raster.pdf"
    doc.save(path)
    doc.close()

    summary = build_fix_comparison(path, path, tmp_path / "out").summary

    assert summary["scale_x"] is None
    assert summary["scale_y"] is None
    assert summary["distortion"] is False


def test_second_page_can_be_compared(tmp_path):
    original = make_pdf(tmp_path / "original.pdf", pages=2)
    fixed = make_pdf(tmp_path / "fixed.pdf", pages=2, scale=(1.0, 1.05))

    summary = build_fix_comparison(original, fixed, tmp_path / "out", page=2).summary

    assert summary["page"] == 2
    assert summary["distortion"] is True


def test_missing_page_raises_russian_error(tmp_path):
    original = make_pdf(tmp_path / "original.pdf")

    with pytest.raises(FixComparePageError) as info:
        build_fix_comparison(original, original, tmp_path / "out", page=2)

    assert "нет страницы 2" in str(info.value)
    assert isinstance(info.value, FixCompareError)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("page", [0, -1])
def test_invalid_page_number_raises(tmp_path, page):
    original = make_pdf(tmp_path / "original.pdf")

    with pytest.raises(FixComparePageError):
        build_fix_comparison(original, original, tmp_path / "out", page=page)


def test_broken_file_raises_russian_error(tmp_path):
    original = make_pdf(tmp_path / "original.pdf")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4\nthis is not a real pdf at all\n")

    with pytest.raises(FixCompareFileError) as info:
        build_fix_comparison(original, broken, tmp_path / "out")

    assert "результат" in str(info.value)
    assert "повреждён" in str(info.value)

    with pytest.raises(FixCompareFileError):
        build_fix_comparison(broken, original, tmp_path / "out")


def test_missing_file_raises_russian_error(tmp_path):
    original = make_pdf(tmp_path / "original.pdf")

    with pytest.raises(FixCompareFileError) as info:
        build_fix_comparison(original, tmp_path / "nope.pdf", tmp_path / "out")

    assert "не найден" in str(info.value)


def test_invalid_dpi_raises(tmp_path):
    original = make_pdf(tmp_path / "original.pdf")

    with pytest.raises(FixCompareError):
        build_fix_comparison(original, original, tmp_path / "out", dpi=5)


def test_summary_is_json_serializable_with_plain_types(tmp_path):
    original = make_pdf(tmp_path / "original.pdf")
    fixed = make_pdf(tmp_path / "fixed.pdf", scale=(0.99, 1.05))

    summary = build_fix_comparison(original, fixed, tmp_path / "out").summary

    assert json.loads(json.dumps(summary, allow_nan=False)) == summary

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        else:
            assert value is None or type(value) in (int, float, bool, str), type(value)

    walk(summary)


# --- Реальные образцы Callas -----------------------------------------------------


def test_real_sample_desktop_fix_is_flagged_as_distorted(tmp_path):
    original = tmp_path / "original.pdf"
    fixed = tmp_path / "desktop_fix.pdf"
    shutil.copyfile(_sample("original"), original)
    shutil.copyfile(_sample("desktop_fix"), fixed)

    summary = build_fix_comparison(original, fixed, tmp_path / "out").summary

    assert summary["distortion"] is True
    assert summary["scale_x"] == pytest.approx(0.990, abs=0.005)
    assert summary["scale_y"] == pytest.approx(1.049, abs=0.005)
    assert summary["scale_y"] > summary["scale_x"]
    assert summary["changed_percent"] > 0
    assert summary["original"]["page_width_mm"] == pytest.approx(104.0, abs=0.1)
    assert summary["original"]["page_height_mm"] == pytest.approx(204.0, abs=0.1)
    assert summary["result"]["page_width_mm"] == pytest.approx(103.0, abs=0.1)
    assert summary["result"]["page_height_mm"] == pytest.approx(214.0, abs=0.1)
    assert summary["result"]["trimbox_width_mm"] == pytest.approx(99.0, abs=0.1)
    assert summary["result"]["trimbox_height_mm"] == pytest.approx(210.0, abs=0.1)


def test_real_sample_against_itself_is_unchanged(tmp_path):
    original = tmp_path / "original.pdf"
    shutil.copyfile(_sample("original"), original)

    summary = build_fix_comparison(original, original, tmp_path / "out").summary

    assert summary["changed_percent"] == pytest.approx(0.0)
    assert summary["distortion"] is False


def test_real_sample_desktop_plus_matches_original_geometry(tmp_path):
    original = tmp_path / "original.pdf"
    plus = tmp_path / "desktop_plus.pdf"
    shutil.copyfile(_sample("original"), original)
    shutil.copyfile(_sample("desktop_plus"), plus)

    summary = build_fix_comparison(original, plus, tmp_path / "out").summary

    assert summary["page_size_changed"] is False
    assert summary["distortion"] is False
