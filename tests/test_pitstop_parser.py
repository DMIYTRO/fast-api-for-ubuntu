import json
import unittest
from pathlib import Path

from datetime import datetime, timezone

from services.batch_adapter import _pitstop_result_to_dto
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopLocation,
    PitStopReport,
    PitStopReportError,
    PitStopSeverity,
    PitStopVerdict,
    parse_occurrences,
    parse_pitstop_payload,
    parse_pitstop_report,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pitstop"


class PitStopParserTests(unittest.TestCase):
    def test_parses_realistic_warning_report(self):
        report = parse_pitstop_report(FIXTURES / "warning_report.json")

        self.assertEqual(report.profile_name, "Sborka_2Corel")
        self.assertEqual(report.pages, 2)
        self.assertEqual(report.counts.warnings, 1)
        self.assertEqual(report.verdict, PitStopVerdict.WARNING)
        self.assertFalse(report.counts.has_errors)
        self.assertEqual(report.issues[0].severity, PitStopSeverity.WARNING)
        self.assertEqual(report.issues[0].action_id, 2078)
        self.assertEqual(report.issues[0].locations[0].page, 1)
        self.assertEqual(report.issues[0].locations[0].max_x, 124.8)

    def test_accepts_single_issue_object_and_multiple_locations(self):
        report = parse_pitstop_report(FIXTURES / "error_report.json")

        self.assertTrue(report.counts.has_errors)
        self.assertEqual(report.verdict, PitStopVerdict.ERROR)
        self.assertEqual([location.page for location in report.issues[0].locations], [1, 2])

    def test_rejects_missing_preflight_report(self):
        with self.assertRaisesRegex(PitStopReportError, "preflightReport"):
            parse_pitstop_payload({})

    def test_rejects_negative_count(self):
        payload = json.loads((FIXTURES / "warning_report.json").read_text())
        payload["preflightReport"]["warningsNumber"] = -1

        with self.assertRaisesRegex(PitStopReportError, "warningsNumber"):
            parse_pitstop_payload(payload)


class ParseOccurrencesTests(unittest.TestCase):
    def test_single_page(self):
        self.assertEqual(parse_occurrences("Text found (3x on page 1)"), (3, (1,)))

    def test_page_range(self):
        self.assertEqual(
            parse_occurrences("Clipping objects found (1670x on pages 1-2)"),
            (1670, (1, 2)),
        )

    def test_comma_list_with_ranges_sorted_unique(self):
        self.assertEqual(
            parse_occurrences("Objects (12x on pages 3-4, 1, 4)"),
            (12, (1, 3, 4)),
        )

    def test_no_suffix(self):
        self.assertIsNone(parse_occurrences("Document has no bleed"))
        self.assertIsNone(parse_occurrences(""))

    def test_malformed_suffix(self):
        self.assertIsNone(parse_occurrences("Objects (3x on pages)"))
        self.assertIsNone(parse_occurrences("Objects (3x on pages a-b)"))
        self.assertIsNone(parse_occurrences("Objects (3x on pages 4-2)"))
        self.assertIsNone(parse_occurrences("Objects (3x on pages 1,,2)"))
        self.assertIsNone(parse_occurrences("Objects (3x on page 0)"))
        self.assertIsNone(parse_occurrences("Objects (3 on page 1)"))

    def test_suffix_must_be_trailing(self):
        self.assertIsNone(parse_occurrences("Objects (3x on page 1) found"))

    def test_parser_fills_total_occurrences(self):
        payload = {
            "preflightReport": {
                "process": {},
                "properties": {},
                "warningsNumber": 2,
                "warnings": {
                    "preflightReportItem": [
                        {
                            "actionID": 1,
                            "message": "Transparent objects have been found (90x on pages 1-2)",
                            "location": {"page": 0},
                        },
                        {"actionID": 2, "message": "No suffix here"},
                    ]
                },
            }
        }
        report = parse_pitstop_payload(payload)
        self.assertEqual(report.issues[0].total_occurrences, 90)
        self.assertEqual(len(report.issues[0].locations), 1)
        self.assertIsNone(report.issues[1].total_occurrences)


def _result_with(*issues: PitStopIssue) -> PitStopCheckResult:
    return PitStopCheckResult(
        status=PitStopExecutionStatus.COMPLETED,
        profile_id="p",
        input_pdf=Path("/tmp/x.pdf"),
        checked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        input_sha256="0" * 64,
        report=PitStopReport(counts=PitStopCounts(warnings=len(issues)), issues=issues),
        job_id="job",
    )


class PitStopDtoOccurrencesTests(unittest.TestCase):
    def test_dto_uses_total_even_when_locations_are_capped(self):
        locations = tuple(PitStopLocation(page=1) for _ in range(100))
        issue = PitStopIssue(
            severity=PitStopSeverity.WARNING,
            message="Clipping objects found (1670x on pages 1-2)",
            action_id=7,
            locations=locations,
            total_occurrences=1670,
        )
        dto = _pitstop_result_to_dto(_result_with(issue), checked_revision=1)
        self.assertEqual(dto["issues"][0]["occurrences"], 1670)
        self.assertEqual(len(dto["issues"][0]["locations"]), 100)

    def test_dto_falls_back_to_location_count(self):
        issue = PitStopIssue(
            severity=PitStopSeverity.WARNING,
            message="No suffix",
            locations=(PitStopLocation(page=1), PitStopLocation(page=2)),
        )
        empty = PitStopIssue(severity=PitStopSeverity.WARNING, message="Nothing")
        dto = _pitstop_result_to_dto(_result_with(issue, empty), checked_revision=1)
        self.assertEqual([i["occurrences"] for i in dto["issues"]], [2, 1])

    def test_fingerprint_is_independent_of_total_occurrences(self):
        base = dict(severity=PitStopSeverity.WARNING, message="m (5x on page 1)", action_id=1)
        a = _pitstop_result_to_dto(_result_with(PitStopIssue(**base)), checked_revision=1)
        b = _pitstop_result_to_dto(
            _result_with(PitStopIssue(**base, total_occurrences=5)), checked_revision=1
        )
        self.assertEqual(a["issues"][0]["fingerprint"], b["issues"][0]["fingerprint"])


if __name__ == "__main__":
    unittest.main()
