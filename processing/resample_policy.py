from dataclasses import dataclass
from enum import Enum

from config.profiles import (
    DEFAULT_AUTO_CORRECT_MAX_MM,
    DEFAULT_CORRECTION_CONFIRM_MAX_MM,
    DEFAULT_SHRINK_AUTO_MAX_MM,
)


class ResampleDecision(str, Enum):
    ACCEPT = "accept"
    AUTO_CORRECT = "auto_correct"
    ASK_CONFIRMATION = "ask_confirmation"
    REJECT = "reject"


@dataclass(frozen=True)
class ResamplePlan:
    decision: ResampleDecision
    target_mm: tuple[float, float]
    rotation_degrees: int
    scale: float
    crop_mm: tuple[float, float]
    effective_dpi: tuple[float, float]
    reason: str


def is_shrink(
    actual_mm: tuple[float, float], target_mm: tuple[float, float], tolerance_mm: float = 0.1
) -> bool:
    """Уменьшение: макет больше формата по обеим сторонам больше чем на погрешность.

    Сторона, равная формату с точностью до шума метаданных (605 × 400.00001 → 600 × 400),
    не превращает обрезку в уменьшение.
    """
    return min(actual_mm[i] - target_mm[i] for i in (0, 1)) > tolerance_mm


def shrink_verdict(
    actual_mm: tuple[float, float],
    target_mm: tuple[float, float],
    source_dpi: float,
    effective_dpi: float,
    min_dpi: float,
    shrink_auto_mm: float | None,
) -> tuple[ResampleDecision, str] | None:
    """Решение по автоматическому уменьшению (масштаб < 1) или ``None`` — правило не меняет план.

    Уменьшение проходит само, если макет больше формата не более чем на
    ``shrink_auto_mm`` (по большей из сторон) или уменьшение поднимает низкое разрешение
    источника (< ``min_dpi``) до ``min_dpi`` — крупный файл с неверным DPI в метаданных.
    Иначе — подтверждение оператора. У векторных страниц (DPI = inf) действует только
    первое правило. ``shrink_auto_mm=None`` выключает правило.
    """
    if shrink_auto_mm is None:
        return None
    epsilon = 0.01
    if source_dpi + epsilon < min_dpi and effective_dpi + epsilon >= min_dpi:
        return (
            ResampleDecision.AUTO_CORRECT,
            f"уменьшение поднимает разрешение {source_dpi:.0f} → {effective_dpi:.0f} DPI",
        )
    excess = max(actual_mm[i] - target_mm[i] for i in (0, 1))
    if excess > shrink_auto_mm + epsilon:
        return (
            ResampleDecision.ASK_CONFIRMATION,
            f"уменьшение на {excess:.1f} мм больше предела авто {shrink_auto_mm:.1f} мм: "
            "требуется подтверждение оператора",
        )
    return None


def analyze_resample(
    actual_mm: tuple[float, float],
    target_mm: tuple[float, float],
    dpi: tuple[float, float],
    min_dpi: float = 250.0,
    metadata_tolerance_mm: float = 0.1,
    auto_crop_mm: float = DEFAULT_AUTO_CORRECT_MAX_MM,
    confirm_crop_mm: float = DEFAULT_CORRECTION_CONFIRM_MAX_MM,
    allow_rotation: bool = True,
    shrink_auto_mm: float | None = DEFAULT_SHRINK_AUTO_MAX_MM,
) -> ResamplePlan:
    """Choose the least destructive cover/crop plan, optionally rotating by 90 degrees.

    A downscale (``scale < 1``) that the crop limits would auto-correct stays automatic
    only when the artwork exceeds the target by at most ``shrink_auto_mm`` on each side,
    or when the source resolution is below ``min_dpi`` and the shrink brings it up to
    ``min_dpi`` (a large file with wrong DPI metadata). Otherwise it needs confirmation.
    ``None`` disables this rule.
    """
    candidates = []
    for rotation in ((0, 90) if allow_rotation else (0,)):
        source = actual_mm if rotation == 0 else (actual_mm[1], actual_mm[0])
        source_dpi = dpi if rotation == 0 else (dpi[1], dpi[0])
        scale = max(target_mm[0] / source[0], target_mm[1] / source[1])
        scaled = (source[0] * scale, source[1] * scale)
        crop = (max(0.0, scaled[0] - target_mm[0]), max(0.0, scaled[1] - target_mm[1]))
        effective = (source_dpi[0] / scale, source_dpi[1] / scale)
        candidates.append((max(crop), rotation != 0, rotation, source, scale, crop, effective))

    _, _, rotation, oriented, scale, crop, effective = min(candidates)
    crop_max = max(crop)
    size_delta = max(abs(oriented[i] - target_mm[i]) for i in (0, 1))

    epsilon = 0.01
    if min(effective) + epsilon < min_dpi:
        decision = ResampleDecision.REJECT
        reason = f"после коррекции эффективное разрешение {min(effective):.0f} DPI ниже {min_dpi:.0f} DPI"
    elif size_delta <= metadata_tolerance_mm + epsilon:
        decision = ResampleDecision.ACCEPT
        reason = f"отклонение до {metadata_tolerance_mm:.1f} мм принято как погрешность метаданных"
    elif crop_max <= auto_crop_mm + epsilon:
        decision = ResampleDecision.AUTO_CORRECT
        reason = "разрешена пропорциональная коррекция с минимальной центральной обрезкой"
    elif crop_max <= confirm_crop_mm + epsilon:
        decision = ResampleDecision.ASK_CONFIRMATION
        reason = (
            f"обрезка {crop_max:.1f} мм больше предела автоматической коррекции "
            f"{auto_crop_mm:.1f} мм: требуется подтверждение оператора"
        )
    else:
        decision = ResampleDecision.REJECT
        reason = (
            f"для приведения к размеру потребуется обрезка до {crop_max:.1f} мм; "
            f"максимум коррекции {confirm_crop_mm:.1f} мм — пришлите макет нужного размера"
        )

    if decision == ResampleDecision.AUTO_CORRECT and is_shrink(oriented, target_mm, metadata_tolerance_mm):
        verdict = shrink_verdict(oriented, target_mm, min(dpi), min(effective), min_dpi, shrink_auto_mm)
        if verdict is not None:
            decision, reason = verdict

    if rotation and decision == ResampleDecision.ACCEPT:
        decision = ResampleDecision.AUTO_CORRECT
        reason = "размер соответствует после поворота; требуется создать файл в целевой ориентации"

    return ResamplePlan(decision, target_mm, rotation, scale, crop, effective, reason)


