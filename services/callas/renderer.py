"""Рендер страниц PDF в PNG только через callas pdfToolbox (без Ghostscript/PyMuPDF)."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from services.callas.client import deterministic_job_id

# (pdf, dpi, target_dir) -> пути PNG по порядку страниц в target_dir.
PdfPageRenderer = Callable[[Path, int, Path], list[Path]]

from processing.batch_processor import (  # noqa: E402
    CALLAS_RENDER_UNAVAILABLE_MESSAGE,
    CallasRenderUnavailableError as CallasRenderNotConfiguredError,
)


def make_callas_pdf_renderer(client: Any) -> PdfPageRenderer:
    """Обёртка над ``CallasClient.render``: копирует страницы и чистит задание."""

    def render(pdf_path: Path, dpi: int, target_dir: Path) -> list[Path]:
        pdf_path = Path(pdf_path)
        job_id = deterministic_job_id(
            f"render-{pdf_path.stem}", str(pdf_path), str(int(dpi)), uuid.uuid4().hex[:12]
        )
        try:
            pages = client.render(pdf_path, int(dpi), job_id=job_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            copied: list[Path] = []
            for number, page in enumerate(pages, start=1):
                target = target_dir / f"page-{number:03d}.png"
                shutil.copyfile(page, target)
                copied.append(target)
            return copied
        finally:
            try:
                client.delete(job_id)
            except Exception:
                pass
            shutil.rmtree(client.job_dir(job_id), ignore_errors=True)

    return render


def unavailable_pdf_renderer(pdf_path: Path, dpi: int, target_dir: Path) -> list[Path]:
    raise CallasRenderNotConfiguredError()


_configured_renderer: PdfPageRenderer | None = None


def configure_pdf_renderer(renderer: PdfPageRenderer | None) -> None:
    """Задаёт процессный рендерер PDF (create_app / CLI при настроенном callas)."""
    global _configured_renderer
    _configured_renderer = renderer


def get_pdf_renderer() -> PdfPageRenderer:
    return _configured_renderer or unavailable_pdf_renderer


def dpi_for_longest_side(width_pt: float, height_pt: float, longest_px: int, max_scale: float = 4.0) -> int:
    """DPI, при котором длинная сторона страницы ≈ longest_px (в пределах callas 36..1200)."""
    scale = min(longest_px / max(width_pt, height_pt, 1.0), max_scale)
    return max(36, min(1200, int(round(72 * scale))))


def render_pdf_to_pngs(pdf_path: Path, dpi: int, destinations: list[Path]) -> list[Path]:
    """Рендерит PDF через callas и кладёт страницы по указанным путям (по порядку)."""
    import tempfile

    first = destinations[0]
    first.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".callas-render-", dir=first.parent) as tmp:
        pages = get_pdf_renderer()(Path(pdf_path), int(dpi), Path(tmp))
        if len(pages) < len(destinations):
            raise ValueError(
                f"callas вернул {len(pages)} стр., ожидалось не меньше {len(destinations)}."
            )
        for page, target in zip(pages, destinations):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(page), target)
    return destinations
