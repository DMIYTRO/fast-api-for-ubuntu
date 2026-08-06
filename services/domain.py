"""Typed domain vocabulary and transition rules for processing runs."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, TypedDict


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrderStatus(StrEnum):
    DETECTED = "detected"
    PROCESSING = "processing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    CORRECTION_CONFIRMED = "correction_confirmed"
    CORRECTION_REJECTED = "correction_rejected"
    PASSED = "passed"
    WARNING = "warning"
    ERROR = "error"
    CANCELLED = "cancelled"
    ACCEPTED_FOR_PRINT = "accepted_for_print"
    RETURNED_FOR_REWORK = "returned_for_rework"


class OrderData(TypedDict, total=False):
    aggregate_id: str
    order_id: str
    customer_id: str
    status: str
    passed: bool
    files: list[dict[str, Any]]
    errors: list[str]
    warnings: list[str]
    pdf_path: str | None
    preview_paths: list[str]
    postpress: dict[str, Any]


class RunData(TypedDict, total=False):
    id: str
    status: str
    stage: str
    progress: int | float
    options: dict[str, Any]
    orders: dict[str, OrderData]


_OPERATOR_TRANSITIONS = {
    OrderStatus.PASSED: {
        OrderStatus.ACCEPTED_FOR_PRINT,
        OrderStatus.RETURNED_FOR_REWORK,
    },
    OrderStatus.WARNING: {
        OrderStatus.ACCEPTED_FOR_PRINT,
        OrderStatus.RETURNED_FOR_REWORK,
    },
    OrderStatus.ERROR: {OrderStatus.RETURNED_FOR_REWORK},
    OrderStatus.ACCEPTED_FOR_PRINT: {OrderStatus.RETURNED_FOR_REWORK},
}


def validate_operator_transition(
    current: str, target: str, *, confirm_failed_processing: bool = False
) -> None:
    """Reject state changes that cannot be initiated by an operator."""
    try:
        current_status = OrderStatus(current)
        target_status = OrderStatus(target)
    except ValueError as exc:
        raise ValueError(f"неизвестный статус перехода: {current} → {target}") from exc
    # An operator may deliberately take responsibility for an unsuccessfully
    # checked layout, but only through the explicit confirmed workflow.
    if (
        confirm_failed_processing
        and current_status is OrderStatus.ERROR
        and target_status is OrderStatus.ACCEPTED_FOR_PRINT
    ):
        return
    if target_status not in _OPERATOR_TRANSITIONS.get(current_status, set()):
        raise ValueError(
            f"недопустимый переход заказа: {current_status.value} → "
            f"{target_status.value}"
        )
