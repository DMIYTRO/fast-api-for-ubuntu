"""Суммарное покрытие красок (TAC) по цветоделениям callas (``sep_process``, PNG, 50 ppi).

Канал PNG — оттенки серого: 255 — краски нет, 0 — 100%, то есть покрытие канала
``(255 - v) / 255 * 100``. TAC пикселя — сумма четырёх процессных красок (Cyan, Magenta,
Yellow, Black; плашечные в ``sep_process`` не входят). Усреднение 50 ppi не завышает TAC
(сумма средних равна среднему сумм), но мелкие объекты выше предела сливаются с фоном,
поэтому значения приблизительные: окончательный вердикт — повторная проверка PitStop.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageMath

from services.callas.black_solid import separation_files

#: Предел PitStop (ActionID 1153 «Total ink coverage of an object is more than 310%»).
TAC_LIMIT_PERCENT = 310.0
PROCESS_INKS = ("Cyan", "Magenta", "Yellow", "Black")
_FULL = 255


def page_tac(paths: dict[str, Path], limit: float = TAC_LIMIT_PERCENT) -> dict[str, float]:
    """``{"max_percent", "over_share"}`` страницы: наибольший TAC (%) и доля площади выше ``limit``."""
    channels: dict[str, Image.Image] = {}
    size = None
    for ink in PROCESS_INKS:
        with Image.open(paths[ink]) as image:
            channel = image.convert("L")
        if size is None:
            size = channel.size
        elif channel.size != size:
            channel = channel.resize(size)
        channels[ink] = channel.convert("I")
    total = size[0] * size[1] if size else 0
    if total == 0:
        return {"max_percent": 0.0, "over_share": 0.0}
    full = _FULL * len(PROCESS_INKS)
    units = ImageMath.lambda_eval(
        lambda a: full - (a["c"] + a["m"] + a["y"] + a["k"]),
        c=channels["Cyan"], m=channels["Magenta"], y=channels["Yellow"], k=channels["Black"],
    )
    _low, high = units.getextrema()
    # Сумма в целых единицах 0..1020: «> limit %» равносильно «> floor(limit * 2.55)»; ImageMath не сравнивает I с float.
    threshold = math.floor(limit / 100.0 * _FULL)
    over = ImageMath.lambda_eval(lambda a: a["s"] > threshold, s=units).convert("L").histogram()[1]
    return {"max_percent": high / _FULL * 100.0, "over_share": over / total}


def analyze_tac(files: dict[tuple[int, str], Path], limit: float = TAC_LIMIT_PERCENT) -> dict[str, Any]:
    pages: dict[int, dict[str, float]] = {}
    for page in sorted({key[0] for key in files}):
        try:
            paths = {ink: files[(page, ink)] for ink in PROCESS_INKS}
        except KeyError:
            continue
        pages[page] = page_tac(paths, limit)
    over = {page: value for page, value in pages.items() if value["over_share"] > 0}
    return {
        "limit": limit,
        "max_percent": round(max((v["max_percent"] for v in pages.values()), default=0.0), 1),
        "detected": bool(over),
        "pages": sorted(over),
        "over_share": round(max((v["over_share"] for v in over.values()), default=0.0), 4),
        "page_values": {
            str(page): {"max_percent": round(v["max_percent"], 1), "over_share": round(v["over_share"], 4)}
            for page, v in sorted(pages.items())
        },
    }


def measure_tac(client: Any, pdf: Path | str, label: str, limit: float = TAC_LIMIT_PERCENT) -> dict[str, Any]:
    with separation_files(client, pdf, f"tac-{label}") as files:
        return {"status": "ok", **analyze_tac(files, limit)}
