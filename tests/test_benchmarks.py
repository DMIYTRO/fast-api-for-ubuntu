"""Smoke tests for the local startup and file-processing benchmark tools."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_benchmark(script: str, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / script), *args],
        cwd=ROOT,
        env={**os.environ, "IMAGE_MAGIC_SBORKA_ENABLED": "0", "IMAGE_MAGIC_PITSTOP_ENABLED": "0"},
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    return json.loads(completed.stdout)


def test_startup_benchmark_reports_fresh_process_and_lifespan_metrics():
    report = _run_benchmark(
        "benchmark_startup.py", "--rounds", "1", "--scenario", "lifespan", "--database", "warm"
    )

    assert report["database_mode"] == "warm"
    assert report["measurements"]["fresh_process_wall_seconds"]["samples"] == 1
    assert report["measurements"]["import_and_module_app_seconds"]["median_seconds"] >= 0
    assert report["measurements"]["alembic_upgrade_seconds"]["median_seconds"] >= 0
    assert report["measurements"]["lifespan_ready_seconds"]["median_seconds"] >= 0
    assert report["measurements"]["lifespan_shutdown_seconds"]["median_seconds"] >= 0


@pytest.mark.skipif(not shutil.which("magick"), reason="ImageMagick is required for file benchmark")
@pytest.mark.skipif(not shutil.which("gs"), reason="Ghostscript is required for full PDF benchmark")
def test_processing_benchmark_fixture_passes_and_creates_pdf_and_preview():
    report = _run_benchmark(
        "benchmark_processing.py", "--orders", "1", "--rounds", "1", "--full", "--previews"
    )

    result = report["round_results"][0]
    assert result["discovered_files"] == 1
    assert result["inspected_orders"] == 1
    assert result["orders_passed_inspection"] == 1
    assert result["coordinator_run_status"] == "completed"
    assert result["pdf_count"] == 1
    assert result["preview_count"] == 1
    assert result["processing_errors"] == 0
    assert result["processing_per_order"]["samples"] == 1
    assert report["measurements"]["coordinator_submit_to_first_order_seconds"]["median_seconds"] >= 0


def test_duration_summary_rejects_empty_or_invalid_samples():
    from benchmarks.metrics import summarize

    with pytest.raises(ValueError, match="at least one sample"):
        summarize([])
    with pytest.raises(ValueError, match="finite, non-negative"):
        summarize([float("nan")])
