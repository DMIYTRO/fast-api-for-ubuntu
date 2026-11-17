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
