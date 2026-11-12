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
