"""Проверки результата исправления Callas: сравнение с оригиналом и повтор анализа.

Каждая функция получает ``(исходный PDF, результат, summary сравнения)`` и возвращает словарь
``{"kind", "level": ok|warning, "messages": [...]}``; сервис кладёт его в ``summary.post_check``
состояния исправления, оператор видит его рядом с картинками «до / после» до принятия.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from services.callas.prepress import NOT_EMBEDDED_MESSAGE, OUTLINED_MESSAGE

PostCheck = Callable[[Path, Path, dict[str, Any]], dict[str, Any]]

#: Порог заметного изменения вида (доля площади, %): ниже — считаем внешний вид прежним.
VISIBLE_CHANGE_PERCENT = 0.5


def _changed(summary: dict[str, Any]) -> float:
    try:
        return float(summary.get("changed_percent") or 0.0)
    except (TypeError, ValueError):
        return 0.0


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
            if after["detected"]:
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

    return {"fonts_to_outlines": fonts, "black_to_rich": black, "remove_clipping": clipping}
