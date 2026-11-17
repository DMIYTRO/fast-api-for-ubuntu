"""«Размер» Callas для растрового исходника и вердикт «только вылеты» через HTTP.

Растр (TIFF/JPEG/PNG) заказа, ждущего решения по размеру, собирается в
``PDF/CallasFix/<имя>.source/source.pdf`` тем же ImageMagick, что и при сборке заказа
(то же DPI, без пересэмплирования и перевода цвета); callas дорисовывает вылеты на
этой копии, оператор принимает результат, и сторона собирается из принятого PDF.
Без сети: фейковые callas (исправление, склейка, рендер), настоящий BatchProcessor.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from core.pdf_inspector import inspect_pdf
from processing.batch_processor import BatchProcessor, build_raster_source_pdf
from server.settings import Settings
from services.batch_adapter import BatchProcessorAdapter
from services.callas_fix import CallasFixService, fix_dir_for
from services.callas_source_fix import fixable_source_path
from tests.fake_callas import FakeCallasMerger
from tests.test_callas_bleed import BleedClient, art_pdf
from tests.test_callas_source_fix import page_sizes_mm
from tests.test_pdf_resize import _fake_resizer

ORDER = "5:1"
WAITING = {"waiting_confirmation", "completed", "failed", "cancelled"}
RASTER = "T_(97x97)_4-0_(5-1)_face.tif"
PDF_FACE = "T_(97x97)_4-4_(5-1)_face.pdf"
PDF_BACK = "T_(97x97)_4-4_(5-1)_back.pdf"


def make_tiff(path: Path, width_mm: float, height_mm: float, dpi: int = 300) -> Path:
    width, height = round(width_mm / 25.4 * dpi), round(height_mm / 25.4 * dpi)
    subprocess.run(
        [shutil.which("magick") or "magick", "-size", f"{width}x{height}", "gradient:cyan-yellow",
         "-colorspace", "CMYK", "-units", "PixelsPerInch", "-density", str(dpi), str(path)],
        check=True, capture_output=True,
    )
    return path


class HttpSourceFixCase(unittest.TestCase):
    """Приложение с фейковым callas, который только дорисовывает вылеты (``BleedClient``)."""

    scale = 1.0

    def make_sources(self) -> list[Path]:
        raise NotImplementedError

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        self.sources = self.make_sources()
        self.originals = {path: path.read_bytes() for path in self.sources}
        self.fix_client = BleedClient(self.root.parent / f"{self.root.name}-jobs", scale=self.scale)
        self.merger = FakeCallasMerger()
        self.resize_calls: list = []
        self.settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        # Пока control_panel сам не делегирует выбор исходника, растры включаются здесь.
        patcher = mock.patch.object(control_panel, "_source_pdf_path", fixable_source_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = None
        self.file_ids: dict[str, str] = {}
        self.start_app()

    def start_app(self) -> None:
        def processor_factory(*args, **kwargs):
            return BatchProcessor(
                *args, pdf_merger=self.merger, pdf_resizer=_fake_resizer(self.resize_calls), **kwargs
            )

        app = control_panel.create_app(
            settings=self.settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: BatchProcessorAdapter(options, processor_factory=processor_factory),
            callas_fix_service=CallasFixService(self.fix_client),
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})

    def stop_app(self) -> None:
        if self.context is not None:
            self.context.__exit__(None, None, None)
            self.context = None

    def tearDown(self) -> None:
        self.stop_app()
        shutil.rmtree(self.fix_client.root, ignore_errors=True)
        self.temporary_directory.cleanup()

    @property
    def coordinator(self):
        return self.client.app.state.coordinator

    def make_run(self) -> str:
        response = self.client.post("/api/checks", json={
            "input_path": str(self.root), "direction": "digital",
            "create_pdfs": True, "generate_previews": False, "copy_failures": False,
        })
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        run = self.coordinator.wait_for(run_id, WAITING, timeout=20)
        self.assertEqual(run["status"], "waiting_confirmation", run)
        return run_id

    def order(self, run_id: str) -> dict:
        items = self.client.get(f"/api/checks/{run_id}/orders").json()["items"]
        self.assertEqual(len(items), 1)
        return items[0]

    def entry(self, order: dict, name: str) -> dict:
        return next(item for item in order["files"] if item["name"] == name)

    def fix_url(self, run_id: str, name: str, suffix: str = "") -> str:
        if name not in self.file_ids:
            self.file_ids[name] = self.entry(self.order(run_id), name)["callas_fix_file_id"]
        return f"/api/checks/{run_id}/orders/{ORDER}/callas/fix/source/{self.file_ids[name]}{suffix}"

    def run_fix(self, run_id: str, name: str) -> dict:
        url = self.fix_url(run_id, name)
        started = self.client.post(url, json={"plan": "size_correction"})
        self.assertEqual(started.status_code, 202, started.text)
        comparison = self.client.get(url).json()["comparison"]
        self.assertEqual(comparison["status"], "ready", comparison)
        return comparison

    def accept(self, run_id: str, name: str) -> dict:
        self.run_fix(run_id, name)
        response = self.client.post(self.fix_url(run_id, name, "/decision"), json={"decision": "accept"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["comparison"]

    def assert_sources_untouched(self) -> None:
        for path, content in self.originals.items():
            self.assertEqual(path.read_bytes(), content, path.name)


class RasterSourceFixTests(HttpSourceFixCase):
    def make_sources(self) -> list[Path]:
        self.raster = make_tiff(self.root / RASTER, 97, 97)
        return [self.raster]

    def test_raster_source_is_fixed_through_source_pdf_and_built_from_the_accepted_pdf(self):
        run_id = self.make_run()
        entry = self.entry(self.order(run_id), RASTER)
        self.assertEqual(entry["resample_decision"], "ask_confirmation")
        self.assertTrue(entry["callas_fixable"])
        self.assertTrue(entry["callas_fix_file_id"])

        comparison = self.run_fix(run_id, RASTER)
        source_pdf = fix_dir_for(self.raster) / "source.pdf"
        # Callas работает с PDF, собранным из растра, а не с самим растром.
        self.assertEqual(self.fix_client.inputs, [source_pdf])
        self.assertEqual(page_sizes_mm(source_pdf), [(97.0, 97.0)])
        image = inspect_pdf(source_pdf).pages[0].images[0]
        self.assertAlmostEqual(image.effective_dpi_x, 300, delta=1)  # то же DPI, без пересэмплирования
        self.assertTrue(comparison["summary"]["bleed_only"], comparison["summary"]["bleed_only_reasons"])

        self.accept(run_id, RASTER)
        run = self.coordinator.wait_for(run_id, timeout=20)
        self.assertEqual(run["status"], "completed", run)
        order = self.order(run_id)
        self.assertIn(order["status"], {"passed", "warning"}, order)
        self.assertEqual(self.merger.calls[-1][0], ["accepted.pdf"])
        self.assertEqual(page_sizes_mm(Path(order["pdf_path"])), [(101.0, 101.0)])

        face = self.entry(order, RASTER)
        self.assertEqual(face["errors"], [])  # полосы вылета 13 dpi не дают ошибку DPI
        self.assertEqual(face["resample_decision"], "accept")
        self.assertAlmostEqual(face["dpi_x"], 300, delta=1)  # содержимое — из растра клиента
        self.assertEqual(face["actual_format"], "TIFF")
        fix = face["callas_source_fix"]
        self.assertIs(fix["bleed_only"], True)
        self.assertEqual(fix["bleed_only_reasons"], [])
        self.assertEqual(fix["baseline_pdf"], str(source_pdf))
        self.assertTrue(fix["pdf_path"].endswith("accepted.pdf"))
        # Растр не пересэмплирован: сторона пришла из принятого PDF.
        self.assertFalse(any(action.get("kind") == "size_correction" for action in face["processing_actions"]))
        self.assert_sources_untouched()


def test_raster_source_pdf_matches_the_normal_build(tmp_path):
    """source.pdf — тот же ImageMagick-вызов, что и страница растра при сборке заказа."""
    raster = make_tiff(tmp_path / RASTER, 97, 97)
    with mock.patch("processing.batch_processor.convert_image_to_pdf") as convert:
        check = build_raster_source_pdf(raster, tmp_path / "out" / "source.pdf", strip_icc_cmyk=True)
    args, kwargs = convert.call_args
    assert args[0] == str(raster)
    assert kwargs == {
        "dpi": f"{check.dpi_x}x{check.dpi_y}", "compression": "none",
        "ignore_tiff_layers": False, "strip_icc": True,
    }


class PdfBleedOnlyTests(HttpSourceFixCase):
    def make_sources(self) -> list[Path]:
        self.face = art_pdf(self.root / PDF_FACE, 97, 97)
        self.back = art_pdf(self.root / PDF_BACK, 97, 97)
        return [self.face, self.back]

    def test_accepted_bleed_only_fix_keeps_its_contract_across_restart(self):
        run_id = self.make_run()
        comparison = self.accept(run_id, PDF_FACE)
        self.assertIs(comparison["summary"]["bleed_only"], True)
        self.assertEqual(comparison["summary"]["bleed_only_reasons"], [])

        face = self.entry(self.order(run_id), PDF_FACE)
        self.assertEqual(face["errors"], [])
        self.assertEqual(face["callas_source_fix"]["baseline_pdf"], str(self.face.resolve()))

        self.stop_app()
        self.start_app()
        face = self.entry(self.order(run_id), PDF_FACE)
        fix = face["callas_source_fix"]
        self.assertEqual(fix["status"], "accepted")
        self.assertIs(fix["bleed_only"], True)
        self.assertEqual(fix["bleed_only_reasons"], [])
        self.assertEqual(fix["baseline_pdf"], str(self.face.resolve()))
        self.assertIs(face["callas_fix"]["comparison"]["summary"]["bleed_only"], True)
        self.assert_sources_untouched()


class PdfScaledFixTests(HttpSourceFixCase):
    scale = 0.96

    def make_sources(self) -> list[Path]:
        self.face = art_pdf(self.root / PDF_FACE, 97, 97)
        self.back = art_pdf(self.root / PDF_BACK, 97, 97)
        return [self.face, self.back]

    def test_fix_that_scaled_the_artwork_keeps_old_checks_and_warns(self):
        run_id = self.make_run()
        comparison = self.accept(run_id, PDF_FACE)
        self.assertIs(comparison["summary"]["bleed_only"], False)

        face = self.entry(self.order(run_id), PDF_FACE)
        fix = face["callas_source_fix"]
        self.assertIs(fix["bleed_only"], False)
        self.assertTrue(fix["bleed_only_reasons"])
        # Всё по исправленному PDF: полосы вылета 12.7 dpi снова ошибка, с пояснением.
        self.assertTrue(any("effective DPI 12.7" in message for message in face["errors"]), face["errors"])
        self.assertTrue(any(
            message.startswith("Исправление Callas изменило не только поля вылета")
            for message in face["warnings"]
        ), face["warnings"])


if __name__ == "__main__":
    unittest.main()
