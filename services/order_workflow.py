"""Operator order transitions independent from the HTTP transport."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
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
    confirm_failed_processing: bool = False


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
        preview_uploader: Callable[[list[Path]], list[str]] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.order_finder = order_finder
        self.lifecycle_factory = lifecycle_factory
        self.prepress_sender = prepress_sender
        self.rework_sender = rework_sender
        self.preview_uploader = preview_uploader
        self._action_locks_guard = Lock()
        self._action_locks: dict[int, tuple[Lock, str]] = {}
        self._background_guard = Lock()
        self._background_actions: set[tuple[str, str, str]] = set()
        self._background_executor: ThreadPoolExecutor | None = None

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

    @staticmethod
    def _return_preview_upload_paths(
        input_path: Path, transition: Any, preview_name: str
    ) -> list[Path]:
        """Find the previews after a return transition for one batch upload."""
        paths = [
            Path(path) for path in transition.preview_paths if Path(path).is_file()
        ]
        if preview_name and not any(path.name == preview_name for path in paths):
            collage = input_path / "Previews" / "Return" / preview_name
            if not collage.is_file():
                raise FileLifecycleError(
                    f"Не найдено превью для загрузки: {preview_name}"
                )
            paths.append(collage)
        return paths

    @staticmethod
    def _remove_uploaded_previews(paths: list[Path]) -> None:
        """Remove local copies only after the remote action is committed."""
        for path in dict.fromkeys(paths):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                # The Sborka action has already completed at this point.  A
                # cleanup failure must not make a completed return retryable.
                logger.warning(
                    "preview.local_cleanup_failed file=%s error=%s", path.name, exc
                )

    def submit(self, command: OrderActionCommand, action: str) -> dict[str, list[dict[str, Any]]]:
        """Queue an operator action and return without waiting for FTP/HTTP I/O."""
        run_key = command.run_id or "active"
        keys = {(run_key, str(order_id), action) for order_id in command.order_ids}
        with self._background_guard:
            already_queued = keys & self._background_actions
            queued = keys - already_queued
            self._background_actions.update(queued)
            if queued and self._background_executor is None:
                self._background_executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="image-magic-actions"
                )
            executor = self._background_executor

        if queued and executor is not None:
            future = executor.submit(self._run_background, command, action, queued)
            future.add_done_callback(self._log_background_failure)
        return {
            "items": [
                {
                    "order_id": str(order_id),
                    "status": "pending",
                    "message": (
                        "Действие уже выполняется в фоне."
                        if (run_key, str(order_id), action) in already_queued
                        else "Задача поставлена в очередь. Загрузка продолжается в фоне."
                    ),
                }
                for order_id in command.order_ids
            ]
        }

    def _run_background(
        self,
        command: OrderActionCommand,
        action: str,
        keys: set[tuple[str, str, str]],
    ) -> None:
        try:
            self.prepare(command, action)
        finally:
            with self._background_guard:
                self._background_actions.difference_update(keys)

    @staticmethod
    def _log_background_failure(future: Future[None]) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.error(
                "order.background_action_failed error=%s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def shutdown(self) -> None:
        """Finish queued actions during a graceful server shutdown."""
        with self._background_guard:
            executor, self._background_executor = self._background_executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

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
                pitstop = order.get("pitstop") or None
                pitstop_ready = (
                    pitstop is None
                    or (
                        pitstop.get("execution_status") == "completed"
                        and pitstop.get("verdict") in {"passed", "warning"}
                        and (
                            order.get("current_pdf_revision") is None
                            or pitstop.get("checked_revision")
                            == order.get("current_pdf_revision")
                        )
                    )
                )
                if (
                    action == "print"
                    and order.get("status")
                    not in {"accepted_for_print", "returned_for_rework"}
                    and not (
                        (
                            order.get("status") in {"passed", "warning"}
                            and pitstop_ready
                        )
                        or (
                            command.confirm_failed_processing
                            and order.get("status") == "error"
                        )
                    )
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
                            str(order.get("status", "")),
                            expected_status,
                            confirm_failed_processing=command.confirm_failed_processing,
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
                        upload_paths: list[Path] = []
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
                            if self.preview_uploader is not None:
                                upload_paths = self._return_preview_upload_paths(
                                    Path(run["options"]["input_path"]),
                                    transition,
                                    return_preview_name or "",
                                )
                                if upload_paths:
                                    self.preview_uploader(upload_paths)
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
                        if upload_paths:
                            self._remove_uploaded_previews(upload_paths)
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
