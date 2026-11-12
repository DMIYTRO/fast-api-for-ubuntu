"""«В печать» после исправления Callas: жёлтый заказ → фикс → повторная проверка → печать."""
from __future__ import annotations

import time
from pathlib import Path

import fitz

from tests.test_callas_fix_api import CallasFixApiTests
from tests.test_callas_fix_service import MM, make_pdf


def make_cmyk_pdf(path: Path, width_mm: float, height_mm: float) -> Path:
    document = fitz.open()
    page = document.new_page(width=width_mm * MM, height=height_mm * MM)
    page.draw_rect(fitz.Rect(10 * MM, 10 * MM, (width_mm - 10) * MM, 30 * MM), color=(0, 0, 0, 1), fill=(1, 0, 0, 0))
    document.save(path)
    document.close()
    return path


def cmyk_converter(source, target, label):
    make_cmyk_pdf(Path(target), 103, 214)
    return {"fixups_applied": 1}


class PrintAfterFixTests(CallasFixApiTests):
    def fix_and_accept(self, run_id):
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        response = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(response.status_code, 200, response.text)

    def order(self, run_id):
        return self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]

    def wait_print(self, run_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            order = self.order(run_id)
            if order["status"] == "accepted_for_print" or (order.get("action_result") or {}).get("status") == "failed":
                return order
            time.sleep(0.1)
        return self.order(run_id)

    def test_yellow_fix_recheck_then_print_after_cmyk_conversion(self):
        self.client.app.state.order_workflow.pdf_color_converter = cmyk_converter
        run_id = self.make_run()
        self.fix_and_accept(run_id)
        order = self.order(run_id)
        self.assertEqual(order["status"], "passed", order)
        prepared = self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        self.assertEqual(prepared.json()["items"][0]["status"], "prepared", prepared.json())
        order = self.wait_print(run_id)
        self.assertEqual(order["status"], "accepted_for_print", order)
        printed = self.root / "PDF" / "Print" / "sample.pdf"
        self.assertEqual(order["pdf_path"], str(printed))
        self.assertEqual(order["current_pdf_revision"], 1)
        self.assertEqual(order["color_conversion"]["status"], "converted")
        self.assertFalse((self.root / "PDF" / "sample.pdf").exists())
        self.assertFalse((self.root / "PDF" / "Rework" / "sample.pdf").exists())

    def test_yellow_order_refixed_prints_latest_revision_and_drops_stale_copy(self):
        run_id = self.make_run()
        make_pdf(self.client_double.fixed_source, 80, 80)
        self.fix_and_accept(run_id)
        order = self.order(run_id)
        self.assertEqual(order["status"], "warning")
        self.assertEqual(Path(order["pdf_path"]).parent.name, "Rework")
        self.assertTrue(order["print_blockers"] == [])  # жёлтый заказ можно провести в печать

        make_pdf(self.client_double.fixed_source, 103, 214)
        self.fix_and_accept(run_id)
        order = self.order(run_id)
        self.assertEqual((order["status"], order["current_pdf_revision"]), ("passed", 2))
        self.assertFalse((self.root / "PDF" / "Rework" / "sample.pdf").exists())

        prepared = self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        self.assertEqual(prepared.json()["items"][0]["status"], "prepared", prepared.text)
        order = self.wait_print(run_id)
        self.assertEqual(order["status"], "accepted_for_print", order)
        printed = self.root / "PDF" / "Print" / "sample.pdf"
        self.assertEqual(order["pdf_path"], str(printed))
        self.assertEqual(order["current_pdf_revision"], 2)
        with fitz.open(printed) as document:
            self.assertAlmostEqual(document[0].rect.height / MM, 214, places=0)
        self.assertEqual([p.name for p in self.root.glob("PDF/*/sample.pdf")], ["sample.pdf"])

    def test_missing_pdf_reports_file_blocker_and_rejects_print(self):
        run_id = self.make_run()
        pdf = Path(self.order(run_id)["pdf_path"])
        pdf.unlink()
        order = self.order(run_id)
        self.assertEqual([b["code"] for b in order["print_blockers"]], ["pdf_missing"])
        self.assertEqual(order["print_blockers"][0]["kind"], "file")
        self.assertIn(pdf.name, order["print_blockers"][0]["message"])
        prepared = self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        item = prepared.json()["items"][0]
        self.assertEqual((item["status"], item["code"]), ("error", "pdf_missing"))
        self.assertIn(pdf.name, item["message"])
        self.assertNotEqual(self.order(run_id)["status"], "accepted_for_print")

    def test_failed_fix_keeps_original_pdf_and_print_works(self):
        from services.callas.errors import CallasValidationError

        run_id = self.make_run()
        before = Path(self.order(run_id)["pdf_path"]).read_bytes()
        self.client_double.error = CallasValidationError("Профиль не найден.")
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        comparison = self.client.get(self.url(run_id)).json()["comparison"]
        self.assertEqual(comparison["status"], "failed")
        order = self.order(run_id)
        self.assertEqual(order["print_blockers"], [])
        self.assertEqual(Path(order["pdf_path"]).read_bytes(), before)
        decision = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(decision.status_code, 409)
        self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        order = self.wait_print(run_id)
        self.assertEqual(order["status"], "accepted_for_print", order)
        self.assertEqual((self.root / "PDF" / "Print" / "sample.pdf").read_bytes(), before)

    def test_fix_waiting_for_approval_is_reported_as_blocker(self):
        run_id = self.make_run()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        order = self.order(run_id)
        self.assertEqual([(b["kind"], b["code"]) for b in order["print_blockers"]], [("approval", "callas_fix_pending")])
        prepared = self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        item = prepared.json()["items"][0]
        self.assertEqual((item["status"], item["code"]), ("rejected", "callas_fix_pending"))
        self.assertIn("решения оператора", item["message"])

    def test_pending_size_correction_confirmation_is_reported_as_blocker(self):
        from services.print_readiness import print_blockers

        blockers = print_blockers({"status": "passed", "pending_confirmations": 1})
        self.assertEqual([(b["kind"], b["code"]) for b in blockers], [("approval", "correction_confirmation_pending")])
        stale = print_blockers({
            "status": "passed", "current_pdf_revision": 2,
            "pitstop": {"execution_status": "completed", "verdict": "passed", "checked_revision": 1},
        })
        self.assertEqual([(b["kind"], b["code"]) for b in stale], [("check", "pitstop_stale")])
