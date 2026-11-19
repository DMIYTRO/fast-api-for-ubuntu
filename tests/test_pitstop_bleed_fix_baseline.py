"""Исправление «только вылеты»: PitStop скрывает всё, чего не было у клиента.

Вылеты, дорисованные Callas (план ``size_correction`` с ``bleed_only``), приносят
растянутые полосы с низким разрешением (1150), обтравку (2079), прозрачность (3004)
и что угодно ещё. Для такого исправления сравниваются ВСЕ действия PitStop, а не
только ``IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS``; находки клиента сохраняют уровень.

Реальный случай — заказ 26124497: обе стороны из принятых исправлений исходников,
у заказа ошибки файлов, поэтому PDF собирался диагностическим, и сравнение там не
выполнялось: 1150 (8x) ошибка и 2079 (8x) предупреждение, добавленные вылетами.
Без сети: фейковые процессор и PitStop.
"""

from __future__ import annotations

import logging
import unittest
from pathlib import Path

from processing.models import FileCheck, OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.pitstop import PitStopIssue, PitStopSeverity
from services.pitstop.baseline import FixBaselinePolicy, apply_fix_baseline, diff_against_baseline
from tests.test_callas_fix_service import make_pdf
from tests.test_pitstop_baseline import CLIPPING, RGB, TRANSPARENCY, dto, issue_for
from tests.test_pitstop_fix_baseline_workflow import check_result, report


LOWRES = (1150, PitStopSeverity.ERROR, "Image resolution is below 250 ppi")


