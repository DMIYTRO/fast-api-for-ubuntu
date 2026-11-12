"""Сравнение PDF «до» и «после» исправления: оригинал, результат и наложение.

Страницы выравниваются по центру TrimBox (если TrimBox нет, по центру страницы)
и кладутся на общий холст по большему размеру, так что видны и края страниц,
и смещение содержимого. Наложение рисует оригинал красным, результат голубым,
совпадающее остаётся тёмным; контур TrimBox оригинала красный сплошной, результата
синий пунктирный.

Модуль только читает исходные PDF и пишет три изображения в ``out_dir``.
Для работы нужен один PyMuPDF: пиксели собираются из байтовых буферов без numpy/PIL.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from core.preview_cache import normalize_preview_format, preview_extension, preview_jpeg_quality


MM_PER_PT = 25.4 / 72.0

MIN_DPI = 36  # нижняя граница DPI рендера callas
MAX_DPI = 600
MAX_CANVAS_PIXELS = 36_000_000
CANVAS_MARGIN_MM = 4.0

BACKGROUND_GRAY = 232  # светло-серый холст, чтобы края страниц были видны
# Порог отличия серого (0..255) для changed_percent: гасит шум сглаживания.
DIFF_THRESHOLD = 40
INK_THRESHOLD = 250  # пиксель темнее этого считается «краской»

# Допуск различия масштабов по осям: больше 1 % считается искажением.
DISTORTION_TOLERANCE = 0.01

ORIGINAL_OUTLINE = (190, 0, 0)
RESULT_OUTLINE = (0, 90, 210)

_MIN_SCALE_PAIRS = 5
_MIN_SCALE_SPREAD = 0.10  # разброс объектов по оси, доля размера страницы
_MIN_INLIER_SHARE = 0.5


class FixCompareError(Exception):
    """Базовая ошибка сравнения; текст понятен оператору."""


class FixCompareFileError(FixCompareError):
    """Файл не найден, повреждён или не открывается."""


class FixComparePageError(FixCompareError):
    """Запрошенной страницы нет в одном из файлов."""


@dataclass(frozen=True)
class FixComparison:
    original_image: Path
    result_image: Path
    overlay_image: Path
    summary: dict[str, Any]


@dataclass(frozen=True)
class _PageGeometry:
    width_pt: float
    height_pt: float
    trim: fitz.Rect  # в координатах страницы PyMuPDF (начало слева сверху)
    has_trimbox: bool

    @property
    def trim_cx(self) -> float:
        return (self.trim.x0 + self.trim.x1) / 2.0

    @property
    def trim_cy(self) -> float:
        return (self.trim.y0 + self.trim.y1) / 2.0


def build_fix_comparison(
    original_pdf: str | Path,
    fixed_pdf: str | Path,
    out_dir: str | Path,
    *,
    page: int = 1,
    dpi: int = 100,
    preview_format: str | None = None,
) -> FixComparison:
    """Собрать original/result/overlay для страницы ``page`` (с 1) в ``out_dir``.

    Исходные PDF не изменяются, вне ``out_dir`` ничего не пишется. ``preview_format``
    (``jpeg``/``png``) по умолчанию берётся из настроек превью проекта.
    """
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise FixComparePageError(f"Номер страницы должен быть целым числом от 1, получено: {page!r}.")
    if isinstance(dpi, bool) or not isinstance(dpi, (int, float)) or not MIN_DPI <= dpi <= MAX_DPI:
        raise FixCompareError(
            f"Разрешение сравнения должно быть от {MIN_DPI} до {MAX_DPI} dpi, получено: {dpi!r}."
        )
    try:
        extension = preview_extension(normalize_preview_format(preview_format))
    except ValueError as exc:
        raise FixCompareError(str(exc)) from exc

    out_path = Path(out_dir)
    scale = float(dpi) / 72.0

    with _open_pdf(original_pdf, "оригинал") as doc_o, _open_pdf(fixed_pdf, "результат") as doc_r:
        page_o = _load_page(doc_o, page, "оригинал")
        page_r = _load_page(doc_r, page, "результат")
        geo_o = _page_geometry(page_o)
        geo_r = _page_geometry(page_r)

        margin = CANVAS_MARGIN_MM / MM_PER_PT
        half_w = max(
            geo_o.trim_cx, geo_o.width_pt - geo_o.trim_cx,
            geo_r.trim_cx, geo_r.width_pt - geo_r.trim_cx,
        ) + margin
        half_h = max(
            geo_o.trim_cy, geo_o.height_pt - geo_o.trim_cy,
            geo_r.trim_cy, geo_r.height_pt - geo_r.trim_cy,
        ) + margin
        canvas_w = max(1, math.ceil(2 * half_w * scale))
        canvas_h = max(1, math.ceil(2 * half_h * scale))
        if canvas_w * canvas_h > MAX_CANVAS_PIXELS:
            raise FixCompareError(
                f"Холст сравнения слишком большой ({canvas_w}x{canvas_h} px). Уменьшите разрешение."
            )

        layer_o = _render_layer(original_pdf, page_o, geo_o, scale, canvas_w, canvas_h, out_path)
        layer_r = _render_layer(fixed_pdf, page_r, geo_r, scale, canvas_w, canvas_h, out_path)

        scale_info = _estimate_scale(page_o, page_r, geo_o, geo_r)

    changed_percent, changed_content_percent = _measure_difference(layer_o, layer_r)
    overlay = _build_overlay(layer_o, layer_r, scale, canvas_w, canvas_h)

    summary = _build_summary(
        geo_o, geo_r, page, dpi, canvas_w, canvas_h,
        changed_percent, changed_content_percent, scale_info,
    )

    try:
        out_path.mkdir(parents=True, exist_ok=True)
        original_image = out_path / f"original{extension}"
        result_image = out_path / f"result{extension}"
        overlay_image = out_path / f"overlay{extension}"
        _write_image(original_image, layer_o.rgb, canvas_w, canvas_h, extension)
        _write_image(result_image, layer_r.rgb, canvas_w, canvas_h, extension)
        _write_image(overlay_image, overlay, canvas_w, canvas_h, extension)
    except OSError as exc:
        raise FixCompareError(f"Не удалось записать изображения сравнения в {out_path}: {exc}") from exc

    return FixComparison(original_image, result_image, overlay_image, summary)


# --- Открытие файлов и геометрия -------------------------------------------------


def _open_pdf(path: str | Path, role: str) -> fitz.Document:
    file_path = Path(path)
    if not file_path.is_file():
        raise FixCompareFileError(f"Файл PDF ({role}) не найден: {file_path}.")
    try:
        doc = fitz.open(file_path)
    except Exception as exc:  # fitz поднимает разные типы для битых файлов
        raise FixCompareFileError(
            f"Не удалось открыть PDF ({role}) {file_path.name}: файл повреждён или это не PDF."
        ) from exc
    if not doc.is_pdf:
        doc.close()
        raise FixCompareFileError(f"Файл ({role}) {file_path.name} не является PDF.")
    if doc.needs_pass:
        doc.close()
        raise FixCompareFileError(f"PDF ({role}) {file_path.name} защищён паролем, сравнение невозможно.")
    return doc


def _load_page(doc: fitz.Document, number: int, role: str) -> fitz.Page:
    if number > doc.page_count:
        raise FixComparePageError(
            f"В PDF ({role}) нет страницы {number}: всего страниц {doc.page_count}."
        )
    try:
        return doc[number - 1]
    except Exception as exc:
        raise FixCompareFileError(f"Не удалось прочитать страницу {number} PDF ({role}): {exc}") from exc


def _page_geometry(page: fitz.Page) -> _PageGeometry:
    rect = page.rect
    crop = page.cropbox
    trim_raw = fitz.Rect(page.trimbox)
    has_trimbox = _rect_valid(trim_raw) and _rect_differs(trim_raw, crop)

    trim = rect
    if _rect_valid(trim_raw):
        # TrimBox задан в координатах MediaBox: сдвиг к началу CropBox и поворот страницы.
        shifted = fitz.Rect(
            trim_raw.x0 - crop.x0, trim_raw.y0 - crop.y0,
            trim_raw.x1 - crop.x0, trim_raw.y1 - crop.y0,
        )
        candidate = fitz.Rect(shifted * page.rotation_matrix)
        candidate.normalize()
        candidate = candidate & rect
        if _rect_valid(candidate):
            trim = candidate
        else:
            has_trimbox = False
    return _PageGeometry(float(rect.width), float(rect.height), fitz.Rect(trim), has_trimbox)


def _rect_valid(rect: fitz.Rect) -> bool:
    return not rect.is_empty and not rect.is_infinite and rect.width > 0.5 and rect.height > 0.5


def _rect_differs(a: fitz.Rect, b: fitz.Rect, tolerance: float = 0.01) -> bool:
    return any(abs(x - y) > tolerance for x, y in zip(tuple(a), tuple(b)))


# --- Отрисовка слоёв --------------------------------------------------------------


@dataclass
class _Layer:
    rgb: bytearray  # цветной холст (серый фон вне страницы)
    gray: bytearray  # серый холст (вне страницы белый, для наложения и метрики)
    box: tuple[int, int, int, int]  # прямоугольник страницы на холсте: x0, y0, x1, y1
    trim: tuple[int, int, int, int]  # прямоугольник TrimBox на холсте
    canvas_w: int
    canvas_h: int


def _render_page_with_callas(pdf_path: str | Path, page_number: int, dpi: int, work_root: Path) -> fitz.Pixmap:
    """Растр страницы только через callas pdfToolbox (без локального рендера PDF)."""
    import tempfile

    from services.callas.renderer import render_pdf_to_pngs

    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".compare-", dir=work_root) as tmp:
        targets = [Path(tmp) / f"p{index + 1}.png" for index in range(page_number)]
        render_pdf_to_pngs(Path(pdf_path), dpi, targets)
        pix = fitz.Pixmap(str(targets[-1]))
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    if pix.colorspace is None or pix.colorspace.n != 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix


def _render_layer(
    pdf_path: str | Path,
    page: fitz.Page,
    geo: _PageGeometry,
    scale: float,
    canvas_w: int,
    canvas_h: int,
    work_root: Path,
) -> _Layer:
    try:
        pix = _render_page_with_callas(pdf_path, page.number + 1, int(round(scale * 72)), work_root)
    except Exception as exc:
        raise FixCompareFileError(f"Не удалось отрисовать страницу {page.number + 1}: {exc}") from exc
    gray_pix = fitz.Pixmap(fitz.csGRAY, pix)

    x_off = round(canvas_w / 2.0 - geo.trim_cx * scale)
    y_off = round(canvas_h / 2.0 - geo.trim_cy * scale)

    rgb = bytearray(bytes([BACKGROUND_GRAY]) * (canvas_w * canvas_h * 3))
    gray = bytearray(b"\xff" * (canvas_w * canvas_h))
    _paste(rgb, canvas_w, canvas_h, pix.samples, pix.width, pix.height, pix.stride, 3, x_off, y_off)
    _paste(gray, canvas_w, canvas_h, gray_pix.samples, gray_pix.width, gray_pix.height, gray_pix.stride, 1, x_off, y_off)

    trim = (
        round(x_off + geo.trim.x0 * scale),
        round(y_off + geo.trim.y0 * scale),
        round(x_off + geo.trim.x1 * scale),
        round(y_off + geo.trim.y1 * scale),
    )
    box = (x_off, y_off, x_off + pix.width, y_off + pix.height)
    return _Layer(rgb, gray, box, trim, canvas_w, canvas_h)


def _paste(
    canvas: bytearray, canvas_w: int, canvas_h: int,
    samples: bytes, src_w: int, src_h: int, stride: int, channels: int,
    x_off: int, y_off: int,
) -> None:
    x_start = max(0, x_off)
    x_end = min(canvas_w, x_off + src_w)
    if x_end <= x_start:
        return
    row_len = (x_end - x_start) * channels
    src_start = (x_start - x_off) * channels
    for sy in range(src_h):
        cy = y_off + sy
        if cy < 0 or cy >= canvas_h:
            continue
        src = sy * stride + src_start
        dst = (cy * canvas_w + x_start) * channels
        canvas[dst:dst + row_len] = samples[src:src + row_len]


def _write_image(path: Path, rgb: bytes | bytearray, width: int, height: int, extension: str) -> None:
    pix = fitz.Pixmap(fitz.csRGB, width, height, bytes(rgb), False)
    if extension == ".jpg":
        data = pix.tobytes("jpeg", jpg_quality=preview_jpeg_quality())
    else:
        data = pix.tobytes("png")
    path.write_bytes(data)


# --- Метрика различий и наложение -------------------------------------------------

_INK_TABLE = bytes(1 if value < INK_THRESHOLD else 0 for value in range(256))


def _rect_area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def _union_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    inter = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return _rect_area(a) + _rect_area(b) - _rect_area(inter)


def _measure_difference(layer_o: _Layer, layer_r: _Layer) -> tuple[float, float]:
    """Доля отличающихся пикселей: от площади страниц и от площади, занятой краской."""
    width, height = layer_o.canvas_w, layer_o.canvas_h
    gray_o, gray_r = layer_o.gray, layer_r.gray

    differing = 0
    ink_union = 0
    for y in range(height):
        row_o = bytes(gray_o[y * width:(y + 1) * width])
        row_r = bytes(gray_r[y * width:(y + 1) * width])
        ink_o = row_o.translate(_INK_TABLE)
        ink_r = row_r.translate(_INK_TABLE)
        if row_o == row_r:
            ink_union += ink_o.count(1)
            continue
        merged = int.from_bytes(ink_o, "big") | int.from_bytes(ink_r, "big")
        ink_union += merged.to_bytes(width, "big").count(1)
        differing += sum(1 for a, b in zip(row_o, row_r) if abs(a - b) > DIFF_THRESHOLD)

    page_area = _union_area(layer_o.box, layer_r.box)
    changed = 100.0 * differing / page_area if page_area else 0.0
    changed_content = 100.0 * differing / ink_union if ink_union else 0.0
    return float(min(100.0, changed)), float(min(100.0, changed_content))


def _build_overlay(layer_o: _Layer, layer_r: _Layer, scale: float, width: int, height: int) -> bytearray:
    """Оригинал красным (R=результат, G=B=оригинал), результат голубым, общее тёмное."""
    out = bytearray(width * height * 3)
    out[0::3] = layer_r.gray
    out[1::3] = layer_o.gray
    out[2::3] = layer_o.gray

    # Вне обеих страниц возвращаем серый фон (в серых слоях там белый).
    background = bytes([BACKGROUND_GRAY]) * 3
    rects = (layer_o.box, layer_r.box)
    for y in range(height):
        spans = sorted(
            (max(0, r[0]), min(width, r[2])) for r in rects if r[1] <= y < r[3] and r[2] > 0 and r[0] < width
        )
        merged: list[list[int]] = []
        for start, end in spans:
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        cursor = 0
        for start, end in merged + [[width, width]]:
            if start > cursor:
                out[(y * width + cursor) * 3:(y * width + start) * 3] = background * (start - cursor)
            cursor = end

    thickness = max(1, round(scale * 72.0 / 100.0))
    _draw_outline(out, width, height, layer_o.trim, ORIGINAL_OUTLINE, thickness, dashed=False)
    _draw_outline(out, width, height, layer_r.trim, RESULT_OUTLINE, thickness, dashed=True)
    return out


def _draw_outline(
    buf: bytearray, width: int, height: int,
    box: tuple[int, int, int, int], color: tuple[int, int, int], thickness: int, *, dashed: bool,
) -> None:
    x0, y0, x1, y1 = box
    dash = max(6, thickness * 6)

    def put(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            index = (y * width + x) * 3
            buf[index:index + 3] = bytes(color)

    for t in range(thickness):
        for x in range(x0, x1 + 1):
            if dashed and ((x - x0) // dash) % 2:
                continue
            put(x, y0 + t)
            put(x, y1 - t)
        for y in range(y0, y1 + 1):
            if dashed and ((y - y0) // dash) % 2:
                continue
            put(x0 + t, y)
            put(x1 - t, y)


# --- Оценка масштаба по векторным объектам -----------------------------------------


@dataclass(frozen=True)
class _ScaleInfo:
    scale_x: float | None = None
    scale_y: float | None = None
    pairs: int = 0
    content_shift_x_pt: float | None = None
    content_shift_y_pt: float | None = None


def _collect_objects(page: fitz.Page) -> list[tuple[tuple, tuple[float, float, float, float]]]:
    """Прямоугольники векторных объектов и картинок со «слепком» для сопоставления."""
    objects: list[tuple[tuple, tuple[float, float, float, float]]] = []
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for item in drawings:
        rect = item.get("rect")
        if rect is None:
            continue
        key = (
            "d",
            item.get("type"),
            _round_color(item.get("fill")),
            _round_color(item.get("color")),
            len(item.get("items") or ()),
        )
        objects.append((key, (rect.x0, rect.y0, rect.x1, rect.y1)))
    try:
        images = page.get_image_info()
    except Exception:
        images = []
    for info in images:
        bbox = info.get("bbox")
        if bbox:
            key = ("i", info.get("width"), info.get("height"))
            objects.append((key, (bbox[0], bbox[1], bbox[2], bbox[3])))
    return objects


def _round_color(value: Any) -> Any:
    if value is None:
        return None
    try:
        return tuple(round(float(component), 2) for component in value)
    except TypeError:
        return value


def _pair_objects(
    objs_o: list[tuple[tuple, tuple[float, float, float, float]]],
    objs_r: list[tuple[tuple, tuple[float, float, float, float]]],
) -> list[tuple[tuple[float, float, float, float], tuple[float, float, float, float]]]:
    groups_o: dict[tuple, list] = {}
    groups_r: dict[tuple, list] = {}
    for key, rect in objs_o:
        groups_o.setdefault(key, []).append(rect)
    for key, rect in objs_r:
        groups_r.setdefault(key, []).append(rect)
    pairs = []
    for key, rects_o in groups_o.items():
        rects_r = groups_r.get(key)
        if rects_r and len(rects_r) == len(rects_o):
            pairs.extend(zip(rects_o, rects_r))
    if len(pairs) < _MIN_SCALE_PAIRS and len(objs_o) == len(objs_r):
        pairs = [(a[1], b[1]) for a, b in zip(objs_o, objs_r)]
    return pairs


def _fit_axis(src: list[float], dst: list[float], page_span: float) -> tuple[float, float, int] | None:
    """Устойчивая линейная подгонка dst = k * src + b; None, если надёжно оценить нельзя."""
    count = len(src)
    if count < _MIN_SCALE_PAIRS or max(src) - min(src) < _MIN_SCALE_SPREAD * page_span:
        return None
    min_gap = 0.05 * page_span
    rng = random.Random(0)
    slopes: list[float] = []
    for _ in range(min(3000, count * 4)):
        i, j = rng.randrange(count), rng.randrange(count)
        if abs(src[i] - src[j]) >= min_gap:
            slopes.append((dst[i] - dst[j]) / (src[i] - src[j]))
    if len(slopes) < 3:
        return None
    slope = _median(slopes)
    intercept = _median([d - slope * s for s, d in zip(src, dst)])

    tolerance = 1.5  # pt
    for _ in range(3):
        residuals = [abs(d - (slope * s + intercept)) for s, d in zip(src, dst)]
        inliers = [i for i, r in enumerate(residuals) if r <= tolerance]
        if len(inliers) < _MIN_SCALE_PAIRS or len(inliers) < _MIN_INLIER_SHARE * count:
            return None
        xs = [src[i] for i in inliers]
        ys = [dst[i] for i in inliers]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        var = sum((x - mean_x) ** 2 for x in xs)
        if var <= 0 or max(xs) - min(xs) < _MIN_SCALE_SPREAD * page_span:
            return None
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var
        intercept = mean_y - slope * mean_x
    return slope, intercept, len(inliers)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _estimate_scale(page_o: fitz.Page, page_r: fitz.Page, geo_o: _PageGeometry, geo_r: _PageGeometry) -> _ScaleInfo:
    try:
        pairs = _pair_objects(_collect_objects(page_o), _collect_objects(page_r))
    except Exception:
        return _ScaleInfo()
    if len(pairs) < _MIN_SCALE_PAIRS:
        return _ScaleInfo()

    cx_o = [(a[0] + a[2]) / 2.0 for a, _ in pairs]
    cx_r = [(b[0] + b[2]) / 2.0 for _, b in pairs]
    cy_o = [(a[1] + a[3]) / 2.0 for a, _ in pairs]
    cy_r = [(b[1] + b[3]) / 2.0 for _, b in pairs]
    fit_x = _fit_axis(cx_o, cx_r, geo_o.width_pt)
    fit_y = _fit_axis(cy_o, cy_r, geo_o.height_pt)

    scale_x = fit_x[0] if fit_x else None
    scale_y = fit_y[0] if fit_y else None
    shift_x = shift_y = None
    if fit_x:
        shift_x = fit_x[0] * geo_o.trim_cx + fit_x[1] - geo_r.trim_cx
    if fit_y:
        shift_y = fit_y[0] * geo_o.trim_cy + fit_y[1] - geo_r.trim_cy
    used = min(fit_x[2] if fit_x else len(pairs), fit_y[2] if fit_y else len(pairs))
    return _ScaleInfo(scale_x, scale_y, used, shift_x, shift_y)


# --- Итоговая сводка ----------------------------------------------------------------


def _mm(value_pt: float) -> float:
    return round(float(value_pt) * MM_PER_PT, 2) + 0.0


def _describe(geo: _PageGeometry) -> dict[str, Any]:
    return {
        "page_width_mm": _mm(geo.width_pt),
        "page_height_mm": _mm(geo.height_pt),
        "trimbox_width_mm": _mm(geo.trim.width),
        "trimbox_height_mm": _mm(geo.trim.height),
        "has_trimbox": bool(geo.has_trimbox),
    }


def _round_or_none(value: float | None, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _build_summary(
    geo_o: _PageGeometry, geo_r: _PageGeometry, page: int, dpi: float,
    canvas_w: int, canvas_h: int,
    changed_percent: float, changed_content_percent: float, scale_info: _ScaleInfo,
) -> dict[str, Any]:
    scale_x = scale_info.scale_x
    scale_y = scale_info.scale_y
    distortion = bool(
        scale_x is not None and scale_y is not None and scale_y != 0
        and abs(scale_x / scale_y - 1.0) > DISTORTION_TOLERANCE
    )
    # Смещение TrimBox внутри страницы (результат минус оригинал), ось y вниз.
    center_shift_x = (geo_r.trim_cx - geo_r.width_pt / 2.0) - (geo_o.trim_cx - geo_o.width_pt / 2.0)
    center_shift_y = (geo_r.trim_cy - geo_r.height_pt / 2.0) - (geo_o.trim_cy - geo_o.height_pt / 2.0)
    center_shift = {
        "x": _mm(center_shift_x),
        "y": _mm(center_shift_y),
        "distance": _mm(math.hypot(center_shift_x, center_shift_y)),
    }
    content_shift = None
    if scale_info.content_shift_x_pt is not None and scale_info.content_shift_y_pt is not None:
        content_shift = {
            "x": _mm(scale_info.content_shift_x_pt),
            "y": _mm(scale_info.content_shift_y_pt),
            "distance": _mm(math.hypot(scale_info.content_shift_x_pt, scale_info.content_shift_y_pt)),
        }
    return {
        "page": int(page),
        "dpi": float(dpi),
        "canvas_px": {"width": int(canvas_w), "height": int(canvas_h)},
        "original": _describe(geo_o),
        "result": _describe(geo_r),
        "page_size_changed": bool(
            abs(geo_o.width_pt - geo_r.width_pt) * MM_PER_PT > 0.05
            or abs(geo_o.height_pt - geo_r.height_pt) * MM_PER_PT > 0.05
        ),
        "center_shift_mm": center_shift,
        "content_shift_mm": content_shift,
        "changed_percent": round(float(changed_percent), 3),
        "changed_content_percent": round(float(changed_content_percent), 3),
        "scale_x": _round_or_none(scale_x, 4),
        "scale_y": _round_or_none(scale_y, 4),
        "scale_pairs": int(scale_info.pairs),
        "distortion": distortion,
    }
