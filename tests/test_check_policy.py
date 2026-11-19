"""Политики проверки: облегчённая проверка корня входной папки и полная для CorelDRAW.

Без сети и без production: фейковые PitStop и процессор, настоящие функции политики.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import tempfile

import pytest

from config.check_policies import (
    CATEGORIES,
    CATEGORIES_BY_KEY,
    POLICIES,
    POLICY_BASIC,
    POLICY_FULL,
)
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck
from server.database import Database
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.check_policy import (
    PolicySettings,
    apply_check_policy,
    classify,
    resolve_order_policy,
    restore_pre_policy,
    source_output_root,
    stored_order_policy,
)
from services.coordinator import _completed_order_status
from services.dto import file_check_to_dto, order_check_to_dto
from services.issue_groups import build_issue_groups
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopReport,
    PitStopSeverity,
)
from services.pitstop.baseline import FixBaselinePolicy, apply_fix_baseline
from services.sql_repository import SqlRunRepository
from tests.test_pitstop_baseline import dto
from tests.test_sql_run_repository import sample_run

ERR, WARN = PitStopSeverity.ERROR, PitStopSeverity.WARNING

LOWRES = (1150, ERR, "Image resolution is 270 x 270 ppi - color or grayscale image is less than")
RGB = (2078, ERR, "RGB color is used")
ICC = (2078, WARN, "ICC based is used")
ICC_TRANSPARENCY = (2078, ERR, "ICC based is used for transparency group")
TRANSPARENCY = (3004, ERR, "Transparent objects have been found")
CLIPPING = (2079, WARN, "Clipping objects found")
FONT_MISSING = (1180, ERR, "Font ArialMT is not embedded")
FONT_COMPOSITE = (1128, ERR, "Font X is a composite font")
THIN = (3140, WARN, "Line weight is less than 0.2 pt")
INVISIBLE = (3138, WARN, "Document contains invisible objects")
UNKNOWN = (9999, ERR, "Brand new PitStop check")
UNKNOWN_NO_ACTION = (None, ERR, "Message without action")


def issue_of(pitstop, action_id):
    return next(item for item in pitstop["issues"] if str(item["action_id"]) == str(action_id))


def build(*checks, **kwargs):
    return dto(*((check, 1) for check in checks), **kwargs)


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize(
    "item, category",
    [
        (LOWRES, "resolution"),
        ((1148, ERR, "Image resolution 1-bit image is less than 600 ppi"), "resolution"),
        (RGB, "color_model"),
        ((3202, ERR, "Page contains a default color space"), "color_model"),
        ((3157, WARN, "Alternate color space is not Device CMYK or Device gray"), "color_model"),
        (ICC, "icc_profile"),
        (ICC_TRANSPARENCY, "transparency"),
        (TRANSPARENCY, "transparency"),
        (CLIPPING, "clipping"),
        ((1164, ERR, "Not all pages in the document have the same size"), "page_size"),
        ((4501, ERR, "Page rotation angle is not 0 degrees"), "page_size"),
        ((1153, WARN, "Total ink coverage of an object is more than 310%"), "ink_coverage"),
        ((3159, WARN, "Document uses 1 separations, should be equal to 0"), "spot_colors"),
        ((3156, ERR, "Spot color All is used"), "spot_colors"),
        ((1185, ERR, "Embedded font X is not complete"), "fonts_missing"),
        (FONT_MISSING, "fonts_missing"),
        (FONT_COMPOSITE, "font_types"),
        ((1131, ERR, "Font is a TrueType font"), "font_types"),
        ((2022, ERR, "Document contains Free text annotation"), "annotations"),
        ((3180, ERR, "Annotation is set to print"), "annotations"),
        ((3136, ERR, "White line art does not knock out"), "overprint"),
        (INVISIBLE, "invisible_objects"),
        (THIN, "thin_lines"),
        ((3211, ERR, "Document contains actions of type GoTo"), "pdf_actions"),
    ],
)
def test_classify_maps_pitstop_findings_to_categories(item, category):
    action, severity, message = item
    found = classify({"action_id": action, "severity": severity.value, "message": f"{message} (3x on pages 1-2)"})
    assert found is not None and found.key == category


def test_classify_2078_variants_are_told_apart_by_message():
    def key(message):
        found = classify({"action_id": "2078", "message": message})
        return found.key if found else None

    assert key("RGB color is used (12x on pages 1-2)") == "color_model"
    assert key("ICC based is used (1975)") == "icc_profile"
    assert key("ICC based is used for transparency group (2x on page 1)") == "transparency"
    assert key("Something else entirely") is None


def test_classify_unknown_action_and_missing_action_have_no_category():
    assert classify({"action_id": "9999", "message": "x"}) is None
    assert classify({"action_id": None, "message": "x"}) is None
    assert classify({"message": "x"}) is None


def test_finding_without_action_id_matches_by_message_only():
    """«Spot color White is used» приходит от PitStop без action_id (запуск 563b8e)."""
    found = classify({"action_id": None, "message": "Spot color White is used (20x on page 1)"})
    assert found is not None and found.key == "spot_colors"
    # Шаблон без action_id не подходит находке, у которой номер есть.
    assert classify({"action_id": "9999", "message": "Spot color White is used"}) is None


def test_every_category_is_described_by_every_policy():
    # Новая проверка = строка в CATEGORIES + правило в каждой политике (кроме full).
    keys = [category.key for category in CATEGORIES]
    assert len(keys) == len(set(keys))
    basic = POLICIES[POLICY_BASIC]
    assert set(keys) <= set(basic.rules)
    assert set(basic.rules) <= set(keys)
    assert all(CATEGORIES_BY_KEY[key].label for key in keys)


# --- apply_check_policy ------------------------------------------------------


def test_basic_demotes_informational_categories_and_order_can_be_green():
    pitstop = build(TRANSPARENCY, CLIPPING, FONT_COMPOSITE, THIN, INVISIBLE)
    assert pitstop["verdict"] == "error"
    result = apply_check_policy(pitstop, "basic")
    assert (result["check_policy"], result["verdict"]) == ("basic", "passed")
    assert (result["verdict_before_policy"], result["counts_before_policy"]["errors"]) == ("error", 2)
    assert result["counts"]["errors"] == 0 and result["counts"]["warnings"] == 0
    assert result["counts"]["informations"] == 5
    for item in result["issues"]:
        assert item["severity"] == "information"
        assert item["policy_demoted"] is True
        assert item["policy_original_severity"] in {"error", "warning"}
        assert item["policy_category"] and item["policy_category_label"]
    # Входной DTO не меняется.
    assert pitstop["verdict"] == "error" and "check_policy" not in pitstop
    status = _status(result)
    assert status == "passed"


@pytest.mark.parametrize(
    "item, expected_verdict",
    [
        (LOWRES, "error"),
        (RGB, "error"),
        (ICC, "warning"),
        (FONT_MISSING, "error"),
        ((1153, WARN, "Total ink coverage of an object is more than 310%"), "warning"),
        ((3180, ERR, "Annotation is set to print"), "error"),
        ((3136, ERR, "White line art does not knock out"), "error"),
        ((1164, ERR, "Not all pages in the document have the same size"), "error"),
        ((3159, WARN, "Document uses 1 separations, should be equal to 0"), "warning"),
    ],
)
def test_basic_keeps_status_categories(item, expected_verdict):
    result = apply_check_policy(build(item), "basic")
    assert result["verdict"] == expected_verdict
    assert "verdict_before_policy" not in result
    assert not issue_of(result, item[0]).get("policy_demoted")
    assert _status(result) == "warning"


def test_basic_mixed_findings_keep_only_status_ones():
    result = apply_check_policy(build(TRANSPARENCY, LOWRES, CLIPPING, ICC_TRANSPARENCY), "basic")
    assert result["verdict"] == "error"
    assert result["counts"]["errors"] == 1 and result["counts"]["warnings"] == 0
    assert result["counts"]["informations"] == 3
    assert issue_of(result, 1150)["severity"] == "error"
    assert issue_of(result, 2078)["policy_demoted"] is True  # ICC прозрачной группы — прозрачность


def test_unknown_finding_keeps_severity_and_is_flagged_unclassified():
    result = apply_check_policy(build(UNKNOWN, THIN), "basic")
    unknown = issue_of(result, 9999)
    assert unknown["severity"] == "error"
    assert unknown["policy_unclassified"] is True
    assert "policy_category" not in unknown
    assert result["verdict"] == "error"
    assert _status(result) == "warning"


def test_full_policy_changes_nothing_but_tags_categories():
    pitstop = build(TRANSPARENCY, CLIPPING, UNKNOWN)
    result = apply_check_policy(pitstop, "full")
    assert result["check_policy"] == "full"
    assert result["verdict"] == pitstop["verdict"] == "error"
    assert result["counts"] == pitstop["counts"]
    assert [item["severity"] for item in result["issues"]] == [item["severity"] for item in pitstop["issues"]]
    assert issue_of(result, 3004)["policy_category"] == "transparency"
    assert issue_of(result, 3004)["policy_category_label"] == "Прозрачность"
    assert not any(item.get("policy_demoted") or item.get("policy_unclassified") for item in result["issues"])
    assert "verdict_before_policy" not in result


def test_unknown_policy_key_falls_back_to_full():
    result = apply_check_policy(build(TRANSPARENCY), "no-such-policy")
    assert result["check_policy"] == "full" and result["verdict"] == "error"


def test_policy_is_idempotent_and_can_be_switched():
    pitstop = build(TRANSPARENCY, LOWRES)
    once = apply_check_policy(pitstop, "basic")
    assert apply_check_policy(once, "basic") == once
    assert apply_check_policy(deepcopy(once), "basic") == once
    # basic -> full возвращает исходное состояние (кроме пометок категорий).
    back = apply_check_policy(once, "full")
    assert back["verdict"] == pitstop["verdict"] and back["counts"] == pitstop["counts"]
    assert issue_of(back, 3004)["severity"] == "error"
    assert "policy_demoted" not in issue_of(back, 3004)
    restored = restore_pre_policy(once)
    assert restored["verdict"] == pitstop["verdict"] and restored["counts"] == pitstop["counts"]
    assert not any(name.startswith("policy_") for item in restored["issues"] for name in item)


def test_technical_failure_is_not_changed_by_policy():
    failed = build(status=PitStopExecutionStatus.FAILED)
    result = apply_check_policy(failed, "basic")
    assert result["execution_status"] != "completed"
    assert result["verdict"] == "error"
    assert _status(result) == "error"
    assert apply_check_policy(None, "basic") is None


def test_policy_after_baseline_leaves_hidden_findings_alone():
    baseline = build((3004, ERR, "Transparent objects"))
    current = build(
        (3004, ERR, "Transparent objects"), CLIPPING, RGB, revision=2
    )
    # Без исходника в baseline клиентская 3004 остаётся; RGB — новая (не в baseline) -> не скрывается
    # при списке действий {2079, 3004}; обтравка (2079) добавлена исправлением -> скрыта.
    based = apply_fix_baseline(
        current, baseline, FixBaselinePolicy(actions=frozenset({"2079", "3004"})), profile_key="digital"
    )
    assert issue_of(based, 2079)["hidden_by_baseline"] is True
    result = apply_check_policy(based, "basic")
    clipping = issue_of(result, 2079)
    assert clipping["hidden_by_baseline"] is True
    assert clipping["severity"] == "information"
    assert "policy_demoted" not in clipping  # уже скрыта сравнением
    assert clipping["policy_category"] == "clipping"
    assert issue_of(result, 3004)["policy_demoted"] is True
    assert issue_of(result, 2079)["original_severity"] == "warning"  # поля сравнения не тронуты
    assert (result["verdict"], result["verdict_raw"]) == ("error", "error")  # RGB остаётся ошибкой
    assert result["baseline_status"] == "applied"
    # Повторное сравнение + политика даёт то же самое.
    again = apply_check_policy(
        apply_fix_baseline(
            result, baseline, FixBaselinePolicy(actions=frozenset({"2079", "3004"})), profile_key="digital"
        ),
        "basic",
    )
    assert again["verdict"] == result["verdict"]
    assert [item["severity"] for item in again["issues"]] == [item["severity"] for item in result["issues"]]


def _status(pitstop):
    from services.pdf_routing import completed_status

    return completed_status(local_errors=False, local_warnings=False, pitstop=pitstop)


# --- resolve_order_policy / settings ----------------------------------------


def test_resolve_policy_by_folder_run_option_and_settings():
    basic = PolicySettings("basic", ("CorelDRAW",))
    assert resolve_order_policy([""], "auto", basic) == {
        "key": "basic", "label": "Облегчённая проверка (Input)", "reason": "input_root",
    }
    assert resolve_order_policy(["CorelDRAW"], "auto", basic) == {
        "key": "full", "label": "Полная проверка (CorelDRAW)", "reason": "strict_folder",
    }
    # Смешанный заказ — полная проверка.
    assert resolve_order_policy(["", "CorelDRAW"], "auto", basic)["key"] == "full"
    assert resolve_order_policy([""], "full", basic) == {
        "key": "full", "label": "Полная проверка (выбрана при запуске)", "reason": "run_option",
    }
    off = PolicySettings("full", ("CorelDRAW",))
    assert resolve_order_policy([""], "auto", off) == {
        "key": "full", "label": "Полная проверка", "reason": "disabled",
    }
    assert resolve_order_policy([], None, basic)["reason"] == "input_root"


def test_stored_order_policy_prefers_stored_value_then_recomputes():
    basic = PolicySettings("basic", ("CorelDRAW",))
    stored = {"key": "full", "label": "Полная проверка (CorelDRAW)", "reason": "strict_folder"}
    assert stored_order_policy({"check_policy": stored}, {}, basic) == stored
    legacy = {"files": [{"source_folder": ""}]}
    assert stored_order_policy(legacy, {}, basic)["key"] == "basic"
    assert stored_order_policy(legacy, {"check_policy": "full"}, basic)["reason"] == "run_option"
    assert stored_order_policy({"files": [{"source_folder": "CorelDRAW"}]}, {}, basic)["key"] == "full"


def test_settings_from_env_switch_and_strict_dirs(monkeypatch, caplog):
    monkeypatch.delenv("IMAGE_MAGIC_INPUT_CHECK_POLICY", raising=False)
    monkeypatch.delenv("IMAGE_MAGIC_STRICT_SOURCE_DIRS", raising=False)
    settings = Settings.from_env()
    assert (settings.input_check_policy, settings.strict_source_dirs) == ("basic", ("CorelDRAW",))

    monkeypatch.setenv("IMAGE_MAGIC_INPUT_CHECK_POLICY", "FULL")
    assert Settings.from_env().input_check_policy == "full"

    monkeypatch.setenv("IMAGE_MAGIC_STRICT_SOURCE_DIRS", "CorelDRAW, Illustrator ,corelDraw")
    assert Settings.from_env().strict_source_dirs == ("CorelDRAW", "Illustrator")
    monkeypatch.setenv("IMAGE_MAGIC_STRICT_SOURCE_DIRS", "")
    assert Settings.from_env().strict_source_dirs == ()

    monkeypatch.setenv("IMAGE_MAGIC_INPUT_CHECK_POLICY", "strange")
    with caplog.at_level("WARNING"):
        assert Settings.from_env().input_check_policy == "full"
    assert "strange" in caplog.text


# --- scan --------------------------------------------------------------------

NAME_A = "100_(90x50)_4-0_(11-1001)_face.pdf"
NAME_B = "100_(90x50)_4-0_(11-1002)_face.pdf"


def _processor(tmp_path, *strict):
    return BatchProcessor(tmp_path, tmp_path / "PDF", strict_source_dirs=strict)


def test_scan_lists_root_and_strict_folder_and_creates_missing_folder(tmp_path):
    (tmp_path / NAME_A).write_bytes(b"x")
    processor = _processor(tmp_path, "CorelDRAW")
    assert not (tmp_path / "CorelDRAW").exists()
    assert processor.scan() == [tmp_path / NAME_A]
    assert (tmp_path / "CorelDRAW").is_dir()

    (tmp_path / "CorelDRAW" / NAME_B).write_bytes(b"x")
    (tmp_path / "CorelDRAW" / "notes.txt").write_bytes(b"x")
    (tmp_path / "CorelDRAW" / "deeper").mkdir()
    (tmp_path / "CorelDRAW" / "deeper" / "100_(90x50)_4-0_(11-1003)_face.pdf").write_bytes(b"x")
    assert processor.scan() == sorted([tmp_path / "CorelDRAW" / NAME_B, tmp_path / NAME_A])
    assert processor.source_folder_of(tmp_path / NAME_A) == ""
    assert processor.source_folder_of(tmp_path / "CorelDRAW" / NAME_B) == "CorelDRAW"


def test_scan_without_strict_dirs_ignores_subfolders_and_creates_nothing(tmp_path):
    (tmp_path / NAME_A).write_bytes(b"x")
    (tmp_path / "CorelDRAW").mkdir()
    (tmp_path / "CorelDRAW" / NAME_B).write_bytes(b"x")
    assert BatchProcessor(tmp_path, tmp_path / "PDF").scan() == [tmp_path / NAME_A]
    empty = tmp_path / "empty"
    empty.mkdir()
    BatchProcessor(empty, empty / "PDF").scan()
    assert not (empty / "CorelDRAW").exists()


def test_scan_survives_a_folder_that_cannot_be_created(tmp_path, caplog):
    (tmp_path / NAME_A).write_bytes(b"x")
    (tmp_path / "CorelDRAW").write_bytes(b"not a folder")
    with caplog.at_level("WARNING"):
        assert _processor(tmp_path, "CorelDRAW").scan() == [tmp_path / NAME_A]
    assert "strict_dir_unavailable" in caplog.text


def test_scan_never_treats_output_folders_as_strict_sources(tmp_path):
    (tmp_path / "PDF").mkdir()
    (tmp_path / "PDF" / NAME_B).write_bytes(b"x")
    (tmp_path / NAME_A).write_bytes(b"x")
    assert _processor(tmp_path, "PDF", "Processed").scan() == [tmp_path / NAME_A]
    assert not (tmp_path / "Processed").exists()


def test_iter_inspect_orders_marks_source_folder_and_groups_across_folders(tmp_path, monkeypatch):
    face = "100_(90x50)_4-4_(11-1001)_face.pdf"
    back = "100_(90x50)_4-4_(11-1001)_back.pdf"
    (tmp_path / "CorelDRAW").mkdir()
    (tmp_path / face).write_bytes(b"x")
    (tmp_path / "CorelDRAW" / back).write_bytes(b"x")
    processor = _processor(tmp_path, "CorelDRAW")
    monkeypatch.setattr(processor, "_inspect_pdf_file", lambda check, *a, **k: None)
    orders = processor.inspect_orders()
    assert len(orders) == 1
    folders = {item.path.name: item.source_folder for item in orders[0].files}
    assert folders == {face: "", back: "CorelDRAW"}


def test_same_name_in_root_and_strict_folder_is_an_error_for_both(tmp_path, monkeypatch):
    (tmp_path / "CorelDRAW").mkdir()
    (tmp_path / NAME_A).write_bytes(b"x")
    (tmp_path / "CorelDRAW" / NAME_A).write_bytes(b"x")
    processor = _processor(tmp_path, "CorelDRAW")
    monkeypatch.setattr(processor, "_inspect_pdf_file", lambda check, *a, **k: None)
    orders = processor.inspect_orders()
    items = [item for order in orders for item in order.files]
    assert len(items) == 2
    assert all(not item.passed for item in items)
    by_folder = {item.source_folder: item.errors for item in items}
    assert any("CorelDRAW" in error and "оставьте одну копию" in error for error in by_folder[""])
    assert any("входной папки" in error and "оставьте одну копию" in error for error in by_folder["CorelDRAW"])
    assert not orders[0].passed


def test_names_differing_by_case_in_one_folder_are_not_duplicates(tmp_path, monkeypatch):
    (tmp_path / "CorelDRAW").mkdir()
    (tmp_path / NAME_A).write_bytes(b"x")
    (tmp_path / NAME_A.upper()).write_bytes(b"x")
    processor = _processor(tmp_path, "CorelDRAW")
    monkeypatch.setattr(processor, "_inspect_pdf_file", lambda check, *a, **k: None)
    checks = [item for order in processor.inspect_orders() for item in order.files]
    checks += processor.unparsed
    assert not any("оставьте одну копию" in error for item in checks for error in item.errors)


def test_reinspected_file_keeps_source_folder(tmp_path, monkeypatch):
    (tmp_path / "CorelDRAW").mkdir()
    path = tmp_path / "CorelDRAW" / NAME_A
    path.write_bytes(b"x")
    processor = _processor(tmp_path, "CorelDRAW")
    monkeypatch.setattr(processor, "_inspect_pdf_file", lambda check, *a, **k: None)
    item = processor.inspect_orders()[0].files[0]
    again = processor.reinspect_pdf_file(item, None)
    assert again.source_folder == "CorelDRAW"


def test_source_output_root_points_to_input_root_for_strict_folder(tmp_path):
    (tmp_path / "PDF").mkdir()
    assert source_output_root(tmp_path / "CorelDRAW" / NAME_A) == tmp_path
    assert source_output_root(tmp_path / NAME_A) == tmp_path
    other = tmp_path / "x"
    other.mkdir()
    # Папка без PDF/ рядом не считается папкой исходников.
    assert source_output_root(other / "CorelDRAW" / NAME_A) == other / "CorelDRAW"


# --- adapter -----------------------------------------------------------------


class PipelineProcessor:
    """Процессор-заглушка: PDF собирается без инструментов, превью нет."""

    def __init__(self, *_args, **kwargs):
        self.kwargs = kwargs

    def create_pdfs(self, orders):
        pdf_path = orders[0].files[0].path.parents[0] / "PDF" / "order.pdf"
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-test")
        return [(orders[0], pdf_path, None)]

    def generate_previews_for_files(self, files, preview_dir, **_kwargs):
        return []

    def iter_inspect_orders(self):
        return iter(())


class FakePitStop:
    def __init__(self, *checks):
        self.checks = checks

    def check_pdf(self, pdf_path, *, profile_id):
        issues = tuple(
            PitStopIssue(severity=sev, action_id=action, message=f"{text} (2x on page 1)", total_occurrences=2)
            for action, sev, text in self.checks
        )
        counts = PitStopCounts(
            errors=sum(1 for i in issues if i.severity is ERR),
            warnings=sum(1 for i in issues if i.severity is WARN),
        )
        return PitStopCheckResult(
            status=PitStopExecutionStatus.COMPLETED, profile_id=profile_id, input_pdf=pdf_path,
            checked_at=datetime.now(UTC), input_sha256="a" * 64,
            report=PitStopReport(counts=counts, issues=issues, pages=1), job_id="check-1",
        )


def _adapter(tmp_path, *checks, policy="basic", strict=("CorelDRAW",), **options):
    return BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital", generate_previews=False, copy_failures=False, **options),
        processor_factory=PipelineProcessor,
        pitstop_service=FakePitStop(*checks),
        input_check_policy=policy,
        strict_source_dirs=strict,
    )


def _order(tmp_path, folder=""):
    directory = tmp_path / folder if folder else tmp_path
    directory.mkdir(exist_ok=True)
    return OrderCheck("1001", "100", files=[FileCheck(path=directory / NAME_A, source_folder=folder)])


def test_adapter_basic_policy_turns_informational_findings_green(tmp_path):
    adapter = _adapter(tmp_path, TRANSPARENCY, CLIPPING, FONT_COMPOSITE)
    order = _order(tmp_path)
    artifacts = adapter.process_order(order)
    assert artifacts.pitstop["check_policy"] == "basic"
    assert artifacts.pitstop["verdict"] == "passed"
    assert artifacts.pdf_path.parent.name == "Checked"
    assert _completed_order_status(order, artifacts) == "passed"
    assert order.check_policy == {"key": "basic", "label": "Облегчённая проверка (Input)", "reason": "input_root"}
    assert order_check_to_dto(order, status="passed")["check_policy"]["key"] == "basic"


def test_adapter_basic_policy_keeps_resolution_findings_as_warning(tmp_path):
    adapter = _adapter(tmp_path, TRANSPARENCY, LOWRES)
    order = _order(tmp_path)
    artifacts = adapter.process_order(order)
    assert _completed_order_status(order, artifacts) == "warning"
    assert artifacts.pdf_path.parent.name == "Rework"


def test_adapter_strict_folder_gets_full_check(tmp_path):
    adapter = _adapter(tmp_path, TRANSPARENCY, CLIPPING)
    order = _order(tmp_path, "CorelDRAW")
    artifacts = adapter.process_order(order)
    assert artifacts.pitstop["check_policy"] == "full"
    assert _completed_order_status(order, artifacts) == "warning"
    assert order.check_policy["reason"] == "strict_folder"
    assert artifacts.pdf_path.parent.name == "Rework"


def test_adapter_mixed_order_is_checked_in_full(tmp_path):
    adapter = _adapter(tmp_path, TRANSPARENCY)
    order = _order(tmp_path)
    (tmp_path / "CorelDRAW").mkdir()
    order.files.append(FileCheck(path=tmp_path / "CorelDRAW" / NAME_B, source_folder="CorelDRAW"))
    artifacts = adapter.process_order(order)
    assert order.check_policy["key"] == "full"
    assert _completed_order_status(order, artifacts) == "warning"


def test_adapter_run_option_full_and_env_switch_behave_like_before(tmp_path):
    order = _order(tmp_path)
    artifacts = _adapter(tmp_path, TRANSPARENCY, check_policy="full").process_order(order)
    assert order.check_policy["reason"] == "run_option"
    assert _completed_order_status(order, artifacts) == "warning"

    switched_off = _order(tmp_path)
    artifacts = _adapter(tmp_path, TRANSPARENCY, policy="full").process_order(switched_off)
    assert switched_off.check_policy["reason"] == "disabled"
    assert artifacts.pitstop["verdict"] == "error"
    assert _completed_order_status(switched_off, artifacts) == "warning"


def test_adapter_technical_pitstop_failure_stays_an_error(tmp_path):
    class Broken:
        def check_pdf(self, pdf_path, *, profile_id):
            return PitStopCheckResult(
                status=PitStopExecutionStatus.FAILED, profile_id=profile_id, input_pdf=pdf_path,
                checked_at=datetime.now(UTC), input_sha256="a" * 64, report=None,
                technical_error="SSH недоступен", job_id="check-x",
            )

    adapter = _adapter(tmp_path)
    adapter.pitstop_service = Broken()
    order = _order(tmp_path)
    artifacts = adapter.process_order(order)
    assert _completed_order_status(order, artifacts) == "error"


def test_adapter_forwards_strict_dirs_to_processor(tmp_path):
    seen = {}

    def factory(*args, **kwargs):
        seen.update(kwargs)
        return PipelineProcessor(*args, **kwargs)

    BatchProcessorAdapter(
        ProcessingOptions(str(tmp_path), "digital"), processor_factory=factory,
        strict_source_dirs=("CorelDRAW",), input_check_policy="basic",
    )
    assert seen["strict_source_dirs"] == ("CorelDRAW",)
    seen.clear()
    BatchProcessorAdapter(ProcessingOptions(str(tmp_path), "digital"), processor_factory=factory)
    assert "strict_source_dirs" not in seen


def test_processing_options_carry_check_policy():
    assert ProcessingOptions("/x", "digital").as_dict()["check_policy"] == "auto"
    assert ProcessingOptions("/x", "digital", check_policy="full").as_dict()["check_policy"] == "full"


# --- DTO / issue groups ------------------------------------------------------


def test_file_dto_has_source_folder():
    item = FileCheck(path=Path("/in/CorelDRAW/a.pdf"), source_folder="CorelDRAW")
    assert file_check_to_dto(item)["source_folder"] == "CorelDRAW"
    assert file_check_to_dto(FileCheck(path=Path("/in/a.pdf")))["source_folder"] == ""


def test_issue_groups_carry_policy_flags():
    pitstop = apply_check_policy(build(TRANSPARENCY, CLIPPING, LOWRES, UNKNOWN), "basic")
    groups = {g["code"]: g for g in build_issue_groups(pitstop_issues=pitstop["issues"])}
    transparency = groups["action:3004"]
    assert transparency["severity"] == "info"
    assert transparency["policy_demoted"] is True and transparency["policy_unclassified"] is False
    assert transparency["category_label"] == "Прозрачность"
    lowres = groups["action:1150"]
    assert lowres["severity"] == "error" and lowres["policy_demoted"] is False
    unknown = groups["action:9999"]
    assert unknown["policy_unclassified"] is True and unknown["category_label"] is None
    # Без политики поля не добавляются.
    plain = build_issue_groups(pitstop_issues=build(TRANSPARENCY)["issues"])
    assert "policy_demoted" not in plain[0]


# --- persistence -------------------------------------------------------------


def test_sql_round_trip_keeps_policy_fields_and_source_folder():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(f"sqlite:///{Path(directory) / 'runs.sqlite3'}")
        database.create_schema()
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        try:
            expected = sample_run(status="completed")
            order = expected["orders"]["25506185"]
            pitstop = apply_check_policy(build(TRANSPARENCY, LOWRES, UNKNOWN), "basic")
            pitstop.update({"check_id": "check-1", "checked_revision": 1})
            order.update({
                "pdf_path": "/orders/input/PDF/order.pdf",
                "current_pdf_revision": 1,
                "current_pdf_sha256": "a" * 64,
                "source_status": "passed", "pitstop_status": "error", "workflow_status": "active",
                "pitstop": pitstop,
                "check_policy": {"key": "basic", "label": "Облегчённая проверка (Input)", "reason": "input_root"},
            })
            order["files"][0]["source_folder"] = "CorelDRAW"
            repository.create_run(expected)

            actual = repository.get_run("run-1")["orders"]["25506185"]
            assert actual["check_policy"] == order["check_policy"]
            assert actual["files"][0]["source_folder"] == "CorelDRAW"
            loaded = actual["pitstop"]
            assert loaded["check_policy"] == "basic"
            assert loaded["verdict"] == pitstop["verdict"]
            assert loaded["verdict_before_policy"] == pitstop["verdict_before_policy"]
            assert loaded["counts_before_policy"] == pitstop["counts_before_policy"]
            by_action = {str(item["action_id"]): item for item in loaded["issues"]}
            assert by_action["3004"]["policy_demoted"] is True
            assert by_action["3004"]["policy_original_severity"] == "error"
            assert by_action["3004"]["severity"] == "information"
            assert by_action["1150"]["policy_category"] == "resolution"
            assert by_action["9999"]["policy_unclassified"] is True
            # Повторное применение политики к загруженному DTO даёт тот же итог.
            assert apply_check_policy(loaded, "basic")["verdict"] == loaded["verdict"]
        finally:
            database.dispose()


# --- file moves of sources from the strict folder ----------------------------


def test_sources_in_strict_folder_move_to_root_level_output_folders(tmp_path):
    from services.file_lifecycle import FileLifecycle

    source = tmp_path / "CorelDRAW" / NAME_A
    source.parent.mkdir()
    source.write_bytes(b"source")
    pdf = tmp_path / "PDF" / "Checked" / "order.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"pdf")
    preview = tmp_path / "Previews" / "order.png"
    preview.parent.mkdir()
    preview.write_bytes(b"png")
    order = {"order_id": "1001", "status": "passed", "files": [{"path": str(source)}],
             "pdf_path": str(pdf), "preview_paths": [str(preview)]}
    transition = FileLifecycle(tmp_path).accept_for_print(order)
    assert not source.exists()
    assert transition.source_paths[str(source.resolve())] == str(tmp_path.resolve() / "Processed" / NAME_A)
    assert (tmp_path / "Processed" / NAME_A).read_bytes() == b"source"
    assert not (tmp_path / "CorelDRAW" / "Processed").exists()


def test_return_for_rework_of_strict_folder_source_lands_in_troubles_at_root(tmp_path):
    from services.file_lifecycle import FileLifecycle

    source = tmp_path / "CorelDRAW" / NAME_A
    source.parent.mkdir()
    source.write_bytes(b"source")
    pdf = tmp_path / "PDF" / "Rework" / "order.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"pdf")
    order = {"order_id": "1001", "status": "warning", "files": [{"path": str(source)}],
             "pdf_path": str(pdf), "preview_paths": []}
    FileLifecycle(tmp_path).return_for_rework(order)
    assert not source.exists()
    assert list((tmp_path / "Troubles").rglob(NAME_A))
