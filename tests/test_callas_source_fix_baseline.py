"""PitStop заказа со стороной из принятого исправления Callas исходника.

Собранный PDF уже содержит обтравку и прозрачность, добавленные callas, а проверки
исходного собранного PDF нет. Поэтому PitStop запускается ещё и на исходных PDF
клиента, и находки сравниваются с их суммой (``services/pitstop/baseline.py``).
Без сети: фейковые PitStop и callas, настоящий BatchProcessor.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from services.pitstop import PitStopExecutionStatus
from services.pitstop.baseline import FixBaselinePolicy
from services.print_readiness import pitstop_ready
from tests.test_callas_source_fix import BACK, FACE, ORDER, SourceFixCase
from tests.test_pitstop_fix_baseline_workflow import CLIPPING, TRANSPARENCY, check_result, report


class FakeSourcePitStop:
    """Отчёт по пути PDF: исходники — из ``by_path``, собранный PDF — ``built``."""

    def __init__(self) -> None:
        self.built = report()
        self.by_path: dict[Path, object] = {}
        self.fail_for: set[Path] = set()
        self.timeout_for: set[Path] = set()
        self.calls: list[Path] = []

    def check_pdf(self, pdf, *, profile_id):
        path = Path(pdf).resolve()
        self.calls.append(path)
        if path in self.fail_for:
            raise ConnectionError("SSH недоступен")
        if path in self.timeout_for:
            result = check_result(pdf, report(), f"timeout-{len(self.calls)}", profile_id)
            return replace(
                result,
                status=PitStopExecutionStatus.FAILED,
                report=None,
                technical_error="PitStop не ответил за отведённое время",
            )
        pitstop_report = self.by_path.get(path, self.built)
        return check_result(pdf, pitstop_report, f"check-{len(self.calls)}-{path.stem}", profile_id)


def issue(pitstop: dict, action_id: int) -> dict:
    return next(item for item in pitstop["issues"] if str(item["action_id"]) == str(action_id))


class SourceFixBaselineTests(SourceFixCase):
    policy = FixBaselinePolicy()

    def adapter_kwargs(self) -> dict:
        return {"pitstop_service": self.pitstop, "pitstop_fix_baseline": self.policy}

    def setUp(self) -> None:
        self.pitstop = FakeSourcePitStop()
        super().setUp()
        self.face_path = self.face.resolve()
        self.back_path = self.back.resolve()

    def build_with_fixed_face(self) -> dict:
        """Лицо — из принятого исправления, оборот — обычная коррекция «Применить»."""
        run_id = self.make_run()
        self.accept(run_id, FACE)
        confirmed = self.client.post(
            f"/api/checks/{run_id}/orders/{ORDER}/correction", json={"decision": "confirm"}
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.wait_done(run_id)
        order = self.order(run_id)
        self.assertTrue(order["pdf_path"], order)
        self.assertEqual(order["pdf_kind"], "production")
        self.assert_sources_untouched()
        return order

    def assert_main_check_first(self, order: dict) -> None:
        # Основная проверка — собранный PDF, первой; затем исходники клиента.
        built = self.pitstop.calls[0]
        self.assertNotIn(built, {self.face_path, self.back_path})
        self.assertEqual(order["pitstop"]["check_id"], f"check-1-{built.stem}")
        self.assertEqual(order["current_pdf_revision"], 1)

    # ------------------------------------------------------------------ tests
    def test_clipping_added_by_fix_is_not_counted_and_client_transparency_stays(self):
        """Реальный случай: 3004 90→90 (у клиента), 2079 168→1670 (добавил callas)."""
        self.pitstop.by_path[self.face_path] = report((TRANSPARENCY, 90), (CLIPPING, 168))
        self.pitstop.by_path[self.back_path] = report()
        self.pitstop.built = report((TRANSPARENCY, 90), (CLIPPING, 1670))
        order = self.build_with_fixed_face()

        self.assert_main_check_first(order)
        self.assertEqual(self.pitstop.calls[1:], [self.face_path, self.back_path])
        pitstop = order["pitstop"]
        self.assertEqual(pitstop["baseline_status"], "applied")
        transparency, clipping = issue(pitstop, 3004), issue(pitstop, 2079)
        self.assertEqual(transparency["severity"], "error")
        self.assertNotIn("introduced_by_fix", transparency)
        self.assertEqual(clipping["severity"], "warning")
        self.assertEqual((clipping["baseline_occurrences"], clipping["introduced_by_fix"]), (168, 1502))
        self.assertEqual(pitstop["verdict"], "error")
        self.assertEqual(order["pitstop_status"], "error")
        baseline = order["pitstop_baseline"]
        self.assertEqual(baseline["kind"], "source_pdfs")
        self.assertEqual([item["path"] for item in baseline["sources"]], [str(self.face_path), str(self.back_path)])

    def test_clipping_only_added_by_fix_is_hidden_and_order_becomes_ready(self):
        self.pitstop.by_path[self.face_path] = report()
        self.pitstop.by_path[self.back_path] = report()
        self.pitstop.built = report((CLIPPING, 1670))
        order = self.build_with_fixed_face()

        pitstop = order["pitstop"]
        clipping = issue(pitstop, 2079)
        self.assertTrue(clipping["hidden_by_baseline"])
        self.assertEqual(clipping["original_severity"], "warning")
        self.assertEqual((pitstop["verdict_raw"], pitstop["verdict"]), ("warning", "passed"))
        # Статус заказа и готовность к печати — по скорректированному вердикту;
        # жёлтый остаётся только из-за местных пометок (исправление, коррекция оборота).
        self.assertEqual(order["pitstop_status"], "passed")
        self.assertTrue(pitstop_ready(order))
        self.assertEqual(order["status"], "warning")
        self.assertEqual(order["pitstop"]["counts"]["warnings"], 0)

        # Сохраняется в БД: последующее исправление собранного PDF сравнивается с ним же.
        run_id = self.client.get("/api/checks").json()["items"][0]["id"]
        self.stop_app()
        self.start_app()
        stored = self.order(run_id)
        self.assertEqual(stored["pitstop_baseline"]["kind"], "source_pdfs")
        self.assertTrue(issue(stored["pitstop"], 2079)["hidden_by_baseline"])
        self.assertEqual(stored["pitstop"]["verdict"], "passed")

        from types import SimpleNamespace
        from services.order_workflow import OrderWorkflowService

        captured, compare_with = OrderWorkflowService._pitstop_baseline_for(
            SimpleNamespace(pitstop_fix_baseline=self.policy), stored, callas_fix=True
        )
        self.assertIsNone(captured)
        self.assertEqual(compare_with["kind"], "source_pdfs")

    def test_findings_of_the_unfixed_side_are_never_hidden(self):
        # Исправленное лицо чистое, а прозрачность и обтравка — у клиента на обороте.
        self.pitstop.by_path[self.face_path] = report()
        self.pitstop.by_path[self.back_path] = report((TRANSPARENCY, 30), (CLIPPING, 12))
        self.pitstop.built = report((TRANSPARENCY, 30), (CLIPPING, 1670))
        order = self.build_with_fixed_face()

        pitstop = order["pitstop"]
        transparency, clipping = issue(pitstop, 3004), issue(pitstop, 2079)
        self.assertEqual(transparency["severity"], "error")
        self.assertNotIn("hidden_by_baseline", transparency)
        self.assertNotIn("hidden_by_baseline", clipping)
        self.assertEqual(clipping["severity"], "warning")
        self.assertEqual((clipping["baseline_occurrences"], clipping["introduced_by_fix"]), (12, 1658))
        self.assertEqual(pitstop["verdict"], "error")
        self.assertEqual(order["pitstop_status"], "error")

    def test_failed_baseline_run_subtracts_nothing_and_order_completes(self):
        self.pitstop.by_path[self.back_path] = report()
        self.pitstop.fail_for.add(self.face_path)
        self.pitstop.built = report((CLIPPING, 1670))
        order = self.build_with_fixed_face()

        self.assert_main_check_first(order)
        pitstop = order["pitstop"]
        self.assertEqual(pitstop["baseline_status"], "failed")
        clipping = issue(pitstop, 2079)
        self.assertEqual(clipping["severity"], "warning")
        self.assertNotIn("hidden_by_baseline", clipping)
        self.assertEqual(pitstop["verdict"], "warning")
        self.assertEqual(order["pitstop_status"], "warning")
        self.assertEqual(order["status"], "warning")
        self.assertNotIn("pitstop_baseline", order)
        self.assertEqual(order["processing_errors"], [])

    def test_timed_out_baseline_run_subtracts_nothing(self):
        self.pitstop.by_path[self.face_path] = report()
        self.pitstop.timeout_for.add(self.back_path)
        self.pitstop.built = report((CLIPPING, 1670))
        order = self.build_with_fixed_face()

        self.assertEqual(order["pitstop"]["baseline_status"], "failed")
        self.assertNotIn("hidden_by_baseline", issue(order["pitstop"], 2079))
        self.assertEqual(order["status"], "warning")
        self.assertNotIn("pitstop_baseline", order)

    def test_order_without_accepted_source_fix_runs_pitstop_once(self):
        self.pitstop.built = report((CLIPPING, 1670))
        run_id = self.make_run()
        confirmed = self.client.post(
            f"/api/checks/{run_id}/orders/{ORDER}/correction", json={"decision": "confirm"}
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.wait_done(run_id)
        order = self.order(run_id)

        self.assertEqual(len(self.pitstop.calls), 1)
        self.assertNotIn("baseline_status", order["pitstop"])
        self.assertNotIn("pitstop_baseline", order)
        self.assertEqual(order["pitstop"]["verdict"], "warning")


class SourceFixBaselineOtherProfileTests(SourceFixCase):
    """Профиль не входит в IMAGE_MAGIC_PITSTOP_FIX_BASELINE_PROFILES: лишних запусков нет."""

    def adapter_kwargs(self) -> dict:
        return {
            "pitstop_service": self.pitstop,
            "pitstop_fix_baseline": FixBaselinePolicy(profiles=frozenset({"offset"})),
        }

    def setUp(self) -> None:
        self.pitstop = FakeSourcePitStop()
        super().setUp()

    def test_profile_outside_policy_makes_no_extra_pitstop_call(self):
        self.pitstop.built = report((CLIPPING, 1670))
        run_id = self.make_run()
        self.accept(run_id, FACE)
        self.accept(run_id, BACK)
        self.wait_done(run_id)
        order = self.order(run_id)

        self.assertTrue(order["pdf_path"])
        self.assertEqual(len(self.pitstop.calls), 1)
        self.assertNotIn("baseline_status", order["pitstop"])
        self.assertNotIn("pitstop_baseline", order)
        self.assertEqual(order["pitstop"]["verdict"], "warning")


# ------------------------------------------------------------ adapter level
def _adapter(tmp_path: Path, pitstop: FakeSourcePitStop, policy: FixBaselinePolicy | None = None):
    from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions

    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        pitstop_service=pitstop,
        pitstop_fix_baseline=policy or FixBaselinePolicy(),
    )


def _order(tmp_path: Path, *, back_name: str = BACK):
    from processing.models import FileCheck, OrderCheck
    from tests.test_callas_fix_service import make_pdf

    face = make_pdf(tmp_path / FACE, 101, 101)
    if back_name.endswith(".pdf"):
        back = make_pdf(tmp_path / back_name, 101, 101)
    else:
        back = tmp_path / back_name
        back.write_bytes(b"raster")
    fixed = make_pdf(tmp_path / "accepted.pdf", 101, 101)
    built = make_pdf(tmp_path / "built.pdf", 101, 101)
    order = OrderCheck(
        order_id="1",
        customer_id="5",
        files=[
            FileCheck(path=face, content_path=fixed, callas_source_fix={"plan": "size_correction"}),
            FileCheck(path=back),
        ],
    )
    return order, built


def _artifacts(built: Path, pitstop_report):
    from services.batch_adapter import OrderArtifacts, _pitstop_result_to_dto

    return OrderArtifacts(
        pdf_path=built,
        pitstop=_pitstop_result_to_dto(check_result(built, pitstop_report, "main"), checked_revision=1),
    )


def test_final_status_uses_the_adjusted_verdict(tmp_path):
    from services.pdf_routing import completed_status

    pitstop = FakeSourcePitStop()
    order, built = _order(tmp_path)
    result = _artifacts(built, report((TRANSPARENCY, 40)))
    _adapter(tmp_path, pitstop)._apply_source_fix_baseline(order, result)

    assert [path.name for path in pitstop.calls] == [FACE, BACK]
    assert issue(result.pitstop, 3004)["hidden_by_baseline"] is True
    assert (result.pitstop["verdict_raw"], result.pitstop["verdict"]) == ("error", "passed")
    assert completed_status(local_errors=False, local_warnings=False, pitstop=result.pitstop) == "passed"
    assert result.pitstop_baseline["kind"] == "source_pdfs"
    assert all("locations" not in item for item in result.pitstop_baseline["issues"])


def test_raster_side_gives_no_baseline_and_no_extra_call(tmp_path):
    pitstop = FakeSourcePitStop()
    order, built = _order(tmp_path, back_name="T_(97x97)_4-4_(5-1)_back.tif")
    result = _artifacts(built, report((TRANSPARENCY, 40)))
    _adapter(tmp_path, pitstop)._apply_source_fix_baseline(order, result)

    assert pitstop.calls == []
    assert result.pitstop["baseline_status"] == "missing"
    assert result.pitstop["verdict"] == "error"
    assert result.pitstop_baseline is None


def test_failed_main_check_runs_no_baseline(tmp_path):
    pitstop = FakeSourcePitStop()
    order, built = _order(tmp_path)
    result = _artifacts(built, report())
    result.pitstop = {**result.pitstop, "execution_status": "failed"}
    before = dict(result.pitstop)
    _adapter(tmp_path, pitstop)._apply_source_fix_baseline(order, result)

    assert pitstop.calls == []
    assert result.pitstop == before
