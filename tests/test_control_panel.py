import tempfile
import unittest
import os
import time
from pathlib import Path
from unittest.mock import patch

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

# ``control_panel`` exposes an application instance at import time.  Production
# requires this setting; the suite supplies an isolated hash before importing it.
os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from processing.models import FileCheck, OrderCheck, ParsedFilename
from server.models import FileResult, OrderAction, OrderResult
from server.settings import Settings
from services.batch_adapter import OrderArtifacts


class EmptyAdapter:
    def __init__(self, options):
        self.options = options

    def scan_and_inspect(self):
        return []

    def pending_files(self, _order):
        return []

    def decide(self, _order, _approved):
        return None

    def process_order(self, _order):
        raise AssertionError("empty adapter has no orders")


class OneOrderAdapter(EmptyAdapter):
    def __init__(self, options):
        super().__init__(options)
        self.input_dir = Path(options.input_path)
        self.source = self.input_dir / "sample-face.jpg"
        self.source.write_bytes(b"source-image")
        parsed = ParsedFilename(
            customer_id="42",
            order_id="1001",
            width_mm=90,
            height_mm=50,
            front_colors=4,
            back_colors=0,
            side="face",
        )
        self.order = OrderCheck(
            order_id="1001",
            customer_id="42",
            files=[
                FileCheck(
                    path=self.source,
                    parsed=parsed,
                    actual_width_mm=94,
                    actual_height_mm=54,
                    dpi=300,
                    dpi_x=300,
                    dpi_y=300,
                    actual_format="JPEG",
                    colorspace="CMYK",
                )
            ],
        )

    def scan_and_inspect(self):
        return [self.order]

    def process_order(self, _order):
        pdf = self.input_dir / "PDF" / "sample.pdf"
        preview = self.input_dir / "Previews" / "sample-face.png"
        pdf.parent.mkdir()
        preview.parent.mkdir()
        pdf.write_bytes(b"%PDF-test")
        preview.write_bytes(b"preview-image")
        return OrderArtifacts(pdf_path=pdf, preview_paths=[preview])


class PitStopOrderAdapter(OneOrderAdapter):
    pitstop_enabled = True

    def process_order(self, order):
        artifacts = super().process_order(order)
        report_dir = self.input_dir / "output_report" / "pitstop" / "check-api"
        report_dir.mkdir(parents=True)
        report_json = report_dir / "report.json"
        report_xml = report_dir / "report.xml"
        report_json.write_text('{"result":"ok"}', encoding="utf-8")
        report_xml.write_text("<result>ok</result>", encoding="utf-8")
        artifacts.current_pdf_revision = 1
        artifacts.current_pdf_sha256 = "a" * 64
        artifacts.pitstop = {
            "check_id": "check-api",
            "execution_status": "completed",
            "verdict": "passed",
            "checked_at": "2026-08-02T12:00:00+00:00",
            "checked_revision": 1,
            "profile": {"key": "digital", "name": "Test", "version": "1"},
            "pages": 1,
            "counts": {"errors": 0, "warnings": 0},
            "issues": [],
            "reports": {
                "json_url": str(report_json),
                "xml_url": str(report_xml),
            },
        }
        return artifacts


class ControlPanelTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=control_panel.PROJECT_DIR
        )
        self.root = Path(self.temporary_directory.name)
        settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        self.adapter_type = EmptyAdapter
        app = control_panel.create_app(
            settings=settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: self.adapter_type(options),
            initialize_schema=True,
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def login(self):
        response = self.client.post(
            "/api/auth/login", json={"password": "correct horse"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])

    def test_spa_is_public_but_api_and_files_require_authentication(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("PrePress Flow", home.text)

        for path in ("/api/config", "/api/checks", "/api/files/missing/source"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(
                response.json()["error"]["code"], "authentication_required"
            )

    def test_login_logout_and_config(self):
        wrong = self.client.post("/api/auth/login", json={"password": "wrong"})
        self.assertEqual(wrong.status_code, 401)
        self.assertNotIn("wrong", wrong.text)

        self.login()
        config = self.client.get("/api/config")
        self.assertEqual(config.status_code, 200)
        self.assertTrue(config.json()["profiles"])
        self.assertTrue(config.json()["return_reasons"])

        logout = self.client.post("/api/auth/logout")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_http_journal_does_not_store_passwords(self):
        secret = "do-not-write-this-password"
        response = self.client.post(
            "/api/auth/login", json={"password": secret}
        )
        self.assertEqual(response.status_code, 401)

        log_path = self.client.app.state.log_path
        journal = log_path.read_text(encoding="utf-8")
        self.assertIn("path=/api/auth/login", journal)
        self.assertIn("status=401", journal)
        self.assertNotIn(secret, journal)
        self.assertTrue((log_path.parent / "image-magic-fault.log").is_file())

    def test_path_outside_allowed_root_is_rejected(self):
        self.login()
        with tempfile.TemporaryDirectory() as outside:
            response = self.client.get("/api/folders", params={"path": outside})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "folder_not_allowed")

    def test_empty_run_persists_and_exports_report(self):
        self.login()
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": "digital",
                "create_pdfs": False,
                "generate_previews": False,
                "copy_failures": False,
            },
        )
        self.assertEqual(response.status_code, 202)
        run_id = response.json()["id"]
        result = self.client.app.state.coordinator.wait_for(run_id, timeout=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_orders"], 0)

        detail = self.client.get(f"/api/checks/{run_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "completed")
        self.assertEqual(
            self.client.get(f"/api/checks/{run_id}/orders").json()["items"], []
        )

        report = self.client.get(f"/runs/{run_id}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("Отчёт Image Magic", report.text)
        exported = self.client.get(f"/api/checks/{run_id}/export.json")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json()["id"], run_id)

    def test_sse_replays_persisted_events(self):
        self.login()
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": "digital",
                "create_pdfs": False,
                "generate_previews": False,
                "copy_failures": False,
            },
        )
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=2)
        with self.client.stream(
            "GET",
            f"/api/checks/{run_id}/events",
            headers={"Last-Event-ID": "0"},
        ) as stream:
            body = "".join(stream.iter_text())
        self.assertIn("event: run.started", body)
        self.assertIn("event: run.completed", body)

    def test_order_files_and_prepared_actions_round_trip_through_api(self):
        self.adapter_type = OneOrderAdapter
        self.login()
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": "digital",
                "create_pdfs": True,
                "generate_previews": True,
                "copy_failures": False,
            },
        )
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=2)

        orders = self.client.get(f"/api/checks/{run_id}/orders").json()["items"]
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order["order_id"], "1001")
        self.assertTrue(order["pdf_url"])
        file_result = order["files"][0]
        self.assertEqual(file_result["side"], "face")
        self.assertEqual(file_result["format"], "JPEG")
        self.assertEqual(
            self.client.get(file_result["source_url"]).content, b"source-image"
        )
        self.assertEqual(
            self.client.get(file_result["preview_url"]).content, b"preview-image"
        )
        self.assertEqual(self.client.get(order["pdf_url"]).content, b"%PDF-test")

        prepared = self.client.post(
            "/api/orders/prepare-print",
            json={"run_id": run_id, "order_ids": ["1001"]},
        )
        self.assertEqual(
            prepared.json()["items"], [{"order_id": "1001", "status": "prepared"}]
        )
        refreshed = self.client.get(
            f"/api/checks/{run_id}/orders"
        ).json()["items"][0]
        self.assertEqual(refreshed["action_result"]["status"], "prepared")
        self.assertEqual(refreshed["status"], "accepted_for_print")
        self.assertTrue((self.root / "Processed" / "sample-face.jpg").is_file())
        self.assertTrue((self.root / "PDF" / "Print" / "sample.pdf").is_file())
        self.assertTrue(
            (self.root / "Previews" / "Processed" / "sample-face.png").is_file()
        )
        self.assertEqual(
            self.client.get(refreshed["files"][0]["source_url"]).content,
            b"source-image",
        )
        self.assertEqual(
            self.client.get(refreshed["pdf_url"]).content,
            b"%PDF-test",
        )

        repeated = self.client.post(
            "/api/orders/prepare-print",
            json={"run_id": run_id, "order_ids": ["1001"]},
        )
        self.assertEqual(
            repeated.json()["items"],
            [{"order_id": "1001", "status": "prepared", "idempotent": True}],
        )
        self.assertTrue((self.root / "Processed" / "sample-face.jpg").is_file())
        self.assertFalse((self.root / "Troubles" / "1001").exists())

        return_without_comment = self.client.post(
            "/api/orders/prepare-reject",
            json={"run_id": run_id, "order_ids": ["1001"]},
        )
        self.assertEqual(return_without_comment.status_code, 200)
        self.assertEqual(
            return_without_comment.json()["items"],
            [
                {
                    "order_id": "1001",
                    "status": "pending",
                    "message": "Задача поставлена в очередь. Загрузка продолжается в фоне.",
                }
            ],
        )
        deadline = time.monotonic() + 2
        history = self.client.get("/api/order-history", params={"action": "reject"})
        while not history.json()["items"] and time.monotonic() < deadline:
            time.sleep(0.02)
            history = self.client.get(
                "/api/order-history", params={"action": "reject"}
            )
        self.assertEqual(history.status_code, 200)
        item = history.json()["items"][0]
        self.assertEqual(item["order_id"], "1001")
        self.assertEqual(item["action"], "reject")
        self.assertNotIn("pdf_url", item)
        self.assertEqual(
            self.client.get(item["previews"][0]["url"]).content,
            b"preview-image",
        )

    def test_pitstop_report_paths_are_replaced_with_protected_api_urls(self):
        self.adapter_type = PitStopOrderAdapter
        self.login()
        response = self.client.post(
            "/api/checks",
            json={"input_path": str(self.root), "direction": "digital"},
        )
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=2)

        order = self.client.get(
            f"/api/checks/{run_id}/orders"
        ).json()["items"][0]
        reports = order["pitstop"]["reports"]
        self.assertEqual(
            reports["json_url"], "/api/pitstop-checks/check-api/reports/json"
        )
        self.assertNotIn(str(self.root), str(reports))
        self.assertEqual(self.client.get(reports["json_url"]).json(), {"result": "ok"})
        xml = self.client.get(reports["xml_url"])
        self.assertEqual(xml.status_code, 200)
        self.assertIn("<result>ok</result>", xml.text)

    def test_order_history_filters_status_and_omits_unavailable_previews(self):
        self.adapter_type = OneOrderAdapter
        self.login()
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": "digital",
                "create_pdfs": True,
                "generate_previews": True,
                "copy_failures": False,
            },
        )
        run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=2)
        prepared = self.client.post(
            "/api/orders/prepare-print",
            json={"run_id": run_id, "order_ids": ["1001"]},
        )
        self.assertEqual(prepared.status_code, 200)

        with self.client.app.state.database.session_factory() as session:
            order = session.scalar(select(OrderResult).where(OrderResult.run_id == run_id))
            self.assertIsNotNone(order)
            session.add(OrderAction(order_result_id=order.id, action="reject", status="failed"))
            session.commit()

        default_history = self.client.get("/api/order-history")
        self.assertEqual(default_history.status_code, 200)
        self.assertEqual(default_history.json()["total"], 1)
        prepared_item = default_history.json()["items"][0]
        self.assertEqual(prepared_item["status"], "prepared")
        self.assertEqual(
            self.client.get(prepared_item["previews"][0]["url"]).content,
            b"preview-image",
        )
        self.assertNotIn("thumbnail_url", prepared_item["previews"][0])

        failed_history = self.client.get("/api/order-history", params={"status": "failed"})
        self.assertEqual(failed_history.status_code, 200)
        self.assertEqual(failed_history.json()["total"], 1)
        self.assertEqual(failed_history.json()["items"][0]["status"], "failed")
        all_history = self.client.get("/api/order-history", params={"status": "all"})
        self.assertEqual(all_history.status_code, 200)
        self.assertEqual(all_history.json()["total"], 2)
        self.assertEqual(
            {item["status"] for item in all_history.json()["items"]},
            {"prepared", "failed"},
        )

        with self.client.app.state.database.session_factory() as session:
            file = session.scalar(select(FileResult).where(FileResult.order_result_id == order.id))
            self.assertIsNotNone(file)
            file.preview_path = str(self.root / "Previews" / "missing.png")
            session.commit()
        self.assertEqual(self.client.get("/api/order-history").json()["items"][0]["previews"], [])

        with tempfile.TemporaryDirectory() as outside:
            outside_preview = Path(outside) / "preview.png"
            outside_preview.write_bytes(b"outside-preview")
            with self.client.app.state.database.session_factory() as session:
                file = session.scalar(select(FileResult).where(FileResult.order_result_id == order.id))
                self.assertIsNotNone(file)
                file.preview_path = str(outside_preview)
                session.commit()
            self.assertEqual(
                self.client.get("/api/order-history").json()["items"][0]["previews"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
