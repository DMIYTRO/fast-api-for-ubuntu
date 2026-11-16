from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from processing.batch_processor import BatchProcessor
from processing.pdf_resize_policy import (
    MAX_VARIABLE_LENGTH,
    METHOD_FILL,
    METHOD_REVEAL,
    METHOD_STRETCH,
    PdfResizeLimits,
    build_resize_quickfix,
    page_min_dpi,
    plan_pdf_page_resize,
    quickfix_variable,
)
from processing.resample_policy import ResampleDecision
from services.callas.page_resize import CallasPdfResizer, PdfResizeError, verify_resized_pdf

PT = 72 / 25.4


def _make_pdf(path: Path, sizes: list[tuple[float, float]], bleed_mm: float | None = None) -> None:
    document = pymupdf.open()
    for width, height in sizes:
        page = document.new_page(width=width * PT, height=height * PT)
        if bleed_mm is not None:
            b = bleed_mm * PT
            page.set_trimbox(pymupdf.Rect(b, b, width * PT - b, height * PT - b))
    document.save(str(path))
    document.close()


LIMITS = PdfResizeLimits(
    min_dpi=250, metadata_tolerance_mm=0.1, auto_mm=4.0, confirm_mm=10.0,
    stretch_auto_pct=1.0, stretch_confirm_pct=3.0,
)


def _plan(actual, expected=(94.0, 54.0), dpi=math.inf, hidden=(), limits=LIMITS):
    return plan_pdf_page_resize(actual, expected, dpi, limits, hidden)


def _fill(target=(94.0, 54.0)):
    return _plan((96.0, 56.0), expected=target)


# --- политика ---------------------------------------------------------------

