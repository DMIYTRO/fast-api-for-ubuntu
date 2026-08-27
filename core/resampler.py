"""
Модуль автоматической коррекции и даунсемплинга макетов (Auto-Resampler Module).

Условия коррекции:
1. Текущее разрешение < 270 DPI (например, 70 или 150 DPI).
2. Физический размер файла больше требуемого размера (извлеченного из имени файла или профиля).
3. При совпадении условий выполняется сжатие/ресемплинг до целевых мм при 300 DPI с фильтром Lanczos.
"""

import os
import re
import shutil
from typing import Tuple, Optional
from core.inspector import ImageMetadata
from core.tool_runner import run_command

def parse_target_dimensions_from_filename(filename: str) -> Optional[Tuple[float, float]]:
    """Извлекает целевой размер в мм из имени файла, например `(70x100)` -> (70.0, 100.0)."""
    match = re.search(r'\((\d+)\s*[xхXХ]\s*(\d+)\)', filename)
    if match:
        w_mm = float(match.group(1))
        h_mm = float(match.group(2))
        return w_mm, h_mm
    
    # Резервный поиск формата WxH без скобок
    match_simple = re.search(r'(\d{2,4})\s*[xхXХ]\s*(\d{2,4})', filename)
    if match_simple:
        w_mm = float(match_simple.group(1))
        h_mm = float(match_simple.group(2))
        return w_mm, h_mm

    return None

def should_resample(meta: ImageMetadata, target_w_mm: float, target_h_mm: float, min_dpi: float = 270.0) -> bool:
    """Определяет, требуется ли автоматический даунсемплинг."""
    is_low_dpi = meta.dpi < min_dpi
    is_larger_size = meta.width_mm >= target_w_mm and meta.height_mm >= target_h_mm
    proportional_height = target_w_mm * meta.height_px / meta.width_px
    keeps_proportions = abs(proportional_height - target_h_mm) <= 0.5
    return is_low_dpi and is_larger_size and keeps_proportions

def resample_image(
    input_path: str,
    output_path: str,
    target_width_mm: float,
    target_height_mm: float,
    target_dpi: float = 300.0,
    rotation_degrees: int = 0,
) -> str:
    """Proportionally fill and center-crop to the target without stretching."""
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick (`magick`) не найден.")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    # Расчет целевых пикселей
    target_w_px = round(target_width_mm * (target_dpi / 25.4))
    target_h_px = round(target_height_mm * (target_dpi / 25.4))

    cmd = [magick_cmd, input_path]
    if rotation_degrees:
        cmd += ["-rotate", str(rotation_degrees)]
    cmd += [
        "-units", "PixelsPerInch",
        "-density", str(target_dpi),
        "-filter", "Lanczos",
        "-resize", f"{target_w_px}x{target_h_px}^",
        "-gravity", "center",
        "-extent", f"{target_w_px}x{target_h_px}",
        output_path
    ]

    run_command(cmd, check=True)
    return output_path
