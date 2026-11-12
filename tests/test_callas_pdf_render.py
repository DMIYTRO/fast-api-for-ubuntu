"""Рендер PDF только через callas: Ghostscript не нужен и не вызывается."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pymupdf
import pytest

import core.tool_runner
import processing.batch_processor as batch_module
from processing.batch_processor import BatchProcessor, CallasRenderUnavailableError
from services.callas import renderer


_real_which = shutil.which


def _which_without_gs(name, *args, **kwargs):
    if name in {"gs", "gswin64c", "gswin32c"}:
        return None
    return _real_which(name, *args, **kwargs)


def _is_gs(command) -> bool:
    first = command[0] if isinstance(command, (list, tuple)) else str(command).split()[0]
    return Path(str(first)).name.startswith("gs")


@pytest.fixture
def no_ghostscript(monkeypatch):
    monkeypatch.setattr(shutil, "which", _which_without_gs)
    real_run = core.tool_runner.run_command
    calls: list[list[str]] = []

    def guarded(command, *args, **kwargs):
        calls.append(list(command))
        if _is_gs(command):
            raise AssertionError(f"Ghostscript вызван: {command}")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(core.tool_runner, "run_command", guarded)
    monkeypatch.setattr(batch_module, "run_command", guarded)
    real_popen = subprocess.Popen

    def popen(command, *args, **kwargs):
        if _is_gs(command):
            raise AssertionError(f"Ghostscript вызван: {command}")
        return real_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    return calls


@pytest.mark.skipif(not _real_which("magick"), reason="нужен ImageMagick")
def test_pipeline_works_without_ghostscript(tmp_path, no_ghostscript, fake_callas_pdf_render):
    inp = tmp_path / "in"
    inp.mkdir()
    for side in ("face", "back"):
        subprocess.run(
            ["magick", "-size", "1111x638", "xc:#b7c5d8", "-colorspace", "CMYK",
             "-units", "PixelsPerInch", "-density", "300",
             str(inp / f"T_(90x50)_4-4_(5-1)_{side}.tif")],
            check=True, capture_output=True,
        )
    proc = BatchProcessor(inp, inp / "PDF")
    results = proc.create_pdfs(proc.inspect_orders())
    assert [err for _, _, err in results] == [None]
    pdf = results[0][1]
    previews = proc.generate_previews_for_all([pdf], inp / "Previews")
    assert previews[0][2] is None and len(previews[0][1]) == 2
    # Контрольный рендер (72 DPI) и превью идут через callas.
    dpis = [dpi for _, dpi in fake_callas_pdf_render.render_calls]
    assert 72 in dpis and len(dpis) == 2
    assert not any(_is_gs(command) for command in no_ghostscript)


def _pdf(path: Path, pages: int = 1) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=100, height=100)
    doc.save(path)
    doc.close()
    return path


@pytest.mark.no_fake_callas_render
def test_control_render_without_callas_fails_with_actionable_error(tmp_path, no_ghostscript):
    proc = BatchProcessor(tmp_path, tmp_path / "PDF")
    with pytest.raises(CallasRenderUnavailableError, match="IMAGE_MAGIC_CALLAS_URL"):
        proc._render_pdf_pages(_pdf(tmp_path / "a.pdf"), tmp_path / "v")


@pytest.mark.no_fake_callas_render
def test_preview_without_callas_fails_and_never_uses_ghostscript(tmp_path, no_ghostscript):
    proc = BatchProcessor(tmp_path, tmp_path / "PDF")
    with pytest.raises(CallasRenderUnavailableError, match="callas"):
        proc.generate_pdf_previews(_pdf(tmp_path / "a.pdf"), tmp_path / "Previews")
    assert no_ghostscript == []


@pytest.mark.no_fake_callas_render
def test_service_renderer_without_callas_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "_configured_renderer", None)
    with pytest.raises(CallasRenderUnavailableError, match="Ghostscript для PDF больше не используется"):
        renderer.render_pdf_to_pngs(_pdf(tmp_path / "a.pdf"), 72, [tmp_path / "p.png"])


def test_service_renderer_uses_callas_and_cleans_job(tmp_path, fake_callas_pdf_render):
    targets = [tmp_path / "p1.png", tmp_path / "p2.png"]
    renderer.render_pdf_to_pngs(_pdf(tmp_path / "a.pdf", 2), 50, targets)
    assert all(target.is_file() for target in targets)
    assert fake_callas_pdf_render.render_calls == [(tmp_path / "a.pdf", 50)]
    assert not any(fake_callas_pdf_render.root.glob("*"))
