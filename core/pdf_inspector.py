"""Read-only inspection of incoming PDF files.

This module deliberately does not normalize, rasterize, recolor, or save the
document being inspected.  It reports facts which the order-processing layer
can later compare with the order rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf


MAX_SUPPORTED_PAGES = 2


def pages_text(count: int) -> str:
    """«1 страница», «3 страницы», «15 страниц»."""
    tail = count % 100
    if 11 <= tail <= 14:
        word = "страниц"
    elif count % 10 == 1:
        word = "страница"
    elif 2 <= count % 10 <= 4:
        word = "страницы"
    else:
        word = "страниц"
    return f"{count} {word}"


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
    # Фоновые объекты (заливка, изображение, градиент), закрывающие всю страницу и
    # выходящие за её край: (запас по ширине, запас по высоте) в мм — наименьший
    # выход объекта за левый/правый и верхний/нижний край MediaBox. По ним коррекция
    # размера решает, можно ли показать скрытые вылеты, раздвинув страницу.
    hidden_margins_mm: tuple[tuple[float, float], ...] = ()


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
    # Only a boolean answer is needed, so use the cheap "blocks" extraction
    # (block[6] is the block type, 0 = text) and short-circuit before the
    # drawings scan; "rawdict" and get_drawings() are very slow on large PDFs.
    has_text = any(block[6] == 0 for block in page.get_text("blocks"))
    has_vector = has_text or bool(page.get_cdrawings())
    if images and has_vector:
        return "mixed"
    if images:
        return "raster"
    return "vector"


_BACKGROUND_KINDS = {"fill-path", "fill-image", "fill-shade"}
_HIDDEN_EPSILON_MM = 0.05


def _hidden_margins(page: pymupdf.Page) -> tuple[tuple[float, float], ...]:
    """Запасы фоновых объектов за краем страницы (см. ``PdfPageInfo.hidden_margins_mm``)."""
    # Координаты bboxlog отсчитываются от CropBox; при CropBox ≠ MediaBox не гадаем.
    if page.cropbox != page.mediabox:
        return ()
    try:
        log = page.get_bboxlog()
    except Exception:
        return ()
    width, height = page.rect.width, page.rect.height
    margins: set[tuple[float, float]] = set()
    for kind, rect in log:
        if kind not in _BACKGROUND_KINDS:
            continue
        x0, y0, x1, y1 = rect
        hx = _mm(min(-x0, x1 - width))
        hy = _mm(min(-y0, y1 - height))
        if hx >= -_HIDDEN_EPSILON_MM and hy >= -_HIDDEN_EPSILON_MM and max(hx, hy) > _HIDDEN_EPSILON_MM:
            margins.add((round(max(hx, 0.0), 3), round(max(hy, 0.0), 3)))
    return tuple(sorted(margins, reverse=True))


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
        hidden_margins_mm=_hidden_margins(page),
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
                f"PDF содержит {pages_text(page_count)}; допускается не более "
                f"{MAX_SUPPORTED_PAGES} (лицо и оборот) — пришлите каждую сторону "
                "отдельной страницей одного заказа"
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


_RGB_OPERATOR = re.compile(
    rb"(?:(?:^|\s)-?[\d.]+\s+-?[\d.]+\s+-?[\d.]+\s+(?:rg|RG)(?=\s|$)|/DeviceRGB\s+(?:cs|CS)(?=\s|$))"
)


def find_rgb_pages(path: str | Path) -> tuple[int, ...]:
    """Номера страниц (с 1), где есть RGB: растры с 3 компонентами и векторные/текстовые операторы цвета DeviceRGB в потоке страницы.

    Только чтение. Lab (тоже 3 компоненты) не считается RGB. Объекты внутри Form XObject не
    просматриваются.
    """
    found: list[int] = []
    with pymupdf.open(str(path)) as document:
        for index in range(document.page_count):
            page = document[index]
            has_rgb = False
            for raw in page.get_image_info():
                name = str(raw.get("cs-name") or "").lower()
                if raw.get("colorspace") == 3 and "lab" not in name:
                    has_rgb = True
                    break
            if not has_rgb:
                # get_cdrawings() пересчитывает CMYK в RGB-тройки, поэтому смотрим
                # операторы цвета DeviceRGB прямо в потоке содержимого страницы.
                has_rgb = bool(_RGB_OPERATOR.search(page.read_contents()))
            if has_rgb:
                found.append(index + 1)
    return tuple(found)


# --------------------------------------------------------------------------- ICC / Lab
_REF = re.compile(r"(\d+)\s+0\s+R")
_ICC_REF = re.compile(r"/ICCBased\s+(\d+)\s+0\s+R")
_PARENT_REF = re.compile(r"/Parent\s+\d+\s+0\s+R")
_LAB = re.compile(r"/Lab(?![A-Za-z0-9])")
#: Числа компонентов ICC-профилей, которые снимает перевод в CMYK: CMYK (4) и Gray (1).
#: ICC-RGB (3) сюда не входит: RGB обрабатывает основная ветка конвертации.
ICC_STRIP_COMPONENTS = (4, 1)
_MAX_COLOR_OBJECTS = 20000


@dataclass(frozen=True)
class ColorProfileScan:
    """Страницы (с 1) с ICC-профилем CMYK/Gray и с цветовым пространством Lab."""

    icc_pages: tuple[int, ...] = ()
    lab_pages: tuple[int, ...] = ()


def _page_resources(document: pymupdf.Document, page_xref: int) -> str:
    """Текст словаря Resources страницы (с учётом наследования от родителей); пусто, если нет."""
    xref = page_xref
    for _ in range(32):
        kind, value = document.xref_get_key(xref, "Resources")
        if kind in ("dict", "xref") and value.strip():
            return value
        parent_kind, parent = document.xref_get_key(xref, "Parent")
        match = _REF.search(parent) if parent_kind == "xref" else None
        if not match:
            return ""
        xref = int(match.group(1))
    return ""


def _scan_page_colors(
    document: pymupdf.Document, page_xref: int, components: tuple[int, ...], cache: dict[int, int | None]
) -> tuple[bool, bool]:
    """(есть ICC с нужным числом компонентов, есть Lab) среди ресурсов страницы."""
    has_icc = has_lab = False
    queue = [_page_resources(document, page_xref)]
    seen: set[int] = set()
    while queue and len(seen) < _MAX_COLOR_OBJECTS:
        text = queue.pop()
        if _LAB.search(text):
            has_lab = True
        icc_refs = {int(number) for number in _ICC_REF.findall(text)}
        for xref in icc_refs:
            if xref not in cache:
                try:
                    kind, value = document.xref_get_key(xref, "N")
                    cache[xref] = int(value) if kind == "int" else None
                except Exception:  # noqa: BLE001
                    cache[xref] = None
            if cache[xref] in components:
                has_icc = True
        for match in _REF.finditer(_PARENT_REF.sub("", text)):
            xref = int(match.group(1))
            if xref in seen or xref in icc_refs:
                continue
            seen.add(xref)
            try:
                queue.append(document.xref_object(xref, compressed=True))
            except Exception:  # noqa: BLE001
                continue
    return has_icc, has_lab


def scan_color_profiles(
    path: str | Path, components: tuple[int, ...] = ICC_STRIP_COMPONENTS
) -> ColorProfileScan:
    """Один проход по ресурсам страниц: где ICCBased с *components* компонентами и где Lab.

    Только чтение. Обходятся словари ресурсов, изображения, Form XObject, паттерны и шейдинги
    (по ссылкам, без ``/Parent``); содержимое страниц не разбирается.
    """
    icc: list[int] = []
    lab: list[int] = []
    with pymupdf.open(str(path)) as document:
        cache: dict[int, int | None] = {}
        for index in range(document.page_count):
            has_icc, has_lab = _scan_page_colors(document, document[index].xref, components, cache)
            if has_icc:
                icc.append(index + 1)
            if has_lab:
                lab.append(index + 1)
    return ColorProfileScan(tuple(icc), tuple(lab))


def find_icc_pages(path: str | Path, components: tuple[int, ...] = ICC_STRIP_COMPONENTS) -> list[int]:
    """Номера страниц (с 1) с ICC-профилем CMYK (N=4) или Gray (N=1) в ресурсах."""
    return list(scan_color_profiles(path, components).icc_pages)


def find_lab_pages(path: str | Path) -> list[int]:
    """Номера страниц (с 1) с цветовым пространством Lab в ресурсах."""
    return list(scan_color_profiles(path).lab_pages)
