"""Benchmark input discovery, image inspection, PDF processing, optional previews.

Requires local ImageMagick. ``--full`` also needs Ghostscript for final PDF QA.
Generated fixtures and all outputs live in a temporary directory. No remote
services are configured or called.

Examples:
  .venv/bin/python benchmarks/benchmark_processing.py --orders 25 --rounds 3
  .venv/bin/python benchmarks/benchmark_processing.py --orders 5 --full --previews
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from metrics import summarize


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _make_fixtures(input_dir: Path, orders: int, magick: str) -> None:
    input_dir.mkdir(parents=True)
    for number in range(orders):
        path = input_dir / f"BENCH_(90x50)_4-0_(42-{number + 1})_face.tif"
        subprocess.run(
            [magick, "-size", "1111x638", "xc:#b7c5d8", "-colorspace", "CMYK",
             "-units", "PixelsPerInch", "-density", "300", "-compress", "LZW", str(path)],
            check=True, capture_output=True, text=True,
        )


def _sample(input_dir: Path, *, full: bool, previews: bool, magick: str) -> dict[str, object]:
    from processing.batch_processor import BatchProcessor
    from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
    from services.coordinator import RunCoordinator
    from services.repository import InMemoryRunRepository

    pdf_dir = input_dir / "PDF"
    processor = BatchProcessor(input_dir, pdf_dir)
    started = time.perf_counter()
    discovered = processor.scan()
    discovery_seconds = time.perf_counter() - started

    started = time.perf_counter()
    iterator = processor.iter_inspect_orders()
    orders = []
    first_order_seconds = None
    for order in iterator:
        if first_order_seconds is None:
            first_order_seconds = time.perf_counter() - started
        orders.append(order)
    inspection_seconds = time.perf_counter() - started
    result: dict[str, object] = {
        "discovered_files": len(discovered),
        "inspected_orders": len(orders),
        "discovery_seconds": discovery_seconds,
        "inspection_time_to_first_order_seconds": first_order_seconds or inspection_seconds,
        "inspection_total_seconds": inspection_seconds,
        "inspected_orders_per_second": len(orders) / inspection_seconds if inspection_seconds else 0,
        "discovered_files_per_second": len(discovered) / discovery_seconds if discovery_seconds else 0,
    }
    result["orders_passed_inspection"] = sum(order.passed for order in orders)
    # Include the actual queued/background path so startup-to-first-result is
    # distinguishable from direct BatchProcessor inspection time.
    coordinator = RunCoordinator(InMemoryRunRepository())
    try:
        options = ProcessingOptions(
            input_path=str(input_dir), direction="digital",
            create_pdfs=False, generate_previews=False,
        )
        submitted = time.perf_counter()
        run = coordinator.submit(options)
        run_id = run["id"]
        with coordinator._changed:
            found_first = coordinator._changed.wait_for(
                lambda: any(event.type == "order.checked" for event in coordinator.events(run_id)),
                timeout=120,
            )
        if not found_first:
            raise TimeoutError("coordinator did not emit order.checked within 120 seconds")
        first_result_seconds = time.perf_counter() - submitted
        completed = coordinator.wait_for(run_id, {"completed", "failed"}, timeout=120)
        completed_seconds = time.perf_counter() - submitted
        result["coordinator_submit_to_first_order_seconds"] = first_result_seconds
        result["coordinator_submit_to_terminal_seconds"] = completed_seconds
        result["coordinator_run_status"] = completed["status"]
    finally:
        coordinator.shutdown(timeout=5)
    if not full:
        return result

    adapter = BatchProcessorAdapter(
        ProcessingOptions(
            input_path=str(input_dir), direction="digital", create_pdfs=True,
            generate_previews=previews, copy_failures=False,
        )
    )
    started = time.perf_counter()
    order_durations = []
    errors = 0
    for order in orders:
        order_started = time.perf_counter()
        artifacts = adapter.process_order(order)
        order_durations.append(time.perf_counter() - order_started)
        errors += bool(artifacts.errors)
    processing_seconds = time.perf_counter() - started
    result.update({
        "processing_total_seconds": processing_seconds,
        "processing_per_order": summarize(order_durations) if order_durations else None,
        "processing_errors": errors,
        "orders_with_processing_errors": errors,
        "pdf_count": len(list(pdf_dir.glob("*.pdf"))) if pdf_dir.exists() else 0,
        "preview_count": len(list((input_dir / "Previews").rglob("*_preview.png"))),
        "processed_orders_per_second": len(orders) / processing_seconds if processing_seconds else 0,
        "preview_generation": previews,
    })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--full", action="store_true", help="also generate and validate per-order PDFs")
    parser.add_argument("--previews", action="store_true", help="also generate preview images (requires --full)")
    args = parser.parse_args()
    if args.orders < 1 or args.rounds < 1:
        parser.error("--orders and --rounds must be at least 1")
    if args.previews and not args.full:
        parser.error("--previews requires --full")
    magick = _tool("magick")
    if magick is None:
        raise SystemExit("ImageMagick magick не найден в PATH; benchmark не запускался.")
    if args.full and _tool("gs") is None:
        raise SystemExit("Ghostscript gs не найден в PATH; запустите без --full для замера поиска/инспекции.")

    samples: list[dict[str, object]] = []
    fixture_setup: list[float] = []
    with tempfile.TemporaryDirectory(prefix="image-magic-processing-bench-") as temporary:
        root = Path(temporary)
        for index in range(args.rounds):
            input_dir = root / f"round-{index}"
            setup_started = time.perf_counter()
            _make_fixtures(input_dir, args.orders, magick)
            fixture_setup.append(time.perf_counter() - setup_started)
            samples.append(_sample(input_dir, full=args.full, previews=args.previews, magick=magick))

    metric_names = [
        "discovery_seconds", "inspection_time_to_first_order_seconds",
        "inspection_total_seconds", "processing_total_seconds",
        "coordinator_submit_to_first_order_seconds",
        "coordinator_submit_to_terminal_seconds",
    ]
    measurements = {
        name: summarize([float(sample[name]) for sample in samples if name in sample])
        for name in metric_names if any(name in sample for sample in samples)
    }
    if args.full:
        per_order = [sample["processing_per_order"] for sample in samples]
        measurements["processing_seconds_per_order_median"] = summarize(
            float(item["median_seconds"]) for item in per_order if isinstance(item, dict)
        )
    print(json.dumps({
        "orders": args.orders,
        "rounds": args.rounds,
        "full_pdf_processing": args.full,
        "previews": args.previews,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "imagemagick": subprocess.run([magick, "-version"], capture_output=True, text=True).stdout.splitlines()[0],
            "ghostscript": subprocess.run([_tool("gs"), "--version"], capture_output=True, text=True).stdout.strip()
            if _tool("gs") else None,
        },
        "measurements": measurements,
        "fixture_setup_seconds_median_excluded": statistics.median(fixture_setup),
        "round_results": samples,
        "notes": [
            "fixture creation time is reported separately and excluded from scan/processing times",
            "no network integrations are configured",
            "compare runs on the same host; no universal machine-dependent threshold is imposed",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
