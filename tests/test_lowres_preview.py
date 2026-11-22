"""«Показать разрешение»: сервис разметки ppi, HTTP-ручки и журнал (callas подменён, без сети)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password"))

import control_panel  # noqa: E402  (pytest only: conftest isolates the database)
from server.settings import Settings  # noqa: E402
from services.batch_adapter import OrderArtifacts  # noqa: E402
from services.callas.errors import CallasJobFailedError  # noqa: E402
from services.lowres_preview import (  # noqa: E402
    LEGEND,
    NO_IMAGES_MESSAGE,
    PROFILE,
    LowResPreviewError,
    LowResPreviewService,
    output_paths,
)
from services.order_journal import build_order_journal  # noqa: E402
from services.return_preview import custom_return_preview_path  # noqa: E402
from tests.test_control_panel import OneOrderAdapter  # noqa: E402

MM = 72 / 25.4
LONG_SIDE = 200


def make_pdf(path: Path, width_mm: float, height_mm: float, pages: int = 1) -> Path:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=width_mm * MM, height=height_mm * MM)
        page.draw_rect(fitz.Rect(5 * MM, 5 * MM, 20 * MM, 20 * MM), color=(0, 0, 0), fill=(0.2 * index, 0.4, 0.7))
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    document.close()
    return path


class FakeCallas:
    """process: копия входа как «размеченный» PDF; render: PNG по размеру страниц и DPI."""

    def __init__(self, root: Path, *, applied: int = 2, error: Exception | None = None) -> None:
        self.root = root
        self.applied = applied
        self.error = error
        self.render_error: Exception | None = None
        self.calls: list[tuple] = []
        self.process_inputs: list[tuple[Path, str | None]] = []

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def delete(self, job_id: str) -> None:
        self.calls.append(("delete", job_id))

    def process(self, pdf, profile, mode, *, job_id=None, report=("xml",), **kwargs):
        self.calls.append(("process", Path(pdf).name, profile, mode))
        self.process_inputs.append((Path(pdf), kwargs.get("source_sha256")))
        if self.error is not None:
            raise self.error
        out = self.job_dir(job_id) / "out"
        out.mkdir(parents=True)
        shutil.copyfile(pdf, out / "fixed.pdf")
        return SimpleNamespace(
            job_id=job_id, mode=mode, status="errors", exit_code=8,
            summary={"errors": 0, "warnings": 0, "infos": 0, "fixups_applied": self.applied, "fixups_failed": 0},
            output_pdf=out / "fixed.pdf", report_xml=None,
        )

    def render(self, pdf, dpi, *, job_id, expected_pages=None, **_kwargs):
        self.calls.append(("render", Path(pdf).name, dpi, expected_pages))
        if self.render_error is not None:
            raise self.render_error
        out = self.job_dir(job_id) / "out"
        out.mkdir(parents=True)
        pages = []
        with fitz.open(pdf) as document:
            for number, page in enumerate(document, start=1):
                size = (round(page.rect.width * dpi / 72), round(page.rect.height * dpi / 72))
                target = out / f"page-{number}.png"
                Image.new("RGB", size, (200, 40 * number, 40)).save(target)
                pages.append(target)
        return pages

    def count(self, kind: str) -> int:
        return sum(1 for call in self.calls if call[0] == kind)


def service_for(client: FakeCallas) -> LowResPreviewService:
    return LowResPreviewService(client, page_long_side_px=LONG_SIDE, font=None)


@pytest.fixture()
def env(tmp_path):
    client = FakeCallas(tmp_path / "jobs")
    return SimpleNamespace(root=tmp_path / "input", client=client, service=service_for(client))


def files_under(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


def test_duplex_order_gets_one_side_by_side_image(env):
    pdf = make_pdf(env.root / "PDF" / "Checked" / "order.pdf", 90, 50, pages=2)
    before = pdf.read_bytes()
    result = env.service.generate(pdf, input_root=env.root, storage_id="1001")

    assert result["status"] == "ready" and result["cached"] is False
    assert result["pages"] == 2 and result["marked_images"] == 2
    assert result["images"] is None  # отчёт профиля не содержит ppi отдельных изображений
    assert result["threshold_ppi"] == 250 and result["pitstop_threshold_ppi"] == 275
    assert result["legend"] == LEGEND and result["profile"] == PROFILE
    assert ("process", "order.pdf", PROFILE, "fix") in env.client.calls
    paths = output_paths(env.root, "1001")
    with Image.open(paths["image"]) as image:
        assert image.format == "JPEG"
        # Две страницы 90×50 по ~200 px в длину рядом, с белым промежутком.
        assert image.width > 2 * image.height
        assert image.width >= 2 * LONG_SIDE
    assert pdf.read_bytes() == before
    # Результат только в Previews/LowRes: ни одного нового PDF в папках статуса.
    assert files_under(env.root) == {
        "PDF/Checked/order.pdf",
        "Previews/LowRes/1001_lowres.jpg",
        "Previews/LowRes/1001_lowres.pdf",
        "Previews/LowRes/1001_lowres.json",
    }
    assert not (env.client.root).exists() or not any(env.client.root.iterdir())


def test_single_page_order_gets_page_as_is(env):
    pdf = make_pdf(env.root / "PDF" / "order.pdf", 80, 45)
    result = env.service.generate(pdf, input_root=env.root, storage_id="42:1001")
    assert result["pages"] == 1
    with Image.open(output_paths(env.root, "42:1001")["image"]) as image:
        assert abs(image.width - LONG_SIDE) <= 2
        assert image.width > image.height


def test_repeat_with_unchanged_pdf_uses_cache(env):
    pdf = make_pdf(env.root / "PDF" / "order.pdf", 90, 50)
    env.service.generate(pdf, input_root=env.root, storage_id="1001")
    again = env.service.generate(pdf, input_root=env.root, storage_id="1001")
    assert again["cached"] is True
    assert env.client.count("process") == 1 and env.client.count("render") == 1
    # Новый PDF (например, принятое исправление) строится заново.
    make_pdf(pdf, 90, 50, pages=2)
    fresh = env.service.generate(pdf, input_root=env.root, storage_id="1001")
    assert fresh["cached"] is False and fresh["pages"] == 2
    assert env.client.count("process") == 2


def test_pdf_without_images_is_reported(env):
    env.client.applied = 0
    pdf = make_pdf(env.root / "PDF" / "order.pdf", 90, 50)
    with pytest.raises(LowResPreviewError) as error:
        env.service.generate(pdf, input_root=env.root, storage_id="1001")
    assert str(error.value) == NO_IMAGES_MESSAGE
    assert error.value.code == "lowres_no_images"
    assert not output_paths(env.root, "1001")["image"].exists()
    assert env.client.count("render") == 0


def test_callas_failure_becomes_operator_error(env):
    env.client.error = CallasJobFailedError("Callas завершил проверку с ошибкой выполнения (код 103).")
    pdf = make_pdf(env.root / "PDF" / "order.pdf", 90, 50)
    with pytest.raises(LowResPreviewError) as error:
        env.service.generate(pdf, input_root=env.root, storage_id="1001")
    assert error.value.status_code == 502
    assert "код 103" in str(error.value)
    assert not output_paths(env.root, "1001")["meta"].exists()
    # Временная папка на общем диске убрана.
    assert [path.name for path in (env.root / "Previews" / "LowRes").iterdir()] == []


def test_render_failure_keeps_previous_result(env):
    pdf = make_pdf(env.root / "PDF" / "order.pdf", 90, 50)
    env.service.generate(pdf, input_root=env.root, storage_id="1001")
    old = output_paths(env.root, "1001")["image"].read_bytes()
    make_pdf(pdf, 90, 50, pages=2)
    env.client.render_error = CallasJobFailedError("render down")
    with pytest.raises(LowResPreviewError) as error:
        env.service.generate(pdf, input_root=env.root, storage_id="1001")
    assert error.value.code == "lowres_render_failed"
    assert output_paths(env.root, "1001")["image"].read_bytes() == old


def test_storage_id_must_not_be_a_path(env):
    pdf = make_pdf(env.root / "PDF" / "order.pdf", 90, 50)
    with pytest.raises(ValueError):
        env.service.generate(pdf, input_root=env.root, storage_id="../1001")


def test_journal_entries_for_generated_and_attached():
    order = {"aggregate_id": "42:1001", "order_id": "1001", "customer_id": "42", "status": "warning", "files": []}
    events = [
        {"id": 5, "type": "order.lowres_preview.generated", "created_at": "2026-11-22T10:00:00+00:00",
         "data": {"aggregate_id": "42:1001", "order_id": "1001", "file": "/mnt/shared/in/PDF/a.pdf",
                  "pages": 2, "marked_images": 4, "threshold_ppi": 250, "pdf_kind": "production", "pdf_revision": 1}},
        {"id": 6, "type": "order.lowres_preview.attached", "created_at": "2026-11-22T10:01:00+00:00",
         "data": {"aggregate_id": "42:1001", "order_id": "1001", "file": "a.pdf",
                  "preview": "/mnt/shared/in/Previews/Custom/1001_return-preview.jpg", "marked_images": 4}},
    ]
    journal = build_order_journal(order, events)
    entries = {entry["code"]: entry for entry in journal["entries"]}
    generated = entries["operator:lowres_preview"]
    assert generated["kind"] == "operator_change" and generated["count"] == 4
    assert generated["file"] == "a.pdf" and generated["outcome"] == "done"
    assert generated["data"]["marked_images"] == 4 and generated["data"]["source_pdf_revision"] == 1
    assert "low_images" not in generated["data"]  # ppi отдельных изображений неизвестны
    attached = entries["operator:lowres_preview_attached"]
    assert attached["outcome"] == "applied"
    assert attached["data"]["preview"] == "1001_return-preview.jpg"
    assert "/mnt/shared" not in str(journal)
    assert journal["summary"]["operator_changes"] == 2


def test_journal_counts_low_images_when_known():
    order = {"aggregate_id": "1001", "order_id": "1001", "files": []}
    events = [{"id": 1, "type": "order.lowres_preview.generated", "created_at": "2026-11-22T10:00:00+00:00",
               "data": {"order_id": "1001", "marked_images": 3, "threshold_ppi": 250,
                        "images": [{"page": 1, "ppi": 118, "low": True}, {"page": 2, "ppi": 173, "low": True},
                                   {"page": 1, "ppi": 292, "low": False}]}}]
    entry = next(item for item in build_order_journal(order, events)["entries"] if item["code"] == "operator:lowres_preview")
    assert entry["data"]["low_images"] == 2 and entry["pages"] == [1, 2]


class RealPdfOrderAdapter(OneOrderAdapter):
    pages = 2

    def process_order(self, _order):
        pdf = make_pdf(self.input_dir / "PDF" / "Checked" / "sample.pdf", 90, 50, pages=self.pages)
        return OrderArtifacts(pdf_path=pdf, preview_paths=[], pdf_kind="production", current_pdf_revision=1)


class NoPdfAdapter(OneOrderAdapter):
    def process_order(self, _order):
        return OrderArtifacts(pdf_path=None, preview_paths=[], errors=["Размер не совпадает"])


class LowResPreviewApiTests(unittest.TestCase):
    adapter_class = RealPdfOrderAdapter
    enabled = True

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        self.callas = FakeCallas(self.root.parent / f".{self.root.name}-jobs")
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
            lowres_preview_service=service_for(self.callas) if self.enabled else None,
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})
        response = self.client.post("/api/checks", json={
            "input_path": str(self.root), "direction": "digital",
            "create_pdfs": True, "generate_previews": False, "copy_failures": False,
        })
        self.run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(self.run_id, timeout=3)

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        self.temporary_directory.cleanup()
        shutil.rmtree(self.callas.root, ignore_errors=True)

    def url(self, suffix: str = "") -> str:
        return f"/api/checks/{self.run_id}/orders/1001/lowres-preview{suffix}"

    def events(self, kind: str) -> list:
        return [event for event in self.client.app.state.coordinator.events(self.run_id) if event.type == kind]

    def test_generate_download_and_cache(self):
        response = self.client.post(self.url())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["pages"], 2)
        self.assertEqual(body["pdf_kind"], "production")
        self.assertEqual(body["legend"], LEGEND)
        self.assertFalse(body["cached"])
        self.assertTrue(body["image_url"].startswith(self.url("/image?v=")))
        image = self.client.get(body["image_url"])
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["content-type"], "image/jpeg")
        self.assertTrue(image.content.startswith(b"\xff\xd8\xff"))
        pdf = self.client.get(body["pdf_url"])
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers["content-type"], "application/pdf")
        self.assertIn("attachment", pdf.headers["content-disposition"])
        self.assertEqual(len(self.events("order.lowres_preview.generated")), 1)
        event = self.events("order.lowres_preview.generated")[0]
        self.assertEqual(event.data["marked_images"], 2)
        self.assertEqual(event.data["aggregate_id"], "42:1001")

        again = self.client.post(self.url()).json()
        self.assertTrue(again["cached"])
        self.assertEqual(self.callas.count("process"), 1)
        self.assertEqual(len(self.events("order.lowres_preview.generated")), 1)
        # Ничего не попало в папки статуса PDF.
        checked = {path.name for path in (self.root / "PDF" / "Checked").iterdir()}
        self.assertEqual(checked, {"sample.pdf"})
        for folder in ("Rework", "Failed", "Print"):
            self.assertFalse((self.root / "PDF" / folder).exists() and any((self.root / "PDF" / folder).iterdir()))

    def test_files_missing_before_generation(self):
        self.assertEqual(self.client.get(self.url("/image")).status_code, 404)
        self.assertEqual(self.client.get(self.url("/pdf")).status_code, 404)
        self.assertEqual(self.client.post(self.url("/attach")).status_code, 409)

    def test_attach_publishes_custom_return_preview(self):
        self.client.post(self.url())
        response = self.client.post(self.url("/attach"))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["filename"], "1001_return-preview.jpg")
        self.assertEqual(body["custom_preview_url"], f"/api/checks/{self.run_id}/orders/1001/return-preview")
        custom = custom_return_preview_path("1001", input_path=self.root)
        self.assertIsNotNone(custom)
        self.assertEqual(custom.read_bytes(), output_paths(self.root, "1001")["image"].read_bytes())
        self.assertEqual(self.client.get(body["url"]).content, custom.read_bytes())
        order = self.client.get(f"/api/checks/{self.run_id}/orders").json()["items"][0]
        self.assertEqual(order["custom_preview_url"], body["url"])
        self.assertEqual(len(self.events("order.lowres_preview.attached")), 1)

        journal = self.client.get(f"/api/checks/{self.run_id}/orders/42:1001/journal").json()
        codes = [entry["code"] for entry in journal["entries"]]
        self.assertIn("operator:lowres_preview", codes)
        self.assertIn("operator:lowres_preview_attached", codes)
        actions = self.client.get(f"/api/checks/{self.run_id}/orders/42:1001/actions").json()["items"]
        self.assertIn("order.lowres_preview.attached", [item["kind"] for item in actions])

    def test_attach_refuses_stale_image(self):
        self.client.post(self.url())
        make_pdf(self.root / "PDF" / "Checked" / "sample.pdf", 90, 50, pages=1)
        response = self.client.post(self.url("/attach"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "lowres_preview_stale")
        self.assertIsNone(custom_return_preview_path("1001", input_path=self.root))

    def test_refused_while_order_in_progress(self):
        self.client.post(self.url())
        coordinator = self.client.app.state.coordinator
        with patch.object(coordinator, "order_in_progress", return_value=True):
            for suffix in ("", "/attach"):
                response = self.client.post(self.url(suffix))
                self.assertEqual(response.status_code, 409, suffix)
                self.assertEqual(response.json()["error"]["code"], "order_in_progress")
                self.assertIn("ещё проверяется", response.json()["error"]["message"])
        self.assertIsNone(custom_return_preview_path("1001", input_path=self.root))

    def test_callas_error_is_an_actionable_message(self):
        self.callas.applied = 0
        response = self.client.post(self.url())
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["message"], NO_IMAGES_MESSAGE)


class LowResPreviewWithoutPdfTests(LowResPreviewApiTests):
    adapter_class = NoPdfAdapter

    def test_generate_download_and_cache(self):
        response = self.client.post(self.url())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "lowres_no_pdf")
        self.assertIn("нет PDF", response.json()["error"]["message"])

    test_attach_publishes_custom_return_preview = None
    test_attach_refuses_stale_image = None
    test_refused_while_order_in_progress = None
    test_callas_error_is_an_actionable_message = None
    test_files_missing_before_generation = None


class LowResPreviewDisabledTests(LowResPreviewApiTests):
    enabled = False

    def test_generate_download_and_cache(self):
        response = self.client.post(self.url())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "lowres_preview_disabled")

    test_attach_publishes_custom_return_preview = None
    test_attach_refuses_stale_image = None
    test_refused_while_order_in_progress = None
    test_callas_error_is_an_actionable_message = None
    test_files_missing_before_generation = None


def test_callas_gets_the_order_pdf_itself_not_a_fresh_share_copy(env):
    """Регрессия: свежая копия на CIFS-шаре меняла mtime после записи, и клиент callas
    отклонял результат («изменился во время обработки callas»). Вход — сам PDF заказа
    с уже посчитанным SHA-256; клиент сам копирует его в папку задания."""
    import hashlib

    pdf = make_pdf(env.root / "PDF" / "Checked" / "order.pdf", 90, 50, pages=1)
    env.service.generate(pdf, input_root=env.root, storage_id="1001")

    [(source, digest)] = env.client.process_inputs
    assert source == pdf
    assert digest == hashlib.sha256(pdf.read_bytes()).hexdigest()
