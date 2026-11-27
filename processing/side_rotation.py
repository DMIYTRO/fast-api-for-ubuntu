"""Автоматический поворот стороны, если лицо и оборот заказа разной ориентации.

Чистые правила без ввода-вывода; применяет их ``BatchProcessor._validate_order``.

Ориентация стороны считается по её ИТОГОВОМУ размеру (после запланированной коррекции,
с учётом ``/Rotate`` страницы PDF); «квадратная» — стороны отличаются не больше чем на
``SQUARE_TOLERANCE_MM`` (как раньше в ``_orientation``). Поворачивается только на ±90°:
повернуть лицо на +D (по часовой) — то же самое, что повернуть оборот на −D, поэтому
лицо всегда поворачивается на ``+D``, оборот — на ``−D`` (D — ``IMAGE_MAGIC_SIDE_ROTATE_DEGREES``,
по умолчанию 90), и относительный поворот оборота к лицу всегда одинаков.

Какая сторона: формат из имени W×H (W≠H) — эталон, поворачивается сторона, чья
ориентация с ним не совпадает; квадратный формат в имени — эталон лицо, поворачивается
оборот. Если хотя бы одна сторона квадратная, поворот ничего не даёт (``None``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Стороны, отличающиеся не больше чем на столько мм, — квадратная сторона.
SQUARE_TOLERANCE_MM = 0.5

SQUARE = "квадратная"
LANDSCAPE = "горизонтальная"
PORTRAIT = "вертикальная"

#: Допустимые значения ``IMAGE_MAGIC_SIDE_ROTATE_DEGREES`` (поворот лица; оборот — обратный).
ALLOWED_DEGREES = (90, -90)

REASON_NAME_FORMAT = "name_format"
REASON_SQUARE_NAME = "square_name"

#: Начало ошибки заказа о повороте (``services/issue_groups.py``, код ``side_rotated``).
SIDE_ROTATION_MESSAGE_PREFIX = "Сторона "
SIDE_ROTATION_MESSAGE_TAIL = "проверьте ориентацию и совмещение лица и оборота"


def orientation(width_mm: float, height_mm: float) -> str:
    if abs(width_mm - height_mm) <= SQUARE_TOLERANCE_MM:
        return SQUARE
    return LANDSCAPE if width_mm > height_mm else PORTRAIT


def normalize_degrees(value: object, default: int = 90) -> int:
    """90 или −90 (270 → −90, −270 → 90); иное — ``default``."""
    try:
        degrees = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    degrees = ((degrees + 180) % 360) - 180  # (-180, 180]
    return degrees if degrees in ALLOWED_DEGREES else default


def format_degrees(degrees: int) -> str:
    """«+90» / «−90» (типографский минус)."""
    return f"+{degrees}" if degrees > 0 else f"−{abs(degrees)}"


def direction_text(degrees: int) -> str:
    return "по часовой стрелке" if degrees > 0 else "против часовой стрелки"


def plan_side_rotation(
    face_size: tuple[float, float],
    back_size: tuple[float, float],
    name_size: tuple[float, float],
    degrees: int = 90,
) -> dict[str, Any] | None:
    """План поворота ``{side, degrees, from, to, reason}`` или ``None``.

    ``None`` — ориентации совпадают или поворот ничего не даёт (квадратная сторона).
    """
    face = orientation(*face_size)
    back = orientation(*back_size)
    if face == back or SQUARE in (face, back):
        return None
    name = orientation(*name_size)
    if name == SQUARE:
        side, reason = "back", REASON_SQUARE_NAME
    else:
        side, reason = ("face" if face != name else "back"), REASON_NAME_FORMAT
    turn = degrees if side == "face" else -degrees
    current = face if side == "face" else back
    target = back if side == "face" else face
    return {"side": side, "degrees": turn, "from": current, "to": target, "reason": reason}


def side_rotation_message(alignment: Mapping[str, Any]) -> str:
    """Ошибка заказа для оператора (заказ красный, но с настоящим итоговым PDF)."""
    degrees = int(alignment.get("degrees") or 0)
    return (
        f"{SIDE_ROTATION_MESSAGE_PREFIX}{alignment.get('side')} автоматически повёрнута на "
        f"{format_degrees(degrees)}°: {SIDE_ROTATION_MESSAGE_TAIL}"
    )
