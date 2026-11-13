"""Автоматические допечатные проверки итогового PDF через callas (без побочных эффектов).

* шрифты: профиль «Image Magic/Warn Fonts» (analyze); считаем уникальные шрифты
  (тип, имя, встроен ли), а не срабатывания по текстовым объектам;
* разрешение: стандартный профиль «List color and grayscale images not within 250 to 450 ppi»,
  фильтр max(v_ppi, h_ppi) > предела (500) с допуском 0.5; однобитные растры отдельно;
* плашка 100% K: цветоделения ``sep_process`` и доля площади (см. ``black_solid``).

Результат — словарь для заказа (``order.prepress``): у каждой проверки ``status`` (``ok`` или
``failed`` с ``error``) и готовое сообщение оператору ``message``. Сбой проверки заказ не роняет.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services.callas.analysis import run_analysis
from services.callas.black_solid import detect_black_solid
from services.callas.prepress_reports import fonts_report, high_ppi_report
from services.issue_groups import plural_ru

logger = logging.getLogger("image_magic.callas")

WARN_FONTS_PROFILE = "Image Magic/Warn Fonts"
PPI_PROFILE = "PDF analysis/List color and grayscale images not within 250 to 450 ppi"

NOT_EMBEDDED_MESSAGE = "Шрифты не встроены, перевод невозможен, нужна проверка."
OUTLINED_MESSAGE = "Шрифты переведены в кривые"


def pages_text(pages: list[int]) -> str:
    return ", ".join(str(page) for page in pages)


def _failed(exc: Exception) -> dict[str, Any]:
    return {"status": "failed", "error": str(exc).strip() or exc.__class__.__name__}


class PrepressAnalyzer:
    def __init__(
        self,
        client: Any,
        *,
        high_ppi_limit: float = 500.0,
        black_solid_threshold: float = 0.25,
    ) -> None:
        self.client = client
        self.high_ppi_limit = high_ppi_limit
        self.black_solid_threshold = black_solid_threshold

    # -------------------------------------------------------------- fonts
    def check_fonts(self, pdf: Path | str, label: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            _result, root = run_analysis(self.client, pdf, WARN_FONTS_PROFILE, f"fonts-{label}")
            report = fonts_report(root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prepress.fonts_failed label=%s error=%s", label, exc)
            return _failed(exc)
        count, missing = report["count"], report["not_embedded"]
        before = previous or {}
        had_fonts = bool(before.get("count")) or bool(before.get("converted"))
        converted = count == 0 and had_fonts
        if count == 0:
            message = OUTLINED_MESSAGE if converted else None
        else:
            names = ", ".join(f"{item['name']} ({item['type']}{'' if item['embedded'] is not False else ', не встроен'})" for item in report["fonts"])
            message = f"Шрифтов: {count}: {names}."
            if missing:
                message += " " + NOT_EMBEDDED_MESSAGE
        return {"status": "ok", **report, "converted": converted, "message": message}

    # -------------------------------------------------------------- ppi
    def check_high_ppi(self, pdf: Path | str, label: str) -> dict[str, Any]:
        try:
            _result, root = run_analysis(self.client, pdf, PPI_PROFILE, f"ppi-{label}")
            report = high_ppi_report(root, self.high_ppi_limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prepress.ppi_failed label=%s error=%s", label, exc)
            return _failed(exc)
        limit = int(self.high_ppi_limit)
        count = report["count"]
        message = None
        if count:
            message = (
                f"Растры выше {limit} ppi: {count} {plural_ru(count, 'объект', 'объекта', 'объектов')}, "
                f"стр. {pages_text(report['pages'])} (до {report['max_ppi']:g} ppi). "
                "Автоматического снижения разрешения нет: проверьте макет."
            )
        bits = report["one_bit"]
        bit_message = None
        if bits["count"]:
            n = bits["count"]
            bit_message = (
                f"Однобитные растры выше {limit} ppi: {n} {plural_ru(n, 'объект', 'объекта', 'объектов')}, "
                f"стр. {pages_text(bits['pages'])}: для штриха это обычно норма, печать не блокируется."
            )
        return {"status": "ok", **report, "message": message, "one_bit_message": bit_message}

    # ------------------------------------------------------------- black
    def check_black_solid(self, pdf: Path | str, label: str) -> dict[str, Any]:
        if self.black_solid_threshold <= 0:
            return {"status": "skipped"}
        try:
            result = detect_black_solid(self.client, pdf, self.black_solid_threshold, f"k100-{label}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("prepress.black_solid_failed label=%s error=%s", label, exc)
            return _failed(exc)
        message = None
        if result["detected"]:
            message = (
                f"Плашка 100% K: {result['share'] * 100:.0f}% площади, стр. {pages_text(result['pages'])}. "
                "Можно перевести в составной чёрный (C60 M40 Y40 K100)."
            )
        elif result["manual_check"]:
            message = (
                f"Крупное чёрное поле, стр. {pages_text(result['manual_pages'])}, не является чистым K100 "
                "(составной или растровый чёрный): автоматический перевод не применим, "
                "при необходимости скорректируйте вручную."
            )
        return {**result, "message": message}

    # -------------------------------------------------------------- all
    def __call__(self, pdf: Path | str, label: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        previous = previous or {}
        return {
            "fonts": self.check_fonts(pdf, label, previous.get("fonts")),
            "high_ppi": self.check_high_ppi(pdf, label),
            "black_solid": self.check_black_solid(pdf, label),
        }
