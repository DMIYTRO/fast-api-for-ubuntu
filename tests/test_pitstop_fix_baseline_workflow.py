"""PitStop после принятого исправления Callas сравнивается с проверкой исходного PDF.

Сквозные сценарии через create_app (SQL-репозиторий): принятие исправления,
цепочка исправлений, ручная замена PDF, отсутствие исходной проверки, перезапуск.
"""

from __future__ import annotations

import hashlib
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import control_panel
from services.batch_adapter import OrderArtifacts, _pitstop_result_to_dto
from services.pitstop import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopReport,
    PitStopSeverity,
)
from services.pitstop.baseline import FixBaselinePolicy
from services.print_readiness import pitstop_ready, print_blockers
from services.repository import InMemoryRunRepository
from tests.test_callas_fix_api import CallasFixApiTests, RealPdfAdapter
from tests.test_callas_fix_service import make_pdf


TRANSPARENCY = (3004, PitStopSeverity.ERROR, "Transparent objects")
CLIPPING = (2079, PitStopSeverity.WARNING, "Clipping objects")
RGB = (2078, PitStopSeverity.ERROR, "RGB color is used")
LOWRES = (1150, PitStopSeverity.ERROR, "Image resolution is below 250 ppi")


def report(*items) -> PitStopReport:
    issues = tuple(
        PitStopIssue(
            severity=severity,
            action_id=action,
            message=f"{text} ({count}x on pages 1-2)",
            total_occurrences=count,
        )
        for (action, severity, text), count in items
    )
    return PitStopReport(
        counts=PitStopCounts(
            errors=sum(1 for item in issues if item.severity is PitStopSeverity.ERROR),
            warnings=sum(1 for item in issues if item.severity is PitStopSeverity.WARNING),
        ),
        issues=issues,
        pages=1,
    )


def check_result(
    pdf: Path, pitstop_report: PitStopReport, job_id: str, profile_id: str = "digital"
) -> PitStopCheckResult:
    return PitStopCheckResult(
        status=PitStopExecutionStatus.COMPLETED,
        profile_id=profile_id,
        input_pdf=Path(pdf),
        checked_at=datetime.now(UTC),
        input_sha256=hashlib.sha256(Path(pdf).read_bytes()).hexdigest(),
        report=pitstop_report,
        job_id=job_id,
    )


class FakePitStop:
    """Отдаёт заранее заданные отчёты по очереди; считает запуски."""

    def __init__(self) -> None:
        self.reports: list[PitStopReport] = []
        self.calls = 0

    def check_pdf(self, pdf, *, profile_id):
        self.calls += 1
        return check_result(pdf, self.reports.pop(0), f"recheck-{self.calls}", profile_id)


