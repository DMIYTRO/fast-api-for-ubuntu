"""Профили направлений допечатной подготовки."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class PrePressProfile:
    """Набор правил одного производственного направления."""

    direction: str
    name: str
    target_dpi: float = 300.0
    min_dpi: float = 270.0
    allowed_colorspaces: tuple[str, ...] = ("CMYK", "COLORSEPARATION")
    max_file_size_mb: float = 2000.0
    safe_zone_mm: float = 4.0
    bleed_mm: float = 2.0
    size_tolerance_mm: float = 0.5
    metadata_tolerance_mm: float = 0.1
    auto_crop_mm: float = 0.5
    confirm_crop_mm: float = 2.0
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
