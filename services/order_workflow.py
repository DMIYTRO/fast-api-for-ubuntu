"""Operator order transitions independent from the HTTP transport."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib

import pymupdf
import logging
import json
import os
import shutil
import tempfile
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
from .pdf_routing import completed_status, pdf_root_for, route_pdf
from .pdf_revision_validation import readable_pdf, validate_pdf_revision
from .return_preview import custom_return_preview_path, prepare_return_preview_name
from .layered_tiff_print import (
    ReviewedLayeredPdfError,
    build_reviewed_layered_print_pdf,
)


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
        pdf_merger: Callable[[Any, Path, str], None] | None = None,
        pdf_page_extractor: Callable[[Path, int, Path, str], None] | None = None,
        accepted_pdf_provider: Callable[[Path], Path | None] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.order_finder = order_finder
        self.lifecycle_factory = lifecycle_factory
        self.prepress_sender = prepress_sender
        self.rework_sender = rework_sender
        self.preview_uploader = preview_uploader
        self.reviewed_pdf_pitstop_service_factory = reviewed_pdf_pitstop_service_factory
        self.pdf_merger = pdf_merger
        self.pdf_page_extractor = pdf_page_extractor
        self.accepted_pdf_provider = accepted_pdf_provider
        self._action_locks_guard = Lock()
        self._action_locks: dict[int, tuple[Lock, str]] = {}
        self._background_guard = Lock()
        self._background_actions: set[tuple[str, str, str]] = set()
        self._background_executor: ThreadPoolExecutor | None = None

    @contextmanager
    def correction_guard(self, run_id: str, order: dict[str, Any]):
        """Serialize correction acceptance with print/return for the same order."""
        with self.session_factory() as session:
            query = select(OrderResult.id).where(
                OrderResult.run_id == run_id,
                OrderResult.order_id == str(order["order_id"]),
            )
            if order.get("customer_id") is not None:
                query = query.where(OrderResult.customer_id == str(order["customer_id"]))
            stored_id = session.scalar(query)
        if stored_id is None:
            raise FileLifecycleError("Заказ для исправления не найден.")
        if self._claim_action(stored_id, "correct") is not None:
            raise FileLifecycleError("Над заказом уже выполняется другое действие.")
        try:
            yield
        finally:
            self._release_action(stored_id)

    def recheck_pdf_revision(
        self, run: dict[str, Any], order: dict[str, Any], candidate: Path
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int, str]:
        """Validate a candidate before replacing the current PDF and its status."""
        direction = str(run["options"].get("direction") or "digital")
        inspection = validate_pdf_revision(candidate, order, direction)
        revision = int(order.get("current_pdf_revision") or 0) + 1
        pitstop = None
        if readable_pdf(inspection) and self.reviewed_pdf_pitstop_service_factory is not None:
            try:
                service = self.reviewed_pdf_pitstop_service_factory(
                    Path(run["options"]["input_path"]), direction
                )
                result = service.check_pdf(candidate, profile_id=direction)
                from .batch_adapter import _pitstop_result_to_dto
                pitstop = _pitstop_result_to_dto(result, checked_revision=revision)
            except Exception as exc:
                pitstop = {
                    "execution_status": "failed", "verdict": "error",
                    "technical_error": f"Повторная проверка PitStop: {type(exc).__name__}: {exc}",
                    "issues": [], "reports": {},
                }
        elif order.get("pitstop"):
            pitstop = {
                "execution_status": "failed", "verdict": "error",
                "technical_error": "Для повторной проверки исправленного PDF не настроен PitStop.",
                "issues": [], "reports": {},
            }
        status = completed_status(
            local_errors=not readable_pdf(inspection),
            local_warnings=bool(inspection.errors or inspection.warnings),
            pitstop=pitstop,
        )
        previews: list[str] = []
        if readable_pdf(inspection):
            preview_dir = Path(run["options"]["input_path"]) / "Previews" / "Final" / str(order.get("aggregate_id") or order["order_id"]) / f"r{revision:04d}"
            preview_dir.mkdir(parents=True, exist_ok=True)
            try:
                with pymupdf.open(candidate) as document:
                    for index, page in enumerate(document):
                        preview = preview_dir / f"page-{index + 1}_preview.png"
                        scale = min(480 / max(page.rect.width, page.rect.height, 1), 4)
                        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(preview)
                        previews.append(str(preview))
            except Exception as exc:
                status = "error"
                inspection = replace(inspection, errors=(*inspection.errors, f"Превью исправленного PDF: {exc}"))
                previews = []
        previous = Path(str(order["pdf_path"])) if order.get("pdf_path") else None
        root = pdf_root_for(previous) if previous else Path(run["options"]["input_path"]) / "PDF"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".revision-", dir=root) as backup_dir:
            backup = Path(backup_dir) / "previous.pdf"
            if previous and previous.is_file():
                shutil.copy2(previous, backup)
            try:
                routed = route_pdf(candidate, status, pdf_dir=root, name=previous.name if previous else None)
            except OSError as exc:
                raise FileLifecycleError(f"Не удалось сохранить исправленный PDF: {exc}") from exc
            sha256 = hashlib.sha256(routed.read_bytes()).hexdigest()
            try:
                updated = self.coordinator.apply_pdf_revision(
                    run["id"], str(order.get("aggregate_id") or order["order_id"]),
                    status=status, pdf_path=str(routed), pitstop=pitstop,
                    current_pdf_revision=revision, current_pdf_sha256=sha256,
                    errors=list(inspection.errors), warnings=list(inspection.warnings),
                    preview_paths=previews,
                )
            except Exception:
                # Storage did not commit the new status: restore its previous bytes.
                if previous and backup.is_file():
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, previous)
                if previous is None or routed != previous:
                    routed.unlink(missing_ok=True)
                raise
        return updated, pitstop, revision, sha256

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
            # Send only orders that will really pass the local validation:
            # the external status change cannot be rolled back if a later
            # local check rejects an order.
            eligible_external_ids = [
                str(order.get("order_id") or order.get("id") or order_ref)
                for order_ref, _run, order in resolved_orders
                if self._batch_print_eligible(order, command)
            ]
            if len(eligible_external_ids) > 1:
                batch_prepress_result = self.prepress_sender(
                    eligible_external_ids, None
                )
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
                            current_pdf = Path(str(order.get("pdf_path") or ""))
                            if current_pdf.is_file() and order.get("current_pdf_sha256") and hashlib.sha256(current_pdf.read_bytes()).hexdigest() != order["current_pdf_sha256"]:
                                order, reviewed_pitstop, reviewed_revision, reviewed_sha256 = self.recheck_pdf_revision(run, order, current_pdf)
                                previous_order = deepcopy(order)
                                transition_order = order
                                if order["status"] != "passed":
                                    raise FileLifecycleError("PDF изменился и не прошёл повторную проверку.")
                            reviewed_pdf = build_reviewed_layered_print_pdf(
                                order,
                                input_root,
                                pdf_merger=self.pdf_merger,
                                pdf_page_extractor=self.pdf_page_extractor,
                            )
                            if (
                                reviewed_pdf is None
                                and self.accepted_pdf_provider is not None
                                and order.get("pdf_path")
                            ):
                                # Принятое оператором исправление Callas идёт в печать
                                # тем же путём, что и PDF из просмотренных TIFF:
                                # проверка PDF, PitStop, новая ревизия.
                                reviewed_pdf = self.accepted_pdf_provider(
                                    Path(str(order["pdf_path"]))
                                )
                            if reviewed_pdf is not None:
                                order, reviewed_pitstop, reviewed_revision, reviewed_sha256 = self.recheck_pdf_revision(
                                    run, order, reviewed_pdf
                                )
                                previous_order = deepcopy(order)
                                if order["status"] != "passed":
                                    raise FileLifecycleError(
                                        "Исправленный PDF требует доработки и не отправлен в печать."
                                    )
                                transition_order = order

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
                    except ReviewedLayeredPdfError as exc:
                        action_record.status = "failed"
                        session.commit()
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "review_required"
                                if exc.generated_file_ids
                                else "error",
                                "message": str(exc),
                                **(
                                    {"generated_layered_file_ids": exc.generated_file_ids}
                                    if exc.generated_file_ids
                                    else {}
                                ),
                            }
                        )
                        continue
                    except FileLifecycleError as exc:
                        action_record.status = "failed"
                        if reviewed_pitstop is not None:
                            action_record.cms_response_json = json.dumps(
                                {"pitstop": reviewed_pitstop}, ensure_ascii=False
                            )
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
                                **(
                                    {"pitstop": reviewed_pitstop}
                                    if reviewed_pitstop is not None
                                    else {}
                                ),
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
                            confirm_failed_processing=command.confirm_failed_processing,
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
    def _batch_print_eligible(
        order: dict[str, Any], command: OrderActionCommand
    ) -> bool:
        """Mirror the per-order checks that must pass before an external send."""
        status = str(order.get("status") or "")
        if status in {"accepted_for_print", "returned_for_rework"}:
            return False
        pitstop = order.get("pitstop") or None
        pitstop_ready = pitstop is None or (
            pitstop.get("execution_status") == "completed"
            and pitstop.get("verdict") in {"passed", "warning"}
            and (
                order.get("current_pdf_revision") is None
                or pitstop.get("checked_revision")
                == order.get("current_pdf_revision")
            )
        )
        if not (
            (status in {"passed", "warning"} and pitstop_ready)
            or (command.confirm_failed_processing and status == "error")
        ):
            return False
        try:
            validate_operator_transition(
                status,
                OrderWorkflowService._terminal_status_for_action("print"),
                confirm_failed_processing=command.confirm_failed_processing,
            )
        except ValueError:
            return False
        return True

    @staticmethod
    def _suggested_conflict_name(destination: Path) -> str:
        stem = destination.stem
        suffix = destination.suffix
        if suffix:
            return f"{stem} (новый){suffix}"
        return f"{stem} (новый)"
