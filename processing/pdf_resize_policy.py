"""Коррекция размера страниц PDF через QuickFix callas pdfToolbox.

Для каждой страницы выбирается наименее разрушительный способ (по порядку):

1. ``reveal`` — страница меньше формата, а фоновый объект уже заходит за её край:
   страница раздвигается (``enlarge_pages``) и скрытые вылеты становятся видимыми.
   Содержимое не масштабируется, DPI не меняется.
2. ``stretch`` — неравномерное масштабирование (``scale_pages`` ``stretch_to_fill``),
   только при малом искажении пропорций (пороги профиля ``pdf_stretch_*_pct``). При
   равном решении вытягивание предпочтительнее уменьшения и обрезки. Кандидат с
   искажением ~0 (< 0.01%) не рассматривается — это пропорциональный масштаб.
   Вытягивание, уменьшающее страницу по обеим осям, подчиняется правилу уменьшения (п. 3).
   На малых форматах редко проходит автоматически: на 94 мм обрезка 4 мм (предел авто) —
   это ~4.3% искажения; на 600 мм 1% искажения — уже 6 мм обрезки.
3. ``shrink`` — пропорциональное уменьшение (масштаб < 1) с центральной обрезкой
   (``scale_pages`` ``fit_from_outside_cut_page``). Автоматически — только если страница
   больше формата не более чем на ``shrink_auto_mm`` по каждой стороне или уменьшение
   поднимает низкое разрешение до ``min_dpi`` (``analyze_resample``); иначе — с
   подтверждением.
4. ``fill`` — те же правила, что у растра (``analyze_resample``): пропорциональное
   заполнение формата (увеличение или только обрезка) и центральная обрезка.

Пороги «авто / подтверждение / отказ» общие с растром: прирост страницы при ``reveal``
и обрезка при ``shrink``/``fill`` сравниваются с ``auto_crop_mm`` и ``confirm_crop_mm``.
После любого способа BleedBox = MediaBox, TrimBox — внутрь на вылет, CropBox = MediaBox.
JSON применяет профиль «Image Magic/Apply QuickFix config» из переменной
``quickfix_config`` (``scripts/build_quickfix_profile.py``).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from config.profiles import DEFAULT_SHRINK_AUTO_MAX_MM

from .resample_policy import ResampleDecision, analyze_resample, is_shrink, shrink_verdict

#: Имя переменной профиля, из которой QuickFix читает JSON.
QUICKFIX_VARIABLE = "quickfix_config"
#: Сервис callas принимает значение переменной не длиннее 1024 символов.
MAX_VARIABLE_LENGTH = 1024

METHOD_FILL = "fill"
METHOD_STRETCH = "stretch"
METHOD_REVEAL = "reveal"
#: Пропорциональное уменьшение (масштаб < 1); QuickFix — как у ``fill``.
METHOD_SHRINK = "shrink"
# При равном решении: показать скрытое → вытянуть (ничего не срезает) → уменьшить →
# заполнить с обрезкой.
_METHOD_ORDER = {METHOD_REVEAL: 0, METHOD_STRETCH: 1, METHOD_SHRINK: 2, METHOD_FILL: 3}

_EPSILON = 0.01
#: Вытягивание с меньшим искажением, %, — это пропорциональный масштаб, не вытягивание.
_MIN_STRETCH_DISTORTION_PCT = 0.01

_DECISION_RANK = {
    ResampleDecision.ACCEPT: 0,
    ResampleDecision.AUTO_CORRECT: 1,
    ResampleDecision.ASK_CONFIRMATION: 2,
    ResampleDecision.REJECT: 3,
}


@dataclass(frozen=True)
class PdfPagePlan:
    """Решение по одной странице PDF."""

    decision: ResampleDecision
    method: str
    #: Формат с вылетами в ориентации страницы.
    target_mm: tuple[float, float]
    #: Масштаб по ширине и высоте (у ``reveal`` — 1).
    scale: tuple[float, float]
    #: Обрезка по ширине и высоте у ``fill``; у остальных — 0.
    crop_mm: tuple[float, float]
    #: Прирост страницы с каждой стороны у ``reveal`` (по ширине, по высоте).
    enlarge_mm: tuple[float, float]
    effective_dpi: tuple[float, float]
    reason: str

    @property
    def distortion_pct(self) -> float:
        """Искажение пропорций, %: 0 у ``fill`` и ``reveal``."""
        return abs(self.scale[0] / self.scale[1] - 1.0) * 100.0


@dataclass(frozen=True)
class PdfResizeLimits:
    min_dpi: float
    metadata_tolerance_mm: float
    auto_mm: float
    confirm_mm: float
    stretch_auto_pct: float = 0.0
    stretch_confirm_pct: float = 0.0
    #: Предел автоматического уменьшения, мм (``PrePressProfile.shrink_auto_mm``).
    shrink_auto_mm: float = DEFAULT_SHRINK_AUTO_MAX_MM


def page_min_dpi(images: Iterable[Any]) -> float:
    """Наименьший эффективный DPI изображений страницы; без растров — без ограничения."""
    values = [
        float(dpi)
        for image in images
        for dpi in (getattr(image, "effective_dpi_x", None), getattr(image, "effective_dpi_y", None))
        if dpi
    ]
    return min(values) if values else math.inf


def _by_size(value_mm: float, limits: PdfResizeLimits) -> ResampleDecision:
    if value_mm <= limits.auto_mm + _EPSILON:
        return ResampleDecision.AUTO_CORRECT
    if value_mm <= limits.confirm_mm + _EPSILON:
        return ResampleDecision.ASK_CONFIRMATION
    return ResampleDecision.REJECT


def _fill_plan(actual, target, dpi, limits) -> PdfPagePlan:
    plan = analyze_resample(
        actual, target, (dpi, dpi),
        min_dpi=limits.min_dpi,
        metadata_tolerance_mm=limits.metadata_tolerance_mm,
        auto_crop_mm=limits.auto_mm,
        confirm_crop_mm=limits.confirm_mm,
        allow_rotation=False,
        shrink_auto_mm=limits.shrink_auto_mm,
    )
    method = METHOD_SHRINK if is_shrink(actual, target, limits.metadata_tolerance_mm) else METHOD_FILL
    return PdfPagePlan(
        plan.decision, method, target, (plan.scale, plan.scale), plan.crop_mm,
        (0.0, 0.0), plan.effective_dpi, plan.reason,
    )


def _reveal_plan(actual, target, dpi, hidden, limits) -> PdfPagePlan | None:
    grow = (target[0] - actual[0], target[1] - actual[1])
    if min(grow) < -limits.metadata_tolerance_mm - _EPSILON:
        return None  # страница больше формата хотя бы по одной стороне — раздвигать нечего
    side = (max(grow[0], 0.0) / 2.0, max(grow[1], 0.0) / 2.0)
    if not any(hx + _EPSILON >= side[0] and hy + _EPSILON >= side[1] for hx, hy in hidden):
        return None
    decision = _by_size(max(grow), limits)
    if decision == ResampleDecision.REJECT:
        return None
    return PdfPagePlan(
        decision, METHOD_REVEAL, target, (1.0, 1.0), (0.0, 0.0), side, (dpi, dpi),
        f"скрытые вылеты макета показаны: страница раздвинута на "
        f"{side[0]:.2f}/{side[1]:.2f} мм с каждой стороны без масштабирования",
    )


def _stretch_plan(actual, target, dpi, limits) -> PdfPagePlan | None:
    if limits.stretch_confirm_pct <= 0:
        return None
    scale = (target[0] / actual[0], target[1] / actual[1])
    distortion = abs(scale[0] / scale[1] - 1.0) * 100.0
    if distortion < _MIN_STRETCH_DISTORTION_PCT:
        return None  # пропорциональный масштаб — это shrink/fill
    effective = (dpi / scale[0], dpi / scale[1])
    if min(effective) + _EPSILON < limits.min_dpi:
        return None
    if distortion <= limits.stretch_auto_pct + 1e-6:
        decision = ResampleDecision.AUTO_CORRECT
    elif distortion <= limits.stretch_confirm_pct + 1e-6:
        decision = ResampleDecision.ASK_CONFIRMATION
    else:
        return None
    reason = (
        f"вытягивание {scale[0] * 100:.1f}% × {scale[1] * 100:.1f}%: "
        f"пропорции изменятся на {distortion:.2f}%"
    )
    if decision == ResampleDecision.AUTO_CORRECT and is_shrink(actual, target, limits.metadata_tolerance_mm):
        # Вытягивание, которое уменьшает страницу по обеим осям, — тоже уменьшение:
        # крупное без подтверждения не применяется (то же правило, что у shrink).
        verdict = shrink_verdict(
            actual, target, dpi, min(effective), limits.min_dpi, limits.shrink_auto_mm
        )
        if verdict is not None and verdict[0] == ResampleDecision.ASK_CONFIRMATION:
            decision, reason = verdict[0], f"{reason}; {verdict[1]}"
    return PdfPagePlan(
        decision, METHOD_STRETCH, target, scale, (0.0, 0.0), (0.0, 0.0), effective, reason,
    )


def plan_pdf_page_resize(
    actual_mm: tuple[float, float],
    expected_mm: tuple[float, float],
    min_page_dpi: float,
    limits: PdfResizeLimits,
    hidden_margins_mm: Iterable[tuple[float, float]] = (),
) -> PdfPagePlan:
    """Лучший план для страницы среди способов и двух ориентаций целевого формата.

    Сначала — мягчайшее решение (принять → авто → подтверждение → отказ), при равном —
    способ по порядку reveal → stretch → shrink → fill, затем меньшая обрезка.

    50×90 и 90×50 равноправны: страница не поворачивается, ориентация цели берётся
    та, что ближе к странице.
    """
    hidden = tuple(hidden_margins_mm)
    candidates: list[PdfPagePlan] = []
    for target in (expected_mm, (expected_mm[1], expected_mm[0])):
        fill = _fill_plan(actual_mm, target, min_page_dpi, limits)
        candidates.append(fill)
        if fill.decision == ResampleDecision.ACCEPT:
            continue
        reveal = _reveal_plan(actual_mm, target, min_page_dpi, hidden, limits)
        if reveal is not None:
            candidates.append(reveal)
        stretch = _stretch_plan(actual_mm, target, min_page_dpi, limits)
        if stretch is not None:
            candidates.append(stretch)
    return min(
        candidates,
        key=lambda plan: (
            _DECISION_RANK[plan.decision], _METHOD_ORDER[plan.method], max(plan.crop_mm)
        ),
    )


def worst_plan(plans: Iterable[PdfPagePlan]) -> PdfPagePlan:
    """Самое строгое решение среди страниц файла (при равенстве — с большей обрезкой)."""
    return max(plans, key=lambda plan: (_DECISION_RANK[plan.decision], max(plan.crop_mm)))


def _selector(pages: list[int], total: int) -> str:
    return "all" if len(pages) == total else ",".join(str(page) for page in pages)


def _box(which: str, offset_mm: float) -> dict[str, Any]:
    """Бокс от MediaBox со сдвигом всех краёв (отрицательный — внутрь страницы)."""
    return {
        "page_selector": "all",
        "which_box": which,
        "from_box_or_absolute": "MediaBox",
        "left": offset_mm,
        "bottom": offset_mm,
        "right": offset_mm,
        "top": offset_mm,
        "unit": "mm",
        "when": "always",
    }


def build_resize_quickfix(plans: Mapping[int, PdfPagePlan], bleed_mm: float) -> dict[str, Any]:
    """QuickFix JSON для страниц PDF по их планам.

    ``scale_pages`` получает короткую и длинную сторону и сохраняет ориентацию
    страницы; боксы задаются от MediaBox (проверено на pdfToolbox 17.0.682:
    положительный сдвиг в ``set_page_box`` — наружу), поэтому подходят и альбомным,
    и книжным страницам.
    """
    if not plans:
        raise ValueError("нет страниц для коррекции")
    edges = {tuple(sorted(plan.target_mm)) for plan in plans.values()}
    if len(edges) != 1:
        raise ValueError("страницы одного PDF приводятся к разным форматам")
    short_edge, long_edge = edges.pop()
    if short_edge - 2 * bleed_mm <= 0:
        raise ValueError(f"вылет {bleed_mm} мм больше половины формата {short_edge} мм")
    total = len(plans)

    by_mode: dict[str, list[int]] = {}
    enlarge: dict[tuple[float, float], list[int]] = {}
    for number, plan in sorted(plans.items()):
        if plan.method == METHOD_REVEAL:
            key = (round(plan.enlarge_mm[0], 3), round(plan.enlarge_mm[1], 3))
            enlarge.setdefault(key, []).append(number)
        else:
            mode = "stretch_to_fill" if plan.method == METHOD_STRETCH else "fit_from_outside_cut_page"
            by_mode.setdefault(mode, []).append(number)

    fixes: list[dict[str, Any]] = []
    if enlarge:
        instructions = []
        for (side_w, side_h), pages in enlarge.items():
            selector = _selector(pages, total)
            for edges_name, amount in (("left_and_right", side_w), ("top_and_bottom", side_h)):
                if amount > 0:
                    instructions.append({
                        "page_selector": selector, "which_edges": edges_name,
                        "enlarge_by": amount, "unit": "mm",
                    })
        if instructions:
            fixes.append({"quickfix": "enlarge_pages", "version": "1.0", "instructions": instructions})
    if by_mode:
        fixes.append({
            "quickfix": "scale_pages",
            "version": "1.0",
            "instructions": [
                {
                    "page_selector": _selector(pages, total),
                    "short_edge": round(short_edge, 3),
                    "long_edge": round(long_edge, 3),
                    "unit": "mm",
                    "page_scale_mode": mode,
                }
                for mode, pages in by_mode.items()
            ],
        })
    fixes.append({
        "quickfix": "set_page_box",
        "version": "1.0",
        "instructions": [
            _box("CropBox", 0), _box("BleedBox", 0), _box("TrimBox", -round(bleed_mm, 3)),
        ],
    })
    return {"quickfixes": fixes}


def quickfix_variable(config: dict[str, Any]) -> str:
    """Компактный JSON для переменной профиля; длиннее лимита сервиса — ошибка."""
    value = json.dumps(config, separators=(",", ":"), ensure_ascii=False)
    if len(value) > MAX_VARIABLE_LENGTH:
        raise ValueError(
            f"QuickFix JSON {len(value)} символов длиннее лимита callas {MAX_VARIABLE_LENGTH}"
        )
    return value
