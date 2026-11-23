"""Печать красного заказа со сборкой итогового PDF: какие заказы допускаются и как собираются."""
from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from processing.batch_processor import BatchProcessor
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.dto import order_check_to_dto
from services.forced_print import (
    ForcedPrintBuildError,
    assess_local_errors,
    assess_order,
    assess_order_check,
)
from services.order_journal import build_order_journal
from services.print_readiness import is_forced_build, needs_print_build, print_blockers, print_mode
from tests.fake_callas import FakeCallasMerger
from tests.test_forced_print_workflow import LOW, TWO_PAGE, make_pdf

PDF_LOW = "PDF страница 1: effective DPI 199.8; требуется не менее 250"
PDF_LOWER = "PDF страница 1: effective DPI 197.0; требуется не менее 250"
RASTER_LOW = "разрешение 180.0x181.0 DPI; по обеим осям требуется не меньше 250 DPI"

# Каждая из этих ошибок блокирует печать даже рядом с низким разрешением.
BLOCKING = {
    "page_rule": "двухстраничный PDF недопустим для односторонней печати",
    "too_many_pages": "в заказе 3 страницы, а для цветности 4-4 допускается 2",
    "size_mismatch": "размер 150.0x150.0 мм; ожидается 164.0x164.0 мм: после коррекции эффективное разрешение 180 DPI ниже 250 DPI",
    "pdf_size": "PDF: размер 150.0x150.0 мм; ожидается 164.0x164.0 мм: обрезка слишком большая",
    "pdf_page_size": "PDF страница 1 имеет размер 150.0x150.0 мм; ожидается 164.0x164.0 мм",
    "correction_rejected": "пользователь отказался от предложенной коррекции размера",
    "unreadable": "не удалось прочитать файл: повреждён",
    "tiff_alpha": "TIFF содержит альфа-канал",
    "tiff_layers": "TIFF содержит несведённые слои, но ImageMagick не смог их прочитать",
    "missing_face": "не найден обязательный файл face",
    "missing_back": "для двусторонней печати не найден файл back",
    "unknown_dpi": "разрешение не определено DPI; по обеим осям требуется не меньше 250 DPI",
    "other": "что-то совсем другое",
}


def diagnostic_order(tmp_path: Path, **patch) -> dict:
    pdf = tmp_path / "diag.pdf"
    pdf.write_bytes(b"%PDF")
    order = {
        "status": "error", "pdf_path": str(pdf), "pdf_kind": "diagnostic", "current_pdf_revision": 1,
        "errors": [], "processing_errors": [],
        "files": [{"path": str(tmp_path / "a.pdf"), "errors": [PDF_LOW, PDF_LOWER], "resample_decision": "accept"}],
        "pitstop": {"execution_status": "completed", "verdict": "error", "checked_revision": 1},
    }
    order.update(patch)
    return order


def test_only_low_dpi_errors_are_eligible_with_lowest_value_and_norm():
    assessment = assess_local_errors([PDF_LOW, PDF_LOWER, RASTER_LOW])
    assert assessment.eligible
    assert (assessment.min_dpi, assessment.required_dpi) == (180.0, 250.0)
    assert len(assessment.low_dpi_messages) == 3


@pytest.mark.parametrize("name", sorted(BLOCKING))
def test_any_other_local_error_blocks(name):
    assessment = assess_local_errors([PDF_LOW, BLOCKING[name]])
    assert not assessment.eligible
    assert assessment.blocking_codes


def test_no_errors_or_processing_failures_are_not_eligible_but_preview_errors_are_ignored():
    assert not assess_local_errors([]).eligible
    assert not assess_local_errors([PDF_LOW], processing_errors=["Диагностический PDF: в диагностический PDF не вошли: b.tif"]).eligible
    assert assess_local_errors([PDF_LOW], processing_errors=["Превью a.pdf: сбой рендера"]).eligible


def test_dto_hard_blocks_pending_confirmation_layers_and_order_errors(tmp_path):
    assert assess_order(diagnostic_order(tmp_path)).eligible
    assert not assess_order(diagnostic_order(tmp_path, pending_confirmations=1)).eligible
    layered = diagnostic_order(tmp_path)
    layered["files"][0]["has_unflattened_layers"] = True
    assert not assess_order(layered).eligible
    assert not assess_order(diagnostic_order(tmp_path, errors=[BLOCKING["missing_back"]])).eligible
    assert not assess_order(diagnostic_order(tmp_path, files=[])).eligible


