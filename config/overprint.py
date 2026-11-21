"""Данные проверки overprint итогового PDF (логика — ``services/overprint.py``)."""

from __future__ import annotations

#: Профиль callas: одно правило (overprint заливки ИЛИ обводки у любого объекта), analyze.
DEFAULT_OVERPRINT_PROFILE = "Image Magic/Overprint - any object"

#: Технические краски по умолчанию (``IMAGE_MAGIC_OVERPRINT_IGNORE_SPOTS``): overprint на них —
#: норма и статус заказа не меняет. Совпадение — ``services.overprint.is_technical_spot``.
DEFAULT_OVERPRINT_IGNORE_SPOTS: tuple[str, ...] = (
    # белила
    "White", "W", "Белый", "Белая", "Белила",
    # лак
    "Varnish", "Lack", "Лак", "UV", "Spot UV",
    # контур реза, высечка, вырубка
    "CutContour", "Cut", "Die", "Dieline", "DieCut", "Контур", "Контур реза", "Высечка", "Вырубка",
    # биговка, перфорация, фальцовка
    "Crease", "Биговка", "Perf", "Perforation", "Перфорация", "Fold", "Фальц",
)
