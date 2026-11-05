#!/usr/bin/env python3
"""Явный живой smoke-тест callas: merge двух маленьких PDF и render результата.

НЕ запускается из pytest. Запускать вручную с машины приложения:

    IMAGE_MAGIC_CALLAS_URL=http://192.168.88.109:8765 \\
    IMAGE_MAGIC_CALLAS_TOKEN="$(tr -d '\\r\\n' < /mnt/shared/callas-image-magic.token)" \\
    .venv/bin/python scripts/callas_smoke.py

Токен берётся только из окружения и нигде не печатается. По умолчанию
задания и их папки на шаре удаляются после проверки (--keep оставляет).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import fitz  # noqa: E402

from server.settings import Settings  # noqa: E402
from services.callas import CallasClient, CallasError  # noqa: E402


def make_pdf(path: Path, label: str, width: float, height: float) -> None:
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((20, 40), label, fontsize=18)
    doc.save(path)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dpi", type=int, default=72)
    parser.add_argument("--keep", action="store_true", help="не удалять задания и файлы")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.callas_url or not settings.callas_token:
        print("Задайте IMAGE_MAGIC_CALLAS_URL и IMAGE_MAGIC_CALLAS_TOKEN в окружении.", file=sys.stderr)
        return 2
    client = CallasClient.from_settings(settings)
    stamp = time.strftime("%Y%m%d%H%M%S")
    merge_id, render_id = f"smoke-merge-{stamp}", f"smoke-render-{stamp}"
    exit_code = 0
    try:
        with tempfile.TemporaryDirectory(prefix="callas-smoke-") as tmp:
            a, b = Path(tmp) / "a.pdf", Path(tmp) / "b.pdf"
            make_pdf(a, "smoke A", 200, 300)
            make_pdf(b, "smoke B", 200, 300)

            started = time.monotonic()
            merged = client.merge([a, b], "merged.pdf", job_id=merge_id)
            with fitz.open(merged) as doc:
                pages = doc.page_count
            print(f"merge: OK за {time.monotonic() - started:.1f} с, {pages} стр., {merged}")
            if pages != 2:
                raise SystemExit(f"merge: ожидалось 2 страницы, получено {pages}")

            started = time.monotonic()
            pngs = client.render(merged, args.dpi, job_id=render_id, expected_pages=2)
            print(f"render: OK за {time.monotonic() - started:.1f} с, {len(pngs)} PNG:")
            for png in pngs:
                print(f"  {png} ({png.stat().st_size} байт)")
    except CallasError as exc:
        print(f"ОШИБКА callas: {exc.message}", file=sys.stderr)
        exit_code = 1
    finally:
        if not args.keep:
            for job_id in (merge_id, render_id):
                try:
                    client.delete(job_id)
                except CallasError as exc:
                    print(f"не удалось удалить задание {job_id}: {exc.message}", file=sys.stderr)
                shutil.rmtree(client.job_dir(job_id), ignore_errors=True)
        client.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