# ---------------------------------------------------------------- baseline.py
class AllActionsDiffTests(unittest.TestCase):
    policy = FixBaselinePolicy()

    def test_order_26124497_everything_added_by_bleed_is_hidden(self):
        current = dto((LOWRES, 8), (CLIPPING, 8), revision=2)
        result = apply_fix_baseline(current, dto(), self.policy, all_actions=True)

        self.assertEqual(result["baseline_status"], "applied")
        for action, severity in ((1150, "error"), (2079, "warning")):
            hidden = issue_for(result, action)
            self.assertEqual(
                (hidden["hidden_by_baseline"], hidden["severity"], hidden["original_severity"]),
                (True, "information", severity),
            )
            self.assertEqual((hidden["baseline_occurrences"], hidden["introduced_by_fix"]), (0, 8))
        self.assertEqual((result["verdict_raw"], result["verdict"]), ("error", "passed"))
        self.assertEqual((result["counts"]["errors"], result["counts"]["warnings"]), (0, 0))
        self.assertEqual(result["counts_raw"]["errors"], 1)

    def test_client_low_resolution_keeps_its_severity(self):
        current = dto((LOWRES, 11), (CLIPPING, 8), revision=2)
        result = apply_fix_baseline(current, dto((LOWRES, 3)), self.policy, all_actions=True)

        lowres = issue_for(result, 1150)
        self.assertEqual(lowres["severity"], "error")
        self.assertNotIn("hidden_by_baseline", lowres)
        self.assertEqual((lowres["baseline_occurrences"], lowres["introduced_by_fix"]), (3, 8))
        self.assertTrue(issue_for(result, 2079)["hidden_by_baseline"])
        self.assertEqual(result["verdict"], "error")

    def test_unchanged_client_findings_are_untouched(self):
        current = dto((RGB, 4), (TRANSPARENCY, 90), revision=2)
        result = apply_fix_baseline(
            current, dto((RGB, 4), (TRANSPARENCY, 90)), self.policy, all_actions=True
        )
        for action in (2078, 3004):
            self.assertEqual(issue_for(result, action)["severity"], "error")
            self.assertNotIn("introduced_by_fix", issue_for(result, action))
        self.assertEqual(result["verdict"], "error")

    def test_non_bleed_fix_compares_only_the_configured_actions(self):
        current = dto((LOWRES, 8), (CLIPPING, 8), revision=2)
        result = apply_fix_baseline(current, dto(), self.policy)

        self.assertEqual(issue_for(result, 1150)["severity"], "error")
        self.assertNotIn("hidden_by_baseline", issue_for(result, 1150))
        self.assertTrue(issue_for(result, 2079)["hidden_by_baseline"])
        self.assertEqual(result["verdict"], "error")

    def test_setting_off_falls_back_to_the_configured_actions(self):
        policy = FixBaselinePolicy(bleed_fix_all_actions=False)
        result = apply_fix_baseline(dto((LOWRES, 8), revision=2), dto(), policy, all_actions=True)
        self.assertEqual(issue_for(result, 1150)["severity"], "error")
        self.assertEqual(result["verdict"], "error")

    def test_all_actions_works_with_an_empty_action_list(self):
        policy = FixBaselinePolicy(actions=frozenset())
        self.assertFalse(policy.applies_to("digital"))
        self.assertTrue(policy.applies_to("digital", all_actions=True))
        self.assertFalse(policy.applies_to("offset", all_actions=True))
        result = apply_fix_baseline(dto((LOWRES, 8), revision=2), dto(), policy, all_actions=True)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(
            apply_fix_baseline(dto((LOWRES, 8), revision=2), dto(), policy)["baseline_status"],
            "disabled",
        )

    def test_profile_restriction_still_applies(self):
        result = apply_fix_baseline(
            dto((LOWRES, 8), revision=2), dto(), self.policy, profile_key=None, all_actions=True
        )
        self.assertEqual(result["baseline_status"], "applied")  # профиль из DTO — digital
        offset = dto((LOWRES, 8), revision=2)
        offset["profile"]["key"] = "offset"
        result = apply_fix_baseline(offset, dto(), self.policy, all_actions=True)
        self.assertEqual(result["baseline_status"], "disabled")
        self.assertEqual(issue_for(result, 1150)["severity"], "error")

    def test_issue_without_action_id_is_matched_by_message_without_digits(self):
        def with_plain_issue(base: dict, message: str, occurrences: int) -> dict:
            base["issues"].append({
                "severity": "warning", "action_id": None, "message": message,
                "occurrences": occurrences, "locations": [],
            })
            base["counts"]["warnings"] += 1
            return base

        baseline = with_plain_issue(dto(), "Overprint in 2 objects on page 1", 2)
        current = with_plain_issue(dto(revision=2), "Overprint in 2 objects on page 2", 2)
        current = with_plain_issue(current, "Hairline below 0.1 pt", 3)
        result = diff_against_baseline(current, baseline, None)

        overprint, hairline = result["issues"]
        self.assertEqual(overprint["severity"], "warning")
        self.assertNotIn("introduced_by_fix", overprint)
        self.assertTrue(hairline["hidden_by_baseline"])
        self.assertEqual(result["verdict"], "warning")
        # Со списком действий находки без action_id не сравниваются вовсе.
        listed = diff_against_baseline(current, baseline, frozenset({"2079"}))
        self.assertEqual([item["severity"] for item in listed["issues"]], ["warning", "warning"])

    def test_all_actions_is_idempotent(self):
        current = dto((LOWRES, 11), (CLIPPING, 8), revision=2)
        baseline = dto((LOWRES, 3))
        once = diff_against_baseline(current, baseline, None)
        self.assertEqual(diff_against_baseline(once, baseline, None), once)
        # Повтор со списком действий восстанавливает сырые значения 1150.
        listed = diff_against_baseline(once, baseline, frozenset({"2079"}))
        self.assertNotIn("introduced_by_fix", issue_for(listed, 1150))