#: Способ коррекции «без масштабирования» (``FileCheck.size_fix``, ``SIZE_FIX_METHODS``).
METHOD_CANVAS = "canvas"


def canvas_margins_mm(
    actual_mm: tuple[float, float], target_mm: tuple[float, float]
) -> tuple[float, float]:
    """Белые поля холста без масштабирования, мм в сумме по каждой стороне (≥ 0)."""
    return (
        max(0.0, target_mm[0] - actual_mm[0]),
        max(0.0, target_mm[1] - actual_mm[1]),
    )


def canvas_change_text(
    actual_mm: tuple[float, float], target_mm: tuple[float, float], separator: str = ": "
) -> str:
    """Пояснение «без масштабирования: белые поля … мм, обрезка … мм» (только ненулевые части).

    ``separator`` — между «без масштабирования» и частями (в пометке ``size_fix`` — запятая).
    """
    pad = canvas_margins_mm(actual_mm, target_mm)
    crop = (
        max(0.0, actual_mm[0] - target_mm[0]),
        max(0.0, actual_mm[1] - target_mm[1]),
    )
    parts = []
    if max(pad) > 0.005:
        parts.append(f"белые поля {pad[0]:.2f}x{pad[1]:.2f} мм")
    if max(crop) > 0.005:
        parts.append(f"обрезка {crop[0]:.2f}x{crop[1]:.2f} мм")
    return "без масштабирования" + separator + (", ".join(parts) or "размер не меняется")


def analyze_canvas(
    actual_mm: tuple[float, float],
    target_mm: tuple[float, float],
    dpi: tuple[float, float],
    *,
    min_dpi: float,
    metadata_tolerance_mm: float,
    auto_crop_mm: float,
    confirm_crop_mm: float,
) -> ResamplePlan:
    """План приведения к формату БЕЗ масштабирования (метки «без масштабирования» в имени).

    Масштаб всегда 1.0, поворота нет, эффективное разрешение равно исходному. По каждой
    стороне разница ``target - actual``: положительная — белые поля (холст больше),
    отрицательная — центральная обрезка. Решение — по наибольшему модулю разницы с теми же
    пределами, что и у пропорциональной коррекции: до погрешности метаданных — принять,
    до ``auto_crop_mm`` — исправить автоматически, до ``confirm_crop_mm`` — с
    подтверждением оператора, иначе — отклонить.

    Правило уменьшения (``shrink_auto_mm``) здесь не действует: ничего не масштабируется.
    Низкое исходное DPI (< ``min_dpi``) не отклоняет план по размеру — холст не меняет
    разрешение, о низком DPI сообщает правило метаданных (``evaluate_metadata_rules``).
    ``min_dpi`` принимается для единообразия с ``analyze_resample``. Для страниц PDF
    передаётся ``dpi=(inf, inf)``.
    """
    del min_dpi  # см. docstring: DPI не участвует в решении по размеру холста
    delta = (target_mm[0] - actual_mm[0], target_mm[1] - actual_mm[1])
    crop = (max(0.0, -delta[0]), max(0.0, -delta[1]))
    amount = max(abs(delta[0]), abs(delta[1]))
    change = canvas_change_text(actual_mm, target_mm)

    epsilon = 0.01
    if amount <= metadata_tolerance_mm + epsilon:
        decision = ResampleDecision.ACCEPT
        reason = f"отклонение до {metadata_tolerance_mm:.1f} мм принято как погрешность метаданных"
    elif amount <= auto_crop_mm + epsilon:
        decision = ResampleDecision.AUTO_CORRECT
        reason = f"коррекция {change}"
    elif amount <= confirm_crop_mm + epsilon:
        decision = ResampleDecision.ASK_CONFIRMATION
        reason = (
            f"коррекция {change}; изменение {amount:.1f} мм больше предела автоматической "
            f"коррекции {auto_crop_mm:.1f} мм: требуется подтверждение оператора"
        )
    else:
        decision = ResampleDecision.REJECT
        reason = (
            f"для приведения к размеру {change}, изменение до {amount:.1f} мм; "
            f"максимум коррекции {confirm_crop_mm:.1f} мм — пришлите макет нужного размера"
        )

    return ResamplePlan(decision, target_mm, 0, 1.0, crop, (dpi[0], dpi[1]), reason)
