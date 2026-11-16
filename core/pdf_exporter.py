"""
Модуль конвертации и сборки входящих растровых/векторных графических файлов в PDF-документ (Image-to-PDF Exporter).
"""

import os
import shutil
from typing import Union

from core.tool_runner import run_command

def convert_image_to_pdf(
    input_image_path: str,
    output_pdf_path: str,
    dpi: Union[float, str] = 300.0,
    compression: str = "none",
    ignore_tiff_layers: bool = False,
    strip_icc: bool = False,
) -> str:
    """Конвертирует одиночное изображение в PDF с сохранением DPI и размера.

    ``strip_icc`` убирает только встроенный ICC-профиль (тег ``ICCBased``);
    значения пикселей не пересчитываются.
    """
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick (`magick`) не найден.")

    os.makedirs(os.path.dirname(os.path.abspath(output_pdf_path)), exist_ok=True)

    cmd = [magick_cmd]
    if ignore_tiff_layers:
        cmd += ["-define", "tiff:ignore-layers=true"]
    cmd += [input_image_path]
    if strip_icc:
        cmd += ["+profile", "icc", "+profile", "icm"]
    cmd += [
        "-units", "PixelsPerInch",
        "-density", str(dpi),
        "-compress", compression,
        output_pdf_path
    ]
    run_command(cmd, check=True)
    return output_pdf_path
