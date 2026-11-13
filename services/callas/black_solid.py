"""Детектор крупной плашки 100% K по цветоделениям callas (``sep_process``, PNG, 50 ppi).

Профиль «Pure CMYK black to rich black» отдельной проверки не имеет, а ink coverage не
отличает плашку от тона. Поэтому считаем по сепарациям: пиксель «чистый K100» это тёмный
канал Black (< 30) при белых Cyan, Magenta, Yellow (>= 225). Доля считается от площади
страницы. Отдельно считается доля любого тёмного Black (составной/растровый чёрный) для
предупреждения о ручной корректировке.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

from services.callas.client import deterministic_job_id

DARK_LEVEL = 30
WHITE_LEVEL = 225


def page_shares(black: Path, cyan: Path, magenta: Path, yellow: Path) -> tuple[float, float]:
    """``(доля чистого K100, доля любого тёмного Black)`` страницы, 0..1."""
    with Image.open(black) as k_image:
        k = k_image.convert("L")
        size = k.size
        others = []
        for path in (cyan, magenta, yellow):
            with Image.open(path) as image:
                channel = image.convert("L")
                others.append(channel if channel.size == size else channel.resize(size))
    total = size[0] * size[1]
    if total == 0:
        return 0.0, 0.0
    dark_mask = k.point(lambda value: 255 if value < DARK_LEVEL else 0)
    # Все три канала белые: наименьший из них >= WHITE_LEVEL.
    darkest = ImageChops.darker(ImageChops.darker(others[0], others[1]), others[2])
    white_mask = darkest.point(lambda value: 255 if value >= WHITE_LEVEL else 0)
    pure_mask = ImageChops.multiply(dark_mask, white_mask)
    dark = dark_mask.histogram()[255]
    pure = pure_mask.histogram()[255]
    return pure / total, dark / total


def analyze_separations(files: dict[tuple[int, str], Path], threshold: float) -> dict[str, Any]:
    pages: dict[int, dict[str, float]] = {}
    for page in sorted({key[0] for key in files}):
        try:
            paths = [files[(page, ink)] for ink in ("Black", "Cyan", "Magenta", "Yellow")]
        except KeyError:
            continue
        pure, dark = page_shares(*paths)
        pages[page] = {"k100": pure, "dark": dark}
    solid = {page: v["k100"] for page, v in pages.items() if v["k100"] >= threshold}
    heavy_other = {
        page: v["dark"] for page, v in pages.items()
        if v["k100"] < threshold and v["dark"] >= threshold
    }
    return {
        "threshold": threshold,
        "detected": bool(solid),
        "share": round(max(solid.values()), 4) if solid else round(max((v["k100"] for v in pages.values()), default=0.0), 4),
        "pages": sorted(solid),
        "page_shares": {str(page): round(v["k100"], 4) for page, v in sorted(pages.items())},
        # Тёмное поле есть, но это не чистый K100 (составной чёрный или растр).
        "manual_check": bool(heavy_other) and not solid,
        "manual_pages": sorted(heavy_other) if not solid else [],
    }


def detect_black_solid(client: Any, pdf: Path | str, threshold: float, label: str) -> dict[str, Any]:
    source = Path(pdf)
    job_id = deterministic_job_id(f"sep-{label}", str(source), uuid.uuid4().hex[:12])
    try:
        files = client.separations(source, resolution=50, job_id=job_id)
        return {"status": "ok", **analyze_separations(files, threshold)}
    finally:
        try:
            client.delete(job_id)
        except Exception:  # noqa: BLE001
            pass
        job_dir = getattr(client, "job_dir", None)
        if job_dir is not None:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
