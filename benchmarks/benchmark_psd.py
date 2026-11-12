"""Time PSD flatten + PDF conversion against the same artwork as a flat TIFF.

Fixtures (CMYK, 300 dpi, three layers) are generated with ImageMagick in a
temporary directory; their creation time is reported separately.  Each round
measures ``inspect_psd`` + ``flatten_psd`` + ``convert_image_to_pdf`` for the
PSD and ``convert_image_to_pdf`` alone for the equivalent flat TIFF.  No
thresholds are imposed: compare runs on the same host.

    .venv/bin/python benchmarks/benchmark_psd.py --rounds 3
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.metrics import summarize  # noqa: E402
from core.pdf_exporter import convert_image_to_pdf  # noqa: E402
from core.psd_flatten import flatten_psd, inspect_psd  # noqa: E402

SIZES_MM = {"90x50": (90, 50), "320x450": (320, 450)}
DPI = 300


def _px(mm: float) -> int:
    return round(mm / 25.4 * DPI)


def _make_fixtures(folder: Path, magick: str) -> dict[str, dict[str, Path]]:
    fixtures: dict[str, dict[str, Path]] = {}
    for label, (w_mm, h_mm) in SIZES_MM.items():
        w, h = _px(w_mm), _px(h_mm)
        psd = folder / f"{label}.psd"
        tif = folder / f"{label}.tif"
        common = ["-units", "PixelsPerInch", "-density", str(DPI), "-depth", "8"]
        subprocess.run([
            magick,
            "-size", f"{w}x{h}", "plasma:fractal", "-colorspace", "CMYK", "-set", "label", "bg",
            "(", "-size", f"{w // 2}x{h // 2}", "gradient:cmyk(255,0,0,0)-cmyk(0,0,255,0)",
            "-colorspace", "CMYK", "-set", "label", "gradient", "-repage", f"+{w // 8}+{h // 8}", ")",
            "(", "-size", f"{w // 3}x{h // 3}", "xc:cmyk(0,0,0,255)", "-colorspace", "CMYK",
            "-set", "label", "black", "-repage", f"+{w // 2}+{h // 2}", ")",
            *common, str(psd),
        ], check=True)
        subprocess.run([magick, str(psd), "-flatten", "-alpha", "off", *common, "-compress", "zip", str(tif)], check=True)
        fixtures[label] = {"psd": psd, "tif": tif}
    return fixtures


def _sample(fixtures: dict[str, dict[str, Path]], work: Path) -> dict[str, float]:
    sample: dict[str, float] = {}
    for label, files in fixtures.items():
        flat = work / f"{label}_flat.tif"
        started = time.perf_counter()
        inspect_psd(files["psd"])
        sample[f"{label}_psd_inspect_seconds"] = time.perf_counter() - started

        started = time.perf_counter()
        flatten_psd(files["psd"], flat)
        sample[f"{label}_psd_flatten_seconds"] = time.perf_counter() - started

        started = time.perf_counter()
        convert_image_to_pdf(str(flat), str(work / f"{label}_from_psd.pdf"), dpi=DPI)
        sample[f"{label}_psd_flat_to_pdf_seconds"] = time.perf_counter() - started
        sample[f"{label}_psd_total_seconds"] = (
            sample[f"{label}_psd_inspect_seconds"]
            + sample[f"{label}_psd_flatten_seconds"]
            + sample[f"{label}_psd_flat_to_pdf_seconds"]
        )

        started = time.perf_counter()
        convert_image_to_pdf(str(files["tif"]), str(work / f"{label}_from_tif.pdf"), dpi=DPI)
        sample[f"{label}_tiff_to_pdf_seconds"] = time.perf_counter() - started
    return sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    magick = shutil.which("magick")
    if not magick:
        raise SystemExit("ImageMagick `magick` is required")

    with tempfile.TemporaryDirectory(prefix="psd-bench-") as tmp:
        folder = Path(tmp)
        started = time.perf_counter()
        (folder / "input").mkdir()
        fixtures = _make_fixtures(folder / "input", magick)
        fixture_seconds = time.perf_counter() - started
        file_sizes = {
            f"{label}_{kind}_bytes": path.stat().st_size
            for label, files in fixtures.items() for kind, path in files.items()
        }
        samples = []
        for index in range(max(1, args.rounds)):
            work = folder / f"round{index}"
            work.mkdir()
            samples.append(_sample(fixtures, work))

    names = sorted({name for sample in samples for name in sample})
    print(json.dumps({
        "rounds": len(samples),
        "dpi": DPI,
        "sizes_mm": SIZES_MM,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "imagemagick": subprocess.run([magick, "-version"], capture_output=True, text=True).stdout.splitlines()[0],
        },
        "fixture_sizes": file_sizes,
        "fixture_setup_seconds_excluded": fixture_seconds,
        "measurements": {name: summarize(sample[name] for sample in samples) for name in names},
        "median_seconds_table": {name: statistics.median(sample[name] for sample in samples) for name in names},
        "notes": [
            "psd_total = inspect + flatten + flat TIFF -> PDF; tiff_to_pdf is the same artwork saved flat",
            "fixtures are ImageMagick-written PSDs (merge_visible strategy); Photoshop composites skip the merge step",
            "compare runs on the same host; no threshold is imposed",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
