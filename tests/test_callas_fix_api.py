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
from dataclasses import replace
from services.callas_fix import CallasFixService
from tests.test_callas_fix_service import FakeClient, make_pdf
from tests.test_control_panel import OneOrderAdapter


class RealPdfAdapter(OneOrderAdapter):
    def process_order(self, _order):
        self.order.files[0].parsed = replace(self.order.files[0].parsed, width_mm=99, height_mm=210)
        pdf = self.input_dir / "PDF" / "sample.pdf"
        preview = self.input_dir / "Previews" / "sample-face.png"
        pdf.parent.mkdir()
        preview.parent.mkdir()
        make_pdf(pdf, 104, 204)
        preview.write_bytes(b"preview-image")
        return OrderArtifacts(pdf_path=pdf, preview_paths=[preview])


class SourcePdfAdapter(OneOrderAdapter):
    """Заказ с ошибкой: исходник — PDF, итоговый PDF не собран."""

    def __init__(self, options):
        super().__init__(options)
        self.source.unlink()
        self.source = self.input_dir / "sample-face.pdf"
        make_pdf(self.source, 104, 204)
        self.order.files[0].path = self.source
        self.order.files[0].actual_format = "PDF"

    def process_order(self, _order):
        return OrderArtifacts(pdf_path=None, preview_paths=[], errors=["Размер не совпадает"])


class CallasFixApiTests(unittest.TestCase):
    enabled = True
    adapter_class = RealPdfAdapter

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
            adapter_factory=lambda options: self.adapter_class(options),
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
        updated = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(updated["status"], "passed")
        self.assertFalse(source.exists())
        self.assertNotEqual(Path(updated["pdf_path"]).read_bytes(), original_bytes)
        self.assertTrue(updated["preview_paths"])
        self.assertEqual(self.client_double.calls[0][1:3], ("Image Magic/size_correction", "fix"))
        actions = self.client.get(f"/api/checks/{run_id}/orders/1001/actions").json()["items"]
        self.assertEqual([item["kind"] for item in actions if "callas_fix" in item["kind"]], [
            "order.callas_fix.started", "order.callas_fix.finished", "order.callas_fix.accepted",
        ])

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

        # Исправленный PDF занимает имя производственного и лежит только в Print.
        printed = self.root / "PDF" / "Print" / "sample.pdf"
        self.assertTrue(printed.is_file())
        with fitz.open(printed) as document:
            self.assertAlmostEqual(document[0].rect.width / (72 / 25.4), 103, places=0)
            self.assertAlmostEqual(document[0].rect.height / (72 / 25.4), 214, places=0)
        self.assertFalse((self.root / "PDF" / "sample.pdf").exists())
        self.assertEqual(list(self.root.glob("PDF/**/*_callas-fixed.pdf")), [])
        self.assertEqual(order["pdf_path"], str(printed))
        self.assertEqual(order["current_pdf_revision"], 1)

    def test_accept_rechecks_dimensions_and_repeated_correction_changes_folder(self):
        run_id = self.make_run()
        make_pdf(self.client_double.fixed_source, 80, 80)
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        accepted = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(order["status"], "warning")
        self.assertEqual(Path(order["pdf_path"]).parent.name, "Rework")
        self.assertTrue(order["revision_check"]["errors"])
        self.assertEqual(order["ink_coverage"]["flags"], [])
        self.assertFalse((self.root / "PDF" / "Print" / "sample.pdf").exists())
        make_pdf(self.client_double.fixed_source, 103, 214)
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        accepted = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(order["status"], "passed")
        self.assertEqual(order["current_pdf_revision"], 2)
        self.assertEqual(order["revision_check"]["errors"], [])
        self.assertEqual(Path(order["pdf_path"]).parent.name, "Print")
        self.assertFalse((self.root / "PDF" / "Rework" / "sample.pdf").exists())
        self.assertTrue(all("r0002" in value for value in order["preview_paths"]))

    def test_failed_correction_save_keeps_previous_checked_revision(self):
        from unittest.mock import patch
        from services import pdf_routing
        run_id = self.make_run()
        original = (self.root / "PDF" / "sample.pdf").read_bytes()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        real_replace = pdf_routing.os.replace
        def fail_publish(source, destination):
            if Path(destination).name == "sample.pdf":
                raise OSError("disk full")
            return real_replace(source, destination)
        with patch("services.pdf_routing.os.replace", side_effect=fail_publish):
            accepted = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(accepted.status_code, 409, accepted.text)
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(order["status"], "passed")
        self.assertEqual(Path(order["pdf_path"]).read_bytes(), original)
        self.assertFalse((self.root / "PDF" / "Print" / "sample.pdf").exists())
        self.assertFalse(list(self.root.glob("PDF/**/.*.tmp.pdf")))
        retry = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(retry.status_code, 200, retry.text)

    def test_failed_revision_metadata_save_restores_previous_pdf(self):
        from unittest.mock import patch
        run_id = self.make_run()
        original = (self.root / "PDF" / "sample.pdf").read_bytes()
        self.client.post(self.url(run_id), json={"plan": "size_correction"})
        repository = self.client.app.state.coordinator.repository
        save = repository.save_run_with_event
        def fail_revision(run, event_type, data):
            if event_type == "order.pdf_revision":
                raise OSError("database unavailable")
            return save(run, event_type, data)
        with patch.object(repository, "save_run_with_event", side_effect=fail_revision):
            accepted = self.client.post(self.url(run_id, "/decision"), json={"decision": "accept"})
        self.assertEqual(accepted.status_code, 409, accepted.text)
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(order["status"], "passed")
        self.assertEqual(Path(order["pdf_path"]).read_bytes(), original)
        self.assertFalse((self.root / "PDF" / "Print" / "sample.pdf").exists())

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
        make_pdf(self.root / "PDF" / "Print" / "sample.pdf", 99, 210)  # текущий PDF изменён
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


