#!/usr/bin/env python3
"""Split, merge and re-save a PDF using Callas pdfToolbox CLI."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = Path(
    "/mnt/shared/inputFolders/10_KS(P)_Bezlam_250_mel_(210x297)_4-4_L25_T50_"
    "(15563-25708349)_creas1-face(проработать вылетели скрытые слои).pdf"
)
DEFAULT_CLI = ROOT / "tools/callas_pdfToolboxCLI_x64_Linux_17-0-682/pdfToolbox"
DEFAULT_CACHE = ROOT / ".callas-cache"
DEFAULT_OUTPUT = ROOT / "output/callas_roundtrip_test"


def run(cli: Path, cache: Path, *args: str) -> None:
    command = [str(cli), f"--cachefolder={cache}", "--noprogress", *args]
    print("+", " ".join(command))
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.stdout:
        print(completed.stdout.strip())
    if completed.stderr:
        print(completed.stderr.strip())
    if completed.returncode >= 100:
        raise RuntimeError(f"Callas command failed ({completed.returncode}): {' '.join(args)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    source = args.input.expanduser().resolve()
    cli = args.cli.expanduser().resolve()
    output = args.output.expanduser().resolve()
    cache = DEFAULT_CACHE.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not cli.is_file():
        raise FileNotFoundError(cli)
    output.mkdir(parents=True, exist_ok=True)
    split_dir = output / "split"
    split_dir.mkdir(exist_ok=True)
    for old_part in split_dir.glob("*.pdf"):
        old_part.unlink()

    # Every source page becomes a separate PDF.
    run(cli, cache, "--splitpdf", "--splitscheme=*1", f"--outputfolder={split_dir}", str(source))
    parts = sorted(split_dir.glob("*.pdf"))
    if not parts:
        raise RuntimeError(f"No split PDFs were produced in {split_dir}")

    merged = output / "merged.pdf"
    run(cli, cache, "--mergepdf", f"--outputfile={merged}", *(str(part) for part in parts))

    # Three re-save variants: default, optimized, and explicitly non-optimized.
    variants = {
        "01_merged_default.pdf": [],
        "02_merged_optimized.pdf": ["--optimizepdf"],
        "03_merged_nooptimization.pdf": ["--nooptimization"],
    }
    for filename, options in variants.items():
        run(cli, cache, "--mergepdf", *options, f"--outputfile={output / filename}", str(merged))

    print("\nCreated files:")
    for path in [*parts, merged, *(output / name for name in variants)]:
        print(path)


if __name__ == "__main__":
    main()
