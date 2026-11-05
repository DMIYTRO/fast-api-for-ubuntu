"""MIME and cache lookup of /api/files/{id}/preview for JPEG and historical PNG previews."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from core.preview_cache import full_preview_cache_path
from server.settings import Settings
from services.batch_adapter import OrderArtifacts
from tests.test_control_panel import OneOrderAdapter


class JpegPreviewAdapter(OneOrderAdapter):
    preview_name = "sample-face.jpg"

    def process_order(self, _order):
        pdf = self.input_dir / "PDF" / "sample.pdf"
        preview = self.input_dir / "Previews" / self.preview_name
        pdf.parent.mkdir()
        preview.parent.mkdir()
        pdf.write_bytes(b"%PDF-test")
        preview.write_bytes(b"preview-image")
        return OrderArtifacts(pdf_path=pdf, preview_paths=[preview])


class PngPreviewAdapter(JpegPreviewAdapter):
    preview_name = "sample-face.png"


class PreviewEndpointFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        self.adapter_type = JpegPreviewAdapter
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
            adapter_factory=lambda options: self.adapter_type(options),
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def _file(self) -> dict:
        run_id = self.client.post(
            "/api/checks",
            json={"input_path": str(self.root), "direction": "digital",
                  "create_pdfs": True, "generate_previews": True,
                  "copy_failures": False},
        ).json()["id"]
        self.client.app.state.coordinator.wait_for(run_id, timeout=2)
        return self.client.get(f"/api/checks/{run_id}/orders").json()["items"][0]["files"][0]

    def _check(self, preview_name: str, mime: str, cache_suffix: str) -> None:
        file_result = self._file()
        with patch.dict(os.environ, {
            "IMAGE_MAGIC_INPUT_DIR": str(self.root),
            "IMAGE_MAGIC_PREVIEW_CACHE_DIR": str(self.root / ".preview-cache"),
        }), patch("core.preview_cache._is_external_mount", return_value=True):
            rendition = full_preview_cache_path(self.root / "Previews" / preview_name)
            self.assertEqual(rendition.suffix, cache_suffix)
            rendition.parent.mkdir(parents=True, exist_ok=True)
            rendition.write_bytes(b"full-resolution")
            thumb = self.client.get(file_result["thumbnail_url"])
            full = self.client.get(file_result["full_preview_url"])
        self.assertEqual(thumb.content, b"preview-image")
        self.assertEqual(thumb.headers["content-type"], mime)
        self.assertEqual(full.content, b"full-resolution")
        self.assertEqual(full.headers["content-type"], mime)

    def test_jpeg_thumbnail_and_full_are_served_as_image_jpeg(self) -> None:
        self.adapter_type = JpegPreviewAdapter
        self._check("sample-face.jpg", "image/jpeg", ".jpg")

    def test_historical_png_is_still_served_as_image_png(self) -> None:
        self.adapter_type = PngPreviewAdapter
        self._check("sample-face.png", "image/png", ".png")


class ReworkHistoryCollageLookupTests(unittest.TestCase):
    def test_finds_jpeg_collage_and_historical_png_collage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            returns = root / "Previews" / "Return"
            returns.mkdir(parents=True)
            self.assertIsNone(control_panel._safe_rework_history_preview_path(str(root), "9"))
            png = returns / "9_return-preview.png"
            png.write_bytes(b"png")
            self.assertEqual(
                control_panel._safe_rework_history_preview_path(str(root), "9"), png.resolve()
            )
            jpg = returns / "9_return-preview.jpg"
            jpg.write_bytes(b"jpg")
            os.utime(png, ns=(1_000_000_000, 1_000_000_000))
            self.assertEqual(
                control_panel._safe_rework_history_preview_path(str(root), "9"), jpg.resolve()
            )
