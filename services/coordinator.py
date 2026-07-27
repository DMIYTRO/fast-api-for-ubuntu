"""Single-worker background coordinator for folder checks."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
import queue
import threading
import traceback
from typing import Any, Callable, Protocol
from uuid import uuid4

from processing.models import OrderCheck

from .batch_adapter import BatchProcessorAdapter, OrderArtifacts, ProcessingOptions
from .dto import order_check_to_dto
from .repository import RunEvent, RunRepository


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_ORDER_STATUSES = {"passed", "warning", "error", "cancelled"}
logger = logging.getLogger("image_magic.worker")


class RunNotFoundError(KeyError):
    pass


class ActiveRunError(RuntimeError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


class ProcessorAdapter(Protocol):
    def scan_and_inspect(self) -> list[OrderCheck]: ...

    def iter_inspect_orders(self): ...

    def pending_files(self, order: OrderCheck) -> list[Any]: ...

    def decide(self, order: OrderCheck, approved: bool) -> None: ...

    def process_order(self, order: OrderCheck) -> OrderArtifacts: ...


class _RunContext:
    def __init__(self, adapter: ProcessorAdapter) -> None:
        self.adapter = adapter
        self.orders: dict[str, OrderCheck] = {}
        self.cancel_requested = threading.Event()


class RunCoordinator:
    """Runs one check at a time while keeping subsequent checks queued.

    The worker is owned by application lifespan, not by an HTTP request.
    Confirmation pauses only the affected order; all other orders are handled
    before the run enters ``waiting_confirmation``.
    """

    def __init__(
        self,
        repository: RunRepository,
        *,
        adapter_factory: Callable[[ProcessingOptions], ProcessorAdapter] = (
            BatchProcessorAdapter
        ),
        autostart: bool = True,
    ) -> None:
        self.repository = repository
        self._adapter_factory = adapter_factory
        self._contexts: dict[str, _RunContext] = {}
        self._pending_runs: deque[str] = deque()
        self._commands: queue.Queue[tuple[str, str, str | None]] = queue.Queue()
        self._active_run_id: str | None = None
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        if autostart:
            self.start_worker()

    def start_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._worker_loop,
                name="image-magic-run-worker",
                daemon=True,
            )
            self._thread.start()
            logger.info("worker.started thread=%s", self._thread.name)

    def shutdown(self, timeout: float | None = 5.0) -> None:
        logger.info("worker.stopping active_run_id=%s", self._active_run_id or "-")
        with self._lock:
            for context in self._contexts.values():
                context.cancel_requested.set()
        self._stopping.set()
        self._commands.put(("stop", "", None))
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise TimeoutError("background worker did not stop in time")
        logger.info("worker.stopped")

    def submit(self, options: ProcessingOptions) -> dict[str, Any]:
        run_id = uuid4().hex
        now = _now()
        run: dict[str, Any] = {
            "id": run_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "options": options.as_dict(),
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "cancel_requested": False,
            "error": None,
            "total_orders": 0,
            "processed_orders": 0,
            "passed_orders": 0,
            "warning_orders": 0,
            "failed_orders": 0,
            "waiting_orders": 0,
            "orders": {},
        }
        # Constructing the adapter validates profile selection immediately but
        # does not perform heavy work.
        context = _RunContext(self._adapter_factory(options))
        with self._lock:
            self.repository.create_run(run)
            self._contexts[run_id] = context
            self._pending_runs.append(run_id)
            self._schedule_next_locked()
            self._changed.notify_all()
            logger.info(
                "run.queued run_id=%s input_path=%s direction=%s",
                run_id,
                options.input_path,
                options.direction,
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        value = self.repository.get_run(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return value

    def list_runs(self) -> list[dict[str, Any]]:
        return self.repository.list_runs()

    def get_active_run(self) -> dict[str, Any] | None:
        with self._lock:
            return (
                self.get_run(self._active_run_id)
                if self._active_run_id is not None
                else None
            )

    def events(self, run_id: str, after_id: int = 0) -> list[RunEvent]:
        self.get_run(run_id)
        return self.repository.list_events(run_id, after_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                return run
            run["cancel_requested"] = True
            self._contexts[run_id].cancel_requested.set()
            if run["status"] == "queued":
                try:
                    self._pending_runs.remove(run_id)
                except ValueError:
                    pass
                self._mark_cancelled_locked(run)
            elif run["status"] == "waiting_confirmation":
                # No worker command is running in this state, so cancellation
                # must be finalized synchronously.
                for item in run["orders"].values():
                    if item["status"] not in TERMINAL_ORDER_STATUSES:
                        item["status"] = "cancelled"
                self._mark_cancelled_locked(run)
            else:
                run["stage"] = "cancelling"
                self.repository.save_run(run)
                self._emit_locked(run_id, "run.cancelling", {})
            self._changed.notify_all()
            return self.get_run(run_id)

    def confirm_correction(self, run_id: str, order_id: str) -> dict[str, Any]:
        return self._decide(run_id, order_id, approved=True)

    def reject_correction(self, run_id: str, order_id: str) -> dict[str, Any]:
        return self._decide(run_id, order_id, approved=False)

    def apply_file_transition(
        self,
        run_id: str,
        order_id: str,
        *,
        status: str,
        source_paths: dict[str, str],
        pdf_path: str | None,
        preview_paths: list[str],
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist paths/status after a deliberate operator file transition."""
        with self._lock:
            run = self.get_run(run_id)
            order = run["orders"].get(order_id)
            if order is None:
                raise RunNotFoundError(f"{run_id}/{order_id}")
            for item in order.get("files") or []:
                old_path = str(item.get("path", ""))
                if old_path in source_paths:
                    item["path"] = source_paths[old_path]
            if pdf_path:
                order["pdf_path"] = pdf_path
            if preview_paths:
                order["preview_paths"] = preview_paths
            if errors:
                order["errors"] = [*order.get("errors", []), *errors]
                order["processing_errors"] = [
                    *order.get("processing_errors", []), *errors
                ]
                order["passed"] = False
            order["status"] = status
            self.repository.save_run(run)
            self._emit_locked(
                run_id,
                "order.file_transition",
                {"order_id": order_id, "status": status, "order": order},
            )
            self._changed.notify_all()
            return self.get_run(run_id)

    def wait_for(
        self,
        run_id: str,
        statuses: set[str] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Convenience for lifecycle/tests; HTTP integrations need not use it."""
        statuses = statuses or TERMINAL_RUN_STATUSES
        with self._changed:
            self._changed.wait_for(
                lambda: self.get_run(run_id)["status"] in statuses,
                timeout=timeout,
            )
            return self.get_run(run_id)

    def _decide(
        self, run_id: str, order_id: str, *, approved: bool
    ) -> dict[str, Any]:
        with self._lock:
            run = self.get_run(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                raise InvalidRunStateError("run is already finished")
            context = self._contexts.get(run_id)
            order = context.orders.get(order_id) if context else None
            stored_order = run["orders"].get(order_id)
            if order is None or not stored_order:
                raise RunNotFoundError(f"{run_id}/{order_id}")
            if stored_order["status"] != "waiting_confirmation":
                raise InvalidRunStateError("order is not waiting for confirmation")

            context.adapter.decide(order, approved)
            decision = "confirmed" if approved else "rejected"
            run["orders"][order_id] = order_check_to_dto(
                order, status=f"correction_{decision}"
            )
            run["status"] = "running"
            run["stage"] = "processing"
            self.repository.save_run(run)
            self._emit_locked(
                run_id,
                f"order.correction_{decision}",
                {"order_id": order_id},
            )
            self._commands.put(("resume", run_id, order_id))
            self._changed.notify_all()
            return self.get_run(run_id)

    def _schedule_next_locked(self) -> None:
        if self._active_run_id is not None or not self._pending_runs:
            return
        run_id = self._pending_runs.popleft()
        self._active_run_id = run_id
        self._commands.put(("start", run_id, None))

    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            command, run_id, order_id = self._commands.get()
            try:
                if command == "stop":
                    return
                existing = self.repository.get_run(run_id)
                if existing is None or existing["status"] in TERMINAL_RUN_STATUSES:
                    continue
                if command == "start":
                    self._run_initial(run_id)
                elif command == "resume" and order_id is not None:
                    self._resume_order(run_id, order_id)
            except Exception as exc:
                # Last-resort containment: a worker bug becomes run.failed and
                # cannot bubble into FastAPI or kill scheduling permanently.
                logger.exception(
                    "worker.command_failed command=%s run_id=%s order_id=%s",
                    command,
                    run_id,
                    order_id or "-",
                )
                self._fail_run(run_id, exc)
            finally:
                self._commands.task_done()

    def _run_initial(self, run_id: str) -> None:
        with self._lock:
            run = self.get_run(run_id)
            if run["cancel_requested"]:
                self._mark_cancelled_locked(run)
                return
            run["status"] = "running"
            run["stage"] = "scanning"
            run["started_at"] = _now()
            self.repository.save_run(run)
            self._emit_locked(run_id, "run.started", {})
            self._changed.notify_all()

        context = self._contexts[run_id]
        iterator_method = getattr(context.adapter, "iter_inspect_orders", None)
        known_total: int | None = None
        if callable(iterator_method):
            orders = iterator_method()
        else:
            # Compatibility for third-party/test adapters using the original
            # list API. The production BatchProcessorAdapter is incremental.
            batch = context.adapter.scan_and_inspect()
            known_total = len(batch)
            orders = iter(batch)

        inspected = 0
        for order in orders:
            if context.cancel_requested.is_set():
                self._cancel_remaining(run_id)
                return
            inspected += 1
            with self._lock:
                run = self.get_run(run_id)
                total_hint = (
                    known_total
                    if known_total is not None
                    else int(getattr(context.adapter, "total_orders", 0) or 0)
                )
                run["total_orders"] = max(
                    run["total_orders"], total_hint, inspected
                )
                run["stage"] = "processing"
                run["progress"] = max(run["progress"], 10)
                context.orders[order.order_id] = order
                detected_dto = order_check_to_dto(order, status="detected")
                run["orders"][order.order_id] = detected_dto
                self.repository.save_run(run)
                self._emit_locked(
                    run_id,
                    "order.detected",
                    {
                        "order_id": order.order_id,
                        "order": detected_dto,
                        "processed": inspected,
                        "total": run["total_orders"],
                    },
                )
                checked_status = _inspection_status(order)
                run["orders"][order.order_id] = order_check_to_dto(
                    order, status=checked_status
                )
                self.repository.save_run(run)
                self._emit_locked(
                    run_id,
                    "order.checked",
                    {"order_id": order.order_id, "status": checked_status},
                )
                self._changed.notify_all()

            pending_files = context.adapter.pending_files(order)
            correction_policy = run["options"].get("correction_policy", "ask")
            auto_approve = (
                run["options"].get("approve_corrections")
                or correction_policy == "auto"
            )
            auto_reject = correction_policy == "reject"
            if pending_files and (auto_approve or auto_reject):
                context.adapter.decide(order, auto_approve)
                self._emit_locked(
                    run_id,
                    (
                        "order.correction_confirmed"
                        if auto_approve
                        else "order.correction_rejected"
                    ),
                    {"order_id": order.order_id, "automatic": True},
                )
            elif pending_files:
                with self._lock:
                    run = self.get_run(run_id)
                    dto = order_check_to_dto(
                        order, status="waiting_confirmation"
                    )
                    run["orders"][order.order_id] = dto
                    self._update_counts(run)
                    self.repository.save_run(run)
                    self._emit_locked(
                        run_id,
                        "order.waiting_confirmation",
                        {
                            "order_id": order.order_id,
                            "order": dto,
                            "processed": run["processed_orders"],
                            "total": run["total_orders"],
                            "progress": run["progress"],
                        },
                    )
                continue
            self._process_one(run_id, order)

        with self._lock:
            run = self.get_run(run_id)
            run["total_orders"] = inspected
            if not inspected:
                run["progress"] = 100
            self.repository.save_run(run)
            self._emit_locked(
                run_id,
                "scan.progress",
                {"processed": inspected, "total": inspected, "progress": 100},
            )
            self._settle_run_locked(run)

    def _resume_order(self, run_id: str, order_id: str) -> None:
        context = self._contexts[run_id]
        if context.cancel_requested.is_set():
            self._cancel_remaining(run_id)
            return
        self._process_one(run_id, context.orders[order_id])
        with self._lock:
            self._settle_run_locked(self.get_run(run_id))

    def _process_one(self, run_id: str, order: OrderCheck) -> None:
        context = self._contexts[run_id]
        artifacts = context.adapter.process_order(order)
        with self._lock:
            run = self.get_run(run_id)
            status = _completed_order_status(order, artifacts)
            dto = order_check_to_dto(
                order,
                status=status,
                pdf_path=str(artifacts.pdf_path) if artifacts.pdf_path else None,
                preview_paths=[str(path) for path in artifacts.preview_paths],
                processing_errors=artifacts.errors,
            )
            run["orders"][order.order_id] = dto
            self._update_counts(run)
            self.repository.save_run(run)
            if artifacts.pdf_path:
                self._emit_locked(
                    run_id,
                    "pdf.created",
                    {"order_id": order.order_id, "path": str(artifacts.pdf_path)},
                )
            for path in artifacts.preview_paths:
                self._emit_locked(
                    run_id,
                    "preview.created",
                    {"order_id": order.order_id, "path": str(path)},
                )
            self._emit_locked(
                run_id,
                "order.completed",
                {
                    "order_id": order.order_id,
                    "status": status,
                    "order": dto,
                    "processed": run["processed_orders"],
                    "total": run["total_orders"],
                    "progress": run["progress"],
                },
            )
            self._changed.notify_all()

    def _settle_run_locked(self, run: dict[str, Any]) -> None:
        self._update_counts(run)
        if run["cancel_requested"]:
            self._mark_cancelled_locked(run)
            return
        waiting = run["waiting_orders"]
        terminal = sum(
            item["status"] in TERMINAL_ORDER_STATUSES
            for item in run["orders"].values()
        )
        if waiting:
            run["status"] = "waiting_confirmation"
            run["stage"] = "waiting_confirmation"
            run["progress"] = self._progress(run)
            self.repository.save_run(run)
        elif terminal == run["total_orders"]:
            run["status"] = "completed"
            run["stage"] = "completed"
            run["progress"] = 100
            run["finished_at"] = _now()
            self.repository.save_run(run)
            self._emit_locked(run["id"], "run.completed", self._summary(run))
            self._release_active_locked(run["id"])
        self._changed.notify_all()

    def _cancel_remaining(self, run_id: str) -> None:
        with self._lock:
            run = self.get_run(run_id)
            for order_id, item in run["orders"].items():
                if item["status"] not in TERMINAL_ORDER_STATUSES:
                    item["status"] = "cancelled"
            self._mark_cancelled_locked(run)

    def _mark_cancelled_locked(self, run: dict[str, Any]) -> None:
        run["status"] = "cancelled"
        run["stage"] = "cancelled"
        run["finished_at"] = _now()
        self._update_counts(run)
        self.repository.save_run(run)
        self._emit_locked(run["id"], "run.cancelled", self._summary(run))
        self._release_active_locked(run["id"])

    def _fail_run(self, run_id: str, exc: Exception) -> None:
        if not run_id:
            return
        with self._lock:
            try:
                run = self.get_run(run_id)
            except RunNotFoundError:
                return
            if run["status"] in TERMINAL_RUN_STATUSES:
                return
            run["status"] = "failed"
            run["stage"] = "failed"
            run["finished_at"] = _now()
            run["error"] = f"{type(exc).__name__}: {exc}"
            self.repository.save_run(run)
            self._emit_locked(
                run_id,
                "run.failed",
                {
                    "error": run["error"],
                    "traceback": traceback.format_exc(limit=5),
                },
            )
            self._release_active_locked(run_id)
            self._changed.notify_all()

    def _release_active_locked(self, run_id: str) -> None:
        if self._active_run_id == run_id:
            self._active_run_id = None
        self._contexts.pop(run_id, None)
        self._schedule_next_locked()

    def _update_counts(self, run: dict[str, Any]) -> None:
        statuses = [item["status"] for item in run["orders"].values()]
        run["processed_orders"] = sum(
            status in TERMINAL_ORDER_STATUSES for status in statuses
        )
        run["passed_orders"] = statuses.count("passed")
        run["warning_orders"] = statuses.count("warning")
        run["failed_orders"] = statuses.count("error")
        run["waiting_orders"] = statuses.count("waiting_confirmation")
        run["progress"] = self._progress(run)

    @staticmethod
    def _progress(run: dict[str, Any]) -> int:
        total = run["total_orders"]
        if not total:
            return 100 if run["stage"] != "scanning" else 0
        # Scanning occupies 10%; terminal orders the remaining 90%.
        return min(99, 10 + int(90 * run["processed_orders"] / total))

    @staticmethod
    def _summary(run: dict[str, Any]) -> dict[str, Any]:
        return {
            "processed": run["processed_orders"],
            "total": run["total_orders"],
            "passed": run["passed_orders"],
            "warnings": run["warning_orders"],
            "failed": run["failed_orders"],
        }

    def _emit_locked(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        event = self.repository.append_event(run_id, event_type, data)
        fields = {
            key: data[key]
            for key in (
                "order_id",
                "status",
                "processed",
                "total",
                "progress",
                "path",
                "reason",
                "previous_status",
            )
            if key in data
        }
        level = logging.ERROR if event_type == "run.failed" else logging.INFO
        logger.log(
            level,
            "run.event run_id=%s event=%s event_id=%s details=%s",
            run_id,
            event_type,
            event.id,
            fields or "-",
        )
        return event


def _inspection_status(order: OrderCheck) -> str:
    if any(item.resample_decision == "ask_confirmation" for item in order.files):
        return "waiting_confirmation"
    if not order.passed:
        return "error"
    if order.warnings or any(item.warnings for item in order.files):
        return "warning"
    return "passed"


def _completed_order_status(
    order: OrderCheck, artifacts: OrderArtifacts
) -> str:
    if artifacts.errors or not order.passed:
        return "error"
    if order.warnings or any(item.warnings for item in order.files):
        return "warning"
    return "passed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
