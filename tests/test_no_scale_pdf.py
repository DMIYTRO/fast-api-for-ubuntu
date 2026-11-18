"""PDF с меткой «без масштабирования» в имени: (SRA3P), (SRA3), (BAN).

Такой файл никогда не масштабируется и не получает полей Callas: размер приводится
только сдвигом MediaBox (способ ``canvas``) — белые поля наружу или центральная обрезка
внутрь. Без сети: фейковые resizer и callas.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from config.profiles import DEFAULT_PROFILE
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck
from processing.pdf_resize_policy import (
    MAX_VARIABLE_LENGTH,
    METHOD_CANVAS,
    METHOD_FILL,
    METHOD_REVEAL,
    METHOD_SHRINK,
    METHOD_STRETCH,
    PdfPagePlan,
    PdfResizeLimits,
    build_resize_quickfix,
    plan_pdf_page_resize,
    quickfix_variable,
)
from processing.resample_policy import ResampleDecision
from services.callas.page_resize import CallasPdfResizer, verify_resized_pdf
from services.callas_auto_bleed import CallasAutoBleed, candidate_reason
from services.callas_fix import CallasFixService
from services.dto import file_check_to_dto
from tests.test_callas_bleed import BleedClient, art_pdf
from tests.test_pdf_resize import _FakeClient, _fake_resizer, _make_pdf

PT = 72 / 25.4
LIMITS = PdfResizeLimits(
    min_dpi=250, metadata_tolerance_mm=0.1, auto_mm=4.0, confirm_mm=10.0,
    stretch_auto_pct=1.0, stretch_confirm_pct=3.0,
)
MARKED = "job_(SRA3)_(90x50)_4-0_(1-100)_input.pdf"
UNMARKED = "job_(90x50)_4-0_(1-100)_input.pdf"


def _plan(actual, expected=(94.0, 54.0), dpi=math.inf, hidden=(), limits=LIMITS, no_scale=True):
    return plan_pdf_page_resize(actual, expected, dpi, limits, hidden, no_scale=no_scale)


def _canvas(target, offset, decision=ResampleDecision.AUTO_CORRECT) -> PdfPagePlan:
    crop = (max(0.0, -2 * offset[0]), max(0.0, -2 * offset[1]))
    return PdfPagePlan(decision, METHOD_CANVAS, target, (1.0, 1.0), crop, offset, (math.inf, math.inf), "")


# --- политика ---------------------------------------------------------------

def test_smaller_page_gets_white_margins_without_scaling() -> None:
    plan = _plan((90.0, 50.0))
    assert plan.method == METHOD_CANVAS
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.target_mm == (94.0, 54.0)
    assert plan.scale == (1.0, 1.0) and plan.distortion_pct == 0
    assert plan.crop_mm == (0.0, 0.0)
    assert plan.enlarge_mm == pytest.approx((2.0, 2.0))


def test_larger_page_is_cropped_from_the_center_without_scaling() -> None:
    plan = _plan((96.0, 56.0))
    assert plan.method == METHOD_CANVAS and plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.scale == (1.0, 1.0)
    assert plan.crop_mm == pytest.approx((2.0, 2.0))
    assert plan.enlarge_mm == pytest.approx((-1.0, -1.0))
    # Без метки тот же размер — пропорциональное уменьшение.
    assert _plan((96.0, 56.0), no_scale=False).method == METHOD_SHRINK


def test_mixed_margin_and_crop() -> None:
    plan = _plan((96.0, 52.0))
    assert plan.method == METHOD_CANVAS
    assert plan.crop_mm == pytest.approx((2.0, 0.0))
    assert plan.enlarge_mm == pytest.approx((-1.0, 1.0))


def test_portrait_page_keeps_its_orientation() -> None:
    plan = _plan((52.0, 96.0))
    assert plan.target_mm == (54.0, 94.0)
    assert plan.enlarge_mm == pytest.approx((1.0, -1.0))
    assert plan.decision == ResampleDecision.AUTO_CORRECT


def test_thresholds_accept_ask_reject() -> None:
    assert _plan((94.05, 54.0)).decision == ResampleDecision.ACCEPT
    assert _plan((88.0, 48.0)).decision == ResampleDecision.ASK_CONFIRMATION  # 6 мм
    rejected = _plan((80.0, 40.0))  # 14 мм > 10
    assert rejected.decision == ResampleDecision.REJECT and rejected.method == METHOD_CANVAS


def test_hidden_margins_are_ignored_for_marked_file() -> None:
    hidden = ((2.0, 2.0),)
    assert _plan((90.0, 50.0), hidden=hidden, no_scale=False).method == METHOD_REVEAL
    plan = _plan((90.0, 50.0), hidden=hidden)
    assert plan.method == METHOD_CANVAS and plan.enlarge_mm == pytest.approx((2.0, 2.0))


def test_stretch_that_would_win_is_not_used_for_marked_file() -> None:
    # Большой формат: вытягивание на 0.5% — автоматически и мягче обрезки.
    unmarked = _plan((597.0, 400.0), expected=(600.0, 400.0), no_scale=False)
    assert unmarked.method == METHOD_STRETCH and unmarked.decision == ResampleDecision.AUTO_CORRECT
    plan = _plan((597.0, 400.0), expected=(600.0, 400.0))
    assert plan.method == METHOD_CANVAS and plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.scale == (1.0, 1.0)
    assert plan.enlarge_mm == pytest.approx((1.5, 0.0))


def test_low_dpi_does_not_change_canvas_decision() -> None:
    # Разрешение не меняется — о низком DPI сообщает отдельная проверка страницы.
    plan = _plan((90.0, 50.0), dpi=100.0)
    assert plan.decision == ResampleDecision.AUTO_CORRECT
    assert plan.effective_dpi == (100.0, 100.0)


# --- QuickFix ---------------------------------------------------------------

def test_quickfix_moves_mediabox_before_final_boxes() -> None:
    config = build_resize_quickfix({1: _plan((90.0, 52.0))}, 2.0)
    names = [fix["quickfix"] for fix in config["quickfixes"]]
    assert names == ["set_page_box", "set_page_box"]  # без scale_pages и enlarge_pages
    canvas, final = config["quickfixes"]
    assert canvas["instructions"] == [{
        "page_selector": "all", "which_box": "MediaBox", "from_box_or_absolute": "MediaBox",
        "left": 2.0, "bottom": 1.0, "right": 2.0, "top": 1.0, "unit": "mm", "when": "always",
    }]
    assert [item["which_box"] for item in final["instructions"]] == ["CropBox", "BleedBox", "TrimBox"]
    assert final["instructions"][2]["left"] == -2.0
    assert not any("scale" in json.dumps(fix) for fix in config["quickfixes"])


def test_quickfix_crop_offsets_are_negative() -> None:
    config = build_resize_quickfix({1: _plan((96.0, 56.0))}, 2.0)
    instruction = config["quickfixes"][0]["instructions"][0]
    assert (instruction["left"], instruction["bottom"], instruction["right"], instruction["top"]) == (
        -1.0, -1.0, -1.0, -1.0,
    )


def test_same_offsets_share_selector_and_zero_offset_is_skipped() -> None:
    plan = _plan((90.0, 50.0))
    config = build_resize_quickfix({1: plan, 2: plan}, 2.0)
    assert [item["page_selector"] for item in config["quickfixes"][0]["instructions"]] == ["all"]

    exact = _canvas((94.0, 54.0), (0.0, 0.0), ResampleDecision.ACCEPT)
    config = build_resize_quickfix({1: plan, 2: exact}, 2.0)
    assert [item["page_selector"] for item in config["quickfixes"][0]["instructions"]] == ["1"]
    only_exact = build_resize_quickfix({1: exact}, 2.0)
    assert [fix["quickfix"] for fix in only_exact["quickfixes"]] == ["set_page_box"]


def test_two_page_worst_case_fits_variable_limit() -> None:
    # Две страницы с разными сдвигами из трёх знаков и большим форматом.
    plans = {
        1: _canvas((1004.125, 704.375), (-4.999, 4.997)),
        2: _canvas((704.375, 1004.125), (4.123, -3.987)),
    }
    config = build_resize_quickfix(plans, 2.5)
    selectors = [item["page_selector"] for item in config["quickfixes"][0]["instructions"]]
    assert selectors == ["1", "2"]
    value = quickfix_variable(config)
    assert len(value) <= MAX_VARIABLE_LENGTH
    assert len(value) <= 950  # ~915: две страницы с разными сдвигами + итоговые боксы
    assert json.loads(value) == config


def test_mixed_canvas_and_fill_pages_are_handled() -> None:
    # В одном файле не бывает (файл либо помечен, либо нет), но функция не ломается.
    fill = _plan((96.0, 56.0), no_scale=False)
    config = build_resize_quickfix({1: fill, 2: _plan((90.0, 50.0))}, 2.0)
    names = [fix["quickfix"] for fix in config["quickfixes"]]
    assert names == ["scale_pages", "set_page_box", "set_page_box"]
    assert config["quickfixes"][0]["instructions"][0]["page_selector"] == "1"
    assert config["quickfixes"][1]["instructions"][0]["page_selector"] == "2"
    assert len(quickfix_variable(config)) <= MAX_VARIABLE_LENGTH


# --- проверка результата callas ----------------------------------------------

def _shifted_pdf(path: Path, size_mm, offset_mm, bleed_mm: float) -> None:
    """PDF, как после canvas: MediaBox начинается не в (0, 0)."""
    document = pymupdf.open()
    page = document.new_page(width=size_mm[0] * PT, height=size_mm[1] * PT)
    x0, y0 = -offset_mm[0] * PT, -offset_mm[1] * PT
    x1, y1 = x0 + size_mm[0] * PT, y0 + size_mm[1] * PT
    b = bleed_mm * PT
    document.xref_set_key(page.xref, "MediaBox", f"[{x0} {y0} {x1} {y1}]")
    document.xref_set_key(page.xref, "CropBox", f"[{x0} {y0} {x1} {y1}]")
    document.xref_set_key(page.xref, "BleedBox", f"[{x0} {y0} {x1} {y1}]")
    document.xref_set_key(page.xref, "TrimBox", f"[{x0 + b} {y0 + b} {x1 - b} {y1 - b}]")
    document.save(str(path))
    document.close()


def test_verify_accepts_mediabox_with_shifted_origin(tmp_path: Path) -> None:
    pdf = tmp_path / "r.pdf"
    _shifted_pdf(pdf, (94.0, 54.0), (2.0, 2.0), 2.0)
    verify_resized_pdf(pdf, {1: (94.0, 54.0)}, 2.0)


def test_resizer_sends_canvas_quickfix(tmp_path: Path) -> None:
    source = tmp_path / "in.pdf"
    _make_pdf(source, [(90.0, 50.0)])
    callas_out = tmp_path / "callas.pdf"
    _shifted_pdf(callas_out, (94.0, 54.0), (2.0, 2.0), 2.0)
    client = _FakeClient(callas_out)
    target = tmp_path / "out.pdf"

    CallasPdfResizer(client)(source, target, {1: _plan((90.0, 50.0))}, 2.0, "order")

    config = json.loads(client.calls[0]["variables"]["quickfix_config"])
    assert config["quickfixes"][0]["instructions"][0]["which_box"] == "MediaBox"
    assert target.is_file()


# --- BatchProcessor ---------------------------------------------------------

def _hidden_bleed_pdf(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=90 * PT, height=50 * PT)
    page.draw_rect(pymupdf.Rect(-2 * PT, -2 * PT, 92 * PT, 52 * PT), color=None, fill=(0, 0, 1))
    document.save(str(path))
    document.close()


def _inspect(root: Path, name: str, maker, **kwargs) -> FileCheck:
    folder = root / name.replace(".pdf", "")
    folder.mkdir()
    maker(folder / name)
    processor = BatchProcessor(folder, folder / "output", pdf_resizer=_fake_resizer([]), **kwargs)
    return processor.inspect_orders()[0].files[0]


def test_marked_pdf_with_hidden_bleed_gets_canvas_not_reveal(tmp_path: Path) -> None:
    unmarked = _inspect(tmp_path, UNMARKED, _hidden_bleed_pdf)
    assert unmarked.pdf_resize_plans[1].method == METHOD_REVEAL
    assert not unmarked.no_scale

    item = _inspect(tmp_path, MARKED, _hidden_bleed_pdf)
    assert item.no_scale
    assert item.needs_resample and item.errors == [] and item.warnings == []
    plan = item.pdf_resize_plans[1]
    assert plan.method == METHOD_CANVAS and plan.scale == (1.0, 1.0)
    assert item.size_fix["method"] == "canvas" and item.size_fix["auto"] is True
    assert item.size_fix["from_mm"] == [90.0, 50.0] and item.size_fix["to_mm"] == [94.0, 54.0]
    assert "без масштабирования: белые поля 2.00/2.00 мм с каждой стороны" in item.size_fix["detail"]
    assert item.resample_scale == 1.0


def test_marked_larger_pdf_is_cropped_not_shrunk(tmp_path: Path) -> None:
    sizes = lambda path: _make_pdf(path, [(96.0, 56.0)])  # noqa: E731
    unmarked = _inspect(tmp_path, UNMARKED, sizes)
    assert unmarked.size_fix["method"] == METHOD_SHRINK

    item = _inspect(tmp_path, "job_(90x50)_(ban)_4-0_(1-100)_input.pdf", sizes)
    assert item.size_fix["method"] == "canvas"
    assert "без масштабирования: обрезка 1.00/1.00 мм с каждой стороны" in item.size_fix["detail"]
    assert item.resample_crop_mm == pytest.approx((2.0, 2.0), abs=1e-3)


def test_marked_mixed_detail_mentions_margins_and_crop(tmp_path: Path) -> None:
    item = _inspect(tmp_path, MARKED, lambda path: _make_pdf(path, [(96.0, 52.0)]))
    assert item.size_fix["detail"].endswith(
        "без масштабирования: белые поля 0.00/1.00 мм с каждой стороны, "
        "обрезка 1.00/0.00 мм с каждой стороны"
    )


def test_marked_pdf_asks_and_confirmation_marks_canvas(tmp_path: Path) -> None:
    item = _inspect(
        tmp_path, MARKED, lambda path: _make_pdf(path, [(88.0, 48.0)])  # 6 мм > авто 4 мм
    )
    assert item.resample_decision == "ask_confirmation" and not item.needs_resample
    assert item.size_fix is None
    assert any("белые поля 3.00/3.00 мм" in warning for warning in item.warnings)

    BatchProcessor.confirm_resample(item, True)
    assert item.needs_resample
    assert item.size_fix["method"] == "canvas" and item.size_fix["auto"] is False


def test_marked_pdf_too_far_off_is_rejected(tmp_path: Path) -> None:
    item = _inspect(tmp_path, MARKED, lambda path: _make_pdf(path, [(80.0, 40.0)]))
    assert item.resample_decision == "reject" and not item.needs_resample
    assert item.errors and "пришлите макет нужного размера" in item.errors[-1]


def test_marked_pdf_is_sent_to_resizer_as_canvas(tmp_path: Path) -> None:
    pdf = tmp_path / "job_(SRA3P)_(90x50)_4-4_(1-100).pdf"
    _make_pdf(pdf, [(90.0, 50.0), (96.0, 56.0)])
    calls: list = []
    processor = BatchProcessor(tmp_path, tmp_path / "output", pdf_resizer=_fake_resizer(calls))
    order = processor.inspect_orders()[0]
    assert order.passed, order.files[0].errors

    refs = processor._build_page_refs(processor._ordered_files_for_creation(order))
    work = tmp_path / "work"
    work.mkdir()
    processor._prepare_page_pdfs(refs, work)

    assert calls == [(pdf.name, {1: (94.0, 54.0), 2: (94.0, 54.0)}, {1: "canvas", 2: "canvas"}, 2.0)]
    assert [(r.expected_width_mm, r.expected_height_mm) for r in refs] == [(94.0, 54.0)] * 2
    action = order.files[0].processing_actions[-1]
    assert [page["method"] for page in action["pages"]] == ["canvas", "canvas"]
    assert action["pages"][0]["enlarge_mm"] == pytest.approx([2.0, 2.0], abs=1e-3)
    assert action["pages"][1]["enlarge_mm"] == pytest.approx([-1.0, -1.0], abs=1e-3)
    assert action["pages"][1]["scale"] == [1.0, 1.0]
    json.dumps(action)  # действие сохраняется в JSON запуска


def test_canvas_check_survives_dto_json_round_trip(tmp_path: Path) -> None:
    # Планы QuickFix живут в памяти запуска (контекст координатора); в БД попадают
    # пометка size_fix и решение — они должны пережить JSON без потерь.
    item = _inspect(tmp_path, MARKED, lambda path: _make_pdf(path, [(96.0, 52.0)]))
    dto = json.loads(json.dumps(file_check_to_dto(item)))
    assert dto["size_fix"] == item.size_fix
    assert dto["size_fix"]["method"] == "canvas"
    assert dto["resample_decision"] == "auto_correct" and dto["resample_scale"] == 1.0
    assert dto["resample_crop_mm"] == pytest.approx([2.0, 0.0], abs=1e-3)


# --- Callas: автоматические поля вылета ---------------------------------------

def test_auto_bleed_skips_marked_file_first() -> None:
    processor = SimpleNamespace(profile=DEFAULT_PROFILE, size_extra_mm=4.0, tolerance_mm=0.5)
    check = FileCheck(path=Path("/x/T_(SRA3)_(97x97)_4-0_(5-1)_face.pdf"))
    # Без разбора имени и размеров — причина метки всё равно первой.
    assert candidate_reason(check, processor) == "no_scale_name"


def test_marked_file_never_gets_callas_bleed(tmp_path: Path) -> None:
    client = BleedClient(tmp_path.parent / f"{tmp_path.name}-jobs")
    hook = CallasAutoBleed(CallasFixService(client), semaphore=threading.BoundedSemaphore(2))
    root = tmp_path / "in"
    root.mkdir()
    art_pdf(root / "T_(SRA3)_(97x97)_4-0_(5-1)_face.pdf", 97, 97)

    processor = BatchProcessor(
        root, root / "PDF", correction_policy="auto", auto_bleed=hook, pdf_resizer=_fake_resizer([]),
    )
    item = processor.inspect_orders()[0].files[0]

    assert client.inputs == []  # Callas не запускался
    assert item.callas_source_fix is None and item.content_path is None
    assert item.size_fix["method"] == "canvas" and item.size_fix["auto"] is True
    assert "белые поля 2.00/2.00 мм" in item.size_fix["detail"]
