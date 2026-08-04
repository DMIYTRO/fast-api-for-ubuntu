"""
Ядро визуальной разметки и генерации превью изображений.
"""

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from core.tool_runner import run_command


SUPPORTED_FOLD_TYPES = frozenset({"half-fold", "c-fold", "z-fold"})


@dataclass(frozen=True)
class FoldOverlay:
    """Validated fold-guide settings for a single preview side.

    The input to this module is intentionally small and transport agnostic so
    the Sborka integration can supply it later without making preview drawing
    depend on an API client.
    """

    fold_type: str
    count: int
    confirmed: bool = False
    side: str = "face"
    axis: str = "width"
    span_mm: float | None = None

    @classmethod
    def from_value(cls, value: "FoldOverlay | Mapping[str, object] | None") -> "FoldOverlay | None":
        if isinstance(value, cls):
            candidate = value
        elif isinstance(value, Mapping):
            try:
                candidate = cls(
                    fold_type=str(value.get("type", value.get("fold_type", ""))),
                    count=int(value.get("count", value.get("fold_count", 0))),
                    confirmed=bool(value.get("confirmed", False)),
                    side=str(value.get("side", "face")),
                    axis=str(value.get("axis", "width")),
                    span_mm=(
                        float(value["span_mm"])
                        if value.get("span_mm") is not None
                        else None
                    ),
                )
            except (TypeError, ValueError):
                return None
        else:
            return None

        if not candidate.confirmed or candidate.fold_type not in SUPPORTED_FOLD_TYPES:
            return None
        if candidate.fold_type == "half-fold":
            candidate = cls("half-fold", 1, True, candidate.side, candidate.axis, candidate.span_mm)
        elif candidate.count < 1:
            return None
        if candidate.axis not in {"width", "height"}:
            return None
        if candidate.span_mm is not None and candidate.span_mm <= 0:
            return None
        return candidate


def _panel_widths(total_px: int, overlay: FoldOverlay) -> list[float]:
    """Port the panel geometry from the falc calculator to preview pixels."""
    panels = overlay.count + 1
    if overlay.fold_type == "half-fold":
        return [total_px / 2.0, total_px / 2.0]
    if overlay.fold_type == "z-fold":
        return [total_px / panels] * panels

    # C-fold: the tucked panel is narrowed by 2 mm per nested fold.  The
    # preview does not know the physical size, so retain the falc proportions
    # by using the same 2-unit compensation relative to the full span.
    base = total_px / panels
    # ``span_mm`` is supplied from the parsed finished format when available.
    # The 100 mm fallback preserves usable guides for legacy callers that
    # cannot yet provide it, without silently opting back into the frames.
    unit = total_px / (overlay.span_mm or 100.0)
    if panels == 3:
        widths = [base - 2 * unit, base, base + 2 * unit]
    elif panels == 4:
        widths = [base - 4 * unit, base - 2 * unit, base, base + 6 * unit]
    else:
        widths = []
        deduction = 0.0
        for index in range(panels - 1):
            amount = max(0, (panels - 1 - index) * 2 - (2 if panels > 4 else 0)) * unit
            widths.append(max(10.0 * unit, base - amount))
            deduction += base - widths[-1]
        widths.append(base + deduction)

    # The back is viewed from the reverse side of the sheet, therefore the
    # tucked C-fold panel belongs at the opposite edge.
    return list(reversed(widths)) if overlay.side.casefold() == "back" else widths


def _fold_draw_commands(w_px: int, h_px: int, overlay: FoldOverlay) -> list[str]:
    span = w_px if overlay.axis == "width" else h_px
    positions: list[float] = []
    cursor = 0.0
    for panel in _panel_widths(span, overlay)[:-1]:
        cursor += panel
        positions.append(cursor)

    if overlay.axis == "width":
        return [f"line {position:.2f},0 {position:.2f},{h_px}" for position in positions]
    return [f"line 0,{position:.2f} {w_px},{position:.2f}" for position in positions]


def _fold_safe_zone_commands(
    w_px: int, h_px: int, overlay: FoldOverlay, safe_px: int
) -> list[str]:
    """Draw a separate safe zone for every folded panel, without an outer frame."""
    span = w_px if overlay.axis == "width" else h_px
    cursor = 0.0
    commands: list[str] = []
    for panel in _panel_widths(span, overlay):
        next_cursor = cursor + panel
        if overlay.axis == "width":
            if panel > safe_px * 2 and h_px > safe_px * 2:
                commands.append(
                    f"rectangle {cursor + safe_px:.2f},{safe_px} "
                    f"{next_cursor - safe_px:.2f},{h_px - safe_px}"
                )
        elif panel > safe_px * 2 and w_px > safe_px * 2:
            commands.append(
                f"rectangle {safe_px},{cursor + safe_px:.2f} "
                f"{w_px - safe_px},{next_cursor - safe_px:.2f}"
            )
        cursor = next_cursor
    return commands

def generate_preview(
    input_path: str,
    output_preview_path: str,
    dpi: float,
    w_px: int,
    h_px: int,
    safe_zone_mm: float = 4.0,
    bleed_mm: float = 1.0,
    fold_overlay: FoldOverlay | Mapping[str, object] | None = None,
) -> str:
    """Render a preview with either regular frames or confirmed fold guides."""
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("Утилита ImageMagick (`magick`) не найдена в системе.")

    os.makedirs(os.path.dirname(os.path.abspath(output_preview_path)), exist_ok=True)

    overlay = FoldOverlay.from_value(fold_overlay)

    # Расчет отступов в пикселях
    safe_px = round(safe_zone_mm * (dpi / 25.4))
    border_px = max(1, round(bleed_mm * (dpi / 25.4)))

    gx1 = safe_px
    gy1 = safe_px
    gx2 = w_px - safe_px
    gy2 = h_px - safe_px

    half_b = border_px / 2.0
    rx1 = half_b
    ry1 = half_b
    rx2 = w_px - half_b
    ry2 = h_px - half_b

    cmd = [magick_cmd, input_path]
    if overlay:
        # Fold guides intentionally replace, rather than supplement, the old
        # cut/safe-zone frames: each preview is produced in one Magick pass.
        cmd.extend([
            "-stroke", "#00ff00",
            "-strokewidth", str(max(1, round(border_px * 0.5))),
            "-fill", "none",
        ])
        for draw in _fold_safe_zone_commands(w_px, h_px, overlay, safe_px):
            cmd.extend(["-draw", draw])
        cmd.extend([
            "-stroke", "#ff00ff",
            "-strokewidth", str(max(2, border_px)),
            "-fill", "none",
        ])
        for draw in _fold_draw_commands(w_px, h_px, overlay):
            cmd.extend(["-draw", draw])
    else:
        cmd.extend([
            "-stroke", "#28a745",
            "-strokewidth", str(border_px),
            "-fill", "none",
            "-draw", f"rectangle {rx1},{ry1} {rx2},{ry2}",
            "-stroke", "#dc3545",
            "-strokewidth", str(max(2, round(border_px * 0.4))),
            "-fill", "none",
            "-draw", f"rectangle {gx1},{gy1} {gx2},{gy2}",
        ])
    cmd.append(output_preview_path)
    run_command(cmd, check=True)
    return output_preview_path