# ------------------------------------------------------------- batch adapter
class FakeProcessor:
    """Минимальный BatchProcessor: собирает PDF заказа, превью не делает."""

    def __init__(self, input_dir, pdf_dir, *, profile, correction_policy):
        self.pdf_dir = Path(pdf_dir)
        self.on_orders_grouped = None

    def create_pdfs(self, orders):
        out = self.pdf_dir / "order.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        return [(orders[0], make_pdf(out, 101, 101), None)]

    def create_diagnostic_pdf(self, order, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        return make_pdf(output_dir / "order.pdf", 101, 101), None

    def generate_previews_for_files(self, *_args, **_kwargs):
        return []

    def generate_previews_for_all(self, *_args, **_kwargs):
        return []

    def copy_failed_to_troubles(self, *_args, **_kwargs):
        return []

    def revalidate_order(self, _order):
        return None

    def reinspect_pdf_file(self, item, _content_path):
        return item


class FakePitStop:
    """PDF заказа (``order.pdf``) — ``built``; исходники — из ``by_path`` (по умолчанию чистые)."""

    def __init__(self) -> None:
        self.built = report()
        self.by_path: dict[Path, object] = {}
        self.calls: list[Path] = []

    def check_pdf(self, pdf, *, profile_id):
        path = Path(pdf).resolve()
        self.calls.append(path)
        default = self.built if path.name == "order.pdf" else report()
        return check_result(pdf, self.by_path.get(path, default), f"check-{len(self.calls)}", profile_id)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _signature(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


class AdapterBleedFixTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.pitstop = FakePitStop()
        self.records = _Records()
        self.logger = logging.getLogger("image_magic.processing")
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.records)

    def tearDown(self) -> None:
        self.logger.removeHandler(self.records)
        self.logger.setLevel(self.previous_level)
        self.temporary.cleanup()

    def adapter(self, policy: FixBaselinePolicy | None = None) -> BatchProcessorAdapter:
        return BatchProcessorAdapter(
            ProcessingOptions(
                input_path=str(self.root), direction="digital",
                generate_previews=False, copy_failures=False,
            ),
            processor_factory=FakeProcessor,
            pitstop_service=self.pitstop,
            pitstop_fix_baseline=policy or FixBaselinePolicy(),
        )

    def side(self, name: str, *, fixed: bool, bleed_only: bool = True, raster: bool = False,
             file_errors: bool = False) -> FileCheck:
        source = self.root / name
        baseline_pdf: Path | None = None
        if raster:
            source.write_bytes(b"raster")
            if fixed:
                directory = self.root / "PDF" / "CallasFix" / f"{name}.source"
                directory.mkdir(parents=True, exist_ok=True)
                baseline_pdf = make_pdf(directory / "source.pdf", 101, 101)
        else:
            make_pdf(source, 101, 101)
            baseline_pdf = source
        item = FileCheck(path=source)
        if file_errors:
            item.errors.append("Разрешение ниже допустимого")
        if fixed:
            item.content_path = make_pdf(self.root / f"{source.stem}.fixed.pdf", 104, 104)
            item.callas_source_fix = {
                "plan": "size_correction",
                "source": _signature(source),
                "bleed_only": bleed_only,
                "bleed_only_reasons": ["bleed_added"] if bleed_only else [],
                "baseline_pdf": str(baseline_pdf),
            }
        return item

    def order(self, *files: FileCheck) -> OrderCheck:
        return OrderCheck(order_id="26124497", customer_id="5", files=list(files))

    def baseline_lines(self) -> list[str]:
        return [line for line in self.records.messages if line.startswith("order.pitstop_source_baseline ")]

    def assert_order_26124497_is_clean(self, order: OrderCheck, result) -> None:
        from services.coordinator import _pitstop_status_from_dto

        pitstop = result.pitstop
        self.assertEqual(pitstop["baseline_status"], "applied")
        self.assertTrue(issue_for(pitstop, 1150)["hidden_by_baseline"])
        self.assertTrue(issue_for(pitstop, 2079)["hidden_by_baseline"])
        self.assertEqual((pitstop["verdict_raw"], pitstop["verdict"]), ("error", "passed"))
        self.assertEqual(_pitstop_status_from_dto(pitstop), "passed")
        self.assertEqual(result.pitstop_baseline["kind"], "source_pdfs")
        self.assertEqual(
            self.pitstop.calls[1:],
            [Path(str(item.callas_source_fix["baseline_pdf"])).resolve() for item in order.files],
        )

    def test_order_26124497_production_branch(self):
        from services.coordinator import _completed_order_status

        self.pitstop.built = report((LOWRES, 8), (CLIPPING, 8))
        order = self.order(self.side("face.pdf", fixed=True), self.side("back.pdf", fixed=True))
        result = self.adapter().process_order(order)

        self.assertEqual(result.pdf_kind, "production")
        self.assert_order_26124497_is_clean(order, result)
        self.assertEqual(_completed_order_status(order, result), "passed")
        self.assertEqual(result.pdf_path.parent.name, "Checked")
        (line,) = self.baseline_lines()
        self.assertIn("branch=production scope=all status=applied", line)

    def test_order_26124497_diagnostic_branch_for_order_with_file_errors(self):
        from services.coordinator import _completed_order_status

        self.pitstop.built = report((LOWRES, 8), (CLIPPING, 8))
        order = self.order(
            self.side("face.pdf", fixed=True, file_errors=True), self.side("back.pdf", fixed=True)
        )
        self.assertFalse(order.passed)
        result = self.adapter().process_order(order)

        self.assertEqual(result.pdf_kind, "diagnostic")
        self.assert_order_26124497_is_clean(order, result)
        # Красным заказ делают только ошибки файлов, не PitStop.
        self.assertEqual(_completed_order_status(order, result), "warning")
        (line,) = self.baseline_lines()
        self.assertIn("branch=diagnostic scope=all status=applied", line)
        self.assertIn("verdict=error->passed", line)

    def test_client_low_resolution_stays_an_error_with_the_added_part(self):
        self.pitstop.built = report((LOWRES, 11), (CLIPPING, 8))
        face = self.side("face.pdf", fixed=True)
        back = self.side("back.pdf", fixed=True)
        self.pitstop.by_path[face.path] = report((LOWRES, 3))
        result = self.adapter().process_order(self.order(face, back))

        lowres = issue_for(result.pitstop, 1150)
        self.assertEqual(lowres["severity"], "error")
        self.assertEqual((lowres["baseline_occurrences"], lowres["introduced_by_fix"]), (3, 8))
        self.assertTrue(issue_for(result.pitstop, 2079)["hidden_by_baseline"])
        self.assertEqual(result.pitstop["verdict"], "error")
        self.assertEqual(result.pdf_path.parent.name, "Rework")

    def test_one_non_bleed_fix_keeps_the_configured_action_list(self):
        self.pitstop.built = report((LOWRES, 8), (CLIPPING, 8))
        order = self.order(
            self.side("face.pdf", fixed=True), self.side("back.pdf", fixed=True, bleed_only=False)
        )
        result = self.adapter().process_order(order)

        self.assertEqual(issue_for(result.pitstop, 1150)["severity"], "error")
        self.assertNotIn("hidden_by_baseline", issue_for(result.pitstop, 1150))
        self.assertTrue(issue_for(result.pitstop, 2079)["hidden_by_baseline"])
        self.assertEqual(result.pitstop["verdict"], "error")
        (line,) = self.baseline_lines()
        self.assertIn("scope=actions status=applied", line)

    def test_findings_of_an_unfixed_side_are_never_hidden(self):
        self.pitstop.built = report((LOWRES, 8), (CLIPPING, 8))
        face = self.side("face.pdf", fixed=True)
        back = self.side("back.pdf", fixed=False)
        self.pitstop.by_path[back.path] = report((LOWRES, 8))
        result = self.adapter().process_order(self.order(face, back))

        self.assertEqual(self.pitstop.calls[1:], [face.path, back.path])
        self.assertEqual(issue_for(result.pitstop, 1150)["severity"], "error")
        self.assertNotIn("introduced_by_fix", issue_for(result.pitstop, 1150))
        self.assertTrue(issue_for(result.pitstop, 2079)["hidden_by_baseline"])

    def test_raster_side_with_fix_is_checked_through_its_baseline_pdf(self):
        self.pitstop.built = report((LOWRES, 8), (CLIPPING, 8))
        face = self.side("face.tif", fixed=True, raster=True)
        back = self.side("back.tif", fixed=True, raster=True)
        result = self.adapter().process_order(self.order(face, back))

        baseline_pdfs = [Path(str(item.callas_source_fix["baseline_pdf"])) for item in (face, back)]
        self.assertTrue(all(path.name == "source.pdf" for path in baseline_pdfs))
        self.assertEqual(self.pitstop.calls[1:], baseline_pdfs)
        self.assertEqual(result.pitstop["verdict"], "passed")

    def test_raster_side_without_fix_still_gives_no_baseline(self):
        self.pitstop.built = report((LOWRES, 8))
        face = self.side("face.pdf", fixed=True)
        back = self.side("back.tif", fixed=False, raster=True)
        result = self.adapter().process_order(self.order(face, back))

        self.assertEqual(len(self.pitstop.calls), 1)
        self.assertEqual(result.pitstop["baseline_status"], "missing")
        self.assertEqual(result.pitstop["verdict"], "error")
        self.assertIsNone(result.pitstop_baseline)
        (line,) = self.baseline_lines()
        self.assertIn("status=missing reason=raster_source:back.tif", line)

    def test_orders_without_accepted_fixes_get_no_extra_pitstop_call(self):
        self.pitstop.built = report((LOWRES, 8))
        for file_errors in (False, True):
            with self.subTest(diagnostic=file_errors):
                self.pitstop.calls.clear()
                order = self.order(
                    self.side("face.pdf", fixed=False, file_errors=file_errors),
                    self.side("back.pdf", fixed=False),
                )
                result = self.adapter().process_order(order)
                self.assertEqual(result.pdf_kind, "diagnostic" if file_errors else "production")
                self.assertEqual(len(self.pitstop.calls), 1)
                self.assertNotIn("baseline_status", result.pitstop)
                self.assertEqual(result.pitstop["verdict"], "error")
        self.assertEqual(self.baseline_lines(), [])


if __name__ == "__main__":
    unittest.main()
