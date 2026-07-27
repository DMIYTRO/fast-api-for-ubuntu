"""SQLAlchemy implementation of the run persistence boundary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from server.models import (
    CheckRun,
    CorrectionDecision,
    FileResult,
    OrderAction,
    OrderResult,
)
from server.models import RunEvent as SqlRunEvent

from .repository import RunEvent


logger = logging.getLogger("image_magic.repository")


_OPTIONS_MARKER = "__run_repository__"
_INTERRUPTED_STATUSES = {
    "queued",
    "running",
    "waiting_confirmation",
    "cancelling",
}
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

    def save_run(self, run: dict[str, Any]) -> None:
        with self._session_factory() as session, session.begin():
            record = session.get(CheckRun, str(run["id"]))
            if record is None:
                raise KeyError(str(run["id"]))
            self._apply_run(session, record, run)

    def save_run_with_event(
        self, run: dict[str, Any], event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        with self._session_factory() as session, session.begin():
            run_id = str(run["id"])
            record = session.get(CheckRun, run_id)
            if record is None:
                raise KeyError(run_id)
            self._apply_run(session, record, run)
            session.flush()
            self._apply_correction_audit(session, run_id, event_type, data)
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

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            statement = (
                select(CheckRun)
                .where(CheckRun.id == run_id)
                .options(
                    selectinload(CheckRun.orders).selectinload(OrderResult.files),
                    selectinload(CheckRun.orders).selectinload(OrderResult.actions),
                )
            )
            record = session.scalar(statement)
            return self._run_to_dict(record) if record is not None else None

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

    def list_events(self, run_id: str, after_id: int = 0) -> list[RunEvent]:
        with self._session_factory() as session:
            statement = (
                select(SqlRunEvent)
                .where(
                    SqlRunEvent.run_id == run_id,
                    SqlRunEvent.id > after_id,
                )
                .order_by(SqlRunEvent.id)
            )
            return [
                RunEvent(
                    id=record.id,
                    type=record.event_type,
                    run_id=record.run_id,
                    data=_json_load(record.payload_json, {}),
                    created_at=_iso(record.created_at) or _now_iso(),
                )
                for record in session.scalars(statement)
            ]

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

    def _apply_run(
        self, session: Session, record: CheckRun, run: dict[str, Any]
    ) -> None:
        options = deepcopy(run.get("options") or {})
        record.input_path = str(options.get("input_path", ""))
        record.direction = str(options.get("direction", ""))
        record.status = str(run.get("status", "queued"))
        record.stage = _optional_str(run.get("stage"))
        record.progress = float(run.get("progress", 0))
        record.created_at = _datetime(run.get("created_at")) or datetime.now(
            timezone.utc
        )
        record.started_at = _datetime(run.get("started_at"))
        record.finished_at = _datetime(run.get("finished_at"))
        record.error = _optional_str(run.get("error"))
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
        record.passed = bool(order.get("passed", False))
        record.errors_json = _json_dump(list(order.get("errors") or []))
        record.warnings_json = _json_dump(list(order.get("warnings") or []))
        record.pdf_path = _optional_str(order.get("pdf_path"))

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
    def _options_envelope(
        run: dict[str, Any],
        file_ids_by_order: dict[str, list[int]],
    ) -> dict[str, Any]:
        run_extras = {
            key: deepcopy(value)
            for key, value in run.items()
            if key not in _RUN_STANDARD_KEYS
        }
        order_extras: dict[str, dict[str, Any]] = {}
        file_extras: dict[str, dict[str, dict[str, Any]]] = {}
        for order_id, order in (run.get("orders") or {}).items():
            order_extras[str(order_id)] = {
                key: deepcopy(value)
                for key, value in order.items()
                if key not in _ORDER_STANDARD_KEYS
            }
            order_extras[str(order_id)]["__storage_key__"] = str(order_id)
            ordered_file_ids = file_ids_by_order.get(str(order_id), [])
            file_extras[str(order_id)] = {
                str(ordered_file_ids[index]): {
                    key: deepcopy(value)
                    for key, value in item.items()
                    if key not in _FILE_MAPPED_KEYS
                }
                for index, item in enumerate(order.get("files") or [])
                if index < len(ordered_file_ids)
            }
        return {
            "options": deepcopy(run.get("options") or {}),
            _OPTIONS_MARKER: {
                "run_extras": run_extras,
                "order_extras": order_extras,
                "file_extras": file_extras,
            },
        }

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
            if order_record.actions:
                order["actions"] = [
                    {
                        "id": action.id,
                        "action": action.action,
                        "comment": action.comment,
                        "status": action.status,
                        "created_at": _iso(action.created_at),
                    }
                    for action in sorted(order_record.actions, key=lambda item: item.id)
                ]
            order.setdefault("preview_paths", preview_paths)
            storage_key = order.pop("__storage_key__", None)
            run["orders"][storage_key or order_record.order_id] = order
        return run


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
