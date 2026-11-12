"""Versioned, JSON-safe order action feed projected from the persistent event journal.

Only recorded actions are exposed. Font/profile changes are extension action kinds,
not inferred from an absent report. Events without aggregate ids are matched only
when the order number is unambiguous within its run.
"""
from __future__ import annotations

from typing import Any, Iterable

from services.repository import RunEvent

_LABELS = {
    "order.pdf_revision": "Исправленный PDF проверен",
    "order.completed": "Проверка завершена",
    "order.correction_confirmed": "Коррекция размера подтверждена",
    "order.correction_rejected": "Коррекция размера отклонена",
    "order.file_transition": "Перемещение файлов",
    "order.file_transition_rolled_back": "Перемещение отменено",
    "order.callas_fix.started": "Исправление Callas запущено",
    "order.callas_fix.finished": "Исправление Callas завершено",
    "order.callas_fix.accepted": "Исправление Callas принято",
    "order.callas_fix.rejected": "Исправление Callas отклонено",
    "pdf.created": "PDF создан",
}


def order_actions(events: Iterable[RunEvent], order: dict[str, Any], *, legacy_unambiguous: bool) -> list[dict[str, Any]]:
    """Return stable event ids and revision references without raw paths/reports."""
    result = []
    for event in events:
        data = event.data
        aggregate = data.get("aggregate_id")
        if aggregate:
            if str(aggregate) != str(order.get("aggregate_id")):
                continue
        elif not legacy_unambiguous or str(data.get("order_id")) != str(order.get("order_id")):
            continue
        if event.type not in _LABELS and not event.type.startswith("pitstop."):
            continue
        snapshot = data.get("order") or {}
        details = {key: data[key] for key in ("plan", "decision", "file_id", "error") if data.get(key) is not None}
        if event.type == "order.completed":
            details["operations"] = [
                {"file": item.get("name"), **operation}
                for item in snapshot.get("files", [])
                for operation in item.get("processing_actions", [])
            ]
            details["tiff_flattened"] = [
                {"file": item.get("name"), "visible_layers": item.get("tiff_visible_layer_count"), "hidden_layers": item.get("tiff_hidden_layer_count")}
                for item in snapshot.get("files", []) if item.get("tiff_flattened")
            ]
        result.append({
            "id": f"{event.run_id}:{event.id}", "kind": event.type,
            "label": _LABELS.get(event.type, "Проверка PitStop"),
            "created_at": event.created_at,
            "status": data.get("status") or snapshot.get("status"),
            "pdf_revision": snapshot.get("current_pdf_revision"),
            "pdf_sha256": snapshot.get("current_pdf_sha256"),
            "details": details,
        })
    return result
