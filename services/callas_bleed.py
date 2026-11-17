"""Вердикт «исправление Callas только дорисовало вылеты» (``bleed_only``).

План ``size_correction`` (профиль ``Image Magic/size_correction``) приводит страницу к
формату с вылетами. Если он лишь дорисовал поля снаружи формата, а макет внутри
TrimBox остался прежним (тот же масштаб, без сдвига, те же пиксели), всё, что
принесли поля (растянутые однопиксельные полосы с разрешением ~13 dpi, обтравка,
прозрачность), не должно переводить заказ в ошибку: проверки содержимого берутся из
исходного файла клиента, геометрия — из исправленного PDF.

Вердикт строится только по сводке сравнения ``core/fix_compare.py`` (её пишет
``CallasFixService.execute`` в ``state.json``) и формату заказа из имени файла. Любое
сомнение (не измерено, другой план, другая страница) — ``False`` с причиной: тогда
всё по-прежнему проверяется по исправленному PDF.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from config.profiles import DEFAULT_PROFILE, PROFILES

BLEED_PLAN_KEY = "size_correction"
#: Масштаб содержимого 1.0 ± допуск (оценка по векторным объектам и картинкам).
SCALE_TOLERANCE = 0.002
#: Сдвиг содержимого и центра формата, мм.
SHIFT_LIMIT_MM = 0.1
#: Доля отличающихся пикселей внутри TrimBox, % (шум сглаживания рендера — доли процента).
TRIM_CHANGE_LIMIT_PERCENT = 0.5
#: Допуск совпадения размеров страницы и TrimBox с форматом, мм.
DEFAULT_TOLERANCE_MM = 0.3


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _distance(value: Any) -> float | None:
    if isinstance(value, Mapping):
        return _number(value.get("distance"))
    return None


def _size(described: Any, width_key: str, height_key: str) -> tuple[float, float] | None:
    if not isinstance(described, Mapping):
        return None
    width, height = _number(described.get(width_key)), _number(described.get(height_key))
    if width is None or height is None:
        return None
    return width, height


def _fmt(size: tuple[float, float]) -> str:
    return f"{size[0]:g}×{size[1]:g} мм"


def _matches(actual: tuple[float, float], expected: tuple[float, float], tolerance: float) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected))


def _trim_change(page: Mapping[str, Any]) -> float | None:
    """Изменение внутри формата, %.

    ``trim_changed_percent`` меряет ровно TrimBox. В состояниях, записанных до этого
    поля, берётся ``overlay_changed_percent`` (подсветка в общей области страниц),
    но только если страница результата не меньше исходной: тогда общая область —
    вся исходная страница, и TrimBox в неё входит.
    """
    measured = _number(page.get("trim_changed_percent"))
    if measured is not None:
        return measured
    original = _size(page.get("original"), "page_width_mm", "page_height_mm")
    result = _size(page.get("result"), "page_width_mm", "page_height_mm")
    if original is None or result is None:
        return None
    if all(r + 0.05 >= o for r, o in zip(result, original)):
        return _number(page.get("overlay_changed_percent"))
    return None


def _page_reasons(page: Mapping[str, Any], label: str) -> list[str]:
    """Причины по одной странице: масштаб, сдвиг, изменения внутри формата."""
    reasons: list[str] = []
    if page.get("distortion") is True:
        reasons.append(f"{label}пропорции макета искажены")
    for axis in ("scale_x", "scale_y"):
        scale = _number(page.get(axis))
        if scale is not None and abs(scale - 1.0) > SCALE_TOLERANCE:
            reasons.append(f"{label}макет масштабирован ({scale * 100:.1f} %)")
            break
    content_shift = _distance(page.get("content_shift_mm"))
    if content_shift is not None and content_shift > SHIFT_LIMIT_MM:
        reasons.append(f"{label}макет сдвинут на {content_shift:.2f} мм")
    center_shift = _distance(page.get("center_shift_mm"))
    if center_shift is not None and center_shift > SHIFT_LIMIT_MM:
        reasons.append(f"{label}формат (TrimBox) сдвинут на {center_shift:.2f} мм")
    change = _trim_change(page)
    if change is None:
        reasons.append(f"{label}изменение внутри формата не измерено")
    elif change > TRIM_CHANGE_LIMIT_PERCENT:
        reasons.append(f"{label}внутри формата изменилось {change:.1f} % изображения")
    return reasons


def _format_reasons(
    page: Mapping[str, Any],
    label: str,
    format_mm: tuple[float, float],
    bleed_mm: float,
    tolerance_mm: float,
) -> list[str]:
    """Страница результата = формат + 2 вылета, TrimBox = формат (50×90 == 90×50)."""
    page_size = _size(page.get("result"), "page_width_mm", "page_height_mm")
    trim_size = _size(page.get("result"), "trimbox_width_mm", "trimbox_height_mm")
    if page_size is None or trim_size is None:
        return [f"{label}размер результата не измерен"]
    width, height = float(format_mm[0]), float(format_mm[1])
    extra = 2.0 * float(bleed_mm)
    orientations = (((width + extra, height + extra), (width, height)),
                    ((height + extra, width + extra), (height, width)))
    if any(
        _matches(page_size, expected_page, tolerance_mm) and _matches(trim_size, expected_trim, tolerance_mm)
        for expected_page, expected_trim in orientations
    ):
        return []
    reasons = []
    if not any(_matches(page_size, expected, tolerance_mm) for expected, _trim in orientations):
        reasons.append(
            f"{label}страница результата {_fmt(page_size)}, ожидается {_fmt(orientations[0][0])}"
        )
    if not any(_matches(trim_size, expected, tolerance_mm) for _page, expected in orientations):
        reasons.append(f"{label}TrimBox результата {_fmt(trim_size)}, ожидается {_fmt(orientations[0][1])}")
    if not reasons:
        reasons.append(f"{label}ориентация страницы и TrimBox результата не совпадают")
    return reasons


def _pages(summary: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Сравнённые страницы: первая — сама сводка, остальные — ``summary["pages"]``."""
    pages: list[tuple[str, Mapping[str, Any]]] = [("", summary)]
    for extra in summary.get("pages") or ():
        if isinstance(extra, Mapping):
            pages.append((f"стр. {extra.get('page')}: ", extra))
    return pages


