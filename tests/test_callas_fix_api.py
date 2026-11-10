"""HTTP-ручки исправления Callas: запуск, состояние, картинки, решение, выключенный режим."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from server.settings import Settings
from services.batch_adapter import OrderArtifacts
from services.callas_fix import CallasFixService
from tests.test_callas_fix_service import FakeClient, make_pdf
from tests.test_control_panel import OneOrderAdapter


class RealPdfAdapter(OneOrderAdapter):
    def process_order(self, _order):
        pdf = self.input_dir / "PDF" / "sample.pdf"
        preview = self.input_dir / "Previews" / "sample-face.png"
        pdf.parent.mkdir()
        preview.parent.mkdir()
        make_pdf(pdf, 104, 204)
        preview.write_bytes(b"preview-image")
        return OrderArtifacts(pdf_path=pdf, preview_paths=[preview])


class CallasFixApiTests(unittest.TestCase):
    enabled = True

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        fixed = make_pdf(self.root / "fixed_source.pdf", 103, 214, scale_y=1.05)
        self.client_double = FakeClient(self.root / "jobs", fixed)
        service = CallasFixService(self.client_double) if self.enabled else None
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
            adapter_factory=lambda options: RealPdfAdapter(options),
            callas_fix_service=service,
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def make_run(self) -> str:
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": "offset",
                "create_pdfs": True,
                "generate_previews": False,
                "copy_failures": False,
            },
        )
        self.assertEqual(response.status_code, 202)
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=3)
        return run_id

    def url(self, run_id: str, suffix: str = "") -> str:
        return f"/api/checks/{run_id}/orders/1001/callas/fix{suffix}"


class EnabledTests(CallasFixApiTests):
    def test_plans_lists_catalog(self):
        plans = self.client.get("/api/callas/plans?direction=offset").json()["plans"]
        self.assertEqual([plan["key"] for plan in plans], ["size_correction"])
        self.assertEqual(self.client.get("/api/callas/plans?direction=nope").json()["plans"], [])

    def test_status_is_empty_before_any_fix(self):
        run_id = self.make_run()
        self.assertEqual(self.client.get(self.url(run_id)).json(), {"comparison": None})
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertNotIn("callas_fix", order)

    def test_full_cycle_start_status_images_accept(self):
        run_id = self.make_run()
        source = self.root / "PDF" / "sample.pdf"
        original_bytes = source.read_bytes()

        started = self.client.post(self.url(run_id), json={"plan": "size_correction"})
        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["comparison"]["status"], "processing")

        # Фоновая задача TestClient завершается до возврата управления.
        comparison = self.client.get(self.url(run_id)).json()["comparison"]
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["plan"]["title"], "Коррекция размера макета")
        self.assertEqual(comparison["summary"]["original_size_mm"], [104.0, 204.0])
        self.assertEqual(comparison["summary"]["result_size_mm"], [103.0, 214.0])
        for key in ("original_url", "result_url", "overlay_url", "pdf_url"):
            response = self.client.get(comparison[key])
            self.assertEqual(response.status_code, 200, key)
        self.assertEqual(self.client.get(comparison["pdf_url"]).headers["content-type"], "application/pdf")
        self.assertTrue(self.client.get(comparison["overlay_url"]).headers["content-type"].startswith("image/"))

        # Результат виден в данных заказа после перезагрузки страницы.
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(order["callas_fix"]["comparison"]["status"], "ready")

        decision = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(decision.status_code, 200)
        self.assertEqual(decision.json()["comparison"]["status"], "accepted")
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(self.client_double.calls[0][1:3], ("Image Magic/size_correction", "fix"))

    def test_unknown_plan_and_bad_decision(self):
        run_id = self.make_run()
        unknown = self.client.post(self.url(run_id), json={"plan": "nope"})
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(unknown.json()["error"]["code"], "unknown_callas_plan")
        self.assertEqual(self.client.post(self.url(run_id, "/decision"), json={"decision": "maybe"}).status_code, 422)
        early = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(early.status_code, 409)

    def test_failed_fix_reports_error_text(self):
        from services.callas.errors import CallasValidationError

        run_id = self.make_run()
        self.client_double.error = CallasValidationError("Профиль не найден в каталоге callas.")
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        comparison = self.client.get(self.url(run_id)).json()["comparison"]
        self.assertEqual(comparison["status"], "failed")
        self.assertIn("Профиль не найден", comparison["error"])
        self.assertEqual(self.client.get(comparison.get("overlay_url") or self.url(run_id, "/files/overlay")).status_code, 404)

    def wait_for_print(self, run_id: str) -> dict:
        import time

        deadline = time.monotonic() + 10
        order: dict = {}
        while time.monotonic() < deadline:
            order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
            if order["status"] == "accepted_for_print" or (order.get("action_result") or {}).get("status") == "failed":
                break
            time.sleep(0.1)
        return order

    def test_accepted_fix_becomes_the_print_pdf(self):
        import fitz

        run_id = self.make_run()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        prepared = self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        order = self.wait_for_print(run_id)
        self.assertEqual(order["status"], "accepted_for_print", order)

        printed = self.root / "PDF" / "Print" / "sample_callas-fixed.pdf"
        self.assertTrue(printed.is_file())
        with fitz.open(printed) as document:
            self.assertAlmostEqual(document[0].rect.width / (72 / 25.4), 103, places=0)
            self.assertAlmostEqual(document[0].rect.height / (72 / 25.4), 214, places=0)
        self.assertFalse((self.root / "PDF" / "Print" / "sample.pdf").exists())
        self.assertEqual(order["current_pdf_revision"], 1)

    def test_rejected_fix_keeps_the_original_pdf_for_print(self):
        run_id = self.make_run()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        self.client.post(self.url(run_id, "/decision"), json={"decision": "reject"})
        self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        order = self.wait_for_print(run_id)
        self.assertEqual(order["status"], "accepted_for_print", order)
        self.assertTrue((self.root / "PDF" / "Print" / "sample.pdf").is_file())
        self.assertFalse((self.root / "PDF" / "Print" / "sample_callas-fixed.pdf").exists())

    def test_print_is_refused_when_source_pdf_changed_after_accept(self):
        run_id = self.make_run()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        make_pdf(self.root / "PDF" / "sample.pdf", 99, 210)  # исходный PDF пересобран
        self.client.post("/api/orders/prepare-print", json={"run_id": run_id, "order_ids": ["1001"]})
        import time

        time.sleep(1.0)
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertNotEqual(order["status"], "accepted_for_print")
        self.assertFalse((self.root / "PDF" / "Print" / "sample.pdf").exists())

    def test_requires_authentication_and_rejects_unknown_file_kind(self):
        run_id = self.make_run()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        self.assertEqual(self.client.get(self.url(run_id, "/files/state.json")).status_code, 404)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get(self.url(run_id)).status_code, 401)
        self.assertEqual(self.client.get("/api/callas/plans").status_code, 401)


class DisabledTests(CallasFixApiTests):
    enabled = False

    def test_endpoints_answer_404_when_callas_fix_is_not_configured(self):
        run_id = self.make_run()
        self.assertEqual(self.client.get("/api/callas/plans").status_code, 404)
        self.assertEqual(self.client.get(self.url(run_id)).json()["error"]["code"], "callas_fix_disabled")
        self.assertEqual(self.client.post(self.url(run_id), json={"plan": "size_correction"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
