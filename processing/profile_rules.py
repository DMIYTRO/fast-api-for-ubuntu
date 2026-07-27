"""Shared metadata rules used by every processing entry point."""

from __future__ import annotations

from dataclasses import dataclass, field

from config.profiles import PrePressProfile


@dataclass(frozen=True)
class MetadataRuleResult:
    errors: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def evaluate_metadata_rules(
    *,
    profile: PrePressProfile,
    size_mb: float,
    colorspace: str | None,
    dpi_x: float | None,
    dpi_y: float | None,
    correction_has_enough_dpi: bool = False,
) -> MetadataRuleResult:
    errors: list[str] = []
    warnings: list[str] = []
    if size_mb > profile.max_file_size_mb:
        errors.append(
            f"размер файла {size_mb:.2f} МБ превышает лимит "
            f"{profile.max_file_size_mb:.0f} МБ"
        )
    allowed = {value.upper() for value in profile.allowed_colorspaces}
    if (colorspace or "").upper() not in allowed:
        warnings.append(
            f"цветовая модель {colorspace or 'не определена'} не входит в профиль "
            f"{profile.name} ({', '.join(profile.allowed_colorspaces)}); "
            "файл будет сохранён без преобразования цветовой модели"
        )
    minimum_dpi = min(
        dpi_x if dpi_x is not None else 0.0,
        dpi_y if dpi_y is not None else 0.0,
    )
    if minimum_dpi < profile.min_dpi and not correction_has_enough_dpi:
        actual = (
            f"{dpi_x:.1f}x{dpi_y:.1f}"
            if dpi_x is not None and dpi_y is not None
            else "не определено"
        )
        errors.append(
            f"разрешение {actual} DPI; по обеим осям требуется не меньше "
            f"{profile.min_dpi:.0f} DPI"
        )
    return MetadataRuleResult(tuple(errors), tuple(warnings))
