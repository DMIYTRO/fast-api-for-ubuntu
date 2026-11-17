"""SQLAlchemy implementation of the run persistence boundary."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from server.models import (
    ArchivedRunEvent,
    CheckRun,
    CorrectionDecision,
    FileResult,
    OrderAction,
    OrderResult,
    PdfRevision,
    PitstopCheck,
    PitstopIssue,
    uuid_hex,
)
from server.models import RunEvent as SqlRunEvent

from .repository import (
    ENVELOPE_DEFER,
    ENVELOPE_FULL,
    ENVELOPE_MODES,
    ENVELOPE_PATCH,
    RunEvent,
)


logger = logging.getLogger("image_magic.repository")


_OPTIONS_MARKER = "__run_repository__"
_INTERRUPTED_STATUSES = {
    "queued",
    "running",
    "waiting_confirmation",
    "cancelling",
}
# Mirrors services.coordinator.TERMINAL_ORDER_STATUSES for restart recovery.
_TERMINAL_ORDER_STATUSES = {"passed", "warning", "error", "cancelled"}
_RUN_STANDARD_KEYS = {
    "id",
    "status",
    "stage",
    "progress",
    "options",
    "created_at",
    "started_at",
    "finished_at",
    "error",
    "orders",
}
_ORDER_STANDARD_KEYS = {
    "order_id",
    "customer_id",
    "status",
    "passed",
    "errors",
    "warnings",
    "pdf_path",
    "files",
    "source_status",
    "pitstop_status",
    "workflow_status",
    "current_pdf_revision",
    "current_pdf_sha256",
    "pitstop",
}
_PITSTOP_ORDER_KEYS = {
    "source_status",
    "pitstop_status",
    "workflow_status",
    "current_pdf_revision",
    "current_pdf_sha256",
    "pitstop",
}
_FILE_MAPPED_KEYS = {
    "file_result_id",
    "path",
    "name",
    "actual_width_mm",
    "actual_height_mm",
    "dpi_x",
    "dpi_y",
    "actual_format",
    "colorspace",
    "errors",
    "warnings",
}


class SqlRunRepository:
    """Persist service DTOs in the normalized server tables.

    Columns remain authoritative for fields used by queries.  DTO fields that
    do not yet have a dedicated database column (for example resampling
    metadata and aggregate counters) are retained in a small compatibility
    envelope inside ``CheckRun.options_json``.  This keeps restart round-trips
    lossless without changing the shared schema.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        recover_interrupted: bool = True,
    ) -> None:
        self._session_factory = session_factory
        if recover_interrupted:
            self.recover_interrupted_runs()

    def create_run(self, run: dict[str, Any]) -> None:
        with self._session_factory() as session, session.begin():
            if session.get(CheckRun, str(run["id"])) is not None:
                raise ValueError(f"run already exists: {run['id']}")
            record = CheckRun(id=str(run["id"]), input_path="", direction="")
            session.add(record)
            self._apply_run(session, record, run)

    def save_run(
        self, run: dict[str, Any], *, include_orders: bool = True
    ) -> None:
        with self._session_factory() as session, session.begin():
            self._save_run_in_session(session, run, include_orders=include_orders)

    def save_run_with_event(
        self,
        run: dict[str, Any],
        event_type: str,
        data: dict[str, Any],
        *,
        include_orders: bool = True,
    ) -> RunEvent:
        with self._session_factory() as session, session.begin():
            run_id = str(run["id"])
            self._save_run_in_session(session, run, include_orders=include_orders)
            session.flush()
            self._apply_correction_audit(session, run_id, event_type, data)
            return self._add_event(session, run_id, event_type, data)

    def save_order_with_event(
        self,
        run: dict[str, Any],
        order_key: str,
        event_type: str,
        data: dict[str, Any],
        *,
        envelope: str = ENVELOPE_PATCH,
    ) -> RunEvent:
        """Persist one order, the run's scalar columns and the event atomically.

        Only the order row (and its files, PDF revision and PitStop check) for
        ``order_key`` is touched, so the statement count does not depend on
        the number of orders in the run.  ``envelope`` selects how the
        whole-run compatibility blob in ``options_json`` is handled (see
        ``services.repository.ENVELOPE_*``); ``defer`` does not read or write
        it at all.

        After a successful commit ``run["orders"][order_key]`` is replaced by a
        copy whose files carry their stable ``file_result_id``, so the caller's
        live aggregate matches what a fresh read would return.
        """
        if envelope not in ENVELOPE_MODES:
            raise ValueError(f"unknown envelope mode: {envelope}")
        run_id = str(run["id"])
        key = str(order_key)
        order = run["orders"][order_key]
        with self._session_factory() as session, session.begin():
            if envelope == ENVELOPE_DEFER:
                record = None
                result = session.execute(
                    update(CheckRun)
                    .where(CheckRun.id == run_id)
                    .values(**self._run_scalar_values(run))
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 0:
                    raise KeyError(run_id)
            else:
                record = session.get(CheckRun, run_id)
                if record is None:
                    raise KeyError(run_id)
                self._apply_run_scalars(record, run)
            order_record = self._find_order_record(session, run_id, key, order)
            if order_record is None:
                order_record = OrderResult(
                    run_id=run_id, order_id=str(order.get("order_id") or key)
                )
                session.add(order_record)
            file_ids = self._apply_order(session, order_record, order)
            session.flush()
            if record is not None and envelope == ENVELOPE_PATCH:
                record.options_json = _json_dump(
                    self._patched_envelope(
                        record.options_json, run, key, order, file_ids
                    )
                )
            elif record is not None:
                record.options_json = _json_dump(
                    self._options_envelope(
                        run,
                        self._envelope_file_ids(
                            session, run_id, run, {key: file_ids}
                        ),
                    )
                )
            self._apply_correction_audit(session, run_id, event_type, data)
            event = self._add_event(session, run_id, event_type, data)
        # Replace (not mutate) the entry: the caller may reuse ``order`` as an
        # event payload, whose shape must stay as it was before persistence.
        # _apply_order returns exactly one ID per file DTO, in order.
        run["orders"][order_key] = {
            **order,
            "files": [
                {**file_dto, "file_result_id": file_id}
                for file_dto, file_id in zip(order.get("files") or [], file_ids)
            ],
        }
        return event

    def run_exists(self, run_id: str) -> bool:
        with self._session_factory() as session:
            return session.scalar(
                select(CheckRun.id).where(CheckRun.id == run_id)
            ) is not None

    def _save_run_in_session(
        self, session: Session, run: dict[str, Any], *, include_orders: bool
    ) -> None:
        run_id = str(run["id"])
        if include_orders:
            record = session.scalar(
                select(CheckRun)
                .where(CheckRun.id == run_id)
                .options(
                    selectinload(CheckRun.orders).selectinload(OrderResult.files),
                    selectinload(CheckRun.orders)
                    .selectinload(OrderResult.pdf_revisions)
                    .selectinload(PdfRevision.checks)
                    .selectinload(PitstopCheck.issues),
                )
            )
            if record is None:
                raise KeyError(run_id)
            self._apply_run(session, record, run)
            return
        record = session.get(CheckRun, run_id)
        if record is None:
            raise KeyError(run_id)
        self._apply_run_scalars(record, run)
        record.options_json = _json_dump(
            self._options_envelope(
                run, self._envelope_file_ids(session, run_id, run, {})
            )
        )

    @staticmethod
    def _add_event(
        session: Session, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        event_record = SqlRunEvent(
            run_id=run_id,
            event_type=event_type,
            payload_json=_json_dump(data),
        )
        session.add(event_record)
        session.flush()
        return RunEvent(
            id=event_record.id,
            type=event_record.event_type,
            run_id=event_record.run_id,
            data=deepcopy(data),
            created_at=_iso(event_record.created_at) or _now_iso(),
        )

    @staticmethod
    def _find_order_record(
        session: Session, run_id: str, key: str, order: dict[str, Any]
    ) -> OrderResult | None:
        order_id = str(order.get("order_id") or key)
        customer_id = str(order.get("customer_id") or "")
        customer_filter = (
            OrderResult.customer_id == customer_id
            if customer_id
            else or_(OrderResult.customer_id.is_(None), OrderResult.customer_id == "")
        )
        return session.scalar(
            select(OrderResult)
            .where(
                OrderResult.run_id == run_id,
                OrderResult.order_id == order_id,
                customer_filter,
            )
            .options(selectinload(OrderResult.files))
            .order_by(OrderResult.id)
            .limit(1)
        )

    @staticmethod
    def _apply_correction_audit(
        session: Session,
        run_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        if event_type not in {
            "order.correction_confirmed",
            "order.correction_rejected",
        }:
            return
        order_id = str(data.get("order_id") or "")
        for value in data.get("decisions") or []:
            statement = (
                select(FileResult)
                .join(OrderResult)
                .where(OrderResult.run_id == run_id)
            )
            if order_id:
                statement = statement.where(OrderResult.order_id == order_id)
            file_result_id = value.get("file_result_id")
            if file_result_id is not None:
                statement = statement.where(
                    FileResult.id == _required_record_id(
                        file_result_id, "file_result_id"
                    )
                )
            else:
                # Backward compatibility for events produced before file IDs
                # were carried by the service DTO.
                statement = statement.where(
                    FileResult.source_path == str(value.get("path") or "")
                )
            file_record = session.scalar(statement.order_by(FileResult.id))
            if file_record is None:
                continue
            session.add(
                CorrectionDecision(
                    file_result_id=file_record.id,
                    decision=str(value.get("decision") or ""),
                    original_parameters_json=_json_dump(
                        value.get("original") or {}
                    ),
                    proposed_parameters_json=_json_dump(
                        value.get("proposed") or {}
                    ),
                )
            )

    def get_run_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            record = session.get(CheckRun, run_id)
            return self._run_to_dict(record, include_orders=False) if record is not None else None

    def list_orders_page(
        self, run_id: str, *, page: int, page_size: int, status: str,
        search: str, active_only: bool = True,
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            run_record = session.get(CheckRun, run_id)
            if run_record is None:
                return None
            base = [OrderResult.run_id == run_id]
            terminal = ("accepted_for_print", "returned_for_rework")
            if active_only:
                base.append(OrderResult.status.notin_(terminal))
            query = search.strip()
            if query:
                base.append(or_(
                    OrderResult.order_id.contains(query, autoescape=True),
                    OrderResult.customer_id.contains(query, autoescape=True),
                    exists(select(FileResult.id).where(
                        FileResult.order_result_id == OrderResult.id,
                        FileResult.filename.contains(query, autoescape=True),
                    )),
                ))
            grouped = session.execute(
                select(OrderResult.status, func.count(OrderResult.id))
                .where(*base).group_by(OrderResult.status)
            ).all()
            status_counts = {str(key or "detected"): int(count) for key, count in grouped}
            counts = {
                "all": sum(status_counts.values()),
                "passed": sum(status_counts.get(value, 0) for value in ("passed", "completed")),
                "warning": status_counts.get("warning", 0),
                "error": sum(status_counts.get(value, 0) for value in ("error", "failed", "technical_error")),
                "waiting_confirmation": status_counts.get("waiting_confirmation", 0),
            }
            filtered = list(base)
            if status == "passed":
                filtered.append(OrderResult.status.in_(("passed", "completed")))
            elif status == "error":
                filtered.append(OrderResult.status.in_(("error", "failed", "technical_error")))
            elif status != "all":
                filtered.append(OrderResult.status == status)
            total = int(session.scalar(
                select(func.count(OrderResult.id)).where(*filtered)
            ) or 0)
            options = (
                selectinload(OrderResult.files),
                selectinload(OrderResult.actions),
                selectinload(OrderResult.pdf_revisions)
                .selectinload(PdfRevision.checks)
                .selectinload(PitstopCheck.issues),
            )
            records = session.scalars(
                select(OrderResult).where(*filtered)
                .options(*options)
                # Keep offset pagination stable while a run is still adding
                # orders. Sorting by customer/order lets a newly discovered
                # low-sorting key shift every row on the active page.
                .order_by(OrderResult.id)
                .offset((page - 1) * page_size).limit(page_size)
            ).all()
            page_record = SimpleNamespace(
                id=run_record.id, status=run_record.status, stage=run_record.stage,
                progress=run_record.progress, options_json=run_record.options_json,
                input_path=run_record.input_path, direction=run_record.direction,
                created_at=run_record.created_at, started_at=run_record.started_at,
                finished_at=run_record.finished_at, error=run_record.error,
                orders=records,
            )
            run = self._run_to_dict(page_record)
            return {
                "run": run,
                "items": list(run["orders"].values()),
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "counts": counts,
            }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            statement = (
                select(CheckRun)
                .where(CheckRun.id == run_id)
                .options(
                    selectinload(CheckRun.orders).selectinload(OrderResult.files),
                    selectinload(CheckRun.orders).selectinload(OrderResult.actions),
                    selectinload(CheckRun.orders)
                    .selectinload(OrderResult.pdf_revisions)
                    .selectinload(PdfRevision.checks)
                    .selectinload(PitstopCheck.issues),
                )
            )
            record = session.scalar(statement)
            return self._run_to_dict(record) if record is not None else None

    def find_file_by_id(self, file_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Resolve a stable database file ID without scanning run history."""
        try:
            record_id = int(file_id)
        except (TypeError, ValueError):
            return None
        with self._session_factory() as session:
            record = session.scalar(
                select(FileResult)
                .where(FileResult.id == record_id)
                .options(
                    selectinload(FileResult.order_result).selectinload(OrderResult.run),
                    selectinload(FileResult.order_result).selectinload(OrderResult.files),
                    selectinload(FileResult.order_result).selectinload(OrderResult.actions),
                    selectinload(FileResult.order_result)
                    .selectinload(OrderResult.pdf_revisions)
                    .selectinload(PdfRevision.checks)
                    .selectinload(PitstopCheck.issues),
                )
            )
            if record is None:
                return None
            order_record = record.order_result
            run_record = order_record.run
            # Reuse the normal DTO serializer with only this order loaded. This
            # avoids materializing the whole run when a card requests preview.
            run = self._run_to_dict(
                SimpleNamespace(
                    id=run_record.id,
                    input_path=run_record.input_path,
                    direction=run_record.direction,
                    status=run_record.status,
                    stage=run_record.stage,
                    progress=run_record.progress,
                    options_json=run_record.options_json,
                    created_at=run_record.created_at,
                    started_at=run_record.started_at,
                    finished_at=run_record.finished_at,
                    error=run_record.error,
                    orders=[order_record],
                )
            )
        for order in (run.get("orders") or {}).values():
            for item in order.get("files") or []:
                if str(item.get("file_result_id") or "") == str(record_id):
                    return run, item
        return None

    def list_runs(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include_orders: bool = True,
    ) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            statement = select(CheckRun).order_by(
                CheckRun.created_at.desc(), CheckRun.id.desc()
            )
            if include_orders:
                statement = statement.options(
                    selectinload(CheckRun.orders).selectinload(OrderResult.files),
                    selectinload(CheckRun.orders).selectinload(OrderResult.actions),
                    selectinload(CheckRun.orders)
                    .selectinload(OrderResult.pdf_revisions)
                    .selectinload(PdfRevision.checks)
                    .selectinload(PitstopCheck.issues),
                )
            if offset:
                statement = statement.offset(offset)
            if limit is not None:
                statement = statement.limit(limit)
            return [
                self._run_to_dict(record, include_orders=include_orders)
                for record in session.scalars(statement)
            ]

    def count_runs(self) -> int:
        with self._session_factory() as session:
            return int(
                session.scalar(select(func.count()).select_from(CheckRun)) or 0
            )

    def append_event(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        with self._session_factory() as session, session.begin():
            if session.get(CheckRun, run_id) is None:
                raise KeyError(run_id)
            record = SqlRunEvent(
                run_id=run_id,
                event_type=event_type,
                payload_json=_json_dump(data),
            )
            session.add(record)
            session.flush()
            event = RunEvent(
                id=record.id,
                type=record.event_type,
                run_id=record.run_id,
                data=deepcopy(data),
                created_at=_iso(record.created_at) or _now_iso(),
            )
        return event

    @staticmethod
    def _event_from_record(record: Any) -> RunEvent:
        return RunEvent(
            id=record.id,
            type=record.event_type,
            run_id=record.run_id,
            data=_json_load(record.payload_json, {}),
            created_at=_iso(record.created_at) or _now_iso(),
        )

    def list_events(
        self, run_id: str, after_id: int = 0, limit: int | None = None
    ) -> list[RunEvent]:
        # Archived rows keep their original IDs and payloads. Merging both tables
        # preserves the SSE Last-Event-ID contract across the archive boundary.
        # With ``limit`` each table is limited in SQL (ordered by id) and the
        # merged result is truncated, which yields the first ``limit`` events.
        if limit is not None and int(limit) <= 0:
            return []
        with self._session_factory() as session:
            archived_statement = (
                select(ArchivedRunEvent)
                .where(
                    ArchivedRunEvent.run_id == run_id,
                    ArchivedRunEvent.id > after_id,
                )
                .order_by(ArchivedRunEvent.id)
            )
            live_statement = (
                select(SqlRunEvent)
                .where(SqlRunEvent.run_id == run_id, SqlRunEvent.id > after_id)
                .order_by(SqlRunEvent.id)
            )
            if limit is not None:
                archived_statement = archived_statement.limit(int(limit))
                live_statement = live_statement.limit(int(limit))
            archived = list(session.scalars(archived_statement))
            live = list(session.scalars(live_statement))
            events = sorted(
                (self._event_from_record(record) for record in (*archived, *live)),
                key=lambda event: event.id,
            )
            return events if limit is None else events[: int(limit)]

    def archive_old_terminal_events(
        self,
        cutoff: datetime,
        *,
        batch_size: int = 500,
    ) -> int:
        """Move one bounded batch of old events to replayable cold storage.

        Run/order/file rows are never changed. Only events created before the
        cutoff for completed, failed or cancelled runs whose finished_at also
        predates the cutoff are eligible. The insert and delete share one
        transaction, so a failure leaves the event in its original table.
        """
        if batch_size < 1:
            raise ValueError("batch_size должен быть больше нуля")
        terminal_statuses = ("completed", "failed", "cancelled")
        with self._session_factory() as session, session.begin():
            records = list(
                session.scalars(
                    select(SqlRunEvent)
                    .join(CheckRun, CheckRun.id == SqlRunEvent.run_id)
                    .where(
                        CheckRun.status.in_(terminal_statuses),
                        CheckRun.finished_at.is_not(None),
                        CheckRun.finished_at <= cutoff,
                        SqlRunEvent.created_at <= cutoff,
                    )
                    .order_by(SqlRunEvent.id)
                    .limit(batch_size)
                )
            )
            if not records:
                return 0
            record_ids = [record.id for record in records]
            session.add_all(
                ArchivedRunEvent(
                    id=record.id,
                    run_id=record.run_id,
                    event_type=record.event_type,
                    payload_json=record.payload_json,
                    created_at=record.created_at,
                )
                for record in records
            )
            session.flush()
            session.query(SqlRunEvent).filter(SqlRunEvent.id.in_(record_ids)).delete(
                synchronize_session=False
            )
            return len(records)

    def recover_interrupted_runs(self) -> int:
        """Mark work lacking an in-memory processor context as interrupted.

        A pending action whose persisted order already has the matching final
        status completed its durable transition and can be promoted. Otherwise
        it is failed so it cannot block the order forever. We deliberately do
        not attempt filesystem rollback here: OrderAction does not contain a
        source/destination manifest, so guessing after a crash could move or
        delete the wrong files.
        """
        recovered = 0
        with self._session_factory() as session, session.begin():
            pending_actions = list(
                session.scalars(
                    select(OrderAction)
                    .where(OrderAction.status == "pending")
                    .order_by(OrderAction.id)
                )
            )
            terminal_status = {
                "print": "accepted_for_print",
                "reject": "returned_for_rework",
            }
            for action_record in pending_actions:
                expected_status = terminal_status.get(action_record.action)
                persisted_status = action_record.order_result.status
                filesystem_reconciled = self._order_files_reconciled(
                    action_record.order_result
                )
                if (
                    expected_status is not None
                    and persisted_status == expected_status
                    and filesystem_reconciled
                ):
                    action_record.status = "prepared"
                    reason = "persisted_transition_completed"
                else:
                    action_record.status = "failed"
                    reason = "application_restart"
                action_record.cms_response_json = _json_dump(
                    {
                        "reason": reason,
                        "previous_status": "pending",
                        "order_status": persisted_status,
                        "filesystem_reconciled": filesystem_reconciled,
                    }
                )
                logger.warning(
                    "order_action.recovered action_id=%s order_result_id=%s "
                    "status=%s reason=%s",
                    action_record.id,
                    action_record.order_result_id,
                    action_record.status,
                    reason,
                )

            records = list(
                session.scalars(
                    select(CheckRun).where(
                        CheckRun.status.in_(_INTERRUPTED_STATUSES)
                    )
                )
            )
            for record in records:
                previous_status = record.status
                record.status = "failed"
                record.stage = "interrupted"
                record.finished_at = datetime.now(timezone.utc)
                record.error = (
                    "Проверка была прервана перезапуском приложения "
                    f"(предыдущее состояние: {previous_status})."
                )
                self._recount_interrupted_run(session, record)
                session.add(
                    SqlRunEvent(
                        run_id=record.id,
                        event_type="run.failed",
                        payload_json=_json_dump(
                            {
                                "error": record.error,
                                "reason": "application_restart",
                                "previous_status": previous_status,
                            }
                        ),
                    )
                )
                logger.warning(
                    "run.recovered_as_interrupted run_id=%s previous_status=%s",
                    record.id,
                    previous_status,
                )
                recovered += 1
        return recovered

    @staticmethod
    def _recount_interrupted_run(session: Session, record: CheckRun) -> None:
        """Align stored run counters with the order rows of a crashed run.

        During an active run the coordinator writes order rows per event but
        refreshes the counters in the compatibility envelope only at
        checkpoints, so after a crash the rows are the more recent truth.
        """
        envelope = _json_load(record.options_json, {})
        metadata = (
            envelope.get(_OPTIONS_MARKER) if isinstance(envelope, dict) else None
        )
        if not isinstance(metadata, dict) or "options" not in envelope:
            return
        statuses = {
            str(status): int(count)
            for status, count in session.execute(
                select(OrderResult.status, func.count(OrderResult.id))
                .where(OrderResult.run_id == record.id)
                .group_by(OrderResult.status)
            ).all()
        }
        run_extras = metadata.get("run_extras")
        if not isinstance(run_extras, dict):
            run_extras = metadata["run_extras"] = {}
        try:
            known_total = int(run_extras.get("total_orders") or 0)
        except (TypeError, ValueError):
            known_total = 0
        run_extras.update(
            {
                "total_orders": max(known_total, sum(statuses.values())),
                "processed_orders": sum(
                    count
                    for status, count in statuses.items()
                    if status in _TERMINAL_ORDER_STATUSES
                ),
                "passed_orders": statuses.get("passed", 0),
                "warning_orders": statuses.get("warning", 0),
                "failed_orders": statuses.get("error", 0),
                "waiting_orders": statuses.get("waiting_confirmation", 0),
            }
        )
        record.options_json = _json_dump(envelope)

    @staticmethod
    def _order_files_reconciled(order_result: OrderResult) -> bool:
        paths: list[str] = []
        if order_result.pdf_path:
            paths.append(order_result.pdf_path)
        for file_record in order_result.files:
            if file_record.source_path:
                paths.append(file_record.source_path)
            if file_record.preview_path:
                paths.append(file_record.preview_path)
        if not paths:
            return False
        return all(Path(path).exists() for path in paths)

    @staticmethod
    def _run_scalar_values(run: dict[str, Any]) -> dict[str, Any]:
        options = run.get("options") or {}
        return {
            "input_path": str(options.get("input_path", "")),
            "direction": str(options.get("direction", "")),
            "status": str(run.get("status", "queued")),
            "stage": _optional_str(run.get("stage")),
            "progress": float(run.get("progress", 0)),
            "created_at": _datetime(run.get("created_at"))
            or datetime.now(timezone.utc),
            "started_at": _datetime(run.get("started_at")),
            "finished_at": _datetime(run.get("finished_at")),
            "error": _optional_str(run.get("error")),
        }

    @classmethod
    def _apply_run_scalars(cls, record: CheckRun, run: dict[str, Any]) -> None:
        for name, value in cls._run_scalar_values(run).items():
            setattr(record, name, value)

    def _apply_run(
        self, session: Session, record: CheckRun, run: dict[str, Any]
    ) -> None:
        self._apply_run_scalars(record, run)
        existing_orders = {
            (item.customer_id or "", item.order_id): item for item in record.orders
        }
        incoming_orders = run.get("orders") or {}
        retained_order_ids: set[int] = set()
        file_ids_by_order: dict[str, list[int]] = {}
        for aggregate_key, order in incoming_orders.items():
            order_id = str(order.get("order_id") or aggregate_key)
            customer_id = str(order.get("customer_id") or "")
            order_record = existing_orders.get((customer_id, order_id))
            if order_record is None:
                order_record = OrderResult(run=record, order_id=order_id)
                session.add(order_record)
            file_ids_by_order[str(aggregate_key)] = self._apply_order(
                session, order_record, order
            )
            session.flush()
            retained_order_ids.add(order_record.id)
        for order_record in existing_orders.values():
            if order_record.id not in retained_order_ids:
                session.delete(order_record)
        record.options_json = _json_dump(
            self._options_envelope(run, file_ids_by_order)
        )

    def _apply_order(
        self,
        session: Session,
        record: OrderResult,
        order: dict[str, Any],
    ) -> list[int]:
        record.customer_id = _optional_str(order.get("customer_id"))
        record.status = str(order.get("status", "pending"))
        if "source_status" in order:
            record.source_status = str(order.get("source_status") or "pending")
        elif record.id is None:
            record.source_status = record.status
        if "pitstop_status" in order:
            record.pitstop_status = str(
                order.get("pitstop_status") or "not_checked"
            )
        if "workflow_status" in order:
            record.workflow_status = str(order.get("workflow_status") or "active")
        record.passed = bool(order.get("passed", False))
        record.errors_json = _json_dump(list(order.get("errors") or []))
        record.warnings_json = _json_dump(list(order.get("warnings") or []))
        record.pdf_path = _optional_str(order.get("pdf_path"))
        self._apply_pitstop(session, record, order)

        existing_files_by_id = {item.id: item for item in record.files}
        existing_files: dict[str, list[FileResult]] = {}
        for item in record.files:
            existing_files.setdefault(item.source_path, []).append(item)
        used_ids: set[int] = set()
        ordered_file_ids: list[int] = []
        preview_paths = list(order.get("preview_paths") or [])
        for index, file_dto in enumerate(order.get("files") or []):
            source_path = str(file_dto.get("path", ""))
            incoming_id = file_dto.get("file_result_id")
            if incoming_id is not None:
                file_id = _required_record_id(incoming_id, "file_result_id")
                file_record = existing_files_by_id.get(file_id)
                if file_record is None:
                    raise ValueError(
                        f"file_result_id {file_id} does not belong to "
                        f"order_result_id {record.id}"
                    )
                if file_id in used_ids:
                    raise ValueError(
                        f"file_result_id {file_id} is used more than once"
                    )
            else:
                # Legacy DTOs do not carry a database ID. Preserve their old
                # path-based matching until they have made one read round-trip.
                candidates = existing_files.get(source_path, [])
                file_record = next(
                    (
                        candidate
                        for candidate in candidates
                        if candidate.id not in used_ids
                    ),
                    None,
                )
            if file_record is None:
                file_record = FileResult(
                    order_result=record,
                    source_path=source_path,
                    filename=str(file_dto.get("name") or source_path),
                )
                session.add(file_record)
                session.flush()
            used_ids.add(file_record.id)
            ordered_file_ids.append(file_record.id)
            self._apply_file(
                file_record,
                file_dto,
                preview_paths[index] if index < len(preview_paths) else None,
            )
        for candidates in existing_files.values():
            for file_record in candidates:
                if file_record.id not in used_ids:
                    session.delete(file_record)
        return ordered_file_ids

    @staticmethod
    def _apply_pitstop(
        session: Session,
        record: OrderResult,
        order: dict[str, Any],
    ) -> None:
        has_revision_contract = (
            order.get("current_pdf_revision") is not None
            or order.get("current_pdf_sha256") is not None
            or bool(order.get("pitstop"))
        )
        if not has_revision_contract:
            return

        pitstop = order.get("pitstop") or {}
        revision_number = order.get("current_pdf_revision")
        if revision_number is None:
            revision_number = pitstop.get("checked_revision")
        revision_number = int(revision_number or 1)
        if revision_number <= 0:
            raise ValueError("current_pdf_revision must be a positive integer")
        pdf_path = _optional_str(order.get("pdf_path"))
        if not pdf_path:
            raise ValueError("pdf_path is required for a PDF revision")

        revisions_by_number = {
            item.revision_number: item for item in record.pdf_revisions
        }
        revision = revisions_by_number.get(revision_number)
        if revision is None:
            revision = PdfRevision(
                order_result=record,
                revision_number=revision_number,
                pdf_path=pdf_path,
            )
            session.add(revision)
        for item in record.pdf_revisions:
            if item is not revision and item.is_current:
                item.is_current = False
        # Flush clearing the previous current flag before setting a new one so
        # the partial unique index is respected by SQLite and PostgreSQL.
        session.flush()
        revision.pdf_path = pdf_path
        revision.sha256 = _optional_str(order.get("current_pdf_sha256"))
        revision.is_current = True
        session.flush()

        if not pitstop:
            return
        check_id = str(pitstop.get("check_id") or uuid_hex())
        check = session.get(PitstopCheck, check_id)
        if check is not None and check.pdf_revision_id != revision.id:
            raise ValueError(
                f"PitStop check {check_id} belongs to another PDF revision"
            )
        if check is None:
            check = PitstopCheck(
                id=check_id,
                pdf_revision=revision,
                execution_status="pending",
                verdict="pending",
            )
            session.add(check)
        check.execution_status = str(
            pitstop.get("execution_status") or "pending"
        )
        check.verdict = str(pitstop.get("verdict") or "pending")
        check.checked_at = _datetime(pitstop.get("checked_at"))
        profile = pitstop.get("profile") or {}
        check.profile_key = _optional_str(profile.get("key"))
        check.profile_name = _optional_str(profile.get("name"))
        check.profile_version = _optional_str(profile.get("version"))
        check.page_count = _optional_int(pitstop.get("pages"))
        counts = pitstop.get("counts") or {}
        check.errors_count = int(counts.get("errors") or 0)
        check.warnings_count = int(counts.get("warnings") or 0)
        check.fixes_count = int(counts.get("fixes") or 0)
        check.critical_failures_count = int(
            counts.get("critical_failures") or 0
        )
        check.noncritical_failures_count = int(
            counts.get("noncritical_failures") or 0
        )
        check.informations_count = int(counts.get("informations") or 0)
        reports = pitstop.get("reports") or {}
        check.report_json_path = _optional_str(reports.get("json_url"))
        check.report_xml_path = _optional_str(reports.get("xml_url"))
        check.technical_error = _optional_str(pitstop.get("technical_error"))
        check.verdict_raw = _optional_str(pitstop.get("verdict_raw"))
        check.baseline_status = _optional_str(pitstop.get("baseline_status"))
        counts_raw = pitstop.get("counts_raw")
        check.counts_raw_json = (
            _json_dump(counts_raw) if isinstance(counts_raw, dict) else None
        )
        session.flush()

        incoming_issues = list(pitstop.get("issues") or [])
        if _issues_unchanged(check.issues, incoming_issues):
            return
        check.issues.clear()
        for issue in incoming_issues:
            check.issues.append(
                PitstopIssue(
                    issue_id=str(issue.get("id") or uuid_hex()),
                    fingerprint=_optional_str(issue.get("fingerprint")),
                    severity=str(issue.get("severity") or "information"),
                    action_id=_optional_str(issue.get("action_id")),
                    message=str(issue.get("message") or ""),
                    occurrences=int(issue.get("occurrences") or 1),
                    locations_json=_json_dump(list(issue.get("locations") or [])),
                    **_issue_baseline_values(issue),
                )
            )

    @staticmethod
    def _apply_file(
        record: FileResult,
        file_dto: dict[str, Any],
        preview_path: str | None,
    ) -> None:
        parsed = file_dto.get("parsed") or {}
        record.filename = str(file_dto.get("name") or record.source_path)
        record.source_path = str(file_dto.get("path", ""))
        record.side = _optional_str(parsed.get("side"))
        record.format = _optional_str(file_dto.get("actual_format"))
        record.colorspace = _optional_str(file_dto.get("colorspace"))
        record.width_mm = _optional_float(file_dto.get("actual_width_mm"))
        record.height_mm = _optional_float(file_dto.get("actual_height_mm"))
        record.dpi_x = _optional_float(file_dto.get("dpi_x"))
        record.dpi_y = _optional_float(file_dto.get("dpi_y"))
        record.status = _file_status(file_dto)
        record.errors_json = _json_dump(list(file_dto.get("errors") or []))
        record.warnings_json = _json_dump(list(file_dto.get("warnings") or []))
        record.preview_path = _optional_str(preview_path)

    @staticmethod
    def _run_extras(run: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value for key, value in run.items() if key not in _RUN_STANDARD_KEYS
        }

    @staticmethod
    def _order_envelope_parts(
        storage_key: str, order: dict[str, Any], ordered_file_ids: list[int]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        order_extra = {
            key: value
            for key, value in order.items()
            if key not in _ORDER_STANDARD_KEYS
        }
        order_extra["__pitstop_fields__"] = sorted(
            key for key in _PITSTOP_ORDER_KEYS if key in order
        )
        order_extra["__storage_key__"] = storage_key
        file_extra = {
            str(ordered_file_ids[index]): {
                key: value
                for key, value in item.items()
                if key not in _FILE_MAPPED_KEYS
            }
            for index, item in enumerate(order.get("files") or [])
            if index < len(ordered_file_ids)
            and ordered_file_ids[index] is not None
        }
        return order_extra, file_extra

    @classmethod
    def _options_envelope(
        cls,
        run: dict[str, Any],
        file_ids_by_order: dict[str, list[int]],
    ) -> dict[str, Any]:
        # The result is serialized immediately, so values are shared with the
        # DTO instead of deep-copied (this runs on every checkpoint).
        order_extras: dict[str, dict[str, Any]] = {}
        file_extras: dict[str, dict[str, dict[str, Any]]] = {}
        for order_id, order in (run.get("orders") or {}).items():
            key = str(order_id)
            order_extras[key], file_extras[key] = cls._order_envelope_parts(
                key, order, file_ids_by_order.get(key, [])
            )
        return {
            "options": run.get("options") or {},
            _OPTIONS_MARKER: {
                "run_extras": cls._run_extras(run),
                "order_extras": order_extras,
                "file_extras": file_extras,
            },
        }

    @classmethod
    def _patched_envelope(
        cls,
        options_json: str | None,
        run: dict[str, Any],
        storage_key: str,
        order: dict[str, Any],
        ordered_file_ids: list[int],
    ) -> dict[str, Any]:
        """Replace one order's extras and the run extras in a stored blob."""
        envelope = _json_load(options_json, {})
        metadata = (
            envelope.get(_OPTIONS_MARKER)
            if isinstance(envelope, dict) and "options" in envelope
            else None
        )
        if not isinstance(metadata, dict):
            # Legacy rows kept no extras for any order.
            metadata = {}
        order_extras = metadata.get("order_extras")
        if not isinstance(order_extras, dict):
            order_extras = {}
        file_extras = metadata.get("file_extras")
        if not isinstance(file_extras, dict):
            file_extras = {}
        order_extras[storage_key], file_extras[storage_key] = (
            cls._order_envelope_parts(storage_key, order, ordered_file_ids)
        )
        return {
            "options": run.get("options") or {},
            _OPTIONS_MARKER: {
                "run_extras": cls._run_extras(run),
                "order_extras": order_extras,
                "file_extras": file_extras,
            },
        }

    @staticmethod
    def _envelope_file_ids(
        session: Session,
        run_id: str,
        run: dict[str, Any],
        known: dict[str, list[int]],
    ) -> dict[str, list[int]]:
        """File row IDs per order for a full envelope rewrite.

        The coordinator's live DTOs carry ``file_result_id`` (written back by
        ``save_order_with_event``), so normally no query is needed.  DTOs
        without IDs are resolved by path with a single query for the run.
        """
        result: dict[str, list[int]] = {}
        missing: list[tuple[str, dict[str, Any]]] = []
        for order_key, order in (run.get("orders") or {}).items():
            key = str(order_key)
            if key in known:
                result[key] = known[key]
                continue
            ids = [item.get("file_result_id") for item in order.get("files") or []]
            if all(value is not None for value in ids):
                result[key] = [int(value) for value in ids]
            else:
                missing.append((key, order))
        if not missing:
            return result
        rows = session.execute(
            select(
                FileResult.id,
                FileResult.source_path,
                OrderResult.customer_id,
                OrderResult.order_id,
            )
            .join(OrderResult, FileResult.order_result_id == OrderResult.id)
            .where(OrderResult.run_id == run_id)
            .order_by(FileResult.id)
        ).all()
        by_order: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for file_id, source_path, customer_id, order_id in rows:
            by_order.setdefault((customer_id or "", order_id), []).append(
                (file_id, source_path)
            )
        for key, order in missing:
            candidates = by_order.get(
                (str(order.get("customer_id") or ""), str(order.get("order_id") or key)),
                [],
            )
            used: set[int] = set()
            ids: list[int] = []
            for item in order.get("files") or []:
                file_id = item.get("file_result_id")
                if file_id is None:
                    file_id = next(
                        (
                            candidate_id
                            for candidate_id, path in candidates
                            if path == str(item.get("path", ""))
                            and candidate_id not in used
                        ),
                        None,
                    )
                if file_id is not None:
                    used.add(int(file_id))
                ids.append(file_id)
            result[key] = ids
        return result

    @staticmethod
    def _run_to_dict(
        record: CheckRun, *, include_orders: bool = True
    ) -> dict[str, Any]:
        envelope = _json_load(record.options_json, {})
        if "options" in envelope and _OPTIONS_MARKER in envelope:
            options = deepcopy(envelope.get("options") or {})
            metadata = envelope.get(_OPTIONS_MARKER) or {}
        else:
            # Backward-compatible with rows created before this repository.
            options = deepcopy(envelope)
            metadata = {}
        options.setdefault("input_path", record.input_path)
        options.setdefault("direction", record.direction)

        run = deepcopy(metadata.get("run_extras") or {})
        run.update(
            {
                "id": record.id,
                "status": record.status,
                "stage": record.stage,
                "progress": int(record.progress)
                if float(record.progress).is_integer()
                else record.progress,
                "options": options,
                "created_at": _iso(record.created_at),
                "started_at": _iso(record.started_at),
                "finished_at": _iso(record.finished_at),
                "error": record.error,
                "orders": {},
            }
        )
        if not include_orders:
            return run
        order_extras = metadata.get("order_extras") or {}
        file_extras = metadata.get("file_extras") or {}
        for order_record in sorted(record.orders, key=lambda item: item.id):
            composite_key = (
                f"{order_record.customer_id}:{order_record.order_id}"
                if order_record.customer_id
                else order_record.order_id
            )
            order = deepcopy(
                order_extras.get(composite_key)
                or order_extras.get(order_record.order_id)
                or {}
            )
            pitstop_fields = set(order.pop("__pitstop_fields__", []))
            files = []
            extras_for_files = (
                file_extras.get(composite_key)
                or file_extras.get(order_record.order_id)
                or []
            )
            preview_paths = []
            for index, file_record in enumerate(
                sorted(order_record.files, key=lambda item: item.id)
            ):
                if isinstance(extras_for_files, dict):
                    extra = deepcopy(
                        extras_for_files.get(str(file_record.id)) or {}
                    )
                else:
                    # Backward compatibility for envelopes written before
                    # file extras were keyed by the stable database ID.
                    extra = (
                        deepcopy(extras_for_files[index])
                        if index < len(extras_for_files)
                        else {}
                    )
                extra.update(
                    {
                        "file_result_id": file_record.id,
                        "path": file_record.source_path,
                        "name": file_record.filename,
                        "actual_width_mm": file_record.width_mm,
                        "actual_height_mm": file_record.height_mm,
                        "dpi_x": file_record.dpi_x,
                        "dpi_y": file_record.dpi_y,
                        "actual_format": file_record.format,
                        "colorspace": file_record.colorspace,
                        "errors": _json_load(file_record.errors_json, []),
                        "warnings": _json_load(file_record.warnings_json, []),
                    }
                )
                files.append(extra)
                if file_record.preview_path:
                    preview_paths.append(file_record.preview_path)
            order.update(
                {
                    "order_id": order_record.order_id,
                    "customer_id": order_record.customer_id,
                    "status": order_record.status,
                    "passed": order_record.passed,
                    "errors": _json_load(order_record.errors_json, []),
                    "warnings": _json_load(order_record.warnings_json, []),
                    "files": files,
                    "pdf_path": order_record.pdf_path,
                }
            )
            current_revision = next(
                (
                    revision
                    for revision in order_record.pdf_revisions
                    if revision.is_current
                ),
                None,
            )
            if "current_pdf_revision" in pitstop_fields:
                order["current_pdf_revision"] = None
            if "current_pdf_sha256" in pitstop_fields:
                order["current_pdf_sha256"] = None
            if "pitstop" in pitstop_fields:
                order["pitstop"] = None
            if current_revision is not None:
                if "current_pdf_revision" in pitstop_fields:
                    order["current_pdf_revision"] = current_revision.revision_number
                if "current_pdf_sha256" in pitstop_fields:
                    order["current_pdf_sha256"] = current_revision.sha256
                if "pitstop" in pitstop_fields and current_revision.checks:
                    latest_check = max(
                        current_revision.checks,
                        key=lambda item: (
                            item.checked_at or item.created_at,
                            item.created_at,
                            item.id,
                        ),
                    )
                    order["pitstop"] = _pitstop_check_to_dto(
                        latest_check,
                        current_revision.revision_number,
                        current_revision.sha256,
                    )
            for field_name in (
                "source_status",
                "pitstop_status",
                "workflow_status",
            ):
                if field_name in pitstop_fields:
                    order[field_name] = getattr(order_record, field_name)
            if order_record.actions:
                action_values = []
                for action in sorted(order_record.actions, key=lambda item: item.id):
                    action_value = {
                        "id": action.id,
                        "action": action.action,
                        "comment": action.comment,
                        "status": action.status,
                        "created_at": _iso(action.created_at),
                    }
                    if action.cms_response_json:
                        try:
                            response_data = json.loads(action.cms_response_json)
                        except (TypeError, ValueError):
                            response_data = None
                        if isinstance(response_data, dict) and isinstance(
                            response_data.get("pitstop"), dict
                        ):
                            action_value["pitstop"] = response_data["pitstop"]
                    action_values.append(action_value)
                order["actions"] = action_values
            order.setdefault("preview_paths", preview_paths)
            storage_key = order.pop("__storage_key__", None)
            if not storage_key:
                # Orders persisted after the last envelope checkpoint of an
                # interrupted run have no extras; keep duplicate order numbers
                # of different customers apart.
                storage_key = (
                    composite_key
                    if order_record.order_id in run["orders"]
                    else order_record.order_id
                )
            run["orders"][storage_key] = order
        return run


def _issues_unchanged(
    existing: list[PitstopIssue], incoming: list[dict[str, Any]]
) -> bool:
    """True when the stored issue rows already match the incoming check."""
    if len(existing) != len(incoming):
        return False
    for record, issue in zip(
        sorted(existing, key=lambda item: item.id or 0), incoming
    ):
        incoming_id = issue.get("id")
        if incoming_id is None or str(incoming_id) != record.issue_id:
            # Issues without an ID get a fresh one on insert, so they can
            # never be proven identical; rewrite them.
            return False
        if (
            record.fingerprint,
            record.severity,
            record.action_id,
            record.message,
            record.occurrences,
            record.locations_json,
            *(getattr(record, name) for name in _ISSUE_BASELINE_COLUMNS),
        ) != (
            _optional_str(issue.get("fingerprint")),
            str(issue.get("severity") or "information"),
            _optional_str(issue.get("action_id")),
            str(issue.get("message") or ""),
            int(issue.get("occurrences") or 1),
            _json_dump(list(issue.get("locations") or [])),
            *_issue_baseline_values(issue).values(),
        ):
            return False
    return True


# Поля сравнения с исходной проверкой (services/pitstop/baseline.py); NULL — не было.
_ISSUE_BASELINE_COLUMNS = (
    "baseline_occurrences",
    "introduced_by_fix",
    "hidden_by_baseline",
    "original_severity",
)


def _issue_baseline_values(issue: dict[str, Any]) -> dict[str, Any]:
    hidden = issue.get("hidden_by_baseline")
    return {
        "baseline_occurrences": _optional_int(issue.get("baseline_occurrences")),
        "introduced_by_fix": _optional_int(issue.get("introduced_by_fix")),
        "hidden_by_baseline": None if hidden is None else bool(hidden),
        "original_severity": _optional_str(issue.get("original_severity")),
    }


def _issue_to_dto(issue: PitstopIssue) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": issue.issue_id,
        "fingerprint": issue.fingerprint,
        "severity": issue.severity,
        "action_id": issue.action_id,
        "message": issue.message,
        "occurrences": issue.occurrences,
        "locations": _json_load(issue.locations_json, []),
    }
    for name in _ISSUE_BASELINE_COLUMNS:
        value = getattr(issue, name)
        if value is not None:
            result[name] = value
    return result


def _file_status(value: dict[str, Any]) -> str:
    if value.get("resample_decision") == "ask_confirmation":
        return "waiting_confirmation"
    if value.get("errors"):
        return "error"
    if value.get("warnings"):
        return "warning"
    return "passed"


def _datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _pitstop_check_to_dto(
    check: PitstopCheck, revision_number: int, input_sha256: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "check_id": check.id,
        "execution_status": check.execution_status,
        "verdict": check.verdict,
        "checked_at": _iso(check.checked_at),
        "checked_revision": revision_number,
        "profile": {
            "key": check.profile_key,
            "name": check.profile_name,
            "version": check.profile_version,
        },
        "pages": check.page_count,
        "counts": {
            "errors": check.errors_count,
            "warnings": check.warnings_count,
            "fixes": check.fixes_count,
            "critical_failures": check.critical_failures_count,
            "noncritical_failures": check.noncritical_failures_count,
            "informations": check.informations_count,
        },
        "issues": [
            _issue_to_dto(issue)
            for issue in sorted(check.issues, key=lambda item: item.id)
        ],
        "reports": {
            "json_url": check.report_json_path,
            "xml_url": check.report_xml_path,
        },
    }
    if input_sha256:
        result["input_sha256"] = input_sha256
    if check.technical_error is not None:
        result["technical_error"] = check.technical_error
    if check.baseline_status is not None:
        result["baseline_status"] = check.baseline_status
    if check.verdict_raw is not None:
        result["verdict_raw"] = check.verdict_raw
    if check.counts_raw_json is not None:
        counts_raw = _json_load(check.counts_raw_json, None)
        if isinstance(counts_raw, dict):
            result["counts_raw"] = counts_raw
    return result


def _required_record_id(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    try:
        record_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if record_id <= 0 or str(value).strip() != str(record_id):
        raise ValueError(f"{field_name} must be a positive integer")
    return record_id


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return deepcopy(default)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return deepcopy(default)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
