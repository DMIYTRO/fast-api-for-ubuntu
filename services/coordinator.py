"""Single-worker background coordinator for folder checks."""

from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
from datetime import datetime, timezone
import inspect
import logging
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from processing.models import OrderCheck

from .batch_adapter import (
    BatchProcessorAdapter,
    OrderArtifacts,
    ProcessingOptions,
    completed_order_status as _completed_order_status,
)
from .callas_source_fix import ORDER_CHANGED_MESSAGE, mark_applied
from .domain import OrderStatus, RunStatus, validate_operator_transition
from .dto import order_check_to_dto
from .issue_groups import attach_issue_groups
from .repository import (
    ENVELOPE_DEFER,
    ENVELOPE_FULL,
    ENVELOPE_PATCH,
    RunEvent,
    RunRepository,
)


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}
TERMINAL_ORDER_STATUSES = {
    OrderStatus.PASSED,
    OrderStatus.WARNING,
    OrderStatus.ERROR,
    OrderStatus.CANCELLED,
}
# Orders mostly wait on external services (PitStop, callas, SMB), so the
# configured worker count may exceed the CPU count, up to this bound.
MAX_ORDER_WORKERS = 32
# Upper bound on worker threads left behind by timed-out orders in one run.
# Beyond it no replacement workers are started (see _OrderPool.abandon).
MAX_ABANDONED_ORDER_THREADS = 32
logger = logging.getLogger("image_magic.worker")


def _order_worker_count() -> int:
    """Return a bounded worker count for independent order processing.

    The default is the CPU count.  An explicit ``IMAGE_MAGIC_ORDER_WORKERS``
    may go above it (work is I/O bound) but never above
    ``max(MAX_ORDER_WORKERS, cpu_count)``.
    """
    available = os.cpu_count() or 1
    configured = os.environ.get("IMAGE_MAGIC_ORDER_WORKERS", "").strip()
    if not configured:
        return available
    try:
        requested = int(configured)
    except ValueError:
        return available
    return max(1, min(requested, max(MAX_ORDER_WORKERS, available)))


def _order_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("IMAGE_MAGIC_ORDER_TIMEOUT_SECONDS", "900")))
    except ValueError:
        return 900.0


def _envelope_checkpoint_seconds() -> float:
    try:
        return max(
            0.0,
            float(os.environ.get("IMAGE_MAGIC_ENVELOPE_CHECKPOINT_SECONDS", "5")),
        )
    except ValueError:
        return 5.0


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
    """In-memory state of a run that is queued, active or waiting.

    ``run`` is the live run aggregate and the source of truth while the
    context exists; it is mutated only under the coordinator lock.  The
    repository receives every change, but is not re-read on the hot path.
    ``summary`` is an immutable published copy without orders for lock-free
    status reads.
    """

    def __init__(self, adapter: ProcessorAdapter, run: dict[str, Any]) -> None:
        self.adapter = adapter
        self.run = run
        self.summary: dict[str, Any] = _summary_copy(run)
        self.orders: dict[str, OrderCheck] = {}
        self.order_keys: dict[int, str] = {}
        self.cancel_requested = threading.Event()
        self.status_counts: Counter[str] = Counter()
        # Explicit late-result guard markers (never matched by message text).
        self.timed_out_keys: set[str] = set()
        self.finished_keys: set[str] = set()
        self.last_checkpoint = time.monotonic()

    def add_order(self, order: OrderCheck) -> str:
        key = order.order_id
        if key in self.orders:
            key = order.aggregate_id
        self.orders[key] = order
        self.order_keys[id(order)] = key
        return key

    def key_for(self, order: OrderCheck) -> str:
        return self.order_keys[id(order)]


class _OrderPool:
    """Daemon worker pool whose capacity survives stuck (timed-out) orders.

    A Python thread cannot be killed.  When an order exceeds its timeout the
    coordinator abandons the worker running it: the thread keeps running as a
    daemon and exits when the call eventually returns, while a replacement
    worker is started so queued orders keep being processed.  Lifecycle
    messages are posted to ``events``: ``("started", key, monotonic)`` right
    before the task runs and ``("done", key, exception_or_None)`` after it.
    """

    def __init__(
        self,
        max_workers: int,
        *,
        name: str = "image-magic-order",
        max_abandoned: int = MAX_ABANDONED_ORDER_THREADS,
    ) -> None:
        self.events: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._tasks: queue.Queue[tuple[str, Callable[[], None]] | None] = (
            queue.Queue()
        )
        self._lock = threading.Lock()
        self._running: dict[str, threading.Thread] = {}
        self._abandoned: set[threading.Thread] = set()
        self._abandoned_total = 0
        self._max_abandoned = max_abandoned
        self._name = name
        self._spawned = 0
        self._live = 0
        self._closed = False
        with self._lock:
            for _ in range(max(1, max_workers)):
                self._spawn_locked()

    @property
    def live_workers(self) -> int:
        with self._lock:
            return self._live

    def _spawn_locked(self) -> None:
        self._spawned += 1
        self._live += 1
        threading.Thread(
            target=self._work,
            name=f"{self._name}-{self._spawned}",
            daemon=True,
        ).start()

    def submit(self, key: str, task: Callable[[], None]) -> None:
        self._tasks.put((key, task))

    def _work(self) -> None:
        me = threading.current_thread()
        while True:
            item = self._tasks.get()
            if item is None:
                return
            key, task = item
            with self._lock:
                self._running[key] = me
            self.events.put(("started", key, time.monotonic()))
            error: BaseException | None = None
            try:
                task()
            except BaseException as exc:  # reported to the coordinator thread
                error = exc
            with self._lock:
                if self._running.get(key) is me:
                    del self._running[key]
                abandoned = me in self._abandoned
                self._abandoned.discard(me)
            if abandoned:
                # The order was already finalized as timed out; this worker
                # was replaced, so it must not take more work.
                logger.warning(
                    "order.abandoned_worker_finished order_key=%s thread=%s",
                    key,
                    me.name,
                )
                return
            self.events.put(("done", key, error))

    def abandon(self, key: str) -> bool:
        """Give up on the worker running ``key``; start a replacement.

        Returns False when the task already finished (nothing to abandon) or
        when the replacement limit is reached.
        """
        with self._lock:
            thread = self._running.pop(key, None)
            if thread is None:
                return False
            self._abandoned.add(thread)
            self._abandoned_total += 1
            self._live -= 1
            if self._closed or self._abandoned_total > self._max_abandoned:
                return False
            self._spawn_locked()
            return True

    def drain(self) -> list[str]:
        """Remove tasks that have not started yet and return their keys."""
        keys: list[str] = []
        while True:
            try:
                item = self._tasks.get_nowait()
            except queue.Empty:
                return keys
            if item is not None:
                keys.append(item[0])

    def shutdown(self) -> list[str]:
        """Stop idle workers without waiting; abandoned ones exit on return."""
        dropped = self.drain()
        with self._lock:
            self._closed = True
            live = self._live
        for _ in range(live):
            self._tasks.put(None)
        return dropped


def _summary_copy(run: dict[str, Any]) -> dict[str, Any]:
    summary = {key: deepcopy(value) for key, value in run.items() if key != "orders"}
    summary["orders"] = {}
    return summary


class AmbiguousOrderError(ValueError):
    """A legacy bare order number maps to more than one customer/order."""


