from dataclasses import dataclass
from enum import Enum


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


def analyze_resample(
    actual_mm: tuple[float, float],
    target_mm: tuple[float, float],
    dpi: tuple[float, float],
    min_dpi: float = 250.0,
    metadata_tolerance_mm: float = 0.1,
    auto_crop_mm: float = 0.5,
    confirm_crop_mm: float = 2.0,
    allow_rotation: bool = True,
) -> ResamplePlan:
    """Choose the least destructive cover/crop plan, optionally rotating by 90 degrees."""
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
        reason = "пропорциональная коррекция требует подтверждения пользователя"
    else:
        decision = ResampleDecision.REJECT
        reason = f"для приведения к размеру потребуется обрезка до {crop_max:.1f} мм"

    if rotation and decision == ResampleDecision.ACCEPT:
        decision = ResampleDecision.AUTO_CORRECT
        reason = "размер соответствует после поворота; требуется создать файл в целевой ориентации"

    return ResamplePlan(decision, target_mm, rotation, scale, crop, effective, reason)
