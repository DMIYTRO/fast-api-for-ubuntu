"""Application use cases shared by HTTP and command-line adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from processing.models import OrderCheck

from .dto import order_check_to_dto
from .repository import RunRepository


def record_completed_cli_run(
    repository: RunRepository,
    *,
    input_path: Path,
    direction: str,
    orders: list[OrderCheck],
    pdf_paths: dict[str, Path] | None = None,
) -> str:
    """Persist a CLI result using the same run aggregate as the web app."""
    run_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    pdf_paths = pdf_paths or {}
    order_values = {}
    for order in orders:
        status = (
            "error"
            if not order.passed
            else "warning"
            if order.warnings or any(item.warnings for item in order.files)
            else "passed"
        )
        dto = order_check_to_dto(
            order,
            status=status,
            pdf_path=(
                str(pdf_paths[order.aggregate_id])
                if order.aggregate_id in pdf_paths
                else str(pdf_paths[order.order_id])
                if order.order_id in pdf_paths
                else None
            ),
        )
        order_values[order.aggregate_id] = dto
    repository.create_run(
        {
            "id": run_id,
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "options": {
                "input_path": str(input_path.resolve()),
                "direction": direction,
                "source": "cli",
            },
            "created_at": now,
            "started_at": now,
            "finished_at": now,
            "cancel_requested": False,
            "error": None,
            "total_orders": len(orders),
            "processed_orders": len(orders),
            "passed_orders": sum(
                value["status"] == "passed" for value in order_values.values()
            ),
            "warning_orders": sum(
                value["status"] == "warning" for value in order_values.values()
            ),
            "failed_orders": sum(
                value["status"] == "error" for value in order_values.values()
            ),
            "waiting_orders": 0,
            "orders": order_values,
        }
    )
    repository.append_event(
        run_id,
        "run.completed",
        {"source": "cli", "total": len(orders)},
    )
    return run_id
