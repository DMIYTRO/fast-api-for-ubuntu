"""Профили направлений допечатной подготовки."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping


#: Предел автоматической коррекции размера по умолчанию, мм обрезки.
DEFAULT_AUTO_CORRECT_MAX_MM = 4.0
#: Жёсткий максимум коррекции с подтверждением оператора, мм обрезки.
DEFAULT_CORRECTION_CONFIRM_MAX_MM = 10.0


@dataclass(frozen=True)
class PrePressProfile:
    """Набор правил одного производственного направления."""

    direction: str
    name: str
    target_dpi: float = 300.0
    min_dpi: float = 250.0
    allowed_colorspaces: tuple[str, ...] = ("CMYK", "COLORSEPARATION")
    max_file_size_mb: float = 2000.0
    safe_zone_mm: float = 4.0
    bleed_mm: float = 2.0
    size_tolerance_mm: float = 0.5
    metadata_tolerance_mm: float = 0.1
    # Предел автоматической коррекции: максимальная обрезка (мм) при
    # пропорциональном масштабировании «с покрытием» и центральной обрезке.
    # Обрезка между ``auto_crop_mm`` и ``confirm_crop_mm`` требует подтверждения
    # оператора; выше ``confirm_crop_mm`` — отклонение. Значения переопределяются
    # через ``with_correction_limit`` (IMAGE_MAGIC_AUTO_CORRECT_MAX_MM и
    # IMAGE_MAGIC_CORRECTION_CONFIRM_MAX_MM).
    auto_crop_mm: float = DEFAULT_AUTO_CORRECT_MAX_MM
    confirm_crop_mm: float = DEFAULT_CORRECTION_CONFIRM_MAX_MM
    # Пороги красочности (callas inkcoverage): минимум и максимум
    # ``total_sum_percent`` страницы — суммы СРЕДНИХ покрытий красок по рамке,
    # не TAC. Ниже минимума — «Недолив», выше максимума — «Перелив».
    # Пороги задаются производственным профилем или настройками;
    # демонстрационные 20/250 не являются производственными нормативами.
    ink_min_percent: float | None = None
    ink_max_percent: float | None = None
    allowed_color_modes: tuple[tuple[int, int], ...] = (
        (1, 0),
        (1, 1),
        (4, 0),
        (4, 4),
        (5, 0),
        (5, 5),
        (6, 0),
        (6, 6),
    )
    sheet_trim: Mapping[str, str] | None = None
    cut_lines: Mapping[str, str] | None = None
    spots: tuple[str, ...] = field(default_factory=tuple)

    def with_correction_limit(
        self, limit_mm: float, confirm_max_mm: float | None = None
    ) -> "PrePressProfile":
        """Профиль с пределом автокоррекции и максимумом коррекции с подтверждением."""
        limit = max(0.0, float(limit_mm))
        cap = self.confirm_crop_mm if confirm_max_mm is None else float(confirm_max_mm)
        return replace(self, auto_crop_mm=limit, confirm_crop_mm=max(limit, cap))

    @property
    def size_extra_mm(self) -> float:
        """Общий припуск по размеру: вылет с обеих сторон."""
        return self.bleed_mm * 2.0


PROFILES: Mapping[str, PrePressProfile] = MappingProxyType(
    {
        "digital": PrePressProfile(
            direction="digital",
            name="Цифровая печать",
            sheet_trim=MappingProxyType(
                {
                    "320x450": "314x434",
                    "330x487": "324x471",
                }
            ),
            cut_lines=MappingProxyType(
                {
                    "рез": "green 100/0/100/0",
                    "биг": "red 0/100/100/0",
                }
            ),
            spots=("White", "Clear"),
        ),
        "offset": PrePressProfile(
            direction="offset",
            name="Офсетная печать",
            sheet_trim=None,
            cut_lines=None,
            spots=(),
        ),
    }
)

DEFAULT_DIRECTION = "digital"
DEFAULT_PROFILE = PROFILES[DEFAULT_DIRECTION]


def get_profile(direction: str = DEFAULT_DIRECTION) -> PrePressProfile:
    """Возвращает профиль или понятную ошибку со списком направлений."""
    try:
        return PROFILES[direction]
    except KeyError as exc:
        available = ", ".join(PROFILES)
        raise ValueError(
            f"неизвестное направление {direction!r}; доступно: {available}"
        ) from exc