def test_print_blockers_offer_forced_build_for_low_dpi_diagnostic_order(tmp_path):
    order = diagnostic_order(tmp_path)
    blockers = print_blockers(order)
    assert [(item["kind"], item["code"]) for item in blockers] == [("confirm", "forced_print_build")]
    assert print_mode(blockers) == "forced" and is_forced_build(blockers)
    message = blockers[0]["message"]
    assert message.startswith("Заказ с ошибками (низкое разрешение: от 197 DPI при норме 250).")
    assert "Будет собран итоговый PDF и отправлен в печать под вашу ответственность." in message
    assert "overprint" not in message
    with_overprint = diagnostic_order(tmp_path, prepress={"overprint": {"status": "ok", "blocking_count": 4}})
    assert "overprint" in print_blockers(with_overprint)[0]["message"]


def test_diagnostic_pitstop_state_does_not_matter_for_a_forced_build(tmp_path):
    for pitstop in ({"execution_status": "failed", "verdict": "error", "checked_revision": 1}, None):
        blockers = print_blockers(diagnostic_order(tmp_path, pitstop=pitstop))
        assert [item["code"] for item in blockers] == ["forced_print_build"]


def test_other_errors_keep_the_no_final_pdf_blocker(tmp_path):
    order = diagnostic_order(tmp_path)
    order["files"][0]["errors"].append(BLOCKING["page_rule"])
    blockers = print_blockers(order)
    assert blockers[0]["code"] == "no_final_pdf" and print_mode(blockers) == "blocked"
    # Решение по исправлению Callas и подтверждение коррекции по-прежнему блокируют.
    assert print_mode(print_blockers(diagnostic_order(tmp_path), {"status": "ready"})) == "blocked"
    assert print_mode(print_blockers(diagnostic_order(tmp_path, pending_confirmations=1))) == "blocked"
    # Готовый производственный PDF — прежнее подтверждение без сборки.
    production = diagnostic_order(tmp_path, pdf_kind="production")
    assert [item["code"] for item in print_blockers(production)] == ["forced_print_confirmation"]
    # Жёлтый заказ с одним лишь низким разрешением: без блокеров, но со сборкой при печати.
    assert print_blockers(diagnostic_order(tmp_path, status="warning")) == []


def test_yellow_diagnostic_order_builds_on_print_or_is_blocked(tmp_path):
    yellow = diagnostic_order(tmp_path, status="warning")
    assert print_blockers(yellow) == [] and needs_print_build(yellow)
    # PitStop диагностического PDF для сборки не важен (проверяется собранный PDF).
    assert print_blockers({**yellow, "current_pdf_revision": 2}) == []
    # Жёлтый заказ с готовым производственным PDF печатается как раньше, без сборки.
    assert not needs_print_build({**yellow, "pdf_kind": "production"})
    assert print_blockers({**yellow, "pdf_kind": "production"}) == []
    # Красный заказ — путь forced_print_build, а не print_build.
    assert not needs_print_build(diagnostic_order(tmp_path))
    # Любая другая локальная ошибка: диагностический PDF не печатается — блокер no_final_pdf.
    for name in ("pdf_size", "missing_back", "unreadable", "other"):
        order = diagnostic_order(tmp_path, status="warning")
        order["files"][0]["errors"].append(BLOCKING[name])
        blockers = print_blockers(order)
        assert [(item["kind"], item["code"]) for item in blockers] == [("file", "no_final_pdf")], name
        assert "диагностический PDF" in blockers[0]["message"]
        assert print_mode(blockers) == "blocked" and not needs_print_build(order)
    no_errors = diagnostic_order(tmp_path, status="warning")
    no_errors["files"][0]["errors"] = []
    assert print_mode(print_blockers(no_errors)) == "blocked"


def processor(root: Path, **kwargs) -> BatchProcessor:
    return BatchProcessor(root, root / "PDF", pdf_merger=FakeCallasMerger(), correction_policy="ask", **kwargs)


def test_forced_print_pdf_uses_the_production_pipeline_without_the_passed_gate(tmp_path, monkeypatch):
    make_pdf(tmp_path / LOW, dpi=200)
    steps = []
    monkeypatch.setattr(BatchProcessor, "_convert_rgb_to_cmyk", lambda self, pdf, order: steps.append("color"))
    batch = processor(tmp_path, pdf_prepress_checker=lambda pdf, order_id, previous: steps.append("prepress") or {"status": "completed"})
    order = batch.inspect_order_files([tmp_path / LOW])
    assert not order.passed
    assert batch.create_pdfs([order]) == []  # обычная сборка красный заказ пропускает

    pdf = batch.create_forced_print_pdf(order, tmp_path / "build")

    assert pdf == tmp_path / "build" / f"{Path(LOW).stem}.pdf" and pdf.is_file()
    assert steps == ["color", "prepress"]
    assert order.prepress == {"status": "completed"}


