#!/usr/bin/env python3
"""Ручной прогон допечатных проверок и планов Callas на реальных PDF (НЕ pytest).

    IMAGE_MAGIC_CALLAS_URL=http://192.168.88.109:8765 \\
    IMAGE_MAGIC_CALLAS_TOKEN="$(tr -d '\\r\\n' < /mnt/shared/callas-image-magic.token)" \\
    .venv/bin/python scripts/callas_prepress_manual.py --out /path/to/out file1.pdf file2.pdf

Для каждого PDF в ``<out>/<имя>/PDF/`` кладётся КОПИЯ (исходник не трогается), затем:
RGB → CMYK с поиском ICC, допечатные проверки, три плана исправления с повторной проверкой.
Токен берётся только из окружения и не печатается. Печатает JSON-сводку.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from config.callas_plans import CALLAS_PLANS  # noqa: E402
from server.settings import Settings  # noqa: E402
from services.callas import CallasClient  # noqa: E402
from services.callas.color_conversion import CallasRgbToCmykConverter, convert_rgb_to_cmyk  # noqa: E402
from services.callas.fix_checks import build_post_checks  # noqa: E402
from services.callas.prepress import PrepressAnalyzer  # noqa: E402
from services.callas.renderer import configure_pdf_renderer, make_callas_pdf_renderer  # noqa: E402
from services.callas_fix import CallasFixError, CallasFixService, fix_dir_for  # noqa: E402



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("pdfs", nargs="+", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.callas_url or not settings.callas_token:
        print("Задайте IMAGE_MAGIC_CALLAS_URL и IMAGE_MAGIC_CALLAS_TOKEN в окружении.", file=sys.stderr)
        return 2
    client = CallasClient.from_settings(settings)
    configure_pdf_renderer(make_callas_pdf_renderer(client))
    analyzer = PrepressAnalyzer(
        client, high_ppi_limit=settings.high_ppi_limit, black_solid_threshold=settings.black_solid_area_threshold
    )
    service = CallasFixService(client, post_checks=build_post_checks(analyzer))
    converter = CallasRgbToCmykConverter(client, settings.callas_rgb_to_cmyk_profile)
    summary: dict = {}
    for source in args.pdfs:
        name = source.stem[:60]
        work = args.out / name / "PDF"
        work.mkdir(parents=True, exist_ok=True)
        copy = work / f"{name}.pdf"
        shutil.copyfile(source, copy)
        entry: dict = {"source": str(source.resolve()), "copy": str(copy)}
        entry["color_conversion"] = convert_rgb_to_cmyk(copy, converter, name)
        entry["prepress"] = analyzer(copy, name)
        entry["fixes"] = {}
        for key in ("fonts_to_outlines", "black_to_rich", "remove_clipping"):
            plan = CALLAS_PLANS[key]
            try:
                service.start(copy, key)
                state = service.execute(copy, key)
            except CallasFixError as exc:
                entry["fixes"][key] = {"status": "rejected", "error": str(exc)}
                continue
            fixed = fix_dir_for(copy) / "fixed.pdf"
            entry["fixes"][key] = {
                "status": state.get("status"), "error": state.get("error"),
                "callas": (state.get("summary") or {}).get("callas"),
                "post_check": (state.get("summary") or {}).get("post_check"),
                "changed_percent": (state.get("summary") or {}).get("changed_percent"),
                "fixed_pdf": str(fixed) if state.get("status") == "ready" else None,
                "profile": plan.profile,
            }
            if state.get("status") == "ready":
                shutil.copyfile(fixed, fix_dir_for(copy) / f"fixed_{key}.pdf")
                entry["fixes"][key]["fixed_pdf"] = str(fix_dir_for(copy) / f"fixed_{key}.pdf")
        summary[name] = entry
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