def test_small_deviation_is_auto_corrected_proportionally() -> None:
    plan = _plan((96.0, 56.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.method == METHOD_FILL
    assert plan.target_mm == (94.0, 54.0)
    assert plan.scale == pytest.approx((94 / 96, 94 / 96))
    assert max(plan.crop_mm) < 4.0


def test_portrait_page_keeps_its_orientation() -> None:
    plan = _plan((56.0, 96.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.target_mm == (54.0, 94.0)


def test_large_crop_needs_confirmation_and_huge_crop_is_rejected() -> None:
    assert _plan((94.0, 60.0)).decision == ResampleDecision.ASK_CONFIRMATION
    assert _plan((94.0, 80.0)).decision == ResampleDecision.REJECT


def test_hidden_bleed_is_revealed_without_scaling() -> None:
    plan = _plan((90.0, 50.0), hidden=[(2.0, 2.0)])
    assert plan.method == METHOD_REVEAL
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.scale == (1.0, 1.0)
    assert plan.enlarge_mm == (2.0, 2.0)


def test_too_little_hidden_content_falls_back_to_fill() -> None:
    plan = _plan((90.0, 50.0), hidden=[(2.0, 1.0)])
    assert plan.method == METHOD_FILL


def test_reveal_is_not_used_when_page_is_larger_on_one_side() -> None:
    assert _plan((95.0, 50.0), hidden=[(5.0, 5.0)]).method == METHOD_FILL


def test_small_format_prefers_fill_because_crop_is_tiny() -> None:
    # 89x51 → 94x54: обрезка 0.24 мм проходит автоматически, вытягивание не нужно.
    plan = _plan((89.0, 51.0))
    assert plan.method == METHOD_FILL and plan.decision == ResampleDecision.AUTO_CORRECT


def test_large_format_small_stretch_is_automatic_instead_of_confirmed_crop() -> None:
    # 605x400 → 600x400: заполнение срежет 5 мм (подтверждение), вытягивание — 0.83%.
    plan = _plan((605.0, 400.0), expected=(600.0, 400.0))
    assert plan.method == METHOD_STRETCH
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.scale == pytest.approx((600 / 605, 1.0))
    assert plan.distortion_pct == pytest.approx((1 - 600 / 605) * 100, rel=1e-3)


def test_stretch_within_confirm_limit_wins_tie_with_confirmed_crop() -> None:
    # 610x400: обрезка 10 мм и искажение 1.67% — оба с подтверждением; без обрезки лучше.
    plan = _plan((610.0, 400.0), expected=(600.0, 400.0))
    assert plan.method == METHOD_STRETCH
    assert plan.decision == ResampleDecision.ASK_CONFIRMATION


def test_too_much_distortion_falls_back_to_fill() -> None:
    plan = _plan((630.0, 400.0), expected=(600.0, 400.0))  # 5% — вытягивание запрещено
    assert plan.method == METHOD_FILL


def test_stretch_respects_min_dpi() -> None:
    plan = _plan((595.0, 400.0), expected=(600.0, 400.0), dpi=251.0)
    assert plan.method != METHOD_STRETCH or min(plan.effective_dpi) >= 250


def test_stretch_is_disabled_by_zero_limits() -> None:
    plan = _plan((605.0, 400.0), expected=(600.0, 400.0), limits=PdfResizeLimits(250, 0.1, 4.0, 10.0))
    assert plan.method == METHOD_FILL


def test_page_min_dpi_ignores_missing_values_and_vector_pages() -> None:
    images = [SimpleNamespace(effective_dpi_x=320.0, effective_dpi_y=None),
              SimpleNamespace(effective_dpi_x=300.0, effective_dpi_y=310.0)]
    assert page_min_dpi(images) == 300.0
    assert page_min_dpi([]) == math.inf


# --- QuickFix JSON ----------------------------------------------------------

def test_fill_quickfix_scales_and_sets_boxes_from_mediabox() -> None:
    config = build_resize_quickfix({1: _fill(), 2: _fill()}, 2.0)
    scale, boxes = config["quickfixes"]
    assert scale["quickfix"] == "scale_pages"
    assert scale["instructions"] == [{
        "page_selector": "all", "short_edge": 54.0, "long_edge": 94.0, "unit": "mm",
        "page_scale_mode": "fit_from_outside_cut_page",
    }]
    assert boxes["quickfix"] == "set_page_box"
    assert [(b["which_box"], b["from_box_or_absolute"], b["left"]) for b in boxes["instructions"]] == [
        ("CropBox", "MediaBox", 0), ("BleedBox", "MediaBox", 0), ("TrimBox", "MediaBox", -2.0),
    ]


def test_mixed_orientation_uses_same_config_and_fits_variable_limit() -> None:
    landscape = build_resize_quickfix({1: _fill()}, 2.0)
    mixed = build_resize_quickfix({1: _fill(), 2: _plan((56.0, 96.0))}, 2.0)
    assert mixed == landscape
    value = quickfix_variable(mixed)
    assert "\n" not in value
    assert json.loads(value) == mixed


def test_methods_per_page_get_own_selectors_and_fit_variable_limit() -> None:
    reveal = _plan((600.0, 400.0), expected=(604.0, 404.0), hidden=[(2.0, 2.0)])
    stretch = _plan((609.0, 404.0), expected=(604.0, 404.0))
    assert (reveal.method, stretch.method) == (METHOD_REVEAL, METHOD_STRETCH)
    config = build_resize_quickfix({1: reveal, 2: stretch}, 2.0)
    kinds = [fix["quickfix"] for fix in config["quickfixes"]]
    assert kinds == ["enlarge_pages", "scale_pages", "set_page_box"]
    enlarge = config["quickfixes"][0]["instructions"]
    assert [(i["page_selector"], i["which_edges"], i["enlarge_by"]) for i in enlarge] == [
        ("1", "left_and_right", 2.0), ("1", "top_and_bottom", 2.0),
    ]
    scale = config["quickfixes"][1]["instructions"]
    assert [(i["page_selector"], i["page_scale_mode"]) for i in scale] == [("2", "stretch_to_fill")]
    # Худший реальный случай (PDF заказа — не больше двух страниц): разные способы и
    # раздвигание по обеим осям на разную величину укладываются в лимит переменной.
    worst = build_resize_quickfix(
        {
            1: _plan((600.4, 400.6), expected=(604.0, 404.0), hidden=[(2.0, 2.0)]),
            2: _plan((609.0, 404.0), expected=(604.0, 404.0)),
        },
        2.0,
    )
    assert len(quickfix_variable(worst)) <= MAX_VARIABLE_LENGTH


def test_quickfix_variable_rejects_too_long_json() -> None:
    with pytest.raises(ValueError, match="лимита"):
        quickfix_variable({"x": "a" * MAX_VARIABLE_LENGTH})


def test_pages_with_different_formats_are_refused() -> None:
    with pytest.raises(ValueError):
        build_resize_quickfix({1: _fill(), 2: _fill((100.0, 60.0))}, 2.0)


# --- исполнитель callas -----------------------------------------------------

class _FakeClient:
    def __init__(self, result_pdf: Path) -> None:
        self.result_pdf = result_pdf
        self.calls: list[dict] = []
        self.deleted: list[str] = []

    def process(self, pdf, profile, mode, **kwargs):
        self.calls.append({"pdf": pdf, "profile": profile, "mode": mode, **kwargs})
        return SimpleNamespace(output_pdf=self.result_pdf, summary={})

    def delete(self, job_id):
        self.deleted.append(job_id)

    def job_dir(self, job_id):
        return self.result_pdf.parent / "no-such-job-dir"


def test_resizer_passes_quickfix_variable_and_publishes_verified_result(tmp_path: Path) -> None:
    source = tmp_path / "in.pdf"
    _make_pdf(source, [(96.0, 56.0)])
    callas_out = tmp_path / "callas.pdf"
    _make_pdf(callas_out, [(94.0, 54.0)], bleed_mm=2.0)
    client = _FakeClient(callas_out)
    target = tmp_path / "out.pdf"

    CallasPdfResizer(client, "Image Magic/Apply QuickFix config")(
        source, target, {1: _fill()}, 2.0, "order"
    )

    call = client.calls[0]
    assert call["profile"] == "Image Magic/Apply QuickFix config"
    assert call["mode"] == "fix"
    config = json.loads(call["variables"]["quickfix_config"])
    assert config["quickfixes"][0]["quickfix"] == "scale_pages"
    assert target.is_file()
    assert client.deleted


def test_resizer_rejects_unchanged_result(tmp_path: Path) -> None:
    # Ошибка в QuickFix JSON: callas возвращает файл без изменений и без ошибки.
    source = tmp_path / "in.pdf"
    _make_pdf(source, [(96.0, 56.0)])
    target = tmp_path / "out.pdf"
    with pytest.raises(PdfResizeError, match="размер страницы"):
        CallasPdfResizer(_FakeClient(source))(source, target, {1: _fill()}, 2.0, "order")
    assert not target.exists()


def test_verify_checks_trimbox_and_page_count(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    _make_pdf(pdf, [(94.0, 54.0)])  # TrimBox = MediaBox, без вылетов
    with pytest.raises(PdfResizeError, match="TrimBox"):
        verify_resized_pdf(pdf, {1: (94.0, 54.0)}, 2.0)
    with pytest.raises(PdfResizeError, match="страниц"):
        verify_resized_pdf(pdf, {1: (94.0, 54.0), 2: (94.0, 54.0)}, 2.0)


# --- BatchProcessor ---------------------------------------------------------

def _fake_resizer(calls: list):
    def resize(source, target, page_plans, bleed_mm, label):
        targets = {n: p.target_mm for n, p in page_plans.items()}
        calls.append((Path(source).name, targets, {n: p.method for n, p in page_plans.items()}, bleed_mm))
        _make_pdf(Path(target), [targets[n] for n in sorted(targets)], bleed_mm=bleed_mm)
        return {}
    return resize


def test_pdf_with_small_size_deviation_is_planned_for_quickfix(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-100)_input.pdf"
    _make_pdf(pdf, [(96.0, 56.0)])

    order = BatchProcessor(tmp_path, tmp_path / "output", pdf_resizer=_fake_resizer([])).inspect_orders()[0]

    item = order.files[0]
    assert order.passed, item.errors
    assert item.needs_resample
    assert item.resample_decision == "auto_correct"
    assert {n: p.target_mm for n, p in item.pdf_resize_plans.items()} == {1: (94.0, 54.0)}
    assert any("скорректирован callas" in warning for warning in item.warnings)


def test_pdf_size_error_is_kept_when_quickfix_is_disabled(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-100)_input.pdf"
    _make_pdf(pdf, [(96.0, 56.0)])

    item = BatchProcessor(tmp_path, tmp_path / "output").inspect_orders()[0].files[0]

    assert any("Автоматическая коррекция" in error for error in item.errors)
    assert not item.needs_resample


def test_ask_policy_requires_confirmation_before_pdf_resize(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-100)_input.pdf"
    _make_pdf(pdf, [(96.0, 56.0)])
    processor = BatchProcessor(
        tmp_path, tmp_path / "output", pdf_resizer=_fake_resizer([]), correction_policy="ask"
    )

    item = processor.inspect_orders()[0].files[0]
    assert item.resample_decision == "ask_confirmation"
    assert not item.needs_resample

    BatchProcessor.confirm_resample(item, True)
    assert item.needs_resample and item.pdf_resize_plans


def test_prepare_page_pdfs_uses_resized_copy_and_page_refs_expect_target(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-4_(1-100).pdf"
    _make_pdf(pdf, [(96.0, 56.0), (96.0, 56.0)])
    calls: list = []
    processor = BatchProcessor(tmp_path, tmp_path / "output", pdf_resizer=_fake_resizer(calls))
    order = processor.inspect_orders()[0]
    assert order.passed, order.files[0].errors

    ordered = processor._ordered_files_for_creation(order)
    refs = processor._build_page_refs(ordered)
    work = tmp_path / "work"
    work.mkdir()
    page_pdfs = processor._prepare_page_pdfs(refs, work)

    assert calls == [(pdf.name, {1: (94.0, 54.0), 2: (94.0, 54.0)}, {1: "fill", 2: "fill"}, 2.0)]
    assert len(page_pdfs) == 1 and Path(page_pdfs[0]).parent == work
    assert [(r.expected_width_mm, r.expected_height_mm) for r in refs] == [(94.0, 54.0)] * 2
    assert pdf.stat().st_size > 0  # исходник не тронут
    action = order.files[0].processing_actions[-1]
    assert action["method"] == "callas_quickfix"
    assert [page["method"] for page in action["pages"]] == ["fill", "fill"]


def test_rotated_pdf_page_is_not_resized(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-100)_input.pdf"
    document = pymupdf.open()
    page = document.new_page(width=96 * PT, height=56 * PT)
    page.set_rotation(90)
    document.save(str(pdf))
    document.close()

    item = BatchProcessor(tmp_path, tmp_path / "output", pdf_resizer=_fake_resizer([])).inspect_orders()[0].files[0]

    assert not item.needs_resample
    assert any("Автоматическая коррекция" in error for error in item.errors)


def test_pdf_with_hidden_bleed_is_planned_as_reveal(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(90x50)_4-0_(1-100)_input.pdf"
    document = pymupdf.open()
    page = document.new_page(width=90 * PT, height=50 * PT)
    page.draw_rect(pymupdf.Rect(-2 * PT, -2 * PT, 92 * PT, 52 * PT), color=None, fill=(0, 0, 1))
    document.save(str(pdf))
    document.close()

    item = BatchProcessor(tmp_path, tmp_path / "output", pdf_resizer=_fake_resizer([])).inspect_orders()[0].files[0]

    assert item.needs_resample, item.errors
    assert item.pdf_resize_plans[1].method == "reveal"
    assert any("скрытые вылеты" in warning for warning in item.warnings)
