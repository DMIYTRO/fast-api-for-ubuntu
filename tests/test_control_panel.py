import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import control_panel


class ControlPanelTests(unittest.TestCase):
    client = TestClient(control_panel.app)

    def setUp(self):
        with control_panel.JOBS_LOCK:
            control_panel.JOBS.clear()

    def test_home_and_config_are_available(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Допечатная проверка заказов", home.text)

        config = self.client.get("/api/config")
        self.assertEqual(config.status_code, 200)
        self.assertTrue(config.json()["profiles"])

    def test_path_outside_allowed_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            with patch.object(
                control_panel, "ALLOWED_ROOTS", (control_panel.PROJECT_DIR.resolve(),)
            ):
                response = self.client.get("/api/folders", params={"path": outside})
        self.assertEqual(response.status_code, 403)

    def test_check_runs_and_builds_report_for_empty_folder(self):
        with tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR) as folder:
            response = self.client.post(
                "/api/checks",
                json={
                    "input_path": folder,
                    "direction": "digital",
                    "create_pdfs": False,
                    "generate_previews": False,
                    "copy_failures": False,
                },
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["id"]

            result = None
            for _ in range(100):
                result = self.client.get(f"/api/checks/{job_id}").json()
                if result["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["total_orders"], 0)
            self.assertTrue(result["report_ready"])
            report = self.client.get(f"/runs/{job_id}/report")
            self.assertEqual(report.status_code, 200)

    def test_report_rewrites_preview_urls_for_http_route(self):
        with tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR) as folder:
            input_dir = Path(folder)
            report_dir = input_dir / "output_report"
            preview_dir = input_dir / "Previews"
            report_dir.mkdir()
            preview_dir.mkdir()
            (preview_dir / "sample_preview.png").write_bytes(b"preview")
            report_path = report_dir / "report.html"
            report_path.write_text(
                '<img src="../Previews/sample_preview.png">', encoding="utf-8"
            )

            options = control_panel.CheckOptions(input_path=folder)
            job = control_panel.CheckJob(options, input_dir)
            job.report_path = report_path
            job.status = "completed"
            with control_panel.JOBS_LOCK:
                control_panel.JOBS[job.id] = job

            report = self.client.get(f"/runs/{job.id}/report")
            self.assertEqual(report.status_code, 200)
            self.assertIn(
                f'/runs/{job.id}/Previews/sample_preview.png', report.text
            )
            preview = self.client.get(
                f"/runs/{job.id}/Previews/sample_preview.png"
            )
            self.assertEqual(preview.status_code, 200)
