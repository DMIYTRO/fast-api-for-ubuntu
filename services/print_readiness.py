"""Чего не хватает заказу для отправки в печать: файла, проверки или согласования."""
from __future__ import annotations

from pathlib import Path
from typing import Any

FILE = "file"
CHECK = "check"
APPROVAL = "approval"


def print_blockers(order: dict[str, Any], fix_state: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Список причин блокировки «В печать»; пустой список — заказ готов.

    Каждая причина: ``kind`` (file | check | approval), ``code`` и текст для оператора.
    """
    if order.get("status") in {"accepted_for_print", "returned_for_rework"}:
        return []
    blockers: list[dict[str, str]] = []

    def add(kind: str, code: str, message: str) -> None:
        blockers.append({"kind": kind, "code": code, "message": message})

    pdf_path = order.get("pdf_path")
    if pdf_path and not Path(str(pdf_path)).is_file():
        add(FILE, "pdf_missing", f"Файл PDF не найден: {pdf_path}. Запустите проверку или исправление заново.")
    if order.get("status") not in {"passed", "warning"}:
        add(CHECK, "check_failed", "Заказ не прошёл проверку: в печать отправляются только заказы со статусом «прошёл» или «предупреждение».")
    if int(order.get("pending_confirmations") or 0) > 0:
        add(APPROVAL, "correction_confirmation_pending", "Ожидается подтверждение коррекции размера оператором.")
    fix_status = (fix_state or {}).get("status")
    if fix_status == "ready":
        add(APPROVAL, "callas_fix_pending", "Исправление Callas готово и ждёт решения оператора: примите или отклоните его.")
    elif fix_status == "processing":
        add(CHECK, "callas_fix_running", "Исправление Callas ещё выполняется.")
    pitstop = order.get("pitstop")
    if pitstop is not None:
        if pitstop.get("execution_status") != "completed" or pitstop.get("verdict") not in {"passed", "warning"}:
            add(CHECK, "pitstop_not_passed", "Проверка PitStop не завершена или не пройдена.")
        elif (
            order.get("current_pdf_revision") is not None
            and pitstop.get("checked_revision") != order.get("current_pdf_revision")
        ):
            add(CHECK, "pitstop_stale", "Нет проверки PitStop для текущей версии PDF: запустите проверку заново.")
    return blockers