def _stored_order_key(run: dict[str, Any], order_id: str) -> str | None:
    orders = run.get("orders") or {}
    exact_matches = [
        key
        for key, value in orders.items()
        if value.get("aggregate_id") == order_id
        or (key == order_id and str(value.get("order_id") or value.get("id") or "") != order_id)
    ]
    if len(exact_matches) > 1:
        raise AmbiguousOrderError(order_id)
    if exact_matches:
        return exact_matches[0]
    number_matches = [
        key
        for key, value in orders.items()
        if str(value.get("order_id") or value.get("id") or "") == order_id
    ]
    if len(number_matches) > 1:
        raise AmbiguousOrderError(order_id)
    return number_matches[0] if number_matches else None


class RunCoordinator:
    """Runs one check at a time while keeping subsequent checks queued.

    The worker is owned by application lifespan, not by an HTTP request.
    Confirmation pauses only the affected order; all other orders are handled
    before the run enters ``waiting_confirmation``.

    While a run has a context (queued, active or waiting for a decision) its
    aggregate lives in memory and is the source of truth for reads.  Each
    per-order event is persisted with ``save_order_with_event`` (cost
    independent of the run size); the repository's whole-run compatibility
    envelope is rewritten at run-level transitions and at most every
    ``envelope_checkpoint_seconds`` during processing.
    """

    def __init__(
        self,
        repository: RunRepository,
        *,
        adapter_factory: Callable[[ProcessingOptions], ProcessorAdapter] = (
            BatchProcessorAdapter
        ),
        autostart: bool = True,
        order_timeout_seconds: float | None = None,
        order_workers: int | None = None,
        envelope_checkpoint_seconds: float | None = None,
    ) -> None:
        self.repository = repository
        self.order_timeout_seconds = (
            _order_timeout_seconds() if order_timeout_seconds is None
            else max(0.01, order_timeout_seconds)
        )
        self.order_workers = order_workers
        self.envelope_checkpoint_seconds = (
            _envelope_checkpoint_seconds()
            if envelope_checkpoint_seconds is None
            else max(0.0, envelope_checkpoint_seconds)
        )
        self._partial_saves = callable(
            getattr(repository, "save_order_with_event", None)
        )
        self._adapter_factory = adapter_factory
        self._contexts: dict[str, _RunContext] = {}
        self._pending_runs: deque[str] = deque()
        self._pending_resumes: deque[tuple[str, str]] = deque()
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
            "stage_label": "В очереди",
            "current_order": None,
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
        context = _RunContext(self._adapter_factory(options), run)
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

    # ------------------------------------------------------------------ reads

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            context = self._contexts.get(run_id)
            if context is not None:
                return deepcopy(context.run)
        value = self.repository.get_run(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return value

    def get_run_summary(self, run_id: str) -> dict[str, Any]:
        """Run fields without orders; lock-free for runs held in memory."""
        context = self._contexts.get(run_id)
        if context is not None:
            return deepcopy(context.summary)
        getter = getattr(self.repository, "get_run_summary", None)
        value = getter(run_id) if getter else self.repository.get_run(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        value.setdefault("orders", {})
        return value

    def active_run_id(self) -> str | None:
        """Id of the run currently owning the worker; no aggregate load."""
        return self._active_run_id

    def get_active_run_summary(self) -> dict[str, Any] | None:
        """Summary (no orders) of the active run; cheap, lock-free."""
        run_id = self._active_run_id
        if run_id is None:
            return None
        try:
            return self.get_run_summary(run_id)
        except RunNotFoundError:
            return None

    def run_exists(self, run_id: str) -> bool:
        if run_id in self._contexts:
            return True
        checker = getattr(self.repository, "run_exists", None)
        if callable(checker):
            return bool(checker(run_id))
        getter = getattr(self.repository, "get_run_summary", None)
        value = getter(run_id) if getter else self.repository.get_run(run_id)
        return value is not None

    def list_orders_page(
        self, run_id: str, *, page: int, page_size: int, status: str,
        search: str, active_only: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            context = self._contexts.get(run_id)
            if context is not None:
                return _orders_page_from_run(
                    context.run, page=page, page_size=page_size, status=status,
                    search=search, active_only=active_only,
                )
        getter = getattr(self.repository, "list_orders_page", None)
        if getter:
            result = getter(
                run_id, page=page, page_size=page_size, status=status,
                search=search, active_only=active_only,
            )
            if result is None:
                raise RunNotFoundError(run_id)
            return result
        return _orders_page_from_run(
            self.get_run(run_id), page=page, page_size=page_size,
            status=status, search=search, active_only=active_only,
        )

    def find_file_by_id(self, file_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Use the repository's stable file index when available."""
        finder = getattr(self.repository, "find_file_by_id", None)
        if finder:
            match = finder(file_id)
            if match is None:
                return None
            run_id = str(match[0].get("id") or "")
            with self._lock:
                context = self._contexts.get(run_id)
                if context is None:
                    return match
                # The stored extras of an active run may lag the live
                # aggregate (checkpointed envelope); answer from memory.
                for key, order in (context.run.get("orders") or {}).items():
                    for item in order.get("files") or []:
                        if str(item.get("file_result_id") or "") == str(file_id):
                            run = _summary_copy(context.run)
                            run["orders"] = {key: deepcopy(order)}
                            return run, deepcopy(item)
            return match
        for run in self.list_runs():
            for order in (run.get("orders") or {}).values():
                for item in order.get("files") or []:
                    if str(item.get("file_result_id") or item.get("id") or "") == file_id:
                        return run, item
        return None

    def list_runs(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include_orders: bool = True,
    ) -> list[dict[str, Any]]:
        runs = self.repository.list_runs(
            limit=limit, offset=offset, include_orders=include_orders
        )
        # Runs held in memory are fresher than their stored counters.
        for index, run in enumerate(runs):
            context = self._contexts.get(str(run.get("id")))
            if context is None:
                continue
            if include_orders:
                with self._lock:
                    runs[index] = deepcopy(context.run)
            else:
                runs[index] = deepcopy(context.summary)
        return runs

    def count_runs(self) -> int:
        return self.repository.count_runs()

    def get_active_run(self) -> dict[str, Any] | None:
        with self._lock:
            return (
                self.get_run(self._active_run_id)
                if self._active_run_id is not None
                else None
            )

    def events(
        self, run_id: str, after_id: int = 0, limit: int | None = None
    ) -> list[RunEvent]:
        if not self.run_exists(run_id):
            raise RunNotFoundError(run_id)
        if limit is None:
            return self.repository.list_events(run_id, after_id)
        if _accepts_limit(self.repository.list_events):
            return self.repository.list_events(run_id, after_id, limit=limit)
        return self.repository.list_events(run_id, after_id)[: max(0, limit)]

    # --------------------------------------------------------------- commands

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._load_run_locked(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                return deepcopy(run)
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
                self._mark_cancelled_locked(run)
            else:
                run["stage"] = "cancelling"
                self._save_and_emit_locked(
                    run, "run.cancelling", {}, include_orders=False
                )
            self._changed.notify_all()
            return self.get_run(run_id)

    def confirm_correction(self, run_id: str, order_id: str) -> dict[str, Any]:
        return self._decide(run_id, order_id, approved=True)

    def reject_correction(self, run_id: str, order_id: str) -> dict[str, Any]:
        return self._decide(run_id, order_id, approved=False)

    def apply_source_fix(
        self,
        run_id: str,
        order_id: str,
        *,
        source_path: Path | str,
        content_path: Path | None,
        fix: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Use (or stop using) an accepted Callas fix of one source PDF of a waiting order.

        The file is re-inspected from ``content_path`` (``None`` — the source
        itself again) with all normal PDF rules; the inspection runs outside
        the coordinator lock and is applied only if the order did not change
        meanwhile.  When no file of the order waits for a size decision any
        more, the order resumes exactly like a confirmed correction.
        """
        with self._lock:
            context, stored_key, order, index, item = self._waiting_source_file_locked(
                run_id, order_id, source_path
            )
            reinspect = getattr(context.adapter, "reinspect_file", None)
            revalidate = getattr(context.adapter, "revalidate_order", None)
            if reinspect is None or revalidate is None:
                raise InvalidRunStateError(
                    "этот режим проверки не поддерживает исправленный PDF исходника"
                )
        replacement = reinspect(item, content_path)
        if content_path is not None:
            mark_applied(replacement, fix or {})
        with self._lock:
            current = self._waiting_source_file_locked(run_id, order_id, source_path)
            if current[0] is not context or current[2] is not order or current[4] is not item:
                raise InvalidRunStateError(ORDER_CHANGED_MESSAGE)
            run = context.run
            order.files[index] = replacement
            revalidate(order)
            pending = context.adapter.pending_files(order)
            file_data = {
                "aggregate_id": order.aggregate_id,
                "order_id": order.order_id,
                "customer_id": order.customer_id,
                "file": item.path.name,
                "applied": content_path is not None,
            }
            if pending or content_path is None:
                self._commit_order_locked(
                    run,
                    stored_key,
                    order_check_to_dto(order, status="waiting_confirmation"),
                    "order.callas_source_fix",
                    lambda dto: {**file_data, "order": dto},
                    envelope=ENVELOPE_PATCH,
                )
                self._changed.notify_all()
                return self.get_run(run_id)
            # Ни один файл больше не ждёт решения: заказ продолжается сам.
            audit = self._source_fix_audit(run, stored_key, order)
            previous_status = (run["status"], run["stage"])
            run["status"] = "running"
            run["stage"] = "processing"
            try:
                self._commit_order_locked(
                    run,
                    stored_key,
                    order_check_to_dto(order, status="correction_confirmed"),
                    "order.correction_confirmed",
                    lambda _dto: {
                        **file_data,
                        "callas_source_fix": True,
                        "decisions": audit,
                    },
                    update_counts=False,
                    envelope=ENVELOPE_PATCH,
                )
            except Exception:
                run["status"], run["stage"] = previous_status
                raise
            self._pending_resumes.append((run_id, stored_key))
            self._schedule_next_locked()
            self._changed.notify_all()
            return self.get_run(run_id)

    def _waiting_source_file_locked(
        self, run_id: str, order_id: str, source_path: Path | str
    ) -> tuple[_RunContext, str, OrderCheck, int, Any]:
        run = self._load_run_locked(run_id)
        context = self._contexts.get(run_id)
        if run["status"] in TERMINAL_RUN_STATUSES or context is None:
            raise InvalidRunStateError(
                "Проверка уже завершена или прервана перезапуском сервиса: "
                "запустите проверку заново и примите исправление в новой проверке."
            )
        stored_key = _stored_order_key(run, order_id)
        order = context.orders.get(stored_key) if stored_key else None
        stored_order = run["orders"].get(stored_key) if stored_key else None
        if order is None or not stored_order:
            raise RunNotFoundError(f"{run_id}/{order_id}")
        if stored_order["status"] != "waiting_confirmation" or stored_order.get("pdf_path"):
            raise InvalidRunStateError(
                "Заказ уже не ждёт решения по коррекции: исправленный PDF исходника "
                "можно принять только до сборки PDF заказа."
            )
        target = Path(source_path).resolve()
        for index, item in enumerate(order.files):
            if Path(item.path).resolve() == target:
                return context, stored_key, order, index, item
        raise RunNotFoundError(f"{run_id}/{order_id}/{target.name}")

    @staticmethod
    def _source_fix_audit(
        run: dict[str, Any], stored_key: str, order: OrderCheck
    ) -> list[dict[str, Any]]:
        """Correction audit rows for files built from an accepted Callas source fix."""
        stored_files = (run["orders"].get(stored_key) or {}).get("files") or []
        ids = {
            str(item.get("path") or ""): item.get("file_result_id")
            for item in stored_files
        }
        audit = []
        for item in order.files:
            if item.callas_source_fix is None:
                continue
            entry: dict[str, Any] = {
                "path": str(item.path),
                "decision": "callas_source_fix",
                "original": {"source": dict(item.callas_source_fix.get("source") or {})},
                "proposed": {
                    "plan": item.callas_source_fix.get("plan"),
                    "pdf_path": str(item.content_path) if item.content_path else None,
                    "width_mm": item.actual_width_mm,
                    "height_mm": item.actual_height_mm,
                },
            }
            if ids.get(str(item.path)) is not None:
                entry["file_result_id"] = ids[str(item.path)]
            audit.append(entry)
        return audit

    def order_in_progress(self, run_id: str, order_id: str) -> bool:
        """Заказ ещё обрабатывается активной проверкой: её итог перезапишет перенос файлов.

        Заказ, ожидающий подтверждения коррекции, обработкой не считается.
        """
        with self._lock:
            context = self._contexts.get(run_id)
            if context is None:
                return False
            try:
                key = _stored_order_key(context.run, order_id) or order_id
            except AmbiguousOrderError:
                return False
            if key not in context.orders:
                return False
            if key in context.finished_keys or key in context.timed_out_keys:
                return False
            order = (context.run.get("orders") or {}).get(key) or {}
            return order.get("status") != "waiting_confirmation"

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
        pitstop: dict[str, Any] | None = None,
        current_pdf_revision: int | None = None,
        current_pdf_sha256: str | None = None,
        confirm_failed_processing: bool = False,
    ) -> dict[str, Any]:
        """Persist paths/status after a deliberate operator file transition."""
        with self._lock:
            run = self._load_run_locked(run_id)
            stored_key = _stored_order_key(run, order_id)
            order = run["orders"].get(stored_key) if stored_key else None
            if order is None:
                raise RunNotFoundError(f"{run_id}/{order_id}")
            validate_operator_transition(
                str(order["status"]),
                status,
                confirm_failed_processing=confirm_failed_processing,
            )
            updated = deepcopy(order)
            for item in updated.get("files") or []:
                old_path = str(item.get("path", ""))
                if old_path in source_paths:
                    item["path"] = source_paths[old_path]
            if pdf_path:
                updated["pdf_path"] = pdf_path
            if preview_paths:
                updated["preview_paths"] = preview_paths
            if pitstop is not None:
                updated["pitstop"] = pitstop
            if ((current_pdf_revision is not None and current_pdf_revision != updated.get("current_pdf_revision"))
                    or (current_pdf_sha256 is not None and current_pdf_sha256 != updated.get("current_pdf_sha256"))):
                updated["ink_coverage"] = {"status": "not_checked", "flags": [], "pages": [], "error": "PDF изменён: требуется повторная проверка красочности."}
            if current_pdf_revision is not None:
                updated["current_pdf_revision"] = current_pdf_revision
            if current_pdf_sha256 is not None:
                updated["current_pdf_sha256"] = current_pdf_sha256
            if errors:
                updated["errors"] = [*updated.get("errors", []), *errors]
                updated["processing_errors"] = [
                    *updated.get("processing_errors", []), *errors
                ]
                updated["passed"] = False
            attach_issue_groups(updated)
            updated["status"] = status
            self._commit_order_locked(
                run,
                stored_key,
                updated,
                "order.file_transition",
                lambda dto: {
                    "aggregate_id": dto.get("aggregate_id") or stored_key,
                    "order_id": dto.get("order_id") or order_id,
                    "customer_id": dto.get("customer_id"),
                    "status": status,
                    "order": dto,
                },
                update_counts=False,
                envelope=ENVELOPE_PATCH,
            )
            self._changed.notify_all()
            return self.get_run(run_id)

    def apply_pdf_revision(
        self,
        run_id: str,
        order_id: str,
        *,
        status: str,
        pdf_path: str,
        pitstop: dict[str, Any] | None,
        current_pdf_revision: int,
        current_pdf_sha256: str,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        preview_paths: list[str] | None = None,
        color_conversion: dict[str, Any] | None = None,
        prepress: dict[str, Any] | None = None,
        pitstop_baseline: dict[str, Any] | None = None,
        invalidate_pitstop_baseline: bool = False,
        created_by_action: str | None = None,
    ) -> dict[str, Any]:
        """Replace the production PDF with a re-checked revision.

        ``created_by_action`` (e.g. ``forced_print_build``) is stored as
        ``current_pdf_created_by`` and becomes ``pdf_revisions.created_by_action``.

        This is a system transition (a corrected PDF was re-inspected and
        routed into its status folder), so the operator transition rules do
        not apply; the order status becomes the recomputed one.  Returns the
        updated order DTO.

        ``pitstop_baseline`` (PitStop DTO of the order's original PDF) is
        stored only once and never overwritten.  ``invalidate_pitstop_baseline``
        records that a revision not made by a Callas fix replaced the PDF, so
        later fixes are no longer compared with the original check.
        """
        if status not in {"passed", "warning", "error"}:
            raise ValueError(f"недопустимый статус ревизии PDF: {status}")
        with self._lock:
            run = self._load_run_locked(run_id)
            stored_key = _stored_order_key(run, order_id)
            order = run["orders"].get(stored_key) if stored_key else None
            if order is None:
                raise RunNotFoundError(f"{run_id}/{order_id}")
            updated = deepcopy(order)
            updated["pdf_path"] = pdf_path
            updated["pdf_kind"] = "production"
            updated["pitstop"] = pitstop
            updated["pitstop_status"] = _pitstop_status_from_dto(pitstop)
            updated.pop("pitstop_skipped_reason", None)
            updated["current_pdf_revision"] = current_pdf_revision
            updated["current_pdf_sha256"] = current_pdf_sha256
            if created_by_action:
                updated["current_pdf_created_by"] = created_by_action
            else:
                updated.pop("current_pdf_created_by", None)
            updated["ink_coverage"] = {
                "status": "not_checked", "flags": [], "pages": [],
                "error": "PDF изменён: требуется повторная проверка красочности.",
            }
            updated["preview_paths"] = list(preview_paths or [])
            updated["revision_check"] = {"errors": errors or [], "warnings": warnings or []}
            if color_conversion is not None:
                updated["color_conversion"] = color_conversion
            if prepress is not None:
                updated["prepress"] = prepress
            if pitstop_baseline is not None and updated.get("pitstop_baseline") is None:
                updated["pitstop_baseline"] = deepcopy(pitstop_baseline)
            if (
                invalidate_pitstop_baseline
                and updated.get("pitstop_baseline") is not None
                and updated.get("pitstop_baseline_invalid_since_revision") is None
            ):
                updated["pitstop_baseline_invalid_since_revision"] = current_pdf_revision
            updated["processing_errors"] = list(errors or [])
            updated["status"] = status
            updated["passed"] = status in {"passed", "warning"}
            attach_issue_groups(updated)
            self._commit_order_locked(
                run,
                stored_key,
                updated,
                "order.pdf_revision",
                lambda dto: {
                    "aggregate_id": dto.get("aggregate_id") or stored_key,
                    "order_id": dto.get("order_id") or order_id,
                    "customer_id": dto.get("customer_id"),
                    "status": status,
                    "order": dto,
                },
                envelope=ENVELOPE_PATCH,
            )
            self._changed.notify_all()
            return deepcopy(run["orders"][stored_key])

    def restore_order_snapshot(
        self, run_id: str, order_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any]:
        """Restore an order after a compensated filesystem transition."""
        with self._lock:
            run = self._load_run_locked(run_id)
            stored_key = _stored_order_key(run, order_id)
            if stored_key is None:
                raise RunNotFoundError(f"{run_id}/{order_id}")
            self._commit_order_locked(
                run,
                stored_key,
                deepcopy(snapshot),
                "order.file_transition_rolled_back",
                lambda dto: {
                    "aggregate_id": dto.get("aggregate_id") or stored_key,
                    "order_id": dto.get("order_id") or order_id,
                    "customer_id": dto.get("customer_id"),
                    "status": dto.get("status"),
                },
                envelope=ENVELOPE_PATCH,
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
                lambda: self._run_status(run_id) in statuses,
                timeout=timeout,
            )
            return self.get_run(run_id)

    def _decide(
        self, run_id: str, order_id: str, *, approved: bool
    ) -> dict[str, Any]:
        with self._lock:
            run = self._load_run_locked(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                raise InvalidRunStateError("run is already finished")
            context = self._contexts.get(run_id)
            stored_key = _stored_order_key(run, order_id)
            order = context.orders.get(stored_key) if context and stored_key else None
            stored_order = run["orders"].get(stored_key) if stored_key else None
            if order is None or not stored_order:
                raise RunNotFoundError(f"{run_id}/{order_id}")
            if stored_order["status"] != "waiting_confirmation":
                raise InvalidRunStateError("order is not waiting for confirmation")

            stored_files_by_path: dict[str, list[dict[str, Any]]] = {}
            for stored_file in stored_order.get("files") or []:
                stored_files_by_path.setdefault(
                    str(stored_file.get("path") or ""), []
                ).append(stored_file)
            decision_audit = []
            for item in context.adapter.pending_files(order):
                audit = {
                    "path": str(item.path),
                    "decision": "confirmed" if approved else "rejected",
                    "original": {
                        "width_mm": item.actual_width_mm,
                        "height_mm": item.actual_height_mm,
                        "dpi_x": item.dpi_x,
                        "dpi_y": item.dpi_y,
                        "resample_decision": item.resample_decision,
                    },
                    "proposed": {
                        "target_mm": list(item.resample_target_mm)
                        if item.resample_target_mm
                        else None,
                        "crop_mm": list(item.resample_crop_mm),
                        "scale": item.resample_scale,
                        "rotation_degrees": item.rotation_degrees,
                    },
                }
                matching_files = stored_files_by_path.get(str(item.path), [])
                if matching_files:
                    stored_file = matching_files.pop(0)
                    if stored_file.get("file_result_id") is not None:
                        audit["file_result_id"] = stored_file["file_result_id"]
                decision_audit.append(audit)
            # Файлы с принятым исправлением Callas исходника решением «Применить» /
            # «Оставить без изменений» не затрагиваются: они собираются из исправленного PDF.
            decided_paths = {entry["path"] for entry in decision_audit}
            decision_audit.extend(
                entry
                for entry in self._source_fix_audit(run, stored_key, order)
                if entry["path"] not in decided_paths
            )
            context.adapter.decide(order, approved)
            decision = "confirmed" if approved else "rejected"
            previous_status = (run["status"], run["stage"])
            run["status"] = "running"
            run["stage"] = "processing"
            try:
                self._commit_order_locked(
                    run,
                    stored_key,
                    order_check_to_dto(order, status=f"correction_{decision}"),
                    f"order.correction_{decision}",
                    lambda _dto: {
                        "aggregate_id": order.aggregate_id,
                        "order_id": order.order_id,
                        "customer_id": order.customer_id,
                        "decisions": decision_audit,
                    },
                    update_counts=False,
                    envelope=ENVELOPE_PATCH,
                )
            except Exception:
                run["status"], run["stage"] = previous_status
                raise
            self._pending_resumes.append((run_id, stored_key))
            self._schedule_next_locked()
            self._changed.notify_all()
            return self.get_run(run_id)

    # -------------------------------------------------------------- scheduling

    def _run_status(self, run_id: str) -> str | None:
        context = self._contexts.get(run_id)
        if context is not None:
            return str(context.run.get("status"))
        try:
            return str(self.get_run_summary(run_id).get("status"))
        except RunNotFoundError:
            return None

    def _load_run_locked(self, run_id: str) -> dict[str, Any]:
        """Live aggregate for runs in memory, otherwise a stored copy."""
        context = self._contexts.get(run_id)
        if context is not None:
            return context.run
        value = self.repository.get_run(run_id)
        if value is None:
            raise RunNotFoundError(run_id)
        return value

    def _schedule_next_locked(self) -> None:
        if self._active_run_id is not None:
            status = self._run_status(self._active_run_id)
            if status is not None and status not in TERMINAL_RUN_STATUSES:
                return
            stale_run_id = self._active_run_id
            self._active_run_id = None
            self._contexts.pop(stale_run_id, None)
            logger.warning(
                "scheduler.recovered_stale_active run_id=%s status=%s",
                stale_run_id,
                status or "missing",
            )
        if self._pending_resumes:
            run_id, order_id = self._pending_resumes.popleft()
            self._active_run_id = run_id
            self._commands.put(("resume", run_id, order_id))
            return
        if not self._pending_runs:
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
                status = self._run_status(run_id)
                if status is None or status in TERMINAL_RUN_STATUSES:
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

    # -------------------------------------------------------------- run phases

    def _run_initial(self, run_id: str) -> None:
        with self._lock:
            context = self._contexts[run_id]
            run = context.run
            if run["cancel_requested"]:
                self._mark_cancelled_locked(run)
                return
            run["status"] = "running"
            run["stage"] = "scanning"
            run["stage_label"] = "Поиск заказов и проверка файлов"
            run["started_at"] = _now()
            self._save_and_emit_locked(
                run, "run.started", {}, include_orders=False
            )
            self._changed.notify_all()

        # Ответ Sborka (комментарий заказчика) может прийти, когда карточки уже показаны,
        # в том числе у заказов, ждущих подтверждения: адаптер сообщит об этом.
        set_listener = getattr(context.adapter, "set_order_info_listener", None)
        if callable(set_listener):
            set_listener(lambda: self._apply_late_customer_comments(run_id, context))

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
        submitted: dict[str, OrderCheck] = {}
        pool = _OrderPool(
            self.order_workers if self.order_workers is not None
            else _order_worker_count(),
            name="image-magic-order",
            max_abandoned=MAX_ABANDONED_ORDER_THREADS,
        )
        try:
            for order in orders:
                if context.cancel_requested.is_set():
                    pool.shutdown()
                    self._cancel_remaining(run_id)
                    return
                inspected += 1
                with self._lock:
                    total_hint = (
                        known_total
                        if known_total is not None
                        else int(getattr(context.adapter, "total_orders", 0) or 0)
                    )
                    run["total_orders"] = max(
                        run["total_orders"], total_hint, inspected
                    )
                    run["stage"] = "processing"
                    run["stage_label"] = f"Обработка заказа {order.order_id}"
                    run["current_order"] = order.order_id
                    run["progress"] = max(run["progress"], 10)
                    aggregate_id = context.add_order(order)
                    # Если ответ Sborka пришёл между проверкой заказа и этим местом,
                    # слушатель его карточку ещё не видел: берём комментарий здесь.
                    self._take_customer_comment(context, order)
                    self._commit_order_locked(
                        run,
                        aggregate_id,
                        order_check_to_dto(order, status="detected"),
                        "order.detected",
                        lambda dto: {
                            "aggregate_id": order.aggregate_id,
                            "order_id": order.order_id,
                            "customer_id": order.customer_id,
                            "order": dto,
                            "processed": inspected,
                            "total": run["total_orders"],
                        },
                        update_counts=False,
                    )
                    checked_status = _inspection_status(order)
                    effective_checked_status = (
                        "processing"
                        if checked_status in {"passed", "warning"}
                        and bool(getattr(context.adapter, "pitstop_enabled", False))
                        else checked_status
                    )
                    self._commit_order_locked(
                        run,
                        aggregate_id,
                        order_check_to_dto(
                            order,
                            status=effective_checked_status,
                            source_status=checked_status,
                        ),
                        "order.checked",
                        lambda _dto: {
                            "aggregate_id": order.aggregate_id,
                            "order_id": order.order_id,
                            "customer_id": order.customer_id,
                            "status": effective_checked_status,
                            "source_status": checked_status,
                        },
                        update_counts=False,
                    )
                    self._changed.notify_all()

                pending_files = context.adapter.pending_files(order)
                correction_policy = run["options"].get("correction_policy", "ask")
                # Адаптер, где автоматический режим сам применяет малые коррекции
                # (BatchProcessorAdapter), оставляет в ожидании только коррекции выше
                # предела: их подтверждает оператор, а не флаг «автоматически».
                auto_approve = (
                    run["options"].get("approve_corrections")
                    or correction_policy == "auto"
                ) and getattr(context.adapter, "auto_approves_pending", True)
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
                        {
                            "aggregate_id": order.aggregate_id,
                            "order_id": order.order_id,
                            "customer_id": order.customer_id,
                            "automatic": True,
                        },
                    )
                elif pending_files:
                    with self._lock:
                        self._commit_order_locked(
                            run,
                            aggregate_id,
                            order_check_to_dto(order, status="waiting_confirmation"),
                            "order.waiting_confirmation",
                            lambda dto: {
                                "aggregate_id": order.aggregate_id,
                                "order_id": order.order_id,
                                "customer_id": order.customer_id,
                                "order": dto,
                                "processed": run["processed_orders"],
                                "total": run["total_orders"],
                                "progress": run["progress"],
                            },
                        )
                    continue
                submitted[aggregate_id] = order
                pool.submit(
                    aggregate_id,
                    lambda order=order: self._process_one(run_id, order, context),
                )
            self._await_orders(run_id, context, pool, submitted)
        finally:
            pool.shutdown()

        with self._lock:
            run["total_orders"] = inspected
            if not inspected:
                run["progress"] = 100
            self._save_and_emit_locked(
                run,
                "scan.progress",
                {"processed": inspected, "total": inspected, "progress": 100},
                include_orders=False,
            )
            self._settle_run_locked(run)

    @staticmethod
    def _take_customer_comment(context: _RunContext, order: OrderCheck) -> str | None:
        """Customer comment of *order* if Sborka has already answered (never waits)."""
        getter = getattr(context.adapter, "customer_comment_for", None)
        if not callable(getter):
            return None
        try:
            text = getter(order)
        except Exception:  # noqa: BLE001 — Sborka must never break the check
            logger.exception(
                "order.customer_comment_failed order_id=%s", order.order_id
            )
            return None
        if text:
            order.customer_comment = text
        return text or None

    def _apply_late_customer_comments(
        self, run_id: str, context: _RunContext
    ) -> None:
        """Add the customer comment to cards that were shown before Sborka answered.

        Runs in the Sborka thread.  Under the coordinator lock the comment is set on
        the live ``OrderCheck`` (so later rebuilds — confirmation, source fix,
        completion — keep it) and on the stored order, which is persisted and
        pushed as ``order.customer_comment`` like any other order update.  An
        order whose stored card already has the text is left alone.
        """
        with self._lock:
            run = context.run
            if (
                self._contexts.get(run_id) is not context
                or run["status"] in TERMINAL_RUN_STATUSES
            ):
                return
            for key, order in list(context.orders.items()):
                text = self._take_customer_comment(context, order)
                stored = run["orders"].get(key)
                if not text or stored is None or stored.get("customer_comment") == text:
                    continue
                updated = deepcopy(stored)
                updated["customer_comment"] = text
                try:
                    self._commit_order_locked(
                        run,
                        key,
                        updated,
                        "order.customer_comment",
                        lambda dto, order=order: {
                            "aggregate_id": order.aggregate_id,
                            "order_id": order.order_id,
                            "customer_id": order.customer_id,
                            "status": dto.get("status"),
                            "order": dto,
                        },
                        update_counts=False,
                    )
                except Exception:  # noqa: BLE001 — a lost comment must not break the run
                    logger.exception(
                        "order.customer_comment_save_failed run_id=%s order_id=%s",
                        run_id,
                        order.order_id,
                    )
            self._changed.notify_all()

    def _await_orders(
        self,
        run_id: str,
        context: _RunContext,
        pool: _OrderPool,
        submitted: dict[str, OrderCheck],
    ) -> None:
        """Wait for queued orders; time each one from its actual start.

        A queued order never times out for waiting.  A timed-out order is
        finalized as an error and its worker is abandoned and replaced, so
        orders that have not started still run on the remaining workers.
        """
        outstanding = set(submitted)
        started: dict[str, float] = {}
        cancel_handled = False
        while outstanding:
            if context.cancel_requested.is_set() and not cancel_handled:
                cancel_handled = True
                for key in pool.drain():
                    outstanding.discard(key)
            if pool.live_workers <= 0:
                not_started = [key for key in pool.drain() if key in outstanding]
                for key in not_started:
                    outstanding.discard(key)
                    self._mark_order_not_started(run_id, submitted[key], context)
            now = time.monotonic()
            for key in [
                key for key in outstanding
                if key in started and now - started[key] >= self.order_timeout_seconds
            ]:
                started.pop(key)
                if self._mark_order_timeout(run_id, submitted[key], context):
                    outstanding.discard(key)
                    replaced = pool.abandon(key)
                    logger.warning(
                        "order.timeout_worker_abandoned run_id=%s order_key=%s "
                        "replacement_started=%s",
                        run_id,
                        key,
                        replaced,
                    )
                # Otherwise the result was accepted just before the deadline
                # and its "done" message is on the way.
            if not outstanding:
                break
            deadlines = [
                started[key] + self.order_timeout_seconds
                for key in outstanding
                if key in started
            ]
            # Wake at least once a second to react to cancellation.
            wait_seconds = 1.0
            if deadlines:
                wait_seconds = min(wait_seconds, max(0.0, min(deadlines) - time.monotonic()))
            try:
                message = pool.events.get(timeout=wait_seconds)
            except queue.Empty:
                continue
            kind, key = message[0], message[1]
            if kind == "started":
                if key in outstanding:
                    started[key] = message[2]
            elif kind == "done":
                outstanding.discard(key)
                started.pop(key, None)
                error = message[2]
                if error is not None:
                    raise error

    def _resume_order(self, run_id: str, order_id: str) -> None:
        context = self._contexts[run_id]
        if context.cancel_requested.is_set():
            self._cancel_remaining(run_id)
            return
        self._process_one(run_id, context.orders[order_id], context)
        with self._lock:
            if self._contexts.get(run_id) is context:
                self._settle_run_locked(context.run)

    def _mark_order_timeout(
        self, run_id: str, order: OrderCheck, context: _RunContext
    ) -> bool:
        """Finalize a timed-out order; False if its result already won."""
        with self._lock:
            if self._contexts.get(run_id) is not context:
                return False
            key = context.key_for(order)
            if key in context.finished_keys or key in context.timed_out_keys:
                return False
            context.timed_out_keys.add(key)
            dto = order_check_to_dto(order, status="error")
            dto["processing_errors"] = [
                f"Превышено время обработки заказа ({self.order_timeout_seconds:g} с); результат не принят."
            ]
            attach_issue_groups(dto)
            run = context.run
            run["stage_label"] = "Завершение обработки после превышения времени"
            self._commit_order_locked(
                run, key, dto, "order.completed",
                lambda value: {
                    "aggregate_id": order.aggregate_id,
                    "order_id": order.order_id,
                    "customer_id": order.customer_id,
                    "status": "error", "order": value,
                    "processed": run["processed_orders"],
                    "total": run["total_orders"], "progress": run["progress"],
                },
            )
            self._changed.notify_all()
            return True

    def _mark_order_not_started(
        self, run_id: str, order: OrderCheck, context: _RunContext
    ) -> None:
        """No worker is left (all stuck in timed-out orders)."""
        with self._lock:
            if self._contexts.get(run_id) is not context:
                return
            key = context.key_for(order)
            context.timed_out_keys.add(key)
            dto = order_check_to_dto(order, status="error")
            dto["processing_errors"] = [
                "Заказ не запущен: все обработчики заняты зависшими заказами; "
                "повторите проверку после перезапуска сервиса."
            ]
            attach_issue_groups(dto)
            run = context.run
            self._commit_order_locked(
                run, key, dto, "order.completed",
                lambda value: {
                    "aggregate_id": order.aggregate_id,
                    "order_id": order.order_id,
                    "customer_id": order.customer_id,
                    "status": "error", "order": value,
                    "processed": run["processed_orders"],
                    "total": run["total_orders"], "progress": run["progress"],
                },
            )
            self._changed.notify_all()

    def _process_one(
        self, run_id: str, order: OrderCheck, context: _RunContext | None = None
    ) -> None:
        context = context or self._contexts.get(run_id)
        if context is None:
            return
        try:
            artifacts = context.adapter.process_order(order)
        except Exception as exc:  # noqa: BLE001 — сбой одного заказа не роняет проверку
            # Заказ 26237531 (19.11): исчезнувшая копия в PDF/Print уронила всю проверку,
            # и остальные заказы остались без итога и без возможности решения.
            logger.exception(
                "order.processing_failed run_id=%s order_id=%s", run_id, order.order_id
            )
            artifacts = OrderArtifacts(
                pdf_path=None,
                preview_paths=[],
                errors=[f"Сбой обработки заказа: {str(exc).strip() or type(exc).__name__}"],
            )
        with self._lock:
            key = context.key_for(order)
            if self._contexts.get(run_id) is not context or key in context.timed_out_keys:
                # The order was finalized as timed out (or the run ended):
                # a late result must not overwrite that state.
                logger.warning(
                    "order.late_result_discarded run_id=%s order_key=%s",
                    run_id,
                    key,
                )
                return
            context.finished_keys.add(key)
            run = context.run
            status = _completed_order_status(order, artifacts)
            source_status = _inspection_status(order)
            pitstop = artifacts.pitstop
            pitstop_status = _pitstop_status_from_dto(pitstop)
            dto = order_check_to_dto(
                order,
                status=status,
                source_status=source_status,
                pitstop_status=pitstop_status,
                pdf_path=str(artifacts.pdf_path) if artifacts.pdf_path else None,
                preview_paths=[str(path) for path in artifacts.preview_paths],
                processing_errors=artifacts.errors,
                pitstop=pitstop,
                current_pdf_revision=artifacts.current_pdf_revision,
                current_pdf_sha256=artifacts.current_pdf_sha256,
                ink_coverage=artifacts.ink_coverage,
            )
            if artifacts.pitstop_skipped_reason:
                dto["pitstop_skipped_reason"] = artifacts.pitstop_skipped_reason
            if artifacts.pdf_kind:
                dto["pdf_kind"] = artifacts.pdf_kind
            if getattr(artifacts, "pitstop_baseline", None):
                # Проверка исходных PDF клиента (сторона из принятого исправления
                # Callas исходника): с ней сравниваются и последующие исправления.
                dto["pitstop_baseline"] = artifacts.pitstop_baseline
            if pitstop:
                pitstop_event = (
                    "pitstop.check_completed"
                    if pitstop.get("execution_status") == "completed"
                    else "pitstop.check_failed"
                )
                self._commit_order_locked(
                    run,
                    key,
                    dto,
                    pitstop_event,
                    lambda value: {
                        "aggregate_id": order.aggregate_id,
                        "order_id": order.order_id,
                        "customer_id": order.customer_id,
                        "status": status,
                        "source_status": source_status,
                        "pitstop_status": pitstop_status,
                        "check_id": pitstop.get("check_id"),
                        "order": value,
                    },
                )
            self._commit_order_locked(
                run,
                key,
                dto,
                "order.completed",
                lambda value: {
                    "aggregate_id": order.aggregate_id,
                    "order_id": order.order_id,
                    "customer_id": order.customer_id,
                    "status": status,
                    "order": value,
                    "processed": run["processed_orders"],
                    "total": run["total_orders"],
                    "progress": run["progress"],
                },
            )
            if artifacts.pdf_path:
                self._emit_locked(
                    run_id,
                    "pdf.created",
                    {
                        "aggregate_id": order.aggregate_id,
                        "order_id": order.order_id,
                        "customer_id": order.customer_id,
                        "path": str(artifacts.pdf_path),
                    },
                )
            for path in artifacts.preview_paths:
                self._emit_locked(
                    run_id,
                    "preview.created",
                    {
                        "aggregate_id": order.aggregate_id,
                        "order_id": order.order_id,
                        "customer_id": order.customer_id,
                        "path": str(path),
                    },
                )
            self._changed.notify_all()

    def _settle_run_locked(self, run: dict[str, Any]) -> None:
        self._update_counts(run)
        if run["cancel_requested"]:
            self._mark_cancelled_locked(run)
            return
        waiting = run["waiting_orders"]
        if waiting:
            run["status"] = "waiting_confirmation"
            run["stage"] = "waiting_confirmation"
            run["stage_label"] = "Ожидает решения оператора"
            run["progress"] = self._progress(run)
            self._save_run_locked(run, include_orders=False)
            if self._active_run_id == run["id"]:
                self._active_run_id = None
                self._schedule_next_locked()
        elif run["processed_orders"] == run["total_orders"]:
            run["status"] = "completed"
            run["stage"] = "completed"
            run["stage_label"] = "Готово"
            run["progress"] = 100
            run["finished_at"] = _now()
            self._save_and_emit_locked(
                run, "run.completed", self._summary(run), include_orders=False
            )
            self._release_active_locked(run["id"])
        elif self._active_run_id == run["id"]:
            # Заказы, подтверждённые, пока шёл предыдущий, ждут в _pending_resumes:
            # проверка остаётся «running», но воркер свободен — без этого очередь
            # возобновлений и все следующие проверки блокируются навсегда.
            self._active_run_id = None
            if not any(pending_run == run["id"] for pending_run, _ in self._pending_resumes):
                logger.warning(
                    "scheduler.run_unsettled run_id=%s processed=%s total=%s",
                    run["id"], run["processed_orders"], run["total_orders"],
                )
            self._schedule_next_locked()
        self._changed.notify_all()

    def _cancel_remaining(self, run_id: str) -> None:
        with self._lock:
            context = self._contexts.get(run_id)
            if context is None:
                return
            self._mark_cancelled_locked(context.run)

    def _mark_cancelled_locked(self, run: dict[str, Any]) -> None:
        for item in (run.get("orders") or {}).values():
            if item["status"] not in TERMINAL_ORDER_STATUSES:
                item["status"] = "cancelled"
        context = self._contexts.get(str(run["id"]))
        if context is not None and context.run is run:
            _recount(context)
        run["status"] = "cancelled"
        run["stage"] = "cancelled"
        run["stage_label"] = "Остановлено"
        run["finished_at"] = _now()
        self._update_counts(run)
        # Many orders may have changed at once: full save.
        self._save_and_emit_locked(run, "run.cancelled", self._summary(run))
        self._release_active_locked(run["id"])

    def _fail_run(self, run_id: str, exc: Exception) -> None:
        if not run_id:
            return
        with self._lock:
            try:
                run = self._load_run_locked(run_id)
            except RunNotFoundError:
                return
            if run["status"] in TERMINAL_RUN_STATUSES:
                return
            run["status"] = "failed"
            run["stage"] = "failed"
            run["stage_label"] = "Ошибка обработки"
            run["finished_at"] = _now()
            run["error"] = f"{type(exc).__name__}: {exc}"
            self._save_and_emit_locked(
                run,
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

    # ------------------------------------------------------------- persistence

    def _live_context(self, run: dict[str, Any]) -> _RunContext | None:
        context = self._contexts.get(str(run.get("id")))
        return context if context is not None and context.run is run else None

    def _update_counts(self, run: dict[str, Any]) -> None:
        context = self._live_context(run)
        if context is not None:
            counts = context.status_counts
        else:
            counts = Counter(item["status"] for item in run["orders"].values())
        run["processed_orders"] = sum(
            counts.get(status, 0) for status in TERMINAL_ORDER_STATUSES
        )
        run["passed_orders"] = counts.get("passed", 0)
        run["warning_orders"] = counts.get("warning", 0)
        run["failed_orders"] = counts.get("error", 0)
        run["waiting_orders"] = counts.get("waiting_confirmation", 0)
        run["progress"] = self._progress(run)

    def _commit_order_locked(
        self,
        run: dict[str, Any],
        key: str,
        dto: dict[str, Any],
        event_type: str,
        build_data: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        update_counts: bool = True,
        envelope: str | None = None,
    ) -> RunEvent:
        """Put one order into the aggregate, update counters, persist + emit.

        On a persistence error the aggregate (order and counters) is restored,
        so memory never runs ahead of the repository for this order.
        """
        orders = run["orders"]
        had_previous = key in orders
        previous = orders.get(key)
        counters = {
            name: run.get(name)
            for name in (
                "processed_orders", "passed_orders", "warning_orders",
                "failed_orders", "waiting_orders", "progress",
            )
        }
        context = self._live_context(run)
        orders[key] = dto
        if context is not None:
            if had_previous:
                context.status_counts[str(previous.get("status"))] -= 1
            context.status_counts[str(dto.get("status"))] += 1
        if update_counts:
            self._update_counts(run)
        try:
            return self._save_order_and_emit_locked(
                run, key, event_type, build_data(dto), envelope=envelope
            )
        except Exception:
            if had_previous:
                orders[key] = previous
            else:
                orders.pop(key, None)
            if context is not None:
                context.status_counts[str(dto.get("status"))] -= 1
                if had_previous:
                    context.status_counts[str(previous.get("status"))] += 1
            run.update(counters)
            raise

    def _save_order_and_emit_locked(
        self,
        run: dict[str, Any],
        key: str,
        event_type: str,
        data: dict[str, Any],
        *,
        envelope: str | None = None,
    ) -> RunEvent:
        context = self._live_context(run)
        if not self._partial_saves:
            return self._save_and_emit_locked(run, event_type, data)
        if envelope is not None:
            pass
        elif context is None or run.get("status") not in {"running", "cancelling"}:
            # Operator actions and stored runs: keep the envelope exact now.
            envelope = ENVELOPE_PATCH
        elif (
            time.monotonic() - context.last_checkpoint
            >= self.envelope_checkpoint_seconds
        ):
            envelope = ENVELOPE_FULL
        else:
            envelope = ENVELOPE_DEFER
        event = self.repository.save_order_with_event(
            run, key, event_type, data, envelope=envelope
        )
        if context is not None:
            if envelope == ENVELOPE_FULL:
                context.last_checkpoint = time.monotonic()
            context.summary = _summary_copy(run)
        self._log_event(event, data)
        return event

    def _save_run_locked(
        self, run: dict[str, Any], *, include_orders: bool = True
    ) -> None:
        if self._partial_saves:
            self.repository.save_run(run, include_orders=include_orders)
        else:
            self.repository.save_run(run)
        self._after_run_save(run)

    def _after_run_save(self, run: dict[str, Any]) -> None:
        context = self._live_context(run)
        if context is not None:
            context.last_checkpoint = time.monotonic()
            context.summary = _summary_copy(run)

    def _emit_locked(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        event = self.repository.append_event(run_id, event_type, data)
        self._log_event(event, data)
        return event

    def _save_and_emit_locked(
        self,
        run: dict[str, Any],
        event_type: str,
        data: dict[str, Any],
        *,
        include_orders: bool = True,
    ) -> RunEvent:
        """Run-level transition: run fields, envelope and event together.

        ``include_orders=False`` skips the order rows; use it only when every
        order of ``run`` was already persisted per order.
        """
        if self._partial_saves:
            event = self.repository.save_run_with_event(
                run, event_type, data, include_orders=include_orders
            )
        else:
            event = self.repository.save_run_with_event(run, event_type, data)
        self._after_run_save(run)
        self._log_event(event, data)
        return event

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

    @staticmethod
    def _log_event(event: RunEvent, data: dict[str, Any]) -> None:
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
        level = logging.ERROR if event.type == "run.failed" else logging.INFO
        logger.log(
            level,
            "run.event run_id=%s event=%s event_id=%s details=%s",
            event.run_id,
            event.type,
            event.id,
            fields or "-",
        )


def _recount(context: _RunContext) -> None:
    context.status_counts = Counter(
        str(item.get("status")) for item in (context.run.get("orders") or {}).values()
    )


def _accepts_limit(method: Callable[..., Any]) -> bool:
    try:
        return "limit" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def _orders_page_from_run(
    run: dict[str, Any], *, page: int, page_size: int, status: str,
    search: str, active_only: bool = True,
) -> dict[str, Any]:
    """In-memory equivalent of ``SqlRunRepository.list_orders_page``.

    Filters the given aggregate and deep-copies only the requested page.
    """
    entries = list((run.get("orders") or {}).items())
    terminal = {"accepted_for_print", "returned_for_rework"}
    if active_only:
        entries = [(key, order) for key, order in entries if order.get("status") not in terminal]
    query = search.strip().casefold()
    if query:
        entries = [(key, order) for key, order in entries if query in " ".join([
            str(order.get("order_id") or ""), str(order.get("customer_id") or ""),
            *(str(file.get("filename") or file.get("name") or "") for file in order.get("files") or []),
        ]).casefold()]
    grouped: dict[str, int] = {}
    for _key, order in entries:
        value = str(order.get("status") or "detected")
        grouped[value] = grouped.get(value, 0) + 1
    counts = {
        "all": sum(grouped.values()),
        "passed": sum(grouped.get(value, 0) for value in ("passed", "completed")),
        "warning": grouped.get("warning", 0),
        "error": sum(grouped.get(value, 0) for value in ("error", "failed", "technical_error")),
        "waiting_confirmation": grouped.get("waiting_confirmation", 0),
    }
    if status == "passed":
        entries = [(key, order) for key, order in entries if order.get("status") in {"passed", "completed"}]
    elif status == "error":
        entries = [(key, order) for key, order in entries if order.get("status") in {"error", "failed", "technical_error"}]
    elif status != "all":
        entries = [(key, order) for key, order in entries if order.get("status") == status]
    # Dict insertion order follows discovery order and keeps pagination
    # stable as new orders arrive during an active run.
    total = len(entries)
    start = (page - 1) * page_size
    page_run = _summary_copy(run)
    page_run["orders"] = {
        key: deepcopy(order) for key, order in entries[start:start + page_size]
    }
    return {"run": page_run, "items": list(page_run["orders"].values()), "page": page,
            "page_size": page_size, "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size), "counts": counts}


def _pitstop_status_from_dto(pitstop: dict[str, Any] | None) -> str:
    if not pitstop:
        return "not_checked"
    if pitstop.get("execution_status") != "completed":
        return "technical_error"
    return str(pitstop.get("verdict") or "error")


def _inspection_status(order: OrderCheck) -> str:
    if order.sborka_cancelled is not None:
        return OrderStatus.CANCELLED.value
    if any(item.resample_decision == "ask_confirmation" for item in order.files):
        return "waiting_confirmation"
    if not order.passed:
        return "error"
    if order.warnings or any(item.warnings for item in order.files):
        return "warning"
    return "passed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