def bleed_only_verdict(
    summary: Mapping[str, Any],
    *,
    format_mm: tuple[float, float] | None,
    bleed_mm: float,
    tolerance_mm: float = DEFAULT_TOLERANCE_MM,
) -> tuple[bool, list[str]]:
    """``(True, [])`` — исправление только дорисовало вылеты; иначе ``False`` и причины.

    ``summary`` — сводка исправления (``state["summary"]``) с ``plan_key``; ``format_mm`` —
    формат из имени файла (без вылетов), ``None`` — формат неизвестен и не проверяется.
    """
    if not isinstance(summary, Mapping):
        return False, ["нет сводки сравнения"]
    plan = summary.get("plan_key")
    if plan != BLEED_PLAN_KEY:
        return False, ["план не «Коррекция размера макета»"]
    reasons: list[str] = []
    original_count = summary.get("original_page_count")
    result_count = summary.get("result_page_count")
    if isinstance(original_count, int) and isinstance(result_count, int):
        if original_count != result_count:
            reasons.append(f"число страниц изменилось: {original_count} → {result_count}")
        compared = {1} | {
            extra.get("page") for extra in summary.get("pages") or () if isinstance(extra, Mapping)
        }
        missing = [number for number in range(1, original_count + 1) if number not in compared]
        if missing and original_count == result_count:
            reasons.append(
                "не сравнены страницы " + ", ".join(str(number) for number in missing)
            )
    for label, page in _pages(summary):
        reasons.extend(_page_reasons(page, label))
        if format_mm is not None:
            reasons.extend(_format_reasons(page, label, format_mm, bleed_mm, tolerance_mm))
    return not reasons, reasons


def state_bleed_only(
    state: Mapping[str, Any] | None,
    *,
    format_mm: tuple[float, float] | None,
    bleed_mm: float,
    tolerance_mm: float = DEFAULT_TOLERANCE_MM,
) -> tuple[bool, list[str]]:
    """Вердикт по целому ``state.json`` (план берётся из ``state["plan"]``)."""
    if not state:
        return False, ["нет результата исправления"]
    summary = dict(state.get("summary") or {})
    plan = state.get("plan") or {}
    summary.setdefault("plan_key", plan.get("key") if isinstance(plan, Mapping) else None)
    return bleed_only_verdict(summary, format_mm=format_mm, bleed_mm=bleed_mm, tolerance_mm=tolerance_mm)


def summary_bleed_only(state: Mapping[str, Any] | None) -> bool:
    """Сохранённый вердикт ``summary["bleed_only"]`` (``False``, если его нет)."""
    summary = (state or {}).get("summary") if isinstance(state, Mapping) else None
    return isinstance(summary, Mapping) and summary.get("bleed_only") is True


def summary_bleed_only_reasons(state: Mapping[str, Any] | None) -> list[str]:
    summary = (state or {}).get("summary") if isinstance(state, Mapping) else None
    if not isinstance(summary, Mapping):
        return []
    return [str(reason) for reason in summary.get("bleed_only_reasons") or ()]


# ----------------------------------------------------------------- контекст заказа


def profile_bleed_mm(direction: str | None) -> float:
    """Вылет профиля направления (``config/profiles.py``), мм."""
    profile = PROFILES.get(str(direction or "")) or DEFAULT_PROFILE
    return float(profile.bleed_mm)


def _parsed_format(entry: Mapping[str, Any] | None) -> tuple[float, float] | None:
    parsed = (entry or {}).get("parsed") if isinstance(entry, Mapping) else None
    if not isinstance(parsed, Mapping):
        return None
    width, height = _number(parsed.get("width_mm")), _number(parsed.get("height_mm"))
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    return width, height


def order_fix_context(
    run: Mapping[str, Any],
    order: Mapping[str, Any],
    entry: Mapping[str, Any] | None = None,
) -> tuple[tuple[float, float] | None, float]:
    """Формат (из имени файла, без вылетов) и вылет профиля для исправления.

    ``entry`` — файл заказа (исправление исходника); без него — собранный PDF заказа,
    формат берётся из имён его файлов (у всех файлов заказа он один, иначе заказ — ошибка).
    """
    direction = (run.get("options") or {}).get("direction") if isinstance(run, Mapping) else None
    bleed = profile_bleed_mm(direction or (run.get("direction") if isinstance(run, Mapping) else None))
    if entry is not None:
        return _parsed_format(entry), bleed
    formats: set[tuple[float, float]] = set()
    for item in order.get("files") or () if isinstance(order, Mapping) else ():
        parsed = _parsed_format(item)
        if parsed is not None:
            formats.add(parsed)
    return (next(iter(formats)) if len(formats) == 1 else None), bleed


def not_bleed_only_warning(reasons: Iterable[str]) -> str:
    """Предупреждение: исправление изменило не только поля — всё проверено по нему."""
    details = "; ".join(str(reason) for reason in reasons if reason) or "не подтверждено сравнением"
    return (
        "Исправление Callas изменило не только поля вылета ("
        f"{details}): все проверки выполнены по исправленному PDF"
    )


BLEED_ONLY_NOTE = (
    "Callas только дорисовал вылеты: разрешение и цвет проверены по исходному файлу "
    "клиента, размер — по исправленному PDF"
)
