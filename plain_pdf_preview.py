#!/usr/bin/env python3
"""Create a plain, single-page PDF preview of an image with ImageMagick."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


def run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def image_info(magick: str, input_path: Path) -> dict[str, object]:
    fields = run(
        [
            magick,
            "identify",
            "-format",
            "%w\n%h\n%x\n%y\n%[units]\n%m\n%[colorspace]",
            f"{input_path}[0]",
        ]
    ).splitlines()

    width_px, height_px = int(fields[0]), int(fields[1])
    resolution_x, resolution_y = float(fields[2]), float(fields[3])
    units = fields[4]
    if units == "PixelsPerCentimeter":
        dpi_x, dpi_y = resolution_x * 2.54, resolution_y * 2.54
    else:
        dpi_x, dpi_y = resolution_x, resolution_y

    return {
        "width_px": width_px,
        "height_px": height_px,
        "dpi_x": round(dpi_x, 3),
        "dpi_y": round(dpi_y, 3),
        "width_mm": round(width_px / dpi_x * 25.4, 2) if dpi_x else None,
        "height_mm": round(height_px / dpi_y * 25.4, 2) if dpi_y else None,
        "format": fields[5],
        "colorspace": fields[6],
    }


def create_plain_pdf_preview(
    magick: str, input_path: Path, output_path: Path, max_pixels: int
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            magick,
            f"{input_path}[0]",
            "-auto-orient",
            "-resize",
            f"{max_pixels}x{max_pixels}>",
            "-strip",
            "-units",
            "PixelsPerInch",
            "-density",
            "150",
            "-compress",
            "jpeg",
            "-quality",
            "88",
            str(output_path),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read image dimensions and create a plain PDF preview."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=2000,
        help="Maximum preview width or height in pixels (default: 2000).",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"Input file does not exist: {args.input}")
    if args.output.suffix.lower() != ".pdf":
        parser.error("Output file must have a .pdf extension")
    if args.max_pixels < 1:
        parser.error("--max-pixels must be positive")

    magick = shutil.which("magick")
    if not magick:
        parser.error("ImageMagick executable 'magick' was not found")

    info = image_info(magick, args.input)
    create_plain_pdf_preview(magick, args.input, args.output, args.max_pixels)

    print(json.dumps({"input": str(args.input), "preview_pdf": str(args.output), **info}, indent=2))


if __name__ == "__main__":
    main()
