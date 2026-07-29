"""Read-only inspection of incoming PDF files.

This module deliberately does not normalize, rasterize, recolor, or save the
document being inspected.  It reports facts which the order-processing layer
can later compare with the order rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf


MAX_SUPPORTED_PAGES = 2
POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4


@dataclass(frozen=True)
class PdfImageInfo:
    page_number: int
    width_px: int
    height_px: int
    colorspace: int | None
    colorspace_name: str | None
    bits_per_component: int | None
    bbox: tuple[float, float, float, float]
    xref: int | None
    has_mask: bool
    xres: int | None
    yres: int | None
    effective_dpi_x: float | None
    effective_dpi_y: float | None


@dataclass(frozen=True)
class PdfPageInfo:
    page_number: int
    width_mm: float
    height_mm: float
    mediabox: tuple[float, float, float, float]
    cropbox: tuple[float, float, float, float]
    trimbox: tuple[float, float, float, float]
    bleedbox: tuple[float, float, float, float]
    artbox: tuple[float, float, float, float]
    rotation: int
    images: tuple[PdfImageInfo, ...]
    content_type: str


@dataclass(frozen=True)
class PdfInspection:
    path: str
    page_count: int = 0
    pages: tuple[PdfPageInfo, ...] = ()
    encrypted: bool = False
    repaired: bool = False
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _rect_tuple(rect: Any) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in (rect.x0, rect.y0, rect.x1, rect.y1))


def _mm(value_pt: float) -> float:
    return value_pt * MM_PER_INCH / POINTS_PER_INCH


def _effective_dpi(pixels: int, extent_pt: float) -> float | None:
    if pixels <= 0 or extent_pt <= 0:
        return None
    return pixels * POINTS_PER_INCH / extent_pt


def _image_info(page_number: int, raw: dict[str, Any]) -> PdfImageInfo:
    bbox = tuple(float(value) for value in raw["bbox"])
    bbox_width = bbox[2] - bbox[0]
    bbox_height = bbox[3] - bbox[1]
    width = int(raw.get("width", 0))
    height = int(raw.get("height", 0))
    return PdfImageInfo(
        page_number=page_number,
        width_px=width,
        height_px=height,
        colorspace=raw.get("colorspace"),
        colorspace_name=raw.get("cs-name"),
        bits_per_component=raw.get("bpc"),
        bbox=bbox,
        xref=raw.get("xref"),
        has_mask=bool(raw.get("has-mask", False)),
        xres=raw.get("xres"),
        yres=raw.get("yres"),
        effective_dpi_x=_effective_dpi(width, bbox_width),
        effective_dpi_y=_effective_dpi(height, bbox_height),
    )


def _page_content_type(page: pymupdf.Page, images: tuple[PdfImageInfo, ...]) -> str:
    # get_image_info() reports displayed images only.  Drawings and text are
    # the observable vector/text content; PDF resource streams alone are not
    # treated as vector content.
    text_blocks = [
        block
        for block in page.get_text("rawdict").get("blocks", [])
        if block.get("type") == 0
    ]
    has_vector = bool(page.get_drawings() or text_blocks)
    if images and has_vector:
        return "mixed"
    if images:
        return "raster"
    return "vector"


def _inspect_page(page: pymupdf.Page, page_number: int) -> PdfPageInfo:
    mediabox = _rect_tuple(page.mediabox)
    images = tuple(
        _image_info(page_number, raw)
        for raw in page.get_image_info(xrefs=True)
    )
    return PdfPageInfo(
        page_number=page_number,
        width_mm=_mm(page.mediabox.width),
        height_mm=_mm(page.mediabox.height),
        mediabox=mediabox,
        cropbox=_rect_tuple(page.cropbox),
        trimbox=_rect_tuple(page.trimbox),
        bleedbox=_rect_tuple(page.bleedbox),
        artbox=_rect_tuple(page.artbox),
        rotation=int(page.rotation),
        images=images,
        content_type=_page_content_type(page, images),
    )


def inspect_pdf(path: str | Path) -> PdfInspection:
    """Inspect *path* without modifying it.

    Errors are returned in ``PdfInspection.errors`` so callers can present a
    useful diagnostic and route the source to Troubles.  The function does
    not infer 4-0 / 4-4 rules and does not use filename conventions.
    """

    source = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    if not source.exists():
        return PdfInspection(str(source), errors=("PDF file does not exist",))
    if not source.is_file():
        return PdfInspection(str(source), errors=("PDF path is not a file",))
    if source.stat().st_size == 0:
        return PdfInspection(str(source), errors=("PDF file is empty",))

    try:
        document = pymupdf.open(str(source))
    except Exception as exc:
        return PdfInspection(
            str(source),
            errors=(f"PDF cannot be opened: {exc}",),
        )

    try:
        encrypted = bool(document.is_encrypted)
        repaired = bool(document.is_repaired)
        page_count = document.page_count
        if encrypted:
            errors.append("Encrypted PDF files are not supported")
        if repaired:
            errors.append("PDF required structural repair while opening")
        if page_count == 0:
            errors.append("PDF contains no pages")
        elif page_count > MAX_SUPPORTED_PAGES:
            errors.append(
                f"PDF contains {page_count} pages; maximum supported is "
                f"{MAX_SUPPORTED_PAGES}"
            )

        pages: list[PdfPageInfo] = []
        if not encrypted:
            for index in range(page_count):
                try:
                    pages.append(_inspect_page(document[index], index + 1))
                except Exception as exc:
                    errors.append(f"Page {index + 1} cannot be inspected: {exc}")
        return PdfInspection(
            path=str(source),
            page_count=page_count,
            pages=tuple(pages),
            encrypted=encrypted,
            repaired=repaired,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
    finally:
        document.close()


def expected_page_count_error(actual_page_count: int, expected_page_count: int) -> str | None:
    """Return a diagnostic when a document has the wrong page count.

    This intentionally accepts counts, not filenames or order models.  The
    order layer can use it for its own 4-0 / 4-4 policy.
    """

    if actual_page_count != expected_page_count:
        return (
            f"PDF contains {actual_page_count} pages; expected "
            f"{expected_page_count}"
        )
    return None
