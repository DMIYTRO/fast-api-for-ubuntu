"""Проверки результата исправления Callas: сравнение с оригиналом и повтор анализа.

Каждая функция получает ``(исходный PDF, результат, summary сравнения)`` и возвращает словарь
``{"kind", "level": ok|warning, "messages": [...]}``; сервис кладёт его в ``summary.post_check``
состояния исправления, оператор видит его рядом с картинками «до / после» до принятия.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from services.callas.prepress import NOT_EMBEDDED_MESSAGE, OUTLINED_MESSAGE, pages_text

PostCheck = Callable[[Path, Path, dict[str, Any]], dict[str, Any]]

#: Порог заметного изменения вида (доля площади, %): ниже — считаем внешний вид прежним.
VISIBLE_CHANGE_PERCENT = 0.5


def _changed(summary: dict[str, Any]) -> float:
    try:
        return float(summary.get("changed_percent") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _share_text(share: float) -> str:
    percent = share * 100
    return "меньше 0.1%" if 0 < percent < 0.05 else f"{percent:.1f}%"


def _result(kind: str, level: str, messages: list[str], **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "level": level, "messages": messages, **extra}


def build_post_checks(analyzer: Any) -> dict[str, PostCheck]:
    def fonts(source: Path, fixed: Path, summary: dict[str, Any]) -> dict[str, Any]:
        report = analyzer.check_fonts(fixed, "fixcheck")
        if report.get("status") != "ok":
            return _result("fonts", "warning", [f"Повторная проверка шрифтов не выполнена: {report.get('error')}"])
        count = report["count"]
        if count == 0:
            return _result("fonts", "ok", [OUTLINED_MESSAGE + ": живых шрифтов в результате нет."], remaining=0)
        messages = [f"После исправления остались шрифты: {count}."]
        if report["not_embedded"]:
            messages.append(NOT_EMBEDDED_MESSAGE)
        invisible = [item["name"] for item in report["fonts"] if item["invisible_only"]]
        if invisible:
            messages.append("Невидимый текст не переводится в кривые: " + ", ".join(invisible) + ".")
        return _result("fonts", "warning", messages, remaining=count)

    def black(source: Path, fixed: Path, summary: dict[str, Any]) -> dict[str, Any]:
        before = analyzer.check_black_solid(source, "fixcheck-before")
        after = analyzer.check_black_solid(fixed, "fixcheck-after")
        messages: list[str] = []
        level = "ok"
        if before.get("status") == "ok" and after.get("status") == "ok":
            messages.append(
                f"Чистый K100 (площадь): было {before['share'] * 100:.0f}%, стало {after['share'] * 100:.0f}%."
            )
            if after.get("fix_available", after["detected"]):
                level = "warning"
                messages.append("Плашка 100% K осталась: проверьте результат вручную.")
        else:
            level = "warning"
            messages.append("Повторная проверка плашки K100 не выполнена.")
        changed = _changed(summary)
        messages.append(f"Изменено {changed:g}% площади в сравнении с оригиналом.")
        messages.append("Проверьте мелкий текст и штрихкоды на наложении: они тоже получили составной чёрный.")
        return _result("black_to_rich", level, messages)

    def clipping(source: Path, fixed: Path, summary: dict[str, Any]) -> dict[str, Any]:
        changed = _changed(summary)
        if changed >= VISIBLE_CHANGE_PERCENT:
            return _result(
                "remove_clipping", "warning",
                [
                    f"Внешний вид изменился: {changed:g}% площади. Вероятно, проявилось содержимое, "
                    "скрытое обтравкой. Сравните «до» и «после» и подтвердите результат только если он верный."
                ],
                changed_percent=changed,
            )
        return _result(
            "remove_clipping", "ok",
            [f"Внешний вид не изменился ({changed:g}% площади): снята служебная обтравка. Подтвердите результат."],
            changed_percent=changed,
        )

    def downsample(source: Path, fixed: Path, summary: dict[str, Any]) -> dict[str, Any]:
        before = analyzer.check_high_ppi(source, "fixcheck-before")
        after = analyzer.check_high_ppi(fixed, "fixcheck-after")
        if after.get("status") != "ok":
            return _result(
                "downsample_images", "warning",
                [f"Повторная проверка разрешения не выполнена: {after.get('error')}"],
            )
        limit = int(analyzer.high_ppi_limit)
        left = after["count"]
        messages = []
        if before.get("status") == "ok":
            messages.append(f"Растров выше {limit} ppi: было {before['count']}, стало {left}.")
        else:
            messages.append(f"Растров выше {limit} ppi после исправления: {left}.")
        level = "ok"
        if left:
            level = "warning"
            messages.append(
                f"Растры выше {limit} ppi остались: стр. {pages_text(after['pages'])} "
                f"(до {after['max_ppi']:g} ppi)."
            )
        messages.append(f"Изменено {_changed(summary):g}% площади в сравнении с оригиналом.")
        messages.append("Проверьте штриховые 1-бит и мелкий текст по превью «до / после».")
        return _result("downsample_images", level, messages, remaining=left)

    def tac(source: Path, fixed: Path, summary: dict[str, Any]) -> dict[str, Any]:
        before = analyzer.check_tac(source, "fixcheck-before")
        after = analyzer.check_tac(fixed, "fixcheck-after")
        messages: list[str] = []
        level = "ok"
        extra: dict[str, Any] = {}
        if after.get("status") == "ok":
            limit = f"{after['limit']:g}"
            extra = {"max_after": after["max_percent"], "remaining_pages": after["pages"]}
            if before.get("status") == "ok":
                extra["max_before"] = before["max_percent"]
                messages.append(
                    f"Макс. суммарное покрытие: было {before['max_percent']:.0f}%, стало {after['max_percent']:.0f}%."
                )
            else:
                messages.append(f"Макс. суммарное покрытие после исправления: {after['max_percent']:.0f}%.")
            if after["detected"]:
                level = "warning"
                messages.append(
                    f"Покрытие выше {limit}% осталось: стр. {pages_text(after['pages'])} "
                    f"({_share_text(after['over_share'])} площади). Проверьте результат вручную."
                )
        else:
            level = "warning"
            messages.append(f"Повторная проверка суммарного покрытия не выполнена: {after.get('error')}")
        messages.append(
            "Цветоделения 50 ppi: значения приблизительные, мелкие объекты усредняются с фоном. "
            "Окончательный вердикт даст повторная проверка PitStop после принятия."
        )
        messages.append(f"Изменено {_changed(summary):g}% площади в сравнении с оригиналом.")
        messages.append("Проверьте цвета тёмных и насыщенных мест по превью «до / после».")
        return _result("reduce_tac", level, messages, **extra)

    return {
        "fonts_to_outlines": fonts, "black_to_rich": black, "remove_clipping": clipping,
        "downsample_images": downsample, "reduce_tac": tac,
    }
