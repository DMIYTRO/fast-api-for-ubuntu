"""Overprint итогового PDF: разбор отчёта callas, технические краски, статус заказа.

Решение оператора: overprint на любом объекте, кроме технических красок (белила, лак,
контур реза…), — ошибка заказа (красная группа) в любой политике проверки. Только
технические краски — справка; сбой проверки — предупреждение. Без сети: фейковый callas,
фейковый PitStop и процессор-заглушка.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config.overprint import DEFAULT_OVERPRINT_IGNORE_SPOTS
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.callas.prepress import OVERPRINT_PROFILE, PrepressAnalyzer
from services.callas.prepress_reports import OVERPRINT_SAMPLE_LIMIT, overprint_report
from services.callas.report import parse_report_root
from services.coordinator import _completed_order_status
from services.dto import order_check_to_dto
from services.issue_groups import issue_groups_for_order_dto
from services.order_journal import _prepress_entries
from services.overprint import (
    is_technical_spot,
    normalize_spot_name,
    overprint_issue_groups,
    overprint_status_effect,
)
from services.pdf_routing import completed_status
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopReport,
    PitStopSeverity,
)
from tests.fake_callas import FakeCallasProcessClient, overprint_report_xml

IGNORE = DEFAULT_OVERPRINT_IGNORE_SPOTS
NAME = "22_KS(P)_(90x50)_4-0_(12287-26237201)"


def report_of(hits, spaces=None, ignore=IGNORE):
    return overprint_report(parse_report_root(overprint_report_xml(hits, spaces).encode()), ignore)


# ----------------------------------------------------------------------------- parser
def test_process_black_is_blocking_with_a_short_sample():
    report = report_of([("Fill", 1, "CS1", [0.0, 0.0, 0.0, 1.0])])
    assert report["count"] == 1 and report["blocking_count"] == 1 and report["blocking_pages"] == [1]
    assert report["by_kind"]["process"] == 1
    assert report["samples"] == ["Заливка K100 (DeviceCMYK), стр. 1"]


def test_rgb_white_is_blocking_and_flagged_as_disappearing():
    report = report_of([("Fill", 2, "CS2", [1.0, 1.0, 1.0]), ("Fill", 1, "CS1", [0.0, 0.0, 0.0, 0.0])])
    assert report["blocking_count"] == 2 and report["by_kind"]["white"] == 2
    assert all("белый объект с overprint пропадёт при печати" in sample for sample in report["samples"])
    assert report["samples"][0].startswith("Заливка RGB 255/255/255 (DeviceRGB), стр. 2")


def test_white_ink_spot_is_technical_and_not_blocking():
    report = report_of([("Fill", 1, "CS3", [1.0])] * 20)
    assert report["count"] == 20 and report["blocking_count"] == 0
    assert report["technical_count"] == 20 and report["technical_pages"] == [1]
    assert report["technical_colorants"] == ["White"] and report["samples"] == []
    assert report["technical_samples"] == ["Заливка «White» 100% (Separation), стр. 1"]


def test_devicen_with_white_and_cyan_is_blocking():
    report = report_of([("Fill", 1, "CS4", [1.0, 1.0])])
    assert report["blocking_count"] == 1 and report["by_kind"]["spot"] == 1
    assert report["colorants"] == ["White", "Cyan"]
    assert "«White»+«Cyan»" in report["samples"][0]


def test_missing_gstate_and_text_without_colorspace_are_blocking():
    report = report_of([("Fill", 1, None, None, None), ("TextFill", 2, None, None)])
    assert report["blocking_count"] == 2 and report["by_kind"]["unknown"] == 2
    assert report["blocking_pages"] == [1, 2]


def test_text_colour_comes_from_textstate_and_icc_black_is_process():
    report = report_of([("TextFill", 1, "CS5", [0.0, 0.0, 0.0, 1.0]), ("Fill", 1, "CS6", [1.0])])
    assert report["by_kind"]["process"] == 2  # ICC CMYK K100 и сепарация Black
    assert report["samples"][0] == "Текст K100 (ICC «ISO Coated v2 (ECI)»), стр. 1"


def test_non_technical_spot_and_varnish():
    report = report_of([("Fill", 1, "CS8", [1.0]), ("Stroke", 1, "CS7", [1.0])])
    assert report["by_kind"]["spot"] == 1 and report["by_kind"]["technical_spot"] == 1
    assert report["blocking_count"] == 1 and report["technical_colorants"] == ["Varnish"]


def test_empty_ignore_list_makes_white_blocking():
    report = report_of([("Fill", 1, "CS3", [1.0])], ignore=())
    assert report["blocking_count"] == 1 and report["by_kind"]["spot"] == 1


def test_many_hits_are_counted_cheaply_with_few_samples():
    hits = [("Fill", 1 + n % 3, "CS1", [0.0, 0.0, 0.0, round(n / 3000, 4)]) for n in range(3000)]
    hits += [("Fill", 1, "CS3", [1.0])] * 500
    root = parse_report_root(overprint_report_xml(hits).encode())
    started = time.perf_counter()
    report = overprint_report(root, IGNORE)
    assert time.perf_counter() - started < 2.0
    assert report["count"] == 3500 and report["blocking_count"] == 3000 and report["technical_count"] == 500
    assert len(report["samples"]) <= OVERPRINT_SAMPLE_LIMIT and len(report["technical_samples"]) == 1
    assert report["per_page"]["1"] == {"blocking": 1000, "technical": 500}
    assert report["pages"] == [1, 2, 3] and report["truncated"] is False


def test_hits_over_the_limit_are_counted_as_blocking(monkeypatch):
    import services.callas.prepress_reports as reports

    monkeypatch.setattr(reports, "OVERPRINT_MAX_HITS", 2)
    report = report_of([("Fill", 1, "CS3", [1.0])] * 5)
    assert report["count"] == 5 and report["processed_hits"] == 2 and report["truncated"] is True
    assert report["technical_count"] == 2 and report["blocking_count"] == 3


def test_clean_report():
    report = report_of([])
    assert report["count"] == 0 and report["blocking_count"] == 0 and report["samples"] == []


# ----------------------------------------------------------------------------- spot names
@pytest.mark.parametrize("name", [
    "White", "WHITE", "white 2", "White_ink", "W", "W1", "Белый", "белила", "Varnish Gloss", "Lack",
    "Лак-1", "UV-Lack", "Spot UV", "SpotUV", "CutContour", "Cut Contour", "cut_contour", "Die cut",
    "Dieline", "Контур реза", "Высечка", "Вырубка", "Биговка", "Crease", "Perf", "Perforation", "Фальц",
])
def test_technical_spot_names(name):
    assert is_technical_spot(name, IGNORE)


@pytest.mark.parametrize("name", ["Whiteboard", "Warm Red", "Diesel", "PANTONE 485 C", "Black", "Cyan", "Gold", "", "Контурная"])
def test_non_technical_spot_names(name):
    assert not is_technical_spot(name, IGNORE)


def test_normalization_and_custom_list():
    assert normalize_spot_name("  Ёлка__Лак ") == "елка лак"
    assert is_technical_spot("Gold Foil", ("gold",)) and not is_technical_spot("White", ())


def test_settings_parse_the_ignore_list(monkeypatch):
    from server.settings import Settings

    monkeypatch.delenv("IMAGE_MAGIC_OVERPRINT_IGNORE_SPOTS", raising=False)
    settings = Settings.from_env()
    assert settings.callas_overprint_enabled is True
    assert settings.callas_overprint_profile == "Image Magic/Overprint - any object"
    assert settings.overprint_ignore_spots == IGNORE
    monkeypatch.setenv("IMAGE_MAGIC_OVERPRINT_IGNORE_SPOTS", " White, Gold Foil ,white,")
    assert Settings.from_env().overprint_ignore_spots == ("White", "Gold Foil")
    monkeypatch.setenv("IMAGE_MAGIC_OVERPRINT_IGNORE_SPOTS", "")
    assert Settings.from_env().overprint_ignore_spots == ()
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_OVERPRINT_ENABLED", "false")
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_OVERPRINT_PROFILE", "Image Magic/Other")
    settings = Settings.from_env()
    assert settings.callas_overprint_enabled is False and settings.callas_overprint_profile == "Image Magic/Other"


# ----------------------------------------------------------------------------- analyzer
def analyzer_for(tmp_path, handler, **kwargs):
    client = FakeCallasProcessClient(tmp_path / "jobs", {OVERPRINT_PROFILE: handler})
    return client, PrepressAnalyzer(client, **kwargs)


def pdf_file(tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4 overprint")
    return path


def test_analyzer_runs_the_profile_in_analyze_mode_and_builds_messages(tmp_path):
    xml = overprint_report_xml([("Fill", 1, "CS1", [0, 0, 0, 1]), ("Fill", 2, "CS3", [1.0])])
    client, analyzer = analyzer_for(tmp_path, {"xml": xml, "exit_code": 3})
    result = analyzer.check_overprint(pdf_file(tmp_path), "o1")
    assert client.calls == [(OVERPRINT_PROFILE, "analyze")] and len(client.deleted) == 1
    assert result["status"] == "ok" and result["blocking_count"] == 1 and result["technical_count"] == 1
    assert result["message"] == (
        "Overprint: 1 объект с наложением (стр. 1): Заливка K100 (DeviceCMYK), стр. 1. Уберите overprint в макете."
    )
    assert result["technical_message"].startswith("Overprint на технических красках (White): 1 объект, стр. 2.")


def test_analyzer_failure_is_reported_not_raised(tmp_path):
    _client, analyzer = analyzer_for(tmp_path, RuntimeError("callas недоступен"))
    assert analyzer.check_overprint(pdf_file(tmp_path), "o1") == {"status": "failed", "error": "callas недоступен"}


def test_analyzer_disabled_or_overprint_only(tmp_path):
    client, analyzer = analyzer_for(tmp_path, {"xml": overprint_report_xml([])}, overprint_profile=None)
    assert analyzer.check_overprint(pdf_file(tmp_path), "o") == {"status": "skipped"}
    assert "overprint" not in analyzer(pdf_file(tmp_path), "o") and (OVERPRINT_PROFILE, "analyze") not in client.calls
    client, only = analyzer_for(tmp_path / "b", {"xml": overprint_report_xml([])}, general_checks=False)
    assert list(only(pdf_file(tmp_path), "o")) == ["overprint"] and client.calls == [(OVERPRINT_PROFILE, "analyze")]


# ----------------------------------------------------------------------------- status rule
BLOCKING = {"overprint": {
    "status": "ok", "count": 3, "blocking_count": 2, "blocking_pages": [1], "technical_count": 1,
    "technical_pages": [2], "samples": ["Заливка K100 (DeviceCMYK), стр. 1"], "technical_samples": ["x"],
    "by_kind": {"process": 2, "technical_spot": 1}, "technical_colorants": ["White"],
    "message": "Overprint: 2 объекта с наложением (стр. 1): Заливка K100 (DeviceCMYK), стр. 1. Уберите overprint в макете.",
    "technical_message": "Overprint на технических красках (White): 1 объект, стр. 2.",
}}
TECHNICAL = {"overprint": {
    "status": "ok", "count": 20, "blocking_count": 0, "blocking_pages": [], "technical_count": 20,
    "technical_pages": [1], "samples": [], "technical_samples": ["Заливка «White» 100% (Separation), стр. 1"],
    "message": None, "technical_message": "Overprint на технических красках (White): 20 объектов, стр. 1.",
}}
FAILED = {"overprint": {"status": "failed", "error": "callas недоступен"}}
PASSED_PITSTOP = {"execution_status": "completed", "verdict": "passed"}


def test_status_effect_and_completed_status():
    # 27.11: overprint (не только технические краски) — предупреждение, не ошибка.
    assert overprint_status_effect(BLOCKING) == "warning"
    assert overprint_status_effect(TECHNICAL) is None
    assert overprint_status_effect(FAILED) == "warning"
    assert overprint_status_effect({"overprint": {"status": "skipped"}}) is None
    assert overprint_status_effect(None) is None and overprint_status_effect({"fonts": {}}) is None

    def status(prepress, **kwargs):
        args = {"local_errors": False, "local_warnings": False, "pitstop": PASSED_PITSTOP, **kwargs}
        return completed_status(**args, prepress=prepress)

    assert status(BLOCKING) == "warning"
    assert status(BLOCKING, local_errors=True) == "error"
    assert status(TECHNICAL) == "passed"
    assert status(FAILED) == "warning"
    # сбой проверки не делает красный заказ зелёным
    assert status(FAILED, local_errors=True) == "error"
    assert status(FAILED, pitstop={"execution_status": "failed"}) == "error"


def test_issue_groups_are_one_line_per_kind():
    groups = overprint_issue_groups(BLOCKING)
    assert [(g["code"], g["severity"], g["count"], g["pages"]) for g in groups] == [
        ("prepress:overprint", "warning", 2, [1]),
        ("prepress:overprint", "info", 1, [2]),
    ]
    assert groups[0]["source"] == "callas" and groups[0]["samples"] == ["Заливка K100 (DeviceCMYK), стр. 1"]
    assert [g["severity"] for g in overprint_issue_groups(TECHNICAL)] == ["info"]
    assert overprint_issue_groups(FAILED)[0]["message"] == "Проверка overprint не выполнена: callas недоступен"
    dto = {"errors": ["размер 1 мм"], "warnings": [], "prepress": BLOCKING, "files": []}
    built = issue_groups_for_order_dto(dto)
    assert [g["code"] for g in built if g["source"] == "callas"] == ["prepress:overprint", "prepress:overprint"]
    assert built[0]["source"] == "local"  # локальная ошибка выше предупреждения callas
    revision = issue_groups_for_order_dto({"revision_check": {"errors": [], "warnings": []}, "prepress": BLOCKING})
    assert revision[0]["code"] == "prepress:overprint" and revision[0]["severity"] == "warning"


def test_journal_entries():
    entry = _prepress_entries({"prepress": BLOCKING})[0]
    assert (entry["code"], entry["severity"], entry["count"], entry["pages"]) == ("prepress:overprint", "warning", 2, [1])
    assert entry["data"]["technical_count"] == 1 and entry["data"]["samples"]
    info = _prepress_entries({"prepress": TECHNICAL})[0]
    assert (info["code"], info["severity"], info["count"]) == ("prepress:overprint", "info", 20)
    failed = _prepress_entries({"prepress": FAILED})[0]
    assert (failed["code"], failed["severity"], failed["outcome"]) == ("prepress:overprint:failed", "warning", "failed")
    assert _prepress_entries({"prepress": {"overprint": {"status": "ok", "blocking_count": 0, "technical_count": 0}}}) == []


def test_journal_marks_fixed_overprint_as_resolved():
    from services.order_journal import build_order_journal

    initial = {"order_id": "1", "status": "warning", "prepress": BLOCKING}
    final = {"order_id": "1", "status": "passed", "prepress": {"overprint": {"status": "ok", "blocking_count": 0, "technical_count": 0}}}
    journal = build_order_journal(
        final,
        events=[{"id": 1, "type": "order.completed", "created_at": "2026-11-21T10:00:00+00:00", "data": {"order": initial}}],
    )
    entry = next(item for item in journal["entries"] if item["code"] == "prepress:overprint")
    assert entry["outcome"] == "resolved" and entry["severity"] == "warning"


# ----------------------------------------------------------------------------- adapter
class Processor:
    """Процессор-заглушка: PDF без инструментов; допечатные проверки — заданный словарь."""

    prepress: dict | None = None
    diagnostic_prepress: dict | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    def create_pdfs(self, orders):
        order = orders[0]
        pdf_path = order.files[0].path.parent / "PDF" / "order.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-test")
        order.prepress = self.prepress
        return [(order, pdf_path, None)]

    def create_diagnostic_pdf(self, order, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = output_dir / "diag.pdf"
        pdf_path.write_bytes(b"%PDF-diag")
        order.prepress = self.diagnostic_prepress
        return pdf_path, None

    def generate_previews_for_files(self, files, preview_dir, **_kwargs):
        return []


class PassingPitStop:
    def check_pdf(self, pdf_path, *, profile_id):
        # прозрачность: в облегчённой проверке — справка, вердикт «passed»
        issue = PitStopIssue(severity=PitStopSeverity.ERROR, action_id=3004,
                             message="Transparent objects have been found (1x on page 1)", total_occurrences=1)
        return PitStopCheckResult(
            status=PitStopExecutionStatus.COMPLETED, profile_id=profile_id, input_pdf=pdf_path,
            checked_at=datetime.now(UTC), input_sha256="a" * 64,
            report=PitStopReport(counts=PitStopCounts(errors=1), issues=(issue,), pages=1), job_id="c1",
        )


def adapter_with(tmp_path, prepress, *, diagnostic=None):
    processor = type("P", (Processor,), {"prepress": prepress, "diagnostic_prepress": diagnostic})
    return BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital", generate_previews=False, copy_failures=False),
        processor_factory=processor,
        pitstop_service=PassingPitStop(),
        input_check_policy="basic",
        strict_source_dirs=("CorelDRAW",),
    )


def order_in(tmp_path, *, errors=()):
    return OrderCheck("1001", "100", files=[FileCheck(path=tmp_path / f"{NAME}.pdf")], errors=list(errors))


@pytest.mark.parametrize("prepress, status, folder", [
    (BLOCKING, "warning", "Rework"),
    (TECHNICAL, "passed", "Checked"),
    (FAILED, "warning", "Rework"),
    (None, "passed", "Checked"),
])
def test_adapter_status_under_basic_policy_with_pitstop_passed(tmp_path, prepress, status, folder):
    order = order_in(tmp_path)
    artifacts = adapter_with(tmp_path, prepress).process_order(order)
    assert order.check_policy["key"] == "basic" and artifacts.pitstop["verdict"] == "passed"
    assert _completed_order_status(order, artifacts) == status
    assert artifacts.pdf_path.parent.name == folder
    dto = order_check_to_dto(order, status=status, pitstop=artifacts.pitstop)
    codes = {(g["code"], g["severity"]) for g in dto["issue_groups"]}
    if prepress is BLOCKING:
        assert ("prepress:overprint", "warning") in codes
    if prepress is FAILED:
        assert ("prepress:overprint:failed", "warning") in codes


def test_adapter_full_policy_blocking_is_warning_too(tmp_path):
    adapter = adapter_with(tmp_path, BLOCKING)
    adapter.options = ProcessingOptions(str(tmp_path), "digital", generate_previews=False,
                                        copy_failures=False, check_policy="full")
    order = order_in(tmp_path)
    artifacts = adapter.process_order(order)
    assert order.check_policy["key"] == "full"
    assert _completed_order_status(order, artifacts) == "warning"


def test_adapter_diagnostic_pdf_with_overprint_is_warning(tmp_path):
    order = order_in(tmp_path, errors=["размер 80x40 мм не соответствует"])
    artifacts = adapter_with(tmp_path, None, diagnostic=BLOCKING).process_order(order)
    assert artifacts.pdf_kind == "diagnostic"
    assert _completed_order_status(order, artifacts) == "warning"
    # без overprint — тоже «warning», как раньше
    clean = order_in(tmp_path, errors=["размер 80x40 мм не соответствует"])
    artifacts = adapter_with(tmp_path, None, diagnostic=TECHNICAL).process_order(clean)
    assert _completed_order_status(clean, artifacts) == "warning"


# ----------------------------------------------------------------------------- BatchProcessor hooks
class OverprintOnly:
    overprint_profile = OVERPRINT_PROFILE

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def check_overprint(self, pdf, label):
        self.calls.append((Path(pdf).name, label))
        if self.error:
            raise self.error
        return self.result

    def __call__(self, pdf, label, previous=None):
        raise RuntimeError("сбой анализатора")


def test_diagnostic_pdf_is_checked_for_overprint_only(tmp_path):
    checker = OverprintOnly(result=BLOCKING["overprint"])
    processor = BatchProcessor(tmp_path, tmp_path, pdf_prepress_checker=checker)
    order = OrderCheck("1", "2")
    processor._check_diagnostic_overprint(pdf_file(tmp_path), order)
    assert checker.calls == [("a.pdf", "1")] and order.prepress == {"overprint": BLOCKING["overprint"]}
    failing = BatchProcessor(tmp_path, tmp_path, pdf_prepress_checker=OverprintOnly(error=RuntimeError("нет связи")))
    order = OrderCheck("1", "2")
    failing._check_diagnostic_overprint(pdf_file(tmp_path), order)
    assert order.prepress == {"overprint": {"status": "failed", "error": "нет связи"}}
    plain = BatchProcessor(tmp_path, tmp_path, pdf_prepress_checker=lambda *a: {})
    order = OrderCheck("1", "2")
    plain._check_diagnostic_overprint(pdf_file(tmp_path), order)
    assert order.prepress is None


def test_whole_prepress_failure_marks_overprint_as_not_checked(tmp_path):
    order = OrderCheck("1", "2")
    BatchProcessor(tmp_path, tmp_path, pdf_prepress_checker=OverprintOnly())._check_prepress(pdf_file(tmp_path), order)
    assert order.prepress["overprint"] == {"status": "failed", "error": "сбой анализатора"}
    assert overprint_status_effect(order.prepress) == "warning"
