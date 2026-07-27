"""Operator order transitions independent from the HTTP transport."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from server.models import OrderAction, OrderResult

from .coordinator import RunCoordinator
from .file_lifecycle import FileLifecycle, FileLifecycleError


logger = logging.getLogger("image_magic.order_workflow")


@dataclass(frozen=True)
class OrderActionCommand:
    order_ids: tuple[str, ...]
    run_id: str | None = None
    comment: str | None = None


class OrderWorkflowService:
    def __init__(
        self,
        coordinator: RunCoordinator,
        session_factory: Callable[[], Session],
        order_finder: Callable[
            [str, str | None], tuple[dict[str, Any], dict[str, Any]]
        ],
        *,
        lifecycle_factory: Callable[[Path], FileLifecycle] = FileLifecycle,
    ) -> None:
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.order_finder = order_finder
        self.lifecycle_factory = lifecycle_factory

    def prepare(
        self, command: OrderActionCommand, action: str
    ) -> dict[str, list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        with self.session_factory() as session:
            for order_id in command.order_ids:
                run, order = self.order_finder(order_id, command.run_id)
                if action == "print" and not (
                    order.get("passed")
                    or order.get("status") in {"passed", "warning"}
                ):
                    results.append(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            "message": "Заказ не прошёл проверку.",
                        }
                    )
                    continue
                stored = session.scalar(
                    select(OrderResult)
                    .where(
                        OrderResult.run_id == run["id"],
                        OrderResult.order_id == order_id,
                    )
                    .order_by(desc(OrderResult.updated_at))
                )
                if stored is None:
                    results.append({"order_id": order_id, "status": "not_found"})
                    continue
                existing_action = session.scalar(
                    select(OrderAction)
                    .where(
                        OrderAction.order_result_id == stored.id,
                        OrderAction.action == action,
                        OrderAction.status == "prepared",
                    )
                    .order_by(desc(OrderAction.created_at), desc(OrderAction.id))
                )
                if existing_action is not None:
                    results.append(
                        {
                            "order_id": order_id,
                            "status": "prepared",
                            "idempotent": True,
                        }
                    )
                    continue
                action_record = OrderAction(
                    order_result_id=stored.id,
                    action=action,
                    comment=(command.comment or "").strip() or None,
                    status="pending",
                )
                session.add(action_record)
                session.commit()
                lifecycle = self.lifecycle_factory(Path(run["options"]["input_path"]))
                previous_order = deepcopy(order)
                try:
                    transition = (
                        lifecycle.accept_for_print(order)
                        if action == "print"
                        else lifecycle.return_for_rework(order)
                    )
                except FileLifecycleError as exc:
                    action_record.status = "failed"
                    session.commit()
                    results.append(
                        {
                            "order_id": order_id,
                            "status": "error",
                            "message": str(exc),
                        }
                    )
                    continue
                try:
                    next_status = (
                        "accepted_for_print"
                        if action == "print"
                        else "returned_for_rework"
                    )
                    self.coordinator.apply_file_transition(
                        run["id"],
                        order_id,
                        status=next_status,
                        source_paths=transition.source_paths,
                        pdf_path=transition.pdf_path,
                        preview_paths=transition.preview_paths,
                    )
                    action_record.status = "prepared"
                    session.commit()
                    results.append({"order_id": order_id, "status": "prepared"})
                except Exception as exc:
                    session.rollback()
                    try:
                        lifecycle.rollback(transition)
                        self.coordinator.restore_order_snapshot(
                            run["id"], order_id, previous_order
                        )
                    except Exception:
                        logger.exception(
                            "order.transition_compensation_failed run_id=%s "
                            "order_id=%s",
                            run["id"],
                            order_id,
                        )
                    action_record = session.get(OrderAction, action_record.id)
                    if action_record is not None:
                        action_record.status = "failed"
                        session.commit()
                    logger.exception(
                        "order.transition_persistence_failed run_id=%s order_id=%s",
                        run["id"],
                        order_id,
                    )
                    results.append(
                        {
                            "order_id": order_id,
                            "status": "error",
                            "message": str(exc),
                        }
                    )
        return {"items": results}
