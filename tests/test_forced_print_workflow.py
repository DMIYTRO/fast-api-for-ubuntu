"""«Провести в печать» красного заказа с одним лишь низким разрешением: сборка при печати.

Сквозные сценарии через create_app (SQL-репозиторий) с настоящим BatchProcessorAdapter:
проверка строит диагностический PDF (как заказ 26252239), а подтверждённая печать
собирает итоговый PDF производственным путём, проверяет его PitStop новой ревизией и
только потом переносит в PDF/Print. Callas, PitStop и Sborka — фейки.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from functools import partial
from pathlib import Path

import pymupdf
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

import control_panel
from processing.batch_processor import BatchProcessor
from server.models import OrderAction, PdfRevision
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter
from tests.fake_callas import FakeCallasMerger
from tests.test_pitstop_fix_baseline_workflow import check_result, report

MM = 72 / 25.4
LOW = "22_NP(P)_Bezlam_150_mel_(160x160)_4-0_L25_T50_(36201-26252239)_falc1-face.pdf"
GOOD_A = "22_NP(P)_Bezlam_150_mel_(160x160)_4-0_L25_T50_(36201-26252240)_falc1-face.pdf"
GOOD_B = "22_NP(P)_Bezlam_150_mel_(160x160)_4-0_L25_T50_(36201-26252241)_falc1-face.pdf"
TWO_PAGE = "22_NP(P)_Bezlam_150_mel_(160x160)_4-0_L25_T50_(36201-26252242)_falc1.pdf"


def make_pdf(path: Path, *, dpi: float | None, pages: int = 1, size_mm: float = 164.0) -> Path:
    """Страница 164×164 мм (160 + 2×2 вылет); ``dpi`` — растр на всю страницу, None — вектор."""
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page(width=size_mm * MM, height=size_mm * MM)
        if dpi is None:
            page.draw_rect(page.rect, color=(0, 0, 0, 1), fill=(0, 0, 0, 0.2))
        else:
            side = int(size_mm / 25.4 * dpi)
            pixmap = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, side, side), False)
            pixmap.set_rect(pixmap.irect, (128,))
            page.insert_image(page.rect, pixmap=pixmap)
    document.save(path)
    document.close()
    return path


class FakePrepress:
    """Допечатные проверки callas: overprint на 4 объектах у заказа 26252239 (как в работе)."""

    overprint_profile = "Image Magic/Overprint - any object"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.blocking = 4

    def _overprint(self, order_id) -> dict:
        blocking = self.blocking if order_id == "26252239" else 0
        return {
            "status": "ok", "count": blocking, "blocking_count": blocking,
            "pages": [1] if blocking else [], "blocking_pages": [1] if blocking else [],
            "technical_count": 0, "technical_pages": [], "colorants": ["Black"],
            "samples": ["Заливка «Black» 100% (Separation), стр. 1"] if blocking else [],
            "message": "Overprint: объекты с наложением. Уберите overprint в макете." if blocking else None,
        }

    def check_overprint(self, pdf, order_id):
        self.calls.append(("overprint", str(pdf)))
        return self._overprint(order_id)

    def __call__(self, pdf, order_id, _previous=None):
        self.calls.append(("full", str(pdf)))
        return {"status": "completed", "overprint": self._overprint(order_id)}


class FakePitStop:
    """PitStop: пустой отчёт; ``fail_forced`` — сбой проверки собранного при печати PDF."""

    def __init__(self) -> None:
        self.checked: list[str] = []
        self.fail_forced = False
        self.fail_diagnostic = False

    def check_pdf(self, pdf, *, profile_id):
        pdf = Path(pdf)
        self.checked.append(str(pdf))
        forced = ".forced-build-" in str(pdf)
        if (forced and self.fail_forced) or (not forced and self.fail_diagnostic and "Failed" in str(pdf)):
            raise RuntimeError("PitStop Server не ответил (error code 2018)")
        return check_result(pdf, report(), f"job-{len(self.checked)}", profile_id)


class ForcedPrintWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        self.prepress = FakePrepress()
        self.pitstop = self.make_pitstop()
        self.merger = FakeCallasMerger()
        processor_factory = partial(
            BatchProcessor, pdf_merger=self.merger, pdf_prepress_checker=self.prepress,
            **self.processor_kwargs(),
        )
        settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        app = control_panel.create_app(
            settings=settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: BatchProcessorAdapter(
                options, processor_factory=processor_factory, pitstop_service=self.pitstop
            ),
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})
        self.workflow.reviewed_pdf_pitstop_service_factory = lambda _root, _direction: self.pitstop
        self.sent: list[object] = []
        self.workflow.prepress_sender = lambda ids, _comment: self.sent.append(ids) or {"ok": True}

    def make_pitstop(self) -> "FakePitStop":
        return FakePitStop()

    def processor_kwargs(self) -> dict:
        """Дополнительные зависимости ``BatchProcessor`` (наследники стенда)."""
        return {}

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    @property
    def workflow(self):
        return self.client.app.state.order_workflow

    def make_run(self) -> str:
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root), "direction": "digital", "create_pdfs": True,
                "generate_previews": True, "copy_failures": False,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        run = self.client.app.state.coordinator.wait_for(run_id, timeout=15)
        self.assertEqual(run["status"], "completed", run)
        return run_id

    def orders(self, run_id: str) -> dict[str, dict]:
        items = self.client.get(f"/api/checks/{run_id}/orders").json()["items"]
        return {item["order_id"]: item for item in items}

    def print_orders(self, run_id: str, *order_ids: str, confirm: bool = True) -> dict[str, dict]:
        response = self.client.post(
            "/api/orders/prepare-print",
            json={"run_id": run_id, "order_ids": list(order_ids), "confirm_failed_processing": confirm},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {item["order_id"]: item for item in response.json()["items"]}

    def test_low_dpi_order_is_built_checked_by_pitstop_and_printed(self):
        make_pdf(self.root / LOW, dpi=200)
        run_id = self.make_run()
        order = self.orders(run_id)["26252239"]
        diagnostic = Path(order["pdf_path"])
        self.assertEqual((order["status"], order["pdf_kind"]), ("error", "diagnostic"))
        self.assertEqual(diagnostic.parent.name, "Failed")
        self.assertEqual(
            [(item["kind"], item["code"]) for item in order["print_blockers"]],
            [("confirm", "forced_print_build")],
        )
        self.assertEqual(order["print_mode"], "forced")
        message = order["print_blockers"][0]["message"]
        self.assertIn("низкое разрешение: от 200 DPI при норме 250", message)
        self.assertIn("Будет собран итоговый PDF", message)
        self.assertIn("overprint", message)

        rejected = self.print_orders(run_id, "26252239", confirm=False)["26252239"]
        self.assertEqual((rejected["status"], rejected["code"]), ("rejected", "forced_print_confirmation"))
        self.assertEqual(self.sent, [])

        item = self.print_orders(run_id, "26252239")["26252239"]
        self.assertEqual(item["status"], "prepared", item)
        build = item["forced_print_build"]
        self.assertEqual((build["pdf_revision"], build["created_by_action"]), (2, "forced_print_build"))
        self.assertEqual(build["status_after_build"], "error")  # overprint остался

        order = self.orders(run_id)["26252239"]
        printed = self.root / "PDF" / "Print" / diagnostic.name
        self.assertEqual(order["status"], "accepted_for_print")
        self.assertEqual(order["pdf_path"], str(printed))
        self.assertEqual((order["pdf_kind"], order["current_pdf_revision"]), ("production", 2))
        self.assertTrue(printed.is_file())
        self.assertEqual(list(self.root.glob(f"PDF/*/{diagnostic.name}")), [printed])
        # Ошибки низкого разрешения остаются видны как история.
        self.assertTrue(any("effective DPI" in error for error in order["files"][0]["errors"]))
        self.assertTrue((self.root / "Processed" / LOW).is_file())
        # Производственный путь: полные допечатные проверки и PitStop собранного PDF.
        self.assertTrue(any(kind == "full" and ".forced-build-" in path for kind, path in self.prepress.calls))
        self.assertTrue(any(".forced-build-" in path for path in self.pitstop.checked))
        self.assertEqual(order["pitstop"]["checked_revision"], 2)
        self.assertEqual(self.sent, ["26252239"])

        with self.client.app.state.database.session_factory() as session:
            revisions = session.scalars(select(PdfRevision).order_by(PdfRevision.revision_number)).all()
            self.assertEqual(
                [(item.revision_number, item.created_by_action) for item in revisions],
                [(1, None), (2, "forced_print_build")],
            )
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "prepared")
            self.assertEqual(json.loads(action.cms_response_json)["forced_print_build"]["pdf_revision"], 2)

        journal = self.client.get(f"/api/checks/{run_id}/orders/36201:26252239/journal").json()
        codes = {entry["code"]: entry for entry in journal["entries"]}
        self.assertIn("forced_print_build", codes)
        self.assertEqual(codes["forced_print_build"]["data"]["pdf_revision"], 2)
        self.assertIn("от 200 DPI при норме 250", codes["forced_print_build"]["detail"])
        self.assertEqual(codes["pdf_revision"]["data"]["created_by_action"], "forced_print_build")

    def test_low_dpi_order_red_from_failed_diagnostic_pitstop_is_built_and_printed(self):
        self.prepress.blocking = 0
        self.pitstop.fail_diagnostic = True
        make_pdf(self.root / LOW, dpi=200)
        run_id = self.make_run()
        order = self.orders(run_id)["26252239"]
        self.assertEqual((order["status"], order["pdf_kind"]), ("error", "diagnostic"))
        # PitStop диагностического PDF не нужен: собранный PDF проверяется заново.
        self.assertEqual([item["code"] for item in order["print_blockers"]], ["forced_print_build"])

        item = self.print_orders(run_id, "26252239")["26252239"]
        self.assertEqual(item["status"], "prepared", item)
        self.assertEqual(item["forced_print_build"]["status_after_build"], "warning")
        self.assertEqual(self.orders(run_id)["26252239"]["status"], "accepted_for_print")

    def test_pitstop_failure_keeps_order_red_and_out_of_print(self):
        make_pdf(self.root / LOW, dpi=200)
        run_id = self.make_run()
        diagnostic = Path(self.orders(run_id)["26252239"]["pdf_path"])
        self.pitstop.fail_forced = True

        item = self.print_orders(run_id, "26252239")["26252239"]

        self.assertEqual((item["status"], item["code"]), ("error", "forced_print_build_failed"), item)
        self.assertIn("PitStop не проверил собранный итоговый PDF", item["message"])
        self.assertIn("2018", item["message"])
        order = self.orders(run_id)["26252239"]
        self.assertEqual((order["status"], order["pdf_kind"], order["current_pdf_revision"]), ("error", "diagnostic", 1))
        self.assertTrue(diagnostic.is_file())
        self.assertFalse((self.root / "PDF" / "Print").exists() and any((self.root / "PDF" / "Print").iterdir()))
        self.assertTrue((self.root / LOW).is_file())
        self.assertEqual(self.sent, [])
        with self.client.app.state.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "failed")
            self.assertEqual(json.loads(action.cms_response_json)["forced_print_build"]["status"], "failed")
            self.assertEqual(len(session.scalars(select(PdfRevision)).all()), 1)
        # Оператор может повторить: заказ всё ещё готов к сборке при печати.
        self.pitstop.fail_forced = False
        self.assertEqual(self.print_orders(run_id, "26252239")["26252239"]["status"], "prepared")

    def test_page_rule_violation_stays_blocked(self):
        make_pdf(self.root / TWO_PAGE, dpi=200, pages=2)
        run_id = self.make_run()
        order = self.orders(run_id)["26252242"]
        self.assertEqual(order["status"], "error")
        self.assertEqual(order["print_mode"], "blocked")
        self.assertIn("no_final_pdf", [item["code"] for item in order["print_blockers"]])

        item = self.print_orders(run_id, "26252242")["26252242"]

        self.assertEqual(item["status"], "rejected")
        self.assertFalse(any(".forced-build-" in path for path in self.pitstop.checked))
        self.assertEqual(self.orders(run_id)["26252242"]["status"], "error")

    def test_order_still_processed_by_active_check_is_not_touched(self):
        make_pdf(self.root / LOW, dpi=200)
        run_id = self.make_run()
        self.client.app.state.coordinator.order_in_progress = lambda *_args: True

        item = self.print_orders(run_id, "26252239")["26252239"]

        self.assertEqual((item["status"], item["code"]), ("rejected", "order_in_progress"))
        self.assertEqual(self.merger.calls[-1][1], "26252239")  # только сборка диагностического PDF
        self.assertEqual(len(self.merger.calls), 1)

    def test_bulk_print_builds_forced_order_and_keeps_others_independent(self):
        make_pdf(self.root / LOW, dpi=200)
        make_pdf(self.root / GOOD_A, dpi=None)
        make_pdf(self.root / GOOD_B, dpi=None)
        run_id = self.make_run()
        statuses = {key: value["status"] for key, value in self.orders(run_id).items()}
        self.assertEqual(statuses["26252240"], "passed", statuses)
        self.pitstop.fail_forced = True

        items = self.print_orders(run_id, "26252240", "26252239", "26252241")

        self.assertEqual(items["26252240"]["status"], "prepared", items)
        self.assertEqual(items["26252241"]["status"], "prepared")
        self.assertEqual(items["26252239"].get("code"), "forced_print_build_failed", items["26252239"])
        # Пакетный запрос Sborka — только заказы, которые точно уйдут в печать.
        self.assertEqual(self.sent, [["26252240", "26252241"]])
        self.assertEqual(self.orders(run_id)["26252239"]["status"], "error")

        self.pitstop.fail_forced = False
        self.assertEqual(self.print_orders(run_id, "26252239")["26252239"]["status"], "prepared")
        self.assertEqual(self.sent[-1], "26252239")


if __name__ == "__main__":
    unittest.main()
