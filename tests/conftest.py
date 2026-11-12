"""Общие фейки: рендер PDF в тестах идёт через фейковый callas, без сети и Ghostscript."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

import pymupdf
import pytest


class FakeCallasRenderClient:
    """Имитирует CallasClient.render: PNG страниц в собственной папке задания."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="fake-callas-"))
        self.render_calls: list[tuple[Path, int]] = []

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def delete(self, job_id: str) -> None:
        pass

    def render(self, input, dpi, *, job_id, expected_pages=None, **_):
        self.render_calls.append((Path(input), int(dpi)))
        out = self.job_dir(job_id) / "out"
        out.mkdir(parents=True, exist_ok=True)
        pages = []
        with pymupdf.open(input) as document:
            for number, page in enumerate(document, start=1):
                target = out / f"page-{number:03d}.png"
                page.get_pixmap(dpi=int(dpi), alpha=False).save(target)
                pages.append(target)
        if not pages:
            raise ValueError("Callas не вернул ни одной PNG-страницы.")
        return pages


@pytest.fixture(autouse=True)
def fake_callas_pdf_render(request, monkeypatch):
    if request.node.get_closest_marker("no_fake_callas_render"):
        yield None
        return
    from processing.batch_processor import BatchProcessor
    from services.callas import renderer

    client = FakeCallasRenderClient()
    monkeypatch.setattr(BatchProcessor, "default_render_client", client)
    monkeypatch.setattr(renderer, "_configured_renderer", renderer.make_callas_pdf_renderer(client))
    yield client
    shutil.rmtree(client.root, ignore_errors=True)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_fake_callas_render: не подставлять фейковый callas-рендер PDF"
    )
