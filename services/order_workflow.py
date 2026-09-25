"""Operator order transitions independent from the HTTP transport."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import hashlib
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
from .return_preview import custom_return_preview_path, prepare_return_preview_name
from .layered_tiff_print import build_reviewed_layered_print_pdf
from core.pdf_inspector import inspect_pdf


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
        reviewed_pdf_pitstop_service_factory: Callable[[Path, str], Any] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.order_finder = order_finder
        self.lifecycle_factory = lifecycle_factory
        self.prepress_sender = prepress_sender
        self.rework_sender = rework_sender
        self.preview_uploader = preview_uploader
        self.reviewed_pdf_pitstop_service_factory = reviewed_pdf_pitstop_service_factory
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
        input_path: Path, transition: Any, preview_name: str, order_id: str
    ) -> list[Path]:
        """Find the previews after a return transition for one batch upload."""
        paths = [Path(path) for path in transition.preview_paths if Path(path).is_file()]
        matching = [path for path in paths if path.name == preview_name]
        if matching:
            return matching
        if preview_name:
            custom_preview = custom_return_preview_path(
                order_id, input_path=input_path
            )
            collage = input_path / "Previews" / "Return" / preview_name
            if custom_preview is not None and custom_preview.name == preview_name:
                return [custom_preview]
            elif not collage.is_file():
                raise FileLifecycleError(
                    f"Не найдено превью для загрузки: {preview_name}"
                )
            else:
                return [collage]
        raise FileLifecycleError("Не сформировано превью для возврата в Sborka.")

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
        current_identity = {"order_id": "", "aggregate_id": "", "include_aggregate_id": False}

        def add_result(item: dict[str, Any]) -> None:
            item.setdefault("order_id", current_identity["order_id"])
            if current_identity["include_aggregate_id"]:
                item.setdefault("aggregate_id", current_identity["aggregate_id"])
            results.append(item)

        batch_prepress_result = None
        resolved_orders = [
            (order_ref, *self.order_finder(order_ref, command.run_id))
            for order_ref in command.order_ids
        ]
        external_order_ids = [
            str(order.get("order_id") or order.get("id") or order_ref)
            for order_ref, _run, order in resolved_orders
        ]
        ambiguous_external_refs: set[tuple[str, str]] = set()
        for order_ref, run, order in resolved_orders:
            real_id = str(order.get("order_id") or order.get("id") or order_ref)
            run_orders = run.get("orders") or {}
            order_values = run_orders.values() if isinstance(run_orders, dict) else run_orders
            matching_order_identities = [
                str(candidate.get("aggregate_id") or candidate.get("order_id") or candidate.get("id") or "")
                for candidate in order_values
                if str(candidate.get("order_id") or candidate.get("id") or "") == real_id
            ]
            if len(set(matching_order_identities)) > 1:
                ambiguous_external_refs.add(
                    (str(run.get("id") or ""), str(order.get("aggregate_id") or order_ref))
                )
        selected_identities: dict[str, set[tuple[str, str]]] = {}
        for order_ref, run, order in resolved_orders:
            real_id = str(order.get("order_id") or order.get("id") or order_ref)
            selected_identities.setdefault(real_id, set()).add(
                (str(run.get("id") or ""), str(order.get("customer_id") or ""))
            )
        duplicate_external_ids = {
            order_id
            for order_id, identities in selected_identities.items()
            if len(identities) > 1
        }
        for order_ref, run, order in resolved_orders:
            real_id = str(order.get("order_id") or order.get("id") or order_ref)
            if real_id in duplicate_external_ids:
                ambiguous_external_refs.add(
                    (str(run.get("id") or ""), str(order.get("aggregate_id") or order_ref))
                )
        sends_to_external_service = (
            (action == "print" and self.prepress_sender is not None)
            or (action == "reject" and self.rework_sender is not None)
        )
        has_reviewed_layered_files = any(
            file.get("has_unflattened_layers")
            and str(file.get("path") or "").lower().endswith((".tif", ".tiff"))
            for _, _, order in resolved_orders
            for file in order.get("files") or []
        )
        if (
            action == "print"
            and self.prepress_sender is not None
            and len(external_order_ids) > 1
            and not duplicate_external_ids
            and not has_reviewed_layered_files
        ):
            batch_prepress_result = self.prepress_sender(external_order_ids, None)
        with self.session_factory() as session:
            for order_ref, run, order in resolved_orders:
                order_id = str(order.get("order_id") or order.get("id") or order_ref)
                aggregate_id = str(order.get("aggregate_id") or order_ref)
                customer_id = order.get("customer_id")
                if customer_id is not None:
                    customer_id = str(customer_id)
                current_identity["order_id"] = order_id
                current_identity["aggregate_id"] = aggregate_id
                current_identity["include_aggregate_id"] = order_ref == aggregate_id and aggregate_id != order_id
                if sends_to_external_service and (str(run.get("id") or ""), aggregate_id) in ambiguous_external_refs:
                    add_result(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            "code": "ambiguous_external_order_id",
                            "message": (
                                "Внешняя система не различает клиентов с одинаковым "
                                "номером заказа; действие не выполнено."
                            ),
                        }
                    )
                    continue
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
                    add_result(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            "message": "Заказ не прошёл проверку.",
                        }
                    )
                    continue
                stored_query = select(OrderResult).where(
                    OrderResult.run_id == run["id"],
                    OrderResult.order_id == order_id,
                    OrderResult.customer_id == customer_id,
                )
                stored = session.scalar(
                    stored_query.order_by(desc(OrderResult.updated_at))
                )
                if stored is None and customer_id is not None:
                    legacy_candidates = session.scalars(
                        select(OrderResult).where(
                            OrderResult.run_id == run["id"],
                            OrderResult.order_id == order_id,
                        )
                    ).all()
                    if (
                        len(legacy_candidates) == 1
                        and legacy_candidates[0].customer_id is None
                    ):
                        stored = legacy_candidates[0]
                if stored is None:
                    add_result({"order_id": order_id, "status": "not_found"})
                    continue
                active_action = self._claim_action(stored.id, action)
                if active_action is not None:
                    add_result(
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
                        add_result(
                            self._busy_result(
                                order_id, action, pending_action.action
                            )
                        )
                        continue
                    expected_status = self._terminal_status_for_action(action)
                    if stored.status == "prepared" and order.get("status") == expected_status:
                        add_result(
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
                        add_result(
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
                        add_result(
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
                            add_result(
                                {
                                    "order_id": order_id,
                                    "status": "error",
                                    "message": str(exc),
                                }
                            )
                            continue
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
                        add_result(self._busy_result(order_id, action, active))
                        continue
                    try:
                        transition_order = order
                        reviewed_pitstop = None
                        reviewed_revision = None
                        reviewed_sha256 = None
                        if action == "print":
                            input_root = Path(run["options"]["input_path"]).resolve()
                            reviewed_pdf = build_reviewed_layered_print_pdf(
                                order, input_root
                            )
                            if reviewed_pdf is not None:
                                inspection = inspect_pdf(reviewed_pdf)
                                if inspection.errors:
                                    raise FileLifecycleError(
                                        "Итоговый PDF из просмотренных TIFF не прошёл проверку: "
                                        + "; ".join(inspection.errors)
                                    )
                                reviewed_revision = int(order.get("current_pdf_revision") or 0) + 1
                                reviewed_sha256 = hashlib.sha256(reviewed_pdf.read_bytes()).hexdigest()
                                if self.reviewed_pdf_pitstop_service_factory is not None:
                                    pitstop_service = self.reviewed_pdf_pitstop_service_factory(
                                        input_root, str(run["options"].get("direction") or "")
                                    )
                                    pitstop_result = pitstop_service.check_pdf(
                                        reviewed_pdf,
                                        profile_id=str(run["options"].get("direction") or ""),
                                    )
                                    from .batch_adapter import _pitstop_result_to_dto

                                    reviewed_pitstop = _pitstop_result_to_dto(
                                        pitstop_result,
                                        checked_revision=reviewed_revision,
                                    )
                                    if not pitstop_result.passed:
                                        reason = pitstop_result.technical_error or (
                                            "PDF содержит ошибки по результатам PitStop."
                                        )
                                        raise FileLifecycleError(
                                            f"Итоговый PDF из просмотренных TIFF не отправлен в печать: {reason}"
                                        )
                                elif order.get("pitstop"):
                                    raise FileLifecycleError(
                                        "Для повторной проверки итогового PDF не настроен PitStop."
                                    )
                                transition_order = {**order, "pdf_path": str(reviewed_pdf)}
                        lifecycle = self.lifecycle_factory(
                            Path(run["options"]["input_path"])
                        )
                        transition = self._invoke_lifecycle(
                            lifecycle.accept_for_print
                            if action == "print"
                            else lifecycle.return_for_rework,
                            transition_order,
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
                        add_result(
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
                        add_result(
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
                            aggregate_id,
                            status=expected_status,
                            source_paths=transition.source_paths,
                            pdf_path=transition.pdf_path,
                            preview_paths=transition.preview_paths,
                            pitstop=reviewed_pitstop,
                            current_pdf_revision=reviewed_revision,
                            current_pdf_sha256=reviewed_sha256,
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
                                    order_id,
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
                        add_result(
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
                                run["id"], aggregate_id, previous_order
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
                        add_result(
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
