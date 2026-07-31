"""Operator order transitions independent from the HTTP transport."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import logging
import json
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models import OrderAction, OrderResult

from .coordinator import RunCoordinator
from .domain import validate_operator_transition
from .file_lifecycle import FileConflictError, FileLifecycle, FileLifecycleError
from .return_preview import prepare_return_preview_name


logger = logging.getLogger("image_magic.order_workflow")


@dataclass(frozen=True)
class OrderActionCommand:
    order_ids: tuple[str, ...]
    run_id: str | None = None
    comment: str | None = None
    design: bool = True
    design_cost: str = "0"
    conflict_strategy: str = "fail"


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
        prepress_sender: Callable[[str | list[str], str | None], dict[str, Any]] | None = None,
        rework_sender: Callable[[str, str, str, bool, str], dict[str, Any]] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.order_finder = order_finder
        self.lifecycle_factory = lifecycle_factory
        self.prepress_sender = prepress_sender
        self.rework_sender = rework_sender
        self._action_locks_guard = Lock()
        self._action_locks: dict[int, tuple[Lock, str]] = {}

    def _claim_action(self, order_result_id: int, action: str) -> str | None:
        """Claim an order without waiting; return the action already in progress."""
        with self._action_locks_guard:
            current = self._action_locks.get(order_result_id)
            if current is not None:
                return current[1]
            lock = Lock()
            lock.acquire()
            self._action_locks[order_result_id] = (lock, action)
            return None

    def _release_action(self, order_result_id: int) -> None:
        with self._action_locks_guard:
            current = self._action_locks.pop(order_result_id, None)
            if current is not None:
                current[0].release()

    @staticmethod
    def _busy_result(
        order_id: str, requested_action: str, active_action: str
    ) -> dict[str, Any]:
        if requested_action == active_action:
            return {
                "order_id": order_id,
                "status": "pending",
                "idempotent": True,
                "message": "Это действие над заказом уже выполняется.",
            }
        return {
            "order_id": order_id,
            "status": "conflict",
            "message": "Над заказом уже выполняется другое действие.",
        }

    @staticmethod
    def _terminal_status_for_action(action: str) -> str:
        return (
            "accepted_for_print"
            if action == "print"
            else "returned_for_rework"
        )

    @staticmethod
    def _invoke_lifecycle(method, order: dict[str, Any], conflict_strategy: str):
        try:
            return method(order, conflict_strategy=conflict_strategy)
        except TypeError as exc:
            if "conflict_strategy" not in str(exc):
                raise
            return method(order)

    def prepare(
        self, command: OrderActionCommand, action: str
    ) -> dict[str, list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        batch_prepress_result = None
        if (
            action == "print"
            and self.prepress_sender is not None
            and len(command.order_ids) > 1
        ):
            batch_prepress_result = self.prepress_sender(list(command.order_ids), None)
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
                active_action = self._claim_action(stored.id, action)
                if active_action is not None:
                    results.append(
                        self._busy_result(order_id, action, active_action)
                    )
                    continue
                try:
                    pending_action = session.scalar(
                        select(OrderAction)
                        .where(
                            OrderAction.order_result_id == stored.id,
                            OrderAction.status == "pending",
                        )
                        .order_by(desc(OrderAction.created_at), desc(OrderAction.id))
                    )
                    if pending_action is not None:
                        results.append(
                            self._busy_result(
                                order_id, action, pending_action.action
                            )
                        )
                        continue
                    expected_status = self._terminal_status_for_action(action)
                    if stored.status == "prepared" and order.get("status") == expected_status:
                        results.append(
                            {
                                "order_id": order_id,
                                "status": "prepared",
                                "idempotent": True,
                            }
                        )
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
                    if (
                        existing_action is not None
                        and order.get("status") == expected_status
                    ):
                        results.append(
                            {
                                "order_id": order_id,
                                "status": "prepared",
                                "idempotent": True,
                            }
                        )
                        continue
                    try:
                        validate_operator_transition(
                            str(order.get("status", "")), expected_status
                        )
                    except ValueError as exc:
                        results.append(
                            {
                                "order_id": order_id,
                                "status": "rejected",
                                "message": str(exc),
                            }
                        )
                        continue
                    previous_order = deepcopy(order)
                    return_preview_name = None
                    if action == "reject" and self.rework_sender is not None:
                        try:
                            return_preview_name = prepare_return_preview_name(
                                order_id,
                                input_path=Path(run["options"]["input_path"]),
                                preview_paths=previous_order.get("preview_paths"),
                                files=previous_order.get("files"),
                            )
                        except Exception as exc:
                            logger.warning(
                                "return.preview_unavailable run_id=%s order_id=%s error=%s",
                                run["id"],
                                order_id,
                                exc,
                            )
                            return_preview_name = ""
                    action_record = OrderAction(
                        order_result_id=stored.id,
                        action=action,
                        comment=(
                            None
                            if batch_prepress_result is not None
                            else (command.comment or "").strip() or None
                        ),
                        status="pending",
                    )
                    session.add(action_record)
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        pending_action = session.scalar(
                            select(OrderAction)
                            .where(
                                OrderAction.order_result_id == stored.id,
                                OrderAction.status == "pending",
                            )
                            .order_by(
                                desc(OrderAction.created_at), desc(OrderAction.id)
                            )
                        )
                        active = (
                            pending_action.action
                            if pending_action is not None
                            else "unknown"
                        )
                        results.append(self._busy_result(order_id, action, active))
                        continue
                    try:
                        lifecycle = self.lifecycle_factory(
                            Path(run["options"]["input_path"])
                        )
                        transition = self._invoke_lifecycle(
                            lifecycle.accept_for_print
                            if action == "print"
                            else lifecycle.return_for_rework,
                            order,
                            command.conflict_strategy,
                        )
                    except FileLifecycleError as exc:
                        action_record.status = "failed"
                        session.commit()
                        conflict = None
                        if isinstance(exc, FileConflictError):
                            conflict = {
                                "source_path": str(exc.source),
                                "destination_path": str(exc.destination),
                                "suggested_name": self._suggested_conflict_name(
                                    exc.destination
                                ),
                            }
                        results.append(
                            {
                                "order_id": order_id,
                                "status": "conflict"
                                if conflict is not None
                                else "error",
                                "message": str(exc),
                                **({"conflict": conflict} if conflict else {}),
                            }
                        )
                        continue
                    except Exception as exc:
                        action_record.status = "failed"
                        session.commit()
                        logger.exception(
                            "order.transition_preparation_failed run_id=%s order_id=%s",
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
                        continue
                    try:
                        self.coordinator.apply_file_transition(
                            run["id"],
                            order_id,
                            status=expected_status,
                            source_paths=transition.source_paths,
                            pdf_path=transition.pdf_path,
                            preview_paths=transition.preview_paths,
                        )
                        prepress_result = None
                        if batch_prepress_result is not None:
                            prepress_result = batch_prepress_result
                        elif action == "print" and self.prepress_sender is not None:
                            prepress_result = self.prepress_sender(
                                order_id, command.comment
                            )
                        elif action == "reject" and self.rework_sender is not None:
                            prepress_result = self.rework_sender(
                                order_id,
                                (command.comment or "").strip(),
                                return_preview_name or "",
                                command.design,
                                command.design_cost,
                            )
                        action_record.status = "prepared"
                        if prepress_result is not None:
                            action_record.cms_response_json = json.dumps(
                                prepress_result, ensure_ascii=False
                            )
                        session.commit()
                        results.append(
                            {
                                "order_id": order_id,
                                "status": "prepared",
                                **({"prepress": prepress_result} if prepress_result else {}),
                            }
                        )
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
                finally:
                    self._release_action(stored.id)
        return {"items": results}

    @staticmethod
    def _suggested_conflict_name(destination: Path) -> str:
        stem = destination.stem
        suffix = destination.suffix
        if suffix:
            return f"{stem} (новый){suffix}"
        return f"{stem} (новый)"
