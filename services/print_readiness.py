"""Чего не хватает заказу для отправки в печать: файла, проверки или согласования.

Печать открыта для зелёных (passed) и жёлтых (warning) заказов. Красный (error) заказ
можно отправить только с явным подтверждением оператора и только если у него есть готовый
итоговый PDF текущей ревизии с завершённой проверкой PitStop.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

FILE = "file"
CHECK = "check"
APPROVAL = "approval"
# Не блокирует, а требует явного подтверждения оператора (печать красного заказа).
CONFIRM = "confirm"

PRINTABLE_STATUSES = {"passed", "warning"}
TERMINAL_STATUSES = {"accepted_for_print", "returned_for_rework"}


def pitstop_ready(order: dict[str, Any]) -> bool:
    """PitStop не нужен (не запускался) либо завершён для текущей ревизии PDF.

    Вердикт не важен: он уже учтён в статусе заказа (жёлтый/красный).
    """
    pitstop = order.get("pitstop") or None
    if pitstop is None:
        return True
    if pitstop.get("execution_status") != "completed":
        return False
    revision = order.get("current_pdf_revision")
    return revision is None or pitstop.get("checked_revision") == revision


def print_blockers(order: dict[str, Any], fix_state: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Список причин блокировки «В печать»; пустой список — заказ готов.

    Каждая причина: ``kind`` (file | check | approval), ``code`` и текст для оператора.
    """
    status = order.get("status")
    if status in TERMINAL_STATUSES:
        return []
    blockers: list[dict[str, str]] = []

    def add(kind: str, code: str, message: str) -> None:
        blockers.append({"kind": kind, "code": code, "message": message})

    pdf_path = order.get("pdf_path")
    if pdf_path and not Path(str(pdf_path)).is_file():
        add(FILE, "pdf_missing", f"Файл PDF не найден: {pdf_path}. Запустите проверку или исправление заново.")
    if status == "error":
        if not pdf_path or order.get("pdf_kind") == "diagnostic":
            add(
                FILE, "no_final_pdf",
                "Готового итогового PDF нет: исправьте макет или запустите исправление PDF и повторите проверку. "
                "Без готового PDF заказ с ошибками отправить в печать нельзя.",
            )
        elif order.get("current_pdf_revision") is None:
            add(FILE, "no_pdf_revision", "У PDF нет проверенной ревизии: запустите проверку заново.")
        elif order.get("pitstop") is None and order.get("pitstop_skipped_reason"):
            add(CHECK, "pitstop_not_run", "Проверка PitStop не выполнялась: запустите проверку заново.")
    elif status not in PRINTABLE_STATUSES:
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
        if pitstop.get("execution_status") != "completed":
            add(CHECK, "pitstop_not_passed", "Проверка PitStop не завершена: дождитесь результата или запустите проверку заново.")
        elif (
            order.get("current_pdf_revision") is not None
            and pitstop.get("checked_revision") != order.get("current_pdf_revision")
        ):
            add(CHECK, "pitstop_stale", "Нет проверки PitStop для текущей версии PDF: запустите проверку заново.")
    if status == "error" and not blockers:
        add(
            CONFIRM, "forced_print_confirmation",
            "Заказ с ошибками. Отправить в печать можно только по подтверждению оператора под его ответственность.",
        )
    return blockers


def print_mode(blockers: list[dict[str, str]]) -> str:
    """normal: печать без вопросов; forced: нужно подтверждение; blocked: печать невозможна."""
    if not blockers:
        return "normal"
    if all(item["kind"] == CONFIRM for item in blockers):
        return "forced"
    return "blocked"


def is_forced_print(order: dict[str, Any], fix_state: dict[str, Any] | None = None) -> bool:
    """Красный заказ, который можно отправить только с подтверждением оператора."""
    return print_mode(print_blockers(order, fix_state)) == "forced"
