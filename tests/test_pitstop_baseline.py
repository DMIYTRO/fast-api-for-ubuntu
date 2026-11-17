"""Сравнение проверки PitStop исправленного PDF с проверкой исходного PDF клиента."""

from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from services.batch_adapter import _pitstop_result_to_dto
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopReport,
    PitStopSeverity,
)
from services.pitstop.baseline import (
    FixBaselinePolicy,
    apply_fix_baseline,
    diff_against_baseline,
    is_original_check,
    merge_source_checks,
)


ACTIONS = frozenset({"2079", "3004"})
TRANSPARENCY = (3004, PitStopSeverity.ERROR, "Transparent objects")
CLIPPING = (2079, PitStopSeverity.WARNING, "Clipping objects")
RGB = (2078, PitStopSeverity.ERROR, "RGB color is used")


def dto(*items: tuple[tuple[int, PitStopSeverity, str], int], revision: int = 1, status=PitStopExecutionStatus.COMPLETED):
    """PitStop DTO, собранный тем же кодом, что и в проде (_pitstop_result_to_dto)."""
    issues = tuple(
        PitStopIssue(
            severity=severity,
            action_id=action,
            message=f"{text} ({count}x on pages 1-2)",
            total_occurrences=count,
        )
        for (action, severity, text), count in items
    )
    counts = PitStopCounts(
        errors=sum(1 for issue in issues if issue.severity is PitStopSeverity.ERROR),
        warnings=sum(1 for issue in issues if issue.severity is PitStopSeverity.WARNING),
        critical_failures=sum(1 for issue in issues if issue.severity is PitStopSeverity.CRITICAL_FAILURE),
    )
    completed = status is PitStopExecutionStatus.COMPLETED
    result = PitStopCheckResult(
        status=status,
        profile_id="digital",
        input_pdf=Path("/tmp/sample.pdf"),
        checked_at=datetime(2026, 11, 17, tzinfo=timezone.utc),
        input_sha256="0" * 64,
        report=PitStopReport(counts=counts, issues=issues, pages=2) if completed else None,
        technical_error=None if completed else "SSH недоступен",
        job_id=f"check-r{revision}",
    )
    return _pitstop_result_to_dto(result, checked_revision=revision)


def issue_for(pitstop, action_id):
    return next(item for item in pitstop["issues"] if str(item["action_id"]) == str(action_id))


