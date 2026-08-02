import json
import unittest
from pathlib import Path

from services.pitstop import (
    PitStopReportError,
    PitStopSeverity,
    PitStopVerdict,
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


if __name__ == "__main__":
    unittest.main()
