"""Сравнение «до / после» исправления Callas показывает лицо и оборот при любом плане.

Раньше картинки строились только по первой странице: удаление обтравочных масок,
шрифты в кривые, сжатие картинок показывали одно лицо.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from services.batch_adapter import OrderArtifacts
from services.callas_fix import CallasFixService, comparison_payload, read_state_file
from tests.test_callas_fix_api import CallasFixApiTests
from tests.test_callas_fix_service import FakeClient
from tests.test_control_panel import OneOrderAdapter

MM = 72 / 25.4


def make_pages(path: Path, pages: int, *, shift_mm: float = 0.0) -> Path:
    """PDF из ``pages`` страниц 94×54 мм; ``shift_mm`` сдвигает рисунок (видимое изменение)."""
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=94 * MM, height=54 * MM)
        top = (8 + index * 3 + shift_mm) * MM
        page.draw_rect(fitz.Rect(10 * MM, top, 80 * MM, top + 10 * MM), color=(0, 0, 0), fill=(0.2, 0.3, 0.6))
    document.save(path)
    document.close()
    return path


@pytest.fixture()
def duplex(tmp_path):
    pdf_dir = tmp_path / "PDF"
    pdf_dir.mkdir()
    source = make_pages(pdf_dir / "order.pdf", 2)
    fixed = make_pages(tmp_path / "fixed_source.pdf", 2, shift_mm=4)
    client = FakeClient(tmp_path / "jobs", fixed)
    return SimpleNamespace(tmp=tmp_path, source=source, client=client, service=CallasFixService(client))


def _run(service, pdf, key):
    service.start(pdf, key)
    return service.execute(pdf, key)


@pytest.mark.parametrize("plan", ["fonts_to_outlines", "remove_clipping", "downsample_images", "size_correction"])
def test_every_plan_builds_front_and_back_images(duplex, plan):
    state = _run(duplex.service, duplex.source, plan)

    assert state["status"] == "ready"
    for kind in ("original", "result", "overlay"):
        front = duplex.service.file_path(duplex.source, kind)
        back = duplex.service.file_path(duplex.source, kind, page=2)
        assert front.is_file() and back.is_file()
        assert front != back
    payload = comparison_payload(read_state_file(duplex.source), "/base")
    assert [page["page"] for page in payload["pages"]] == [1, 2]
    front, back = payload["pages"]
    assert front["overlay_url"] == payload["overlay_url"]
    assert back["overlay_url"].startswith("/base/files/overlay?v=") and back["overlay_url"].endswith("&page=2")
    assert back["changed_percent"] is not None


def test_non_size_plan_compares_at_most_two_pages(tmp_path):
    pdf_dir = tmp_path / "PDF"
    pdf_dir.mkdir()
    source = make_pages(pdf_dir / "order.pdf", 4)
    fixed = make_pages(tmp_path / "fixed_source.pdf", 4, shift_mm=2)
    service = CallasFixService(FakeClient(tmp_path / "jobs", fixed))

    state = _run(service, source, "fonts_to_outlines")

    assert [page["page"] for page in state["summary"]["pages"]] == [2]
    assert service.file_path(source, "overlay", page=3) is None


def test_single_page_pdf_has_one_page_tile(tmp_path):
    pdf_dir = tmp_path / "PDF"
    pdf_dir.mkdir()
    source = make_pages(pdf_dir / "order.pdf", 1)
    service = CallasFixService(FakeClient(tmp_path / "jobs", make_pages(tmp_path / "fixed.pdf", 1, shift_mm=3)))

    _run(service, source, "remove_clipping")

    payload = comparison_payload(read_state_file(source), "/base")
    assert [page["page"] for page in payload["pages"]] == [1]
    assert service.file_path(source, "overlay", page=2) is None


def test_old_state_without_back_images_shows_front_only():
    """Коррекция размера до этой правки: сводка оборота без картинок."""
    state = {
        "status": "ready",
        "finished_at": "2026-11-18T10:00:00Z",
        "images": {"original": "original.jpg", "result": "result.jpg", "overlay": "overlay.jpg"},
        "summary": {"pages": [{"page": 2, "changed_percent": 1.0}]},
    }
    payload = comparison_payload(state, "/base")
    assert [page["page"] for page in payload["pages"]] == [1]
    assert comparison_payload({"status": "failed"}, "/base")["pages"] == []


def test_pdf_kind_has_no_page_variant(duplex):
    _run(duplex.service, duplex.source, "fonts_to_outlines")
    assert duplex.service.file_path(duplex.source, "pdf", page=2) is None
    assert duplex.service.file_path(duplex.source, "pdf") is not None


class DuplexPdfAdapter(OneOrderAdapter):
    def process_order(self, _order):
        self.order.files[0].parsed = replace(self.order.files[0].parsed, width_mm=90, height_mm=50)
        pdf = self.input_dir / "PDF" / "sample.pdf"
        pdf.parent.mkdir()
        make_pages(pdf, 2)
        return OrderArtifacts(pdf_path=pdf, preview_paths=[])


class BackPageApiTests(CallasFixApiTests):
    adapter_class = DuplexPdfAdapter

    def setUp(self) -> None:
        super().setUp()
        self.client_double.fixed_source = make_pages(self.root / "fixed_duplex.pdf", 2, shift_mm=4)

    def test_back_page_images_are_served(self):
        run_id = self.make_run()
        self.assertEqual(self.client.post(self.url(run_id), json={"plan": "remove_clipping"}).status_code, 202)

        comparison = self.client.get(self.url(run_id)).json()["comparison"]
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual([page["page"] for page in comparison["pages"]], [1, 2])
        back = comparison["pages"][1]
        for key in ("original_url", "result_url", "overlay_url"):
            response = self.client.get(back[key])
            self.assertEqual(response.status_code, 200, key)
            self.assertTrue(response.headers["content-type"].startswith("image/"))
            self.assertNotEqual(response.content, self.client.get(comparison[key]).content, key)
        self.assertEqual(self.client.get(self.url(run_id, "/files/overlay?page=3")).status_code, 404)
        self.assertEqual(self.client.get(self.url(run_id, "/files/overlay?page=0")).status_code, 422)