class DiffAgainstBaselineTests(unittest.TestCase):
    def test_same_client_transparency_stays_an_error_untouched(self):
        baseline = dto((TRANSPARENCY, 90))
        current = dto((TRANSPARENCY, 90), revision=2)
        result = diff_against_baseline(current, baseline, ACTIONS)
        issue = issue_for(result, 3004)
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["occurrences"], 90)
        for field in ("baseline_occurrences", "introduced_by_fix", "hidden_by_baseline", "original_severity"):
            self.assertNotIn(field, issue)
        self.assertEqual((result["verdict"], result["verdict_raw"]), ("error", "error"))
        self.assertEqual(result["baseline_status"], "applied")

    def test_partially_introduced_issue_keeps_severity_and_reports_split(self):
        result = diff_against_baseline(
            dto((TRANSPARENCY, 150), revision=2), dto((TRANSPARENCY, 90)), ACTIONS
        )
        issue = issue_for(result, 3004)
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["occurrences"], 150)
        self.assertEqual((issue["baseline_occurrences"], issue["introduced_by_fix"]), (90, 60))
        self.assertNotIn("hidden_by_baseline", issue)
        self.assertEqual(result["verdict"], "error")
        self.assertEqual(result["counts"]["errors"], 1)

    def test_issue_absent_in_baseline_is_hidden_as_information(self):
        result = diff_against_baseline(dto((TRANSPARENCY, 60), revision=2), dto(), ACTIONS)
        issue = issue_for(result, 3004)
        self.assertTrue(issue["hidden_by_baseline"])
        self.assertEqual(issue["original_severity"], "error")
        self.assertEqual(issue["severity"], "information")
        self.assertEqual((issue["baseline_occurrences"], issue["introduced_by_fix"]), (0, 60))
        self.assertEqual(result["counts"]["errors"], 0)
        self.assertEqual(result["counts"]["informations"], 1)
        self.assertEqual(result["counts_raw"]["errors"], 1)
        self.assertEqual((result["verdict"], result["verdict_raw"]), ("passed", "error"))

    def test_real_size_correction_case(self):
        """Реальные данные (профиль Sborka): 3004 90→90, 2079 168→1670."""
        baseline = dto((TRANSPARENCY, 90), (CLIPPING, 168))
        current = dto((TRANSPARENCY, 90), (CLIPPING, 1670), revision=2)
        result = diff_against_baseline(current, baseline, ACTIONS)
        transparency = issue_for(result, 3004)
        clipping = issue_for(result, 2079)
        self.assertEqual(transparency["severity"], "error")
        self.assertNotIn("introduced_by_fix", transparency)
        self.assertEqual(clipping["severity"], "warning")
        self.assertEqual((clipping["baseline_occurrences"], clipping["introduced_by_fix"]), (168, 1502))
        self.assertEqual(result["verdict"], "error")
        self.assertEqual(result["counts"], result["counts_raw"])

    def test_missing_baseline_returns_current_unchanged(self):
        current = dto((TRANSPARENCY, 60), revision=2)
        result = diff_against_baseline(current, None, ACTIONS)
        self.assertEqual(result["baseline_status"], "missing")
        result.pop("baseline_status")
        self.assertEqual(result, current)

    def test_failed_baseline_or_current_returns_current_unchanged(self):
        failed = dto(status=PitStopExecutionStatus.FAILED)
        current = dto((TRANSPARENCY, 60), revision=2)
        for result, source in (
            (diff_against_baseline(current, failed, ACTIONS), current),
            (diff_against_baseline(dto(status=PitStopExecutionStatus.FAILED, revision=2), dto(), ACTIONS),
             dto(status=PitStopExecutionStatus.FAILED, revision=2)),
        ):
            self.assertEqual(result["baseline_status"], "failed")
            result.pop("baseline_status")
            self.assertEqual(result, source)

    def test_unknown_action_is_untouched_even_if_new(self):
        result = diff_against_baseline(dto((RGB, 5), revision=2), dto(), ACTIONS)
        issue = issue_for(result, 2078)
        self.assertEqual(issue["severity"], "error")
        self.assertNotIn("hidden_by_baseline", issue)
        self.assertEqual(result["verdict"], "error")

    def test_matching_is_by_action_id_not_message(self):
        baseline = dto((TRANSPARENCY, 90))
        baseline["issues"][0]["message"] = "Прозрачные объекты (90x on pages 1-2)"
        result = diff_against_baseline(dto((TRANSPARENCY, 90), revision=2), baseline, ACTIONS)
        self.assertNotIn("hidden_by_baseline", issue_for(result, 3004))

    def test_verdict_recomputed_error_to_warning_to_passed(self):
        baseline = dto()
        to_warning = diff_against_baseline(
            dto((TRANSPARENCY, 90), (CLIPPING, 500), (CLIPPING[:2] + ("Other",), 1), revision=2),
            dto((CLIPPING, 600)),
            ACTIONS,
        )
        self.assertEqual((to_warning["verdict_raw"], to_warning["verdict"]), ("error", "warning"))
        self.assertEqual((to_warning["counts"]["errors"], to_warning["counts"]["warnings"]), (0, 2))
        to_passed = diff_against_baseline(
            dto((TRANSPARENCY, 90), (CLIPPING, 1670), revision=2), baseline, ACTIONS
        )
        self.assertEqual((to_passed["verdict_raw"], to_passed["verdict"]), ("error", "passed"))
        self.assertEqual(to_passed["counts"]["informations"], 2)
        self.assertEqual(
            sum(to_passed["counts"].values()), sum(to_passed["counts_raw"].values())
        )

    def test_baseline_budget_is_shared_between_issues_of_one_action(self):
        current = dto((CLIPPING, 100), (CLIPPING[:2] + ("Clipping objects again",), 50), revision=2)
        result = diff_against_baseline(current, dto((CLIPPING, 120)), ACTIONS)
        first, second = result["issues"]
        self.assertNotIn("introduced_by_fix", first)
        self.assertEqual((second["baseline_occurrences"], second["introduced_by_fix"]), (20, 30))
        self.assertEqual(second["severity"], "warning")

    def test_inputs_are_not_mutated(self):
        baseline = dto((TRANSPARENCY, 90))
        current = dto((TRANSPARENCY, 150), (CLIPPING, 1670), revision=2)
        baseline_before, current_before = deepcopy(baseline), deepcopy(current)
        result = diff_against_baseline(current, baseline, ACTIONS)
        self.assertEqual((baseline, current), (baseline_before, current_before))
        result["issues"][0]["severity"] = "changed"
        self.assertEqual(current, current_before)

    def test_reapplying_to_adjusted_dto_does_not_double_adjust(self):
        baseline = dto((TRANSPARENCY, 90), (CLIPPING, 168))
        current = dto((TRANSPARENCY, 150), (CLIPPING, 1670), (RGB, 1), revision=2)
        once = diff_against_baseline(current, baseline, ACTIONS)
        twice = diff_against_baseline(once, baseline, ACTIONS)
        self.assertEqual(twice, once)
        hidden = diff_against_baseline(current, dto(), ACTIONS)
        self.assertEqual(diff_against_baseline(hidden, dto(), ACTIONS), hidden)
        # Повторное сравнение без baseline восстанавливает сырые данные PitStop.
        restored = diff_against_baseline(hidden, None, ACTIONS)
        restored.pop("baseline_status")
        self.assertEqual(restored, current)


