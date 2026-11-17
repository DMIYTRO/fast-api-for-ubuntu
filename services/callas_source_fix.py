"""Принятое исправление Callas исходного PDF заказа, который ждёт решения по коррекции.

Заказ с отклонением размера останавливается в ``waiting_confirmation`` ещё до сборки
PDF. Оператор может исправить каждый исходный PDF планом Callas (``PDF/CallasFix/
<имя>.source/fixed.pdf``) и принять результат по каждой стороне отдельно:

* принятие копирует ``fixed.pdf`` в ``accepted.pdf`` и сразу проверяет файл заново по
  этой копии (``RunCoordinator.apply_source_fix``): все обычные правила PDF действуют,
  исходный файл не меняется, имя/сторона/перемещения остаются за исходником;
* если после этого в заказе не осталось файлов, ждущих решения, заказ продолжается
  сам (тот же путь, что «Применить»); иначе «Применить» / «Оставить без изменений»
  относятся только к оставшимся файлам;
* если исходник изменился после исправления, исправление не используется (проверка
  при сборке, ``BatchProcessorAdapter``), файл проверяется заново с предупреждением.

Здесь — тексты и правила, общие для HTTP-ручек, координатора и адаптера.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from processing.models import FileCheck

from .callas_fix import STATUS_ACCEPTED, STATUS_READY, comparison_payload

SIZE_PLAN_KEY = "size_correction"

# Заказ ещё идёт: только такие проверки могут продолжить ожидающий заказ.
LIVE_RUN_STATUSES = frozenset({"running", "waiting_confirmation"})

STALE_SOURCE_WARNING = (
    "Исходный файл изменился после принятия исправления Callas: исправленный PDF "
    "не использован, файл проверен заново"
)
NOT_ACCEPTABLE_MESSAGE = (
    "У заказа нет собранного PDF: исправленный PDF можно скачать и проверить заново, "
    "принять его в печать из этого блока нельзя."
)
# Решение по исходному файлу и решение «Применить» для заказа пришли одновременно.
ORDER_CHANGED_MESSAGE = (
    "Заказ изменился, пока проверялся исправленный PDF (решение уже принято или "
    "проверка завершена). Обновите страницу."
)


def applied_note(fix: Mapping[str, Any]) -> str:
    """Предупреждение на файле, собранном из принятого исправления Callas."""
    if fix.get("plan") == SIZE_PLAN_KEY:
        return "Размер исправлен Callas (поля дорисованы), использован исправленный PDF"
    title = str(fix.get("plan_title") or fix.get("plan") or "исправление Callas")
    return f"Исправлено Callas («{title}»), использован исправленный PDF"


def mark_applied(check: FileCheck, fix: Mapping[str, Any]) -> FileCheck:
    """Отметить заново проверенный по ``check.content_path`` файл принятым исправлением."""
    check.callas_source_fix = dict(fix)
    check.warnings.insert(0, applied_note(fix))
    return check


def order_accepts_source_fix(run: Mapping[str, Any], order: Mapping[str, Any]) -> bool:
    """Принять исправление исходника можно, пока заказ ждёт решения и PDF не собран."""
    return (
        str(run.get("status") or "") in LIVE_RUN_STATUSES
        and order.get("status") == "waiting_confirmation"
        and not order.get("pdf_path")
    )


def source_entry_for(order: Mapping[str, Any], source: Path | str) -> dict[str, Any] | None:
    """Запись файла заказа для исходного PDF ``source`` (пути сравниваются после resolve)."""
    target = Path(source).resolve()
    for entry in order.get("files") or []:
        raw = entry.get("path") or entry.get("source_path")
        if raw and Path(str(raw)).resolve() == target:
            return entry
    return None


def entry_applied(entry: Mapping[str, Any] | None) -> bool:
    fix = (entry or {}).get("callas_source_fix")
    return isinstance(fix, Mapping) and fix.get("status") == STATUS_ACCEPTED


def source_comparison(
    state: dict[str, Any] | None,
    base_url: str,
    run: Mapping[str, Any],
    order: Mapping[str, Any],
    entry: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Контракт ``comparison`` исходного PDF с точки зрения этого заказа.

    ``applied`` — заказ собирается из принятого исправления. Принятое в другой
    проверке (или до повторной проверки) исправление этим заказом не используется,
    поэтому для него показывается «ждёт решения», и его можно принять снова.
    """
    if not state:
        return None
    state_status = state.get("status")
    applied = state_status == STATUS_ACCEPTED and entry_applied(entry)
    status = STATUS_READY if state_status == STATUS_ACCEPTED and not applied else None
    can_accept = (
        not applied
        and state_status in {STATUS_READY, STATUS_ACCEPTED}
        and bool(state.get("fixed_pdf"))
        and order_accepts_source_fix(run, order)
    )
    payload = comparison_payload(
        state, base_url, source_only=True, can_accept=can_accept, applied=applied, status=status,
    )
    if payload is not None and status is not None:
        payload["decision"] = None
    return payload
