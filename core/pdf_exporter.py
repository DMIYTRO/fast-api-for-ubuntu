"""
Модуль конвертации и сборки входящих растровых/векторных графических файлов в PDF-документ (Image-to-PDF Exporter).
"""

import os
import shutil
from typing import List, Union

import pymupdf

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


def merge_pdfs_with_pymupdf(input_pdf_paths: List[str], output_pdf_path: str) -> str:
    """Merge complete PDF documents by copying pages without rasterization."""
    if not input_pdf_paths:
        raise ValueError("Список PDF-файлов для объединения пуст.")

    output_path = os.path.abspath(output_pdf_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    expected_page_count = 0
    merged = pymupdf.open()

    try:
        for input_pdf_path in input_pdf_paths:
            if not os.path.isfile(input_pdf_path):
                raise FileNotFoundError(f"Входной PDF не найден: {input_pdf_path}")
            if os.path.getsize(input_pdf_path) == 0:
                raise ValueError(f"Входной PDF пуст: {input_pdf_path}")

            try:
                source = pymupdf.open(input_pdf_path)
            except Exception as exc:
                raise ValueError(f"Не удалось открыть входной PDF: {input_pdf_path}") from exc

            try:
                if source.page_count == 0:
                    raise ValueError(f"Входной PDF не содержит страниц: {input_pdf_path}")
                merged.insert_pdf(source)
                expected_page_count += source.page_count
            finally:
                source.close()

        if expected_page_count == 0:
            raise ValueError("Входные PDF не содержат страниц.")
        merged.save(output_path)
    except Exception:
        merged.close()
        raise
    else:
        merged.close()

    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        raise ValueError("PyMuPDF не создал итоговый PDF или файл пуст.")

    try:
        check = pymupdf.open(output_path)
    except Exception as exc:
        raise ValueError("PyMuPDF создал нечитаемый итоговый PDF.") from exc
    try:
        if check.page_count != expected_page_count:
            raise ValueError(
                "Неверное количество страниц в итоговом PDF: "
                f"{check.page_count}; ожидалось {expected_page_count}."
            )
    finally:
        check.close()

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