class MergeSourceChecksTests(unittest.TestCase):
    def test_counts_of_all_sources_are_summed_per_action(self):
        merged = merge_source_checks([
            ("face.pdf", dto((CLIPPING, 168), (TRANSPARENCY, 90))),
            ("back.pdf", dto((TRANSPARENCY, 30))),
        ])
        self.assertEqual(merged["execution_status"], "completed")
        self.assertEqual(merged["kind"], "source_pdfs")
        self.assertEqual(merged["profile"]["key"], "digital")
        self.assertEqual([item["path"] for item in merged["sources"]], ["face.pdf", "back.pdf"])
        result = diff_against_baseline(
            dto((TRANSPARENCY, 120), (CLIPPING, 1670), revision=1), merged, ACTIONS
        )
        self.assertNotIn("introduced_by_fix", issue_for(result, 3004))
        self.assertEqual(issue_for(result, 2079)["introduced_by_fix"], 1502)

    def test_any_failed_source_fails_the_baseline(self):
        merged = merge_source_checks([
            ("face.pdf", dto((CLIPPING, 168))),
            ("back.pdf", dto(status=PitStopExecutionStatus.FAILED)),
        ])
        self.assertEqual(merged["execution_status"], "failed")
        self.assertEqual(merged["technical_error"], "SSH недоступен")
        result = diff_against_baseline(dto((CLIPPING, 1670)), merged, ACTIONS)
        self.assertEqual(result["baseline_status"], "failed")
        self.assertEqual(merge_source_checks([])["execution_status"], "failed")


class PolicyTests(unittest.TestCase):
    def test_other_profile_or_disabled_policy_only_marks_status(self):
        current = dto((TRANSPARENCY, 60), revision=2)
        for policy in (FixBaselinePolicy(enabled=False), FixBaselinePolicy(profiles=frozenset({"offset"}))):
            result = apply_fix_baseline(current, dto(), policy)
            self.assertEqual(result["baseline_status"], "disabled")
            self.assertEqual(issue_for(result, 3004)["severity"], "error")

    def test_enabled_policy_uses_configured_actions(self):
        current = dto((TRANSPARENCY, 60), (CLIPPING, 10), revision=2)
        result = apply_fix_baseline(current, dto(), FixBaselinePolicy(actions=frozenset({"2079"})))
        self.assertEqual(issue_for(result, 3004)["severity"], "error")
        self.assertTrue(issue_for(result, 2079)["hidden_by_baseline"])

    def test_baseline_of_another_profile_is_not_compared(self):
        baseline = dto()
        baseline["profile"]["key"] = "offset"
        result = apply_fix_baseline(dto((TRANSPARENCY, 60), revision=2), baseline, FixBaselinePolicy())
        self.assertEqual(result["baseline_status"], "missing")
        self.assertEqual(issue_for(result, 3004)["severity"], "error")

    def test_original_check_is_the_first_revision(self):
        self.assertTrue(is_original_check(dto(), 1))
        self.assertTrue(is_original_check(dto(), None))
        self.assertFalse(is_original_check(dto(revision=2), 2))
        self.assertFalse(is_original_check(dto(), 2))  # PDF уже заменён, проверка устарела
        self.assertFalse(is_original_check(None, 1))


if __name__ == "__main__":
    unittest.main()