def test_forced_print_pdf_never_upscales_a_low_dpi_raster(tmp_path):
    make_pdf(tmp_path / LOW, dpi=200)
    batch = processor(tmp_path)
    order = batch.inspect_order_files([tmp_path / LOW])
    item = order.files[0]
    item.actual_format, item.dpi_x, item.dpi_y = "JPEG", 150.0, 150.0
    item.content_path = tmp_path / "photo.jpg"
    item.needs_resample, item.resample_target_mm = True, (164.0, 164.0)
    with pytest.raises(ValueError, match="увеличение разрешения"):
        batch.create_forced_print_pdf(order, tmp_path / "build")


def adapter(root: Path) -> BatchProcessorAdapter:
    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(root), direction="digital"),
        processor_factory=partial(BatchProcessor, pdf_merger=FakeCallasMerger()),
    )


def test_adapter_reinspects_sources_and_builds(tmp_path):
    make_pdf(tmp_path / LOW, dpi=200)
    checker = adapter(tmp_path)
    dto = order_check_to_dto(checker.processor.inspect_order_files([tmp_path / LOW]))
    assert assess_order({**dto, "status": "error", "pdf_kind": "diagnostic"}).eligible

    build = checker.build_forced_print_pdf(dto, tmp_path / "build")

    assert build.pdf_path.is_file()
    assert build.assessment.eligible and round(build.assessment.min_dpi) == 200


def test_adapter_refuses_page_rule_violation_and_missing_sources(tmp_path):
    make_pdf(tmp_path / TWO_PAGE, dpi=200, pages=2)
    checker = adapter(tmp_path)
    order = checker.processor.inspect_order_files([tmp_path / TWO_PAGE])
    assert not assess_order_check(order).eligible
    dto = order_check_to_dto(order)
    # Даже если бы DTO выглядел подходящим, свежий осмотр видит нарушение правила страниц.
    dto["errors"], dto["files"][0]["errors"] = [], [PDF_LOW]
    with pytest.raises(ForcedPrintBuildError, match="другие ошибки"):
        checker.build_forced_print_pdf(dto, tmp_path / "build")
    dto["files"][0]["path"] = str(tmp_path / "gone.pdf")
    with pytest.raises(ForcedPrintBuildError, match="не найден исходный файл"):
        checker.build_forced_print_pdf(dto, tmp_path / "build")


def test_journal_shows_forced_build_decision_and_revision_origin():
    events = [
        {"id": 1, "type": "order.pdf_revision", "created_at": "2026-11-23T10:00:00+00:00",
         "data": {"status": "error", "order": {"current_pdf_revision": 2, "current_pdf_created_by": "forced_print_build"}}},
        {"id": 2, "type": "order.forced_print", "created_at": "2026-11-23T10:00:01+00:00",
         "data": {"errors": [PDF_LOWER], "pdf_revision": 2, "forced": True, "build": True,
                  "created_by_action": "forced_print_build",
                  "forced_print_build": {"min_dpi": 197.0, "required_dpi": 250.0, "low_dpi_count": 17,
                                         "status_after_build": "error", "pitstop_verdict": "error"}}},
    ]
    actions = [{"id": 5, "action": "print", "status": "prepared", "created_at": "2026-11-23T10:00:02",
                "cms_response": {"forced_print_build": {"pdf_revision": 2, "min_dpi": 197.0, "status_after_build": "error"}}}]
    entries = {entry["code"]: entry for entry in build_order_journal({"order_id": "26252239"}, events, actions)["entries"]}
    forced = entries["forced_print_build"]
    assert forced["severity"] == "warning" and forced["count"] == 17
    assert forced["detail"] == "Низкое разрешение: от 197 DPI при норме 250"
    assert forced["data"]["pdf_revision"] == 2
    assert "forced_print" not in entries
    assert entries["pdf_revision"]["data"]["created_by_action"] == "forced_print_build"
    assert entries["action:print"]["data"]["forced_print_build"]["pdf_revision"] == 2