class SourcePdfTests(CallasFixApiTests):
    adapter_class = SourcePdfAdapter

    def source_url(self, run_id: str, suffix: str = "") -> tuple[str, str]:
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        entry = order["files"][0]
        self.assertTrue(entry["callas_fixable"])
        file_id = entry["callas_fix_file_id"]
        return order, f"/api/checks/{run_id}/orders/1001/callas/fix/source/{file_id}{suffix}"

    def test_order_without_built_pdf_exposes_fixable_source_pdf(self):
        run_id = self.make_run()
        order, url = self.source_url(run_id)
        self.assertFalse(order.get("pdf_path"))
        self.assertIsNone(order["files"][0]["callas_fix"]["comparison"])
        self.assertEqual(self.client.get(url).json(), {"comparison": None})
        # Без file_id у заказа нет собранного PDF — маршрут сборки отвечает «не готов».
        self.assertEqual(self.client.get(self.url(run_id)).status_code, 404)

    def test_source_pdf_fix_is_preview_only_and_never_touches_source(self):
        run_id = self.make_run()
        _order, url = self.source_url(run_id)
        source = self.root / "sample-face.pdf"
        original = source.read_bytes()

        started = self.client.post(url, json={"plan": "size_correction"})
        self.assertEqual(started.status_code, 202, started.text)
        comparison = self.client.get(url).json()["comparison"]
        self.assertEqual(comparison["status"], "ready")
        self.assertTrue(comparison["source_only"])
        self.assertEqual(comparison["summary"]["result_size_mm"], [103.0, 214.0])
        for key in ("original_url", "result_url", "overlay_url", "pdf_url"):
            self.assertIn("/callas/fix/source/", comparison[key])
            self.assertEqual(self.client.get(comparison[key]).status_code, 200, key)
        self.assertEqual(source.read_bytes(), original)
        # Результат лежит в PDF/CallasFix, а не рядом с исходниками.
        self.assertTrue((self.root / "PDF" / "CallasFix" / "sample-face.source" / "fixed.pdf").is_file())

        # Состояние видно после перезагрузки страницы.
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertEqual(order["files"][0]["callas_fix"]["comparison"]["status"], "ready")

        refused = self.client.post(url + "/decision", json={"decision": "accept"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "callas_fix_source_only")

    def test_unknown_file_and_raster_file_are_rejected(self):
        run_id = self.make_run()
        base = f"/api/checks/{run_id}/orders/1001/callas/fix/source/"
        self.assertEqual(self.client.get(base + "nope").status_code, 404)
        self.assertEqual(self.client.post(base + "nope", json={"plan": "size_correction"}).status_code, 404)


class RasterOrderTests(CallasFixApiTests):
    adapter_class = OneOrderAdapter

    def test_raster_source_is_not_offered(self):
        run_id = self.make_run()
        order = self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]
        self.assertNotIn("callas_fixable", order["files"][0])


class DisabledTests(CallasFixApiTests):
    enabled = False

    def test_endpoints_answer_404_when_callas_fix_is_not_configured(self):
        run_id = self.make_run()
        self.assertEqual(self.client.get("/api/callas/plans").status_code, 404)
        self.assertEqual(self.client.get(self.url(run_id)).json()["error"]["code"], "callas_fix_disabled")
        self.assertEqual(self.client.post(self.url(run_id), json={"plan": "size_correction"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
