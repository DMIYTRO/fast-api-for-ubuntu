"""Живой сквозной прогон BatchProcessor: merge и render через callas, превью JPEG.

Запуск вручную (токен только из окружения):
  IMAGE_MAGIC_CALLAS_URL=http://192.168.88.109:8765 IMAGE_MAGIC_CALLAS_TOKEN=... \
  IMAGE_MAGIC_PREVIEW_FORMAT=jpeg .venv/bin/python scripts/callas_e2e.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from processing.batch_processor import BatchProcessor  # noqa: E402
from server.settings import Settings  # noqa: E402
from services.callas import CallasClient  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    if not settings.callas_url or not settings.callas_token:
        raise SystemExit("Нужны IMAGE_MAGIC_CALLAS_URL и IMAGE_MAGIC_CALLAS_TOKEN.")
    with tempfile.TemporaryDirectory(prefix="callas-e2e-") as tmp:
        inp = Path(tmp) / "in"
        inp.mkdir()
        for side in ("face", "back"):
            subprocess.run(
                ["magick", "-size", "1111x638", "xc:#b7c5d8", "-colorspace", "CMYK",
                 "-units", "PixelsPerInch", "-density", "300", "-compress", "LZW",
                 str(inp / f"E2E_(90x50)_4-4_(77-1)_{side}.tif")],
                check=True, capture_output=True,
            )
        proc = BatchProcessor(
            inp, inp / "PDF",
            callas_client=CallasClient.from_settings(settings),
            callas_merge=True, callas_render=True,
        )
        orders = proc.inspect_orders()
        print("orders:", [(o.order_id, o.passed) for o in orders])
        results = proc.create_pdfs(orders)
        print("pdf:", [(str(p.name), err) for _, p, err in results])
        pdfs = [p for _, p, err in results if err is None]
        previews = proc.generate_previews_for_all(pdfs, inp / "Previews")
        print("previews:", [(str(p.name), [x.name for x in v], e) for p, v, e in previews])
        import pymupdf
        for pdf in pdfs:
            print(pdf.name, "pages:", pymupdf.open(pdf).page_count)
        for _, v, _ in previews:
            for x in v:
                print(x.name, subprocess.run(["magick", "identify", str(x)], capture_output=True, text=True).stdout.strip())
        ok = pdfs and all(e is None for _, _, e in previews) and all(v for _, v, _ in previews)
        print("RESULT:", "OK" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
