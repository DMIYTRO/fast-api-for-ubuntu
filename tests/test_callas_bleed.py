"""Вердикт «Callas только дорисовал вылеты» и проверка содержимого по исходнику.

Реальный случай (заказ 26124497): callas дорисовал вылеты 2 мм, растянув крайнюю
строку пикселей: четыре картинки 1×4770 px на 2×101 мм (13 dpi), а макет 312 dpi.
Проверка давала «effective DPI 12.7» → ошибка файла → заказ красный. Без сети:
рендер — фейковый callas из conftest, PDF собираются PyMuPDF.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from core.fix_compare import build_fix_comparison
from processing.batch_processor import CHECK_SCOPES, BatchProcessor
from processing.models import FileCheck
from services.callas_bleed import (
    bleed_only_verdict,
    not_bleed_only_warning,
    order_fix_context,
    state_bleed_only,
)
from services.callas_fix import CallasFixService, comparison_payload, fix_dir_for, read_state_file
from services.callas_source_fix import mark_applied
from tests.test_pdf_resize import _fake_resizer

MM = 72 / 25.4
FACE = "T_(97x97)_4-0_(5-1)_face.pdf"

# Сводки из настоящих state.json (PDF/CallasFix, только чтение), значимые поля.
REAL_97_TO_101 = {  # …(126816-26124497)_curve4_12-face.source: только вылеты
    "page": 1,
    "original": {"page_width_mm": 97.0, "page_height_mm": 97.0,
                 "trimbox_width_mm": 97.0, "trimbox_height_mm": 97.0, "has_trimbox": False},
    "result": {"page_width_mm": 101.0, "page_height_mm": 101.0,
               "trimbox_width_mm": 97.0, "trimbox_height_mm": 97.0, "has_trimbox": True},
    "page_size_changed": True,
    "center_shift_mm": {"x": 0.0, "y": 0.0, "distance": 0.0},
    "content_shift_mm": None,
    "changed_percent": 7.599,
    "changed_content_percent": 7.673,
    "scale_x": None,
    "scale_y": None,
    "scale_pairs": 0,
    "distortion": False,
    "overlay_changed_percent": 0.0,
}
REAL_98_TO_94_SCALED = {  # …(14717790-2)_offset-face_fix4.source: макет уменьшен до 95.9 %
    "page": 1,
    "original": {"page_width_mm": 98.0, "page_height_mm": 58.0,
                 "trimbox_width_mm": 90.0, "trimbox_height_mm": 50.0, "has_trimbox": True},
    "result": {"page_width_mm": 94.0, "page_height_mm": 54.0,
               "trimbox_width_mm": 90.0, "trimbox_height_mm": 50.0, "has_trimbox": True},
    "page_size_changed": True,
    "center_shift_mm": {"x": 0.0, "y": 0.0, "distance": 0.0},
    "content_shift_mm": {"x": -0.01, "y": 0.01, "distance": 0.01},
    "changed_percent": 37.301,
    "changed_content_percent": 37.74,
    "scale_x": 0.959,
    "scale_y": 0.959,
    "scale_pairs": 1208,
    "distortion": False,
    "overlay_changed_percent": 45.74,
}


def state(summary: dict, plan: str = "size_correction") -> dict:
    return {"status": "ready", "plan": {"key": plan}, "summary": dict(summary)}


# --------------------------------------------------------------------------- вердикт


def test_real_97_to_101_bleed_case_is_bleed_only():
    ok, reasons = state_bleed_only(state(REAL_97_TO_101), format_mm=(97, 97), bleed_mm=2.0)
    assert ok, reasons
    assert reasons == []


def test_real_scaled_98x58_to_94x54_case_is_not_bleed_only():
    ok, reasons = state_bleed_only(state(REAL_98_TO_94_SCALED), format_mm=(90, 50), bleed_mm=2.0)
    assert not ok
    assert any("масштабирован (95.9 %)" in reason for reason in reasons)


def test_other_plan_is_never_bleed_only():
    ok, reasons = state_bleed_only(state(REAL_97_TO_101, plan="remove_clipping"), format_mm=(97, 97), bleed_mm=2)
    assert not ok and reasons == ["план не «Коррекция размера макета»"]
    # Без плана в сводке — тоже нет.
    assert bleed_only_verdict(REAL_97_TO_101, format_mm=(97, 97), bleed_mm=2)[0] is False


def measured(**changes) -> dict:
    summary = {**REAL_97_TO_101, "plan_key": "size_correction", "trim_changed_percent": 0.0}
    summary.update(changes)
    return summary


def test_both_orientations_of_the_format_are_valid():
    summary = measured(
        result={"page_width_mm": 94.0, "page_height_mm": 54.0, "trimbox_width_mm": 90.0, "trimbox_height_mm": 50.0},
    )
    assert bleed_only_verdict(summary, format_mm=(90, 50), bleed_mm=2)[0]
    assert bleed_only_verdict(summary, format_mm=(50, 90), bleed_mm=2)[0]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"trim_changed_percent": 3.2}, "внутри формата изменилось 3.2 %"),
        ({"scale_x": 1.0, "scale_y": 1.01}, "масштабирован (101.0 %)"),
        ({"distortion": True}, "пропорции макета искажены"),
        ({"content_shift_mm": {"distance": 0.4}}, "макет сдвинут на 0.40 мм"),
        ({"center_shift_mm": {"distance": 0.25}}, "формат (TrimBox) сдвинут на 0.25 мм"),
        ({"result": {"page_width_mm": 99.0, "page_height_mm": 99.0,
                     "trimbox_width_mm": 97.0, "trimbox_height_mm": 97.0}},
         "страница результата 99×99 мм, ожидается 101×101 мм"),
        ({"result": {"page_width_mm": 101.0, "page_height_mm": 101.0,
                     "trimbox_width_mm": 101.0, "trimbox_height_mm": 101.0}},
         "TrimBox результата 101×101 мм, ожидается 97×97 мм"),
        ({"original_page_count": 2, "result_page_count": 2}, "не сравнены страницы 2"),
        ({"original_page_count": 1, "result_page_count": 2}, "число страниц изменилось: 1 → 2"),
    ],
)
def test_anything_beyond_drawing_bleed_is_not_bleed_only(changes, expected):
    ok, reasons = bleed_only_verdict(measured(**changes), format_mm=(97, 97), bleed_mm=2)
    assert not ok
    assert any(expected in reason for reason in reasons), reasons


def test_scale_within_tolerance_and_unknown_format_are_accepted():
    assert bleed_only_verdict(measured(scale_x=1.0015, scale_y=0.999), format_mm=(97, 97), bleed_mm=2)[0]
    # Формат неизвестен — размеры не сверяются, остальное — да.
    assert bleed_only_verdict(measured(), format_mm=None, bleed_mm=2)[0]


def test_every_compared_page_must_be_bleed_only():
    second = {**measured(), "page": 2, "trim_changed_percent": 12.0}
    summary = measured(original_page_count=2, result_page_count=2, pages=[second])
    ok, reasons = bleed_only_verdict(summary, format_mm=(97, 97), bleed_mm=2)
    assert not ok and reasons == ["стр. 2: внутри формата изменилось 12.0 % изображения"]
    second["trim_changed_percent"] = 0.1
    assert bleed_only_verdict(summary, format_mm=(97, 97), bleed_mm=2) == (True, [])


def test_unmeasured_trim_change_is_not_bleed_only():
    summary = measured(trim_changed_percent=None, overlay_changed_percent=None)
    ok, reasons = bleed_only_verdict(summary, format_mm=(97, 97), bleed_mm=2)
    assert not ok and reasons == ["изменение внутри формата не измерено"]


def test_order_context_uses_name_format_and_profile_bleed():
    run = {"options": {"direction": "digital"}}
    entry = {"parsed": {"width_mm": 97.0, "height_mm": 97.0}}
    order = {"files": [entry, {"parsed": {"width_mm": 97.0, "height_mm": 97.0}}]}
    assert order_fix_context(run, order, entry) == ((97.0, 97.0), 2.0)
    assert order_fix_context(run, order) == ((97.0, 97.0), 2.0)
    mixed = {"files": [entry, {"parsed": {"width_mm": 90.0, "height_mm": 50.0}}]}
    assert order_fix_context(run, mixed) == (None, 2.0)


def test_every_check_kind_is_classified():
    assert set(CHECK_SCOPES.values()) == {"geometry", "content"}
    assert CHECK_SCOPES["pdf_effective_dpi"] == "content"
    assert CHECK_SCOPES["pdf_page_size"] == "geometry"


# --------------------------------------------------------------------- PDF-фикстуры


def gradient(width: int, height: int) -> fitz.Pixmap:
    """CMYK-растр с вертикальным градиентом (без RGB-предупреждений)."""
    rows = b"".join(bytes((y * 255 // max(1, height - 1), 70, 20, 0)) * width for y in range(height))
    return fitz.Pixmap(fitz.csCMYK, width, height, rows, 0)


def art_pdf(path: Path, width_mm: float, height_mm: float, *, dpi: float = 300.0) -> Path:
    """Макет клиента: картинка на всю страницу с разрешением ``dpi``, без TrimBox."""
    document = fitz.open()
    page = document.new_page(width=width_mm * MM, height=height_mm * MM)
    pixmap = gradient(round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi))
    page.insert_image(page.rect, pixmap=pixmap)
    document.save(path)
    document.close()
    return path


def bleed_pdf(source: Path, target: Path, *, bleed_mm: float = 2.0, scale: float = 1.0) -> Path:
    """Как callas size_correction: страница + 2 вылета, полосы по 1 px растянуты на вылеты.

    ``scale`` ≠ 1 — callas вместо полей масштабировал макет (не «только вылеты»).
    """
    original = fitz.open(source)
    width, height = original[0].rect.width, original[0].rect.height
    bleed = bleed_mm * MM
    document = fitz.open()
    page = document.new_page(width=width + 2 * bleed, height=height + 2 * bleed)
    art_w, art_h = width * scale, height * scale
    left = bleed + (width - art_w) / 2
    top = bleed + (height - art_h) / 2
    # Страница клиента без изменений (как XObject), поверх — только поля.
    page.show_pdf_page(fitz.Rect(left, top, left + art_w, top + art_h), original, 0)
    original.close()
    full_w, full_h = page.rect.width, page.rect.height
    for rect, size in (
        (fitz.Rect(0, 0, bleed, full_h), (1, 40)),
        (fitz.Rect(full_w - bleed, 0, full_w, full_h), (1, 40)),
        (fitz.Rect(0, 0, full_w, bleed), (40, 1)),
        (fitz.Rect(0, full_h - bleed, full_w, full_h), (40, 1)),
    ):
        page.insert_image(rect, pixmap=gradient(*size))
    page.set_trimbox(fitz.Rect(bleed, bleed, bleed + width, bleed + height))
    document.save(target)
    document.close()
    return target


class BleedClient:
    """Фейковый callas size_correction: дорисовывает вылеты по входному PDF."""

    def __init__(self, root: Path, *, scale: float = 1.0) -> None:
        self.root = root
        self.scale = scale
        self.inputs: list[Path] = []

    def process(self, pdf, profile, mode, *, job_id=None, **_kwargs):
        self.inputs.append(Path(pdf))
        out = self.root / job_id / "out"
        out.mkdir(parents=True)
        bleed_pdf(Path(pdf), out / "fixed.pdf", scale=self.scale)
        return SimpleNamespace(
            job_id=job_id, mode=mode, status="fixed", exit_code=5,
            summary={"errors": 0, "warnings": 0, "infos": 0, "fixups_applied": 2, "fixups_failed": 0},
            output_pdf=out / "fixed.pdf", report_xml=None,
        )

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id


# ------------------------------------------------------------- сравнение и сервис


def test_compare_measures_change_strictly_inside_the_trim(tmp_path):
    source = art_pdf(tmp_path / "source.pdf", 97, 97)
    fixed = bleed_pdf(source, tmp_path / "fixed.pdf")
    summary = build_fix_comparison(source, fixed, tmp_path / "compare").summary
    assert summary["trim_changed_percent"] <= 0.5
    assert summary["original_page_count"] == summary["result_page_count"] == 1
    assert bleed_only_verdict({**summary, "plan_key": "size_correction"}, format_mm=(97, 97), bleed_mm=2) == (True, [])

    scaled = bleed_pdf(source, tmp_path / "scaled.pdf", scale=0.96)
    changed = build_fix_comparison(source, scaled, tmp_path / "compare-scaled").summary
    assert changed["trim_changed_percent"] > 0.5
    ok, reasons = bleed_only_verdict({**changed, "plan_key": "size_correction"}, format_mm=(97, 97), bleed_mm=2)
    assert not ok and any("внутри формата" in reason for reason in reasons)


def test_execute_writes_bleed_only_into_state_and_payload(tmp_path):
    source = art_pdf(tmp_path / FACE, 97, 97)
    service = CallasFixService(BleedClient(tmp_path / "jobs"))
    service.start(source, "size_correction")
    result = service.execute(source, "size_correction", format_mm=(97, 97), bleed_mm=2.0)

    assert result["status"] == "ready", result.get("error")
    persisted = read_state_file(source)["summary"]
    assert persisted["bleed_only"] is True
    assert persisted["bleed_only_reasons"] == []
    assert persisted["plan_key"] == "size_correction"
    payload = comparison_payload(read_state_file(source), "/base")
    assert payload["summary"]["bleed_only"] is True
    assert payload["summary"]["bleed_only_reasons"] == []

    _content, info = service.accepted_source_copy(source) if service.decide(source, "accept") else (None, None)
    assert info["bleed_only"] is True and info["bleed_only_reasons"] == []
    assert info["baseline_pdf"] == str(source)


def test_execute_with_scaled_result_or_other_plan_is_not_bleed_only(tmp_path):
    source = art_pdf(tmp_path / FACE, 97, 97)
    service = CallasFixService(BleedClient(tmp_path / "jobs", scale=0.96))
    service.start(source, "size_correction")
    summary = service.execute(source, "size_correction", format_mm=(97, 97), bleed_mm=2.0)["summary"]
    assert summary["bleed_only"] is False
    assert any("внутри формата" in reason for reason in summary["bleed_only_reasons"])

    service = CallasFixService(BleedClient(tmp_path / "jobs-2"))
    service.start(source, "remove_clipping")
    summary = service.execute(source, "remove_clipping", format_mm=(97, 97))["summary"]
    assert summary["bleed_only"] is False
    assert summary["bleed_only_reasons"] == ["план не «Коррекция размера макета»"]


def test_two_page_pdf_compares_every_page(tmp_path):
    first = art_pdf(tmp_path / "p1.pdf", 97, 97)
    source = tmp_path / "T_(97x97)_4-4_(5-1).pdf"
    with fitz.open(first) as one, fitz.open() as both:
        both.insert_pdf(one)
        both.insert_pdf(one)
        both.save(source)

    class TwoPageBleed(BleedClient):
        def process(self, pdf, profile, mode, *, job_id=None, **kwargs):
            result = super().process(pdf, profile, mode, job_id=job_id, **kwargs)
            pages = fitz.open()
            for number in range(2):
                with fitz.open(pdf) as document:
                    single = fitz.open()
                    single.insert_pdf(document, from_page=number, to_page=number)
                    part = self.root / job_id / f"part{number}.pdf"
                    single.save(part)
                    single.close()
                fixed_part = self.root / job_id / f"fixed{number}.pdf"
                bleed_pdf(part, fixed_part)
                with fitz.open(fixed_part) as fixed:
                    pages.insert_pdf(fixed)
            pages.save(result.output_pdf)
            pages.close()
            return result

    service = CallasFixService(TwoPageBleed(tmp_path / "jobs"))
    service.start(source, "size_correction")
    summary = service.execute(source, "size_correction", format_mm=(97, 97), bleed_mm=2.0)["summary"]
    assert [page["page"] for page in summary["pages"]] == [2]
    assert summary["bleed_only"] is True, summary["bleed_only_reasons"]


# ------------------------------------------------ повторная проверка: геометрия и содержимое


def processor(root: Path) -> BatchProcessor:
    return BatchProcessor(root, root / "PDF", correction_policy="ask", pdf_resizer=_fake_resizer([]))


def waiting_item(root: Path, *, dpi: float = 300.0) -> tuple[BatchProcessor, FileCheck]:
    art_pdf(root / FACE, 97, 97, dpi=dpi)
    batch = processor(root)
    item = batch.inspect_orders()[0].files[0]
    # 300 dpi — коррекцию можно подтвердить; 150 dpi — заполнение недопустимо.
    assert item.resample_decision == ("ask_confirmation" if dpi >= 250 else "reject")
    return batch, item


def dpi_errors(check: FileCheck) -> list[str]:
    return [message for message in check.errors if "effective DPI" in message]


def test_bleed_strips_of_a_bleed_only_fix_raise_no_dpi_error(tmp_path):
    batch, item = waiting_item(tmp_path)
    fixed = bleed_pdf(item.path, tmp_path / "fixed.pdf")

    replacement = batch.reinspect_pdf_file(item, fixed, fix={"bleed_only": True})
    assert replacement.errors == []
    assert replacement.resample_decision == "accept"
    assert round(replacement.actual_width_mm) == 101  # геометрия — исправленный PDF
    assert replacement.dpi == pytest.approx(300, abs=1)  # содержимое — исходник
    assert replacement.input_path == fixed and replacement.path == item.path

    # Прежнее поведение (не «только вылеты»): полосы 12.7 dpi — ошибка.
    everything = batch.reinspect_pdf_file(item, fixed, fix={"bleed_only": False})
    assert dpi_errors(everything) and "effective DPI 12.7" in dpi_errors(everything)[0]


def test_low_resolution_client_artwork_still_fails_after_a_bleed_only_fix(tmp_path):
    batch, item = waiting_item(tmp_path, dpi=150.0)
    fixed = bleed_pdf(item.path, tmp_path / "fixed.pdf")
    replacement = batch.reinspect_pdf_file(item, fixed, fix={"bleed_only": True})
    assert dpi_errors(replacement) == ["PDF страница 1: effective DPI 150.0; требуется не менее 250"]
    assert replacement.resample_decision == "accept"


def test_reinspection_reads_bleed_only_from_the_fix_state(tmp_path):
    batch, item = waiting_item(tmp_path)
    directory = fix_dir_for(item.path)
    directory.mkdir(parents=True)
    accepted = bleed_pdf(item.path, directory / "accepted.pdf")
    state_file = directory / "state.json"
    state_file.write_text(json.dumps({
        "status": "accepted", "source": {"name": item.path.name},
        "summary": {"bleed_only": True},
    }), encoding="utf-8")
    assert batch.reinspect_pdf_file(item, accepted).errors == []

    state_file.write_text(json.dumps({
        "status": "accepted", "source": {"name": item.path.name},
        "summary": {"bleed_only": False},
    }), encoding="utf-8")
    assert dpi_errors(batch.reinspect_pdf_file(item, accepted))


def test_mark_applied_explains_the_verdict():
    check = FileCheck(path=Path("face.pdf"))
    reasons = ["внутри формата изменилось 3.2 % изображения"]
    mark_applied(check, {"plan": "size_correction", "bleed_only": False,
                         "bleed_only_reasons": reasons, "baseline_pdf": Path("/x/face.pdf")})
    assert check.warnings[1] == not_bleed_only_warning(reasons)
    assert "все проверки выполнены по исправленному PDF" in check.warnings[1]
    assert check.callas_source_fix["baseline_pdf"] == "/x/face.pdf"

    bleed = FileCheck(path=Path("face.pdf"))
    mark_applied(bleed, {"plan": "size_correction", "bleed_only": True, "bleed_only_reasons": []})
    assert "проверены по исходному файлу" in bleed.warnings[1]
    assert bleed.callas_source_fix["bleed_only"] is True
