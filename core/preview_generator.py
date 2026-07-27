"""
Ядро визуальной разметки и генерации превью изображений.
"""

import os
import shutil
from core.tool_runner import run_command

def generate_preview(
    input_path: str,
    output_preview_path: str,
    dpi: float,
    w_px: int,
    h_px: int,
    safe_zone_mm: float = 4.0,
    bleed_mm: float = 1.0
) -> str:
    """Отрисовывает превью макета с наложением зеленой наружной (1 мм) и красной внутренней (4 мм) рамок."""
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("Утилита ImageMagick (`magick`) не найдена в системе.")

    os.makedirs(os.path.dirname(os.path.abspath(output_preview_path)), exist_ok=True)

    # Расчет отступов в пикселях
    safe_px = round(safe_zone_mm * (dpi / 25.4))
    border_px = max(1, round(bleed_mm * (dpi / 25.4)))

    gx1 = safe_px
    gy1 = safe_px
    gx2 = w_px - safe_px
    gy2 = h_px - safe_px

    half_b = border_px / 2.0
    rx1 = half_b
    ry1 = half_b
    rx2 = w_px - half_b
    ry2 = h_px - half_b

    cmd = [
        magick_cmd, input_path,
        "-stroke", "#28a745",
        "-strokewidth", str(border_px),
        "-fill", "none",
        "-draw", f"rectangle {rx1},{ry1} {rx2},{ry2}",
        "-stroke", "#dc3545",
        "-strokewidth", str(max(2, round(border_px * 0.4))),
        "-fill", "none",
        "-draw", f"rectangle {gx1},{gy1} {gx2},{gy2}",
        output_preview_path
    ]
    run_command(cmd, check=True)
    return output_preview_path