class BaselineFixTests(CallasFixApiTests):
    original_report: PitStopReport | None = report((TRANSPARENCY, 90), (CLIPPING, 168))

    def setUp(self) -> None:
        test = self

        class Adapter(RealPdfAdapter):
            def process_order(self, order):
                artifacts = super().process_order(order)
                if test.original_report is None:
                    return artifacts
                pdf = artifacts.pdf_path
                return OrderArtifacts(
                    pdf_path=pdf,
                    preview_paths=artifacts.preview_paths,
                    pitstop=_pitstop_result_to_dto(
                        check_result(pdf, test.original_report, "original-check"),
                        checked_revision=1,
                    ),
                    current_pdf_revision=1,
                    current_pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                )

        self.adapter_class = Adapter
        super().setUp()
        self.pitstop = FakePitStop()
        self.workflow.reviewed_pdf_pitstop_service_factory = lambda _root, _direction: self.pitstop

    @property
    def workflow(self):
        return self.client.app.state.order_workflow

    def make_run(self, direction: str = "digital") -> str:
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": direction,
                "create_pdfs": True,
                "generate_previews": False,
                "copy_failures": False,
            },
        )
        self.assertEqual(response.status_code, 202)
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=3)
        return run_id

    def fix_and_accept(self, run_id: str, *reports: PitStopReport) -> None:
        self.pitstop.reports.extend(reports)
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        response = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(response.status_code, 200, response.text)

    def api_order(self, run_id: str) -> dict:
        return self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]

    def stored_order(self, run_id: str) -> dict:
        run = self.client.app.state.coordinator.get_run(run_id)
        return next(iter(run["orders"].values()))

    @staticmethod
    def issue(pitstop: dict, action_id: int) -> dict:
        return next(item for item in pitstop["issues"] if str(item["action_id"]) == str(action_id))

    def test_size_correction_keeps_client_transparency_and_discounts_added_clipping(self):
        run_id = self.make_run()
        self.fix_and_accept(run_id, report((TRANSPARENCY, 90), (CLIPPING, 1670)))
        self.assertEqual(self.pitstop.calls, 1)  # сравнение не добавляет запусков PitStop
        order = self.api_order(run_id)
        pitstop = order["pitstop"]
        self.assertEqual(pitstop["baseline_status"], "applied")
        self.assertEqual(self.issue(pitstop, 3004)["severity"], "error")
        self.assertNotIn("introduced_by_fix", self.issue(pitstop, 3004))
        clipping = self.issue(pitstop, 2079)
        self.assertEqual(
            (clipping["occurrences"], clipping["baseline_occurrences"], clipping["introduced_by_fix"]),
            (1670, 168, 1502),
        )
        self.assertEqual(order["pitstop_baseline"]["check_id"], "original-check")
        self.assertEqual(
            order["pitstop_baseline"]["reports"],
            {"json_url": None, "xml_url": None},
        )

    def test_chained_fixes_compare_against_the_original_check(self):
        run_id = self.make_run()
        self.fix_and_accept(run_id, report((TRANSPARENCY, 150), (CLIPPING, 1670)))
        first = self.stored_order(run_id)
        self.assertEqual(self.issue(first["pitstop"], 3004)["introduced_by_fix"], 60)

        make_pdf(self.client_double.fixed_source, 103, 214)
        self.fix_and_accept(run_id, report((TRANSPARENCY, 200), (CLIPPING, 168)))
        second = self.stored_order(run_id)
        self.assertEqual(second["current_pdf_revision"], 3)
        self.assertEqual(second["pitstop_baseline"]["check_id"], "original-check")
        self.assertEqual(second["pitstop_baseline"]["checked_revision"], 1)
        transparency = self.issue(second["pitstop"], 3004)
        # 90 из исходного PDF, а не 150 из предыдущего исправления.
        self.assertEqual((transparency["baseline_occurrences"], transparency["introduced_by_fix"]), (90, 110))
        self.assertNotIn("introduced_by_fix", self.issue(second["pitstop"], 2079))

    def test_order_whose_only_blocker_was_added_by_fix_becomes_ready(self):
        self.original_report = report()
        run_id = self.make_run()
        self.assertEqual(self.api_order(run_id)["pitstop"]["verdict"], "passed")
        self.fix_and_accept(run_id, report((TRANSPARENCY, 40), (CLIPPING, 1670)))
        order = self.api_order(run_id)
        pitstop = order["pitstop"]
        self.assertEqual((pitstop["verdict_raw"], pitstop["verdict"]), ("error", "passed"))
        self.assertEqual(pitstop["counts"]["errors"], 0)
        self.assertEqual(pitstop["counts_raw"]["errors"], 1)
        hidden = self.issue(pitstop, 3004)
        self.assertEqual((hidden["hidden_by_baseline"], hidden["original_severity"], hidden["severity"]),
                         (True, "error", "information"))
        self.assertEqual(order["status"], "passed")
        self.assertEqual(order["pitstop_status"], "passed")
        self.assertEqual(order["print_blockers"], [])
        self.assertEqual(order["print_mode"], "normal")
        stored = self.stored_order(run_id)
        self.assertTrue(pitstop_ready(stored))
        self.assertEqual(print_blockers(stored), [])
        prepared = self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        self.assertEqual(prepared.json()["items"][0]["status"], "prepared", prepared.text)

    def recheck(self, run_id: str, pitstop_report: PitStopReport, **kwargs):
        run = self.client.app.state.coordinator.get_run(run_id)
        order = next(iter(run["orders"].values()))
        candidate = make_pdf(self.root / "PDF" / "callas-candidate.pdf", 103, 214)
        self.pitstop.reports.append(pitstop_report)
        return self.workflow.recheck_pdf_revision(run, order, candidate, callas_fix=True, **kwargs)

    def test_bleed_only_fix_of_built_pdf_hides_everything_new(self):
        self.original_report = report()
        run_id = self.make_run()
        order, pitstop, _revision, _sha = self.recheck(
            run_id, report((LOWRES, 8), (CLIPPING, 8)), bleed_only=True
        )
        self.assertEqual(self.pitstop.calls, 1)
        for action, severity in ((1150, "error"), (2079, "warning")):
            hidden = self.issue(pitstop, action)
            self.assertEqual((hidden["hidden_by_baseline"], hidden["original_severity"]), (True, severity))
        self.assertEqual((pitstop["verdict_raw"], pitstop["verdict"]), ("error", "passed"))
        self.assertEqual(order["status"], "passed")
        self.assertEqual(order["pitstop_status"], "passed")
        self.assertTrue(pitstop_ready(order))
        self.assertEqual(print_blockers(order), [])

    def test_start_request_passes_the_check_policy_run_option(self):
        body = {"input_path": str(self.root), "direction": "digital", "generate_previews": False,
                "copy_failures": False}
        response = self.client.post("/api/checks", json={**body, "check_policy": "full"})
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=3)
        run = self.client.app.state.coordinator.get_run(run_id)
        self.assertEqual(run["options"]["check_policy"], "full")
        self.assertEqual(self.client.post("/api/checks", json={**body, "check_policy": "bogus"}).status_code, 422)
        default = self.client.post("/api/checks", json=body)
        self.assertEqual(default.status_code, 202, default.text)
        self.client.app.state.coordinator.wait_for(default.json()["id"], timeout=3)
        self.assertEqual(
            self.client.app.state.coordinator.get_run(default.json()["id"])["options"]["check_policy"], "auto"
        )

    def test_recheck_applies_the_basic_policy_after_the_baseline(self):
        from services.check_policy import PolicySettings

        self.original_report = report((TRANSPARENCY, 90))
        run_id = self.make_run()
        self.workflow.policy_settings = PolicySettings("basic", ("CorelDRAW",))
        order, pitstop, _revision, _sha = self.recheck(run_id, report((TRANSPARENCY, 90), (CLIPPING, 5)))
        transparency = self.issue(pitstop, 3004)
        # Клиентская прозрачность сохранена сравнением, но для облегчённой проверки справочная.
        self.assertEqual((transparency["severity"], transparency["policy_demoted"]), ("information", True))
        self.assertEqual(transparency["policy_original_severity"], "error")
        self.assertEqual(pitstop["check_policy"], "basic")
        self.assertEqual(pitstop["baseline_status"], "applied")
        self.assertEqual((pitstop["verdict_before_policy"], pitstop["verdict"]), ("error", "passed"))
        self.assertEqual(order["status"], "passed")

    def test_recheck_uses_the_policy_stored_with_the_order(self):
        from services.check_policy import PolicySettings

        self.original_report = report((TRANSPARENCY, 90))
        run_id = self.make_run()
        self.workflow.policy_settings = PolicySettings("basic", ("CorelDRAW",))
        run = self.client.app.state.coordinator.get_run(run_id)
        order = next(iter(run["orders"].values()))
        order["check_policy"] = {
            "key": "full", "label": "Полная проверка (CorelDRAW)", "reason": "strict_folder",
        }
        candidate = make_pdf(self.root / "PDF" / "callas-candidate.pdf", 103, 214)
        self.pitstop.reports.append(report((TRANSPARENCY, 90)))
        updated, pitstop, _revision, _sha = self.workflow.recheck_pdf_revision(
            run, order, candidate, callas_fix=True
        )
        self.assertEqual(pitstop["check_policy"], "full")
        self.assertEqual(self.issue(pitstop, 3004)["severity"], "error")
        self.assertEqual(updated["status"], "warning")

    def test_bleed_only_fix_keeps_client_findings_and_their_severity(self):
        self.original_report = report((TRANSPARENCY, 90), (LOWRES, 3))
        run_id = self.make_run()
        _order, pitstop, _revision, _sha = self.recheck(
            run_id, report((TRANSPARENCY, 90), (LOWRES, 11), (CLIPPING, 8)), bleed_only=True
        )
        self.assertEqual(self.issue(pitstop, 3004)["severity"], "error")
        self.assertNotIn("introduced_by_fix", self.issue(pitstop, 3004))
        lowres = self.issue(pitstop, 1150)
        self.assertEqual(
            (lowres["severity"], lowres["baseline_occurrences"], lowres["introduced_by_fix"]),
            ("error", 3, 8),
        )
        self.assertTrue(self.issue(pitstop, 2079)["hidden_by_baseline"])
        self.assertEqual(pitstop["verdict"], "error")

    def test_fix_that_is_not_bleed_only_compares_only_the_configured_actions(self):
        self.original_report = report()
        run_id = self.make_run()
        order, pitstop, _revision, _sha = self.recheck(run_id, report((LOWRES, 8), (CLIPPING, 8)))
        self.assertEqual(self.issue(pitstop, 1150)["severity"], "error")
        self.assertNotIn("hidden_by_baseline", self.issue(pitstop, 1150))
        self.assertTrue(self.issue(pitstop, 2079)["hidden_by_baseline"])
        self.assertEqual(pitstop["verdict"], "error")
        self.assertEqual(order["pitstop_status"], "error")

    def test_accepted_bleed_fix_sent_to_print_hides_everything_new(self):
        import json

        from services.callas_fix import STATE_NAME, fix_dir_for

        self.original_report = report()
        run_id = self.make_run()
        pdf = Path(self.stored_order(run_id)["pdf_path"])
        state_dir = fix_dir_for(pdf)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / STATE_NAME).write_text(
            json.dumps({"status": "accepted", "plan": "size_correction", "summary": {"bleed_only": True}}),
            encoding="utf-8",
        )
        candidate = make_pdf(self.root / "PDF" / "sample_callas-fixed.pdf", 103, 214)
        self.workflow.accepted_pdf_provider = lambda _pdf: candidate
        self.pitstop.reports.append(report((LOWRES, 8), (CLIPPING, 8)))
        self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.stored_order(run_id)["status"] != "accepted_for_print":
            time.sleep(0.05)
        order = self.stored_order(run_id)
        self.assertEqual(order["status"], "accepted_for_print")
        self.assertEqual(self.pitstop.calls, 1)
        self.assertTrue(self.issue(order["pitstop"], 1150)["hidden_by_baseline"])
        self.assertEqual(order["pitstop"]["verdict"], "passed")

    def test_disabled_comparison_keeps_raw_pitstop(self):
        self.workflow.pitstop_fix_baseline = FixBaselinePolicy(enabled=False)
        self.original_report = report()
        run_id = self.make_run()
        self.fix_and_accept(run_id, report((TRANSPARENCY, 40)))
        order = self.api_order(run_id)
        self.assertEqual(order["pitstop"]["baseline_status"], "disabled")
        self.assertEqual(order["pitstop"]["verdict"], "error")
        self.assertEqual(order["status"], "warning")

    def test_other_profile_is_not_compared(self):
        self.original_report = report()
        run_id = self.make_run("offset")
        self.fix_and_accept(run_id, report((TRANSPARENCY, 40)))
        order = self.api_order(run_id)
        self.assertEqual(order["pitstop"]["baseline_status"], "disabled")
        self.assertEqual(self.issue(order["pitstop"], 3004)["severity"], "error")

    def test_manual_replacement_is_not_compared_and_ends_the_chain(self):
        self.original_report = report()
        run_id = self.make_run()
        self.fix_and_accept(run_id, report((TRANSPARENCY, 40)))
        run = self.client.app.state.coordinator.get_run(run_id)
        order = next(iter(run["orders"].values()))
        self.assertTrue(self.issue(order["pitstop"], 3004)["hidden_by_baseline"])

        replacement = make_pdf(self.root / "manual.pdf", 103, 214)
        self.pitstop.reports.append(report((TRANSPARENCY, 40)))
        order, pitstop, revision, _sha = self.workflow.recheck_pdf_revision(run, order, replacement)
        self.assertNotIn("baseline_status", pitstop)
        self.assertEqual(self.issue(pitstop, 3004)["severity"], "error")
        self.assertEqual(order["pitstop_baseline_invalid_since_revision"], revision)
        self.assertEqual(order["pitstop_baseline"]["check_id"], "original-check")

        # Исправление после ручной замены уже не с чем сравнивать.
        candidate = make_pdf(self.root / "fixed-again.pdf", 103, 214)
        self.pitstop.reports.append(report((TRANSPARENCY, 40)))
        order, pitstop, _revision, _sha = self.workflow.recheck_pdf_revision(
            self.client.app.state.coordinator.get_run(run_id), order, candidate, callas_fix=True
        )
        self.assertEqual(pitstop["baseline_status"], "missing")
        self.assertEqual(self.issue(pitstop, 3004)["severity"], "error")

    def test_changed_pdf_recheck_on_print_is_not_compared(self):
        self.original_report = report()
        run_id = self.make_run()
        pdf = Path(self.stored_order(run_id)["pdf_path"])
        make_pdf(pdf, 103, 214)  # оператор заменил PDF вручную
        self.pitstop.reports.append(report((TRANSPARENCY, 40)))
        self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.stored_order(run_id)["status"] != "accepted_for_print":
            time.sleep(0.05)
        order = self.stored_order(run_id)
        self.assertEqual(self.pitstop.calls, 1)
        self.assertNotIn("baseline_status", order["pitstop"])
        self.assertEqual(self.issue(order["pitstop"], 3004)["severity"], "error")
        self.assertNotIn("pitstop_baseline", order)

    def test_without_original_check_the_fix_result_is_unchanged(self):
        self.original_report = None
        run_id = self.make_run()
        self.assertIsNone(self.stored_order(run_id).get("pitstop"))
        self.fix_and_accept(run_id, report((TRANSPARENCY, 40)))
        order = self.api_order(run_id)
        self.assertEqual(order["pitstop"]["baseline_status"], "missing")
        self.assertEqual(order["pitstop"]["verdict"], "error")
        self.assertNotIn("verdict_raw", order["pitstop"])
        self.assertNotIn("pitstop_baseline", order)

    def test_baseline_and_adjusted_fields_survive_restart(self):
        self.original_report = report((CLIPPING, 168))
        run_id = self.make_run()
        self.fix_and_accept(run_id, report((TRANSPARENCY, 40), (CLIPPING, 1670)))
        before = self.stored_order(run_id)

        self.context.__exit__(None, None, None)
        app = control_panel.create_app(
            settings=self.client.app.state.settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: self.adapter_class(options),
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        after = self.stored_order(run_id)
        self.assertEqual(after["pitstop_baseline"], before["pitstop_baseline"])
        for key in ("verdict", "verdict_raw", "counts", "counts_raw", "baseline_status"):
            self.assertEqual(after["pitstop"][key], before["pitstop"][key], key)
        hidden = self.issue(after["pitstop"], 3004)
        self.assertEqual(
            {key: hidden[key] for key in ("hidden_by_baseline", "original_severity", "severity",
                                          "baseline_occurrences", "introduced_by_fix")},
            {"hidden_by_baseline": True, "original_severity": "error", "severity": "information",
             "baseline_occurrences": 0, "introduced_by_fix": 40},
        )
        clipping = self.issue(after["pitstop"], 2079)
        self.assertEqual((clipping["baseline_occurrences"], clipping["introduced_by_fix"]), (168, 1502))
        self.assertNotIn("hidden_by_baseline", clipping)
        self.assertEqual(after["status"], "warning")


class InMemoryBaselinePersistenceTests(unittest.TestCase):
    """apply_pdf_revision на in-memory репозитории: baseline записывается один раз."""

    def test_baseline_is_stored_once_and_survives_reload(self):
        from services.coordinator import RunCoordinator

        repository = InMemoryRunRepository()
        coordinator = RunCoordinator(repository, autostart=False)
        repository.create_run({
            "id": "run-1", "status": "completed", "stage": "completed", "progress": 100,
            "total_orders": 1, "processed_orders": 0,
            "options": {"input_path": "/tmp", "direction": "digital"},
            "orders": {"1001": {"order_id": "1001", "status": "warning", "files": [],
                                "pdf_path": "/tmp/a.pdf", "current_pdf_revision": 1}},
        })
        original = {"check_id": "original", "checked_revision": 1, "execution_status": "completed"}
        coordinator.apply_pdf_revision(
            "run-1", "1001", status="passed", pdf_path="/tmp/a.pdf", pitstop=None,
            current_pdf_revision=2, current_pdf_sha256="a", pitstop_baseline=original,
        )
        coordinator.apply_pdf_revision(
            "run-1", "1001", status="passed", pdf_path="/tmp/a.pdf", pitstop=None,
            current_pdf_revision=3, current_pdf_sha256="b",
            pitstop_baseline={"check_id": "other", "checked_revision": 2},
        )
        stored = repository.get_run("run-1")["orders"]["1001"]
        self.assertEqual(stored["pitstop_baseline"], original)
        self.assertNotIn("pitstop_baseline_invalid_since_revision", stored)
        coordinator.apply_pdf_revision(
            "run-1", "1001", status="passed", pdf_path="/tmp/a.pdf", pitstop=None,
            current_pdf_revision=4, current_pdf_sha256="c", invalidate_pitstop_baseline=True,
        )
        stored = RunCoordinator(repository, autostart=False).get_run("run-1")["orders"]["1001"]
        self.assertEqual(stored["pitstop_baseline"], original)
        self.assertEqual(stored["pitstop_baseline_invalid_since_revision"], 4)
