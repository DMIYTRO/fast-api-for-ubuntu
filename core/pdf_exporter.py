"""
Модуль конвертации и сборки входящих растровых/векторных графических файлов в PDF-документ (Image-to-PDF Exporter).
"""

import os
import shutil
from typing import List, Union
from core.tool_runner import run_command

def convert_image_to_pdf(
    input_image_path: str,
    output_pdf_path: str,
    dpi: Union[float, str] = 300.0,
    compression: str = "none",
) -> str:
    """Конвертирует одиночное изображение в PDF с сохранением DPI и размера."""
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick (`magick`) не найден.")

    os.makedirs(os.path.dirname(os.path.abspath(output_pdf_path)), exist_ok=True)

    cmd = [
        magick_cmd,
        input_image_path,
        "-units", "PixelsPerInch",
        "-density", str(dpi),
        "-compress", compression,
        output_pdf_path
    ]
    run_command(cmd, check=True)
    return output_pdf_path


def merge_pdfs_with_ghostscript(input_pdf_paths: List[str], output_pdf_path: str) -> str:
    """Merge PDF pages with Ghostscript without downsampling or color conversion."""
    if not input_pdf_paths:
        raise ValueError("Список PDF-файлов для объединения пуст.")

    gs_cmd = shutil.which("gs")
    if not gs_cmd:
        raise FileNotFoundError(
            "Ghostscript (`gs`) не найден. Установите его командой `brew install ghostscript`."
        )

    os.makedirs(os.path.dirname(os.path.abspath(output_pdf_path)), exist_ok=True)
    cmd = [
        gs_cmd,
        "-q",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        "-dAutoRotatePages=/None",
        "-sColorConversionStrategy=LeaveColorUnchanged",
        "-dDownsampleColorImages=false",
        "-dDownsampleGrayImages=false",
        "-dDownsampleMonoImages=false",
        "-dEncodeColorImages=false",
        "-dEncodeGrayImages=false",
        "-dEncodeMonoImages=false",
        "-dPassThroughJPEGImages=true",
        "-dPassThroughJPXImages=true",
        "-dCompressPages=false",
        "-dCompressStreams=false",
        f"-sOutputFile={output_pdf_path}",
    ] + input_pdf_paths
    run_command(cmd, check=True)
    return output_pdf_path

def combine_images_to_pdf(
    input_image_paths: List[str],
    output_pdf_path: str,
    dpi: float = 300.0,
    compression: str = "none",
) -> str:
    """Объединяет список изображений в один многостраничный PDF документ."""
    if not input_image_paths:
        raise ValueError("Список файлов для сборки PDF пуст.")

    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick (`magick`) не найден.")

    os.makedirs(os.path.dirname(os.path.abspath(output_pdf_path)), exist_ok=True)

    cmd = [
        magick_cmd,
        "-units", "PixelsPerInch",
        "-density", str(dpi),
    ] + input_image_paths + [
        "-compress", compression,
        output_pdf_path,
    ]
    run_command(cmd, check=True)
    return output_pdf_path
