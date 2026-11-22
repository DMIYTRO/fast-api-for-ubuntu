"""Детектор крупной плашки 100% K по цветоделениям callas (``sep_process``, PNG, 50 ppi).

Профиль «Pure CMYK black to rich black» отдельной проверки не имеет, а ink coverage не
отличает плашку от тона. Поэтому считаем по сепарациям: пиксель «чистый K100» это тёмный
канал Black (< 30) при белых Cyan, Magenta, Yellow (>= 225). Доля считается от площади
страницы. Отдельно считается доля любого тёмного Black (составной/растровый чёрный) для
предупреждения о ручной корректировке.

Плашкой считается только сплошное поле: маска проходит морфологическое открытие квадратом
``min_size_mm`` (эрозия, затем дилатация), поэтому текст и тонкие линии — живой текст, кривые
или растр, всё равно — в долю не попадают: штрих тоньше квадрата исчезает целиком. Текст
должен оставаться чистым K100, поэтому ни плашкой, ни поводом для кнопки «в составной чёрный»
он не является. Тонкий чистый K100 (сырая доля минус поле) возвращается отдельно как ``fine``,
чтобы проверка после исправления видела, не превратился ли текст в составной.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from services.callas.client import deterministic_job_id

DARK_LEVEL = 30
WHITE_LEVEL = 225
#: Разрешение цветоделений (ppi): грубо, но быстро; мелкие объекты усредняются с фоном.
SEPARATIONS_PPI = 50
#: Наименьшая сторона сплошного поля (мм), которое считается плашкой; всё тоньше — текст/линии.
#: 4 мм оставляют за бортом штрихи жирного текста примерно до 72 pt.
DEFAULT_MIN_SOLID_SIZE_MM = 4.0


def _open_radius(min_size_mm: float, ppi: float) -> int:
    """Радиус квадрата открытия в пикселях (сторона ``2r + 1`` ≈ ``min_size_mm``); 0 — без открытия."""
    if min_size_mm <= 0 or ppi <= 0:
        return 0
    side_px = min_size_mm * ppi / 25.4
    return max(1, round((side_px - 1) / 2))


def solid_areas(mask: Image.Image, radius: int) -> Image.Image:
    """Морфологическое открытие маски (255 — объект): остаются поля, вмещающие квадрат ``2r + 1``.

    Эрозия и дилатация повторами фильтра 3×3 дают точно тот же квадрат, что и один большой
    фильтр, но дешевле на больших страницах.
    """
    if radius <= 0:
        return mask
    result = mask
    for _ in range(radius):
        result = result.filter(ImageFilter.MinFilter(3))
    for _ in range(radius):
        result = result.filter(ImageFilter.MaxFilter(3))
    # Открытие не расширяет маску, но обработка краёв в фильтрах PIL не гарантирована — обрезаем.
    return ImageChops.multiply(result, mask)


def page_shares(
    black: Path, cyan: Path, magenta: Path, yellow: Path, *, radius: int = 0
) -> dict[str, float]:
    """Доли страницы, 0..1: ``k100`` и ``dark`` — сплошные поля (после открытия), ``k100_raw`` —
    весь чистый K100, ``fine`` — тонкий чистый K100 (текст, линии), ``k100_raw - k100``."""
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
        return {"k100": 0.0, "dark": 0.0, "k100_raw": 0.0, "fine": 0.0}
    dark_mask = k.point(lambda value: 255 if value < DARK_LEVEL else 0)
    # Все три канала белые: наименьший из них >= WHITE_LEVEL.
    darkest = ImageChops.darker(ImageChops.darker(others[0], others[1]), others[2])
    white_mask = darkest.point(lambda value: 255 if value >= WHITE_LEVEL else 0)
    pure_mask = ImageChops.multiply(dark_mask, white_mask)
    pure_raw = pure_mask.histogram()[255]
    pure = solid_areas(pure_mask, radius).histogram()[255]
    dark = solid_areas(dark_mask, radius).histogram()[255]
    return {
        "k100": pure / total,
        "dark": dark / total,
        "k100_raw": pure_raw / total,
        "fine": (pure_raw - pure) / total,
    }


def analyze_separations(
    files: dict[tuple[int, str], Path],
    threshold: float,
    fix_threshold: float | None = None,
    *,
    min_size_mm: float = DEFAULT_MIN_SOLID_SIZE_MM,
    ppi: float = SEPARATIONS_PPI,
) -> dict[str, Any]:
    radius = _open_radius(min_size_mm, ppi)
    pages: dict[int, dict[str, float]] = {}
    for page in sorted({key[0] for key in files}):
        try:
            paths = [files[(page, ink)] for ink in ("Black", "Cyan", "Magenta", "Yellow")]
        except KeyError:
            continue
        pages[page] = page_shares(*paths, radius=radius)
    # Кнопка исправления: чистый K100 есть хотя бы на малой доле страницы (порог не выше порога плашки).
    fix_limit = threshold if fix_threshold is None else min(fix_threshold, threshold)
    fixable = {page: v["k100"] for page, v in pages.items() if v["k100"] > 0 and v["k100"] >= fix_limit}
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
        # Есть чистый K100, который профиль «в составной чёрный» может заменить (плашка или меньше).
        "fix_available": bool(fixable),
        "fix_threshold": fix_limit,
        "fix_pages": sorted(fixable),
        "fix_share": round(max(fixable.values()), 4) if fixable else 0.0,
        "page_shares": {str(page): round(v["k100"], 4) for page, v in sorted(pages.items())},
        # Сплошным полем считается область, вмещающая квадрат min_size_mm; тоньше — текст/линии.
        "min_size_mm": min_size_mm,
        # Тонкий чистый K100 (текст, линии, мелкие элементы): не плашка, должен остаться K100.
        "fine_share": round(max((v["fine"] for v in pages.values()), default=0.0), 4),
        "fine_page_shares": {str(page): round(v["fine"], 4) for page, v in sorted(pages.items())},
        # Тёмное поле есть, но это не чистый K100 (составной чёрный или растр).
        "manual_check": bool(heavy_other) and not solid,
        "manual_pages": sorted(heavy_other) if not solid else [],
    }


@contextmanager
def separation_files(
    client: Any, pdf: Path | str, label: str, *, resolution: int = SEPARATIONS_PPI
) -> Iterator[dict[tuple[int, str], Path]]:
    """Цветоделения ``{(страница, краска): PNG}``; задание callas и его папка удаляются на выходе."""
    source = Path(pdf)
    job_id = deterministic_job_id(f"sep-{label}", str(source), uuid.uuid4().hex[:12])
    try:
        yield client.separations(source, resolution=resolution, job_id=job_id)
    finally:
        try:
            client.delete(job_id)
        except Exception:  # noqa: BLE001
            pass
        job_dir = getattr(client, "job_dir", None)
        if job_dir is not None:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)


def detect_black_solid(
    client: Any,
    pdf: Path | str,
    threshold: float,
    label: str,
    *,
    fix_threshold: float | None = None,
    min_size_mm: float = DEFAULT_MIN_SOLID_SIZE_MM,
) -> dict[str, Any]:
    with separation_files(client, pdf, label) as files:
        return {
            "status": "ok",
            **analyze_separations(
                files, threshold, fix_threshold, min_size_mm=min_size_mm, ppi=SEPARATIONS_PPI
            ),
        }
