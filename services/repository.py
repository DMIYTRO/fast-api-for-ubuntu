"""Persistence boundary for runs and their SSE event journal."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any, Protocol


@dataclass(frozen=True)
class RunEvent:
    id: int
    type: str
    run_id: str
    data: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "run_id": self.run_id,
            "created_at": self.created_at,
            **deepcopy(self.data),
        }

    def as_sse(self) -> str:
        """Serialize without an SSE dependency; suitable for StreamingResponse."""
        import json

        return (
            f"id: {self.id}\n"
            f"event: {self.type}\n"
            f"data: {json.dumps(self.as_dict(), ensure_ascii=False)}\n\n"
        )


# How ``save_order_with_event`` treats the whole-run compatibility data
# (run counters, per-order extras) that the SQL repository keeps in one blob:
# * ``patch``: update only this order's part and the run counters.  Correct on
#   its own; used for operator actions on stored runs.
# * ``defer``: leave the blob untouched.  The caller owns the live aggregate and
#   promises a later ``full`` write or ``save_run*`` (run-level transition).
# * ``full``: rewrite the blob from ``run`` (periodic checkpoint).
ENVELOPE_PATCH = "patch"
ENVELOPE_DEFER = "defer"
ENVELOPE_FULL = "full"
ENVELOPE_MODES = frozenset({ENVELOPE_PATCH, ENVELOPE_DEFER, ENVELOPE_FULL})


class RunRepository(Protocol):
    """Minimal boundary that a SQLite/SQLAlchemy implementation can satisfy.

    ``include_orders=False`` on ``save_run``/``save_run_with_event`` persists
    run-level fields only; the caller guarantees every order was already
    stored with ``save_order_with_event``.
    """

    def create_run(self, run: dict[str, Any]) -> None: ...

    def save_run(
        self, run: dict[str, Any], *, include_orders: bool = True
    ) -> None: ...

    def save_run_with_event(
        self,
        run: dict[str, Any],
        event_type: str,
        data: dict[str, Any],
        *,
        include_orders: bool = True,
    ) -> RunEvent: ...

    def save_order_with_event(
        self,
        run: dict[str, Any],
        order_key: str,
        event_type: str,
        data: dict[str, Any],
        *,
        envelope: str = ENVELOPE_PATCH,
    ) -> RunEvent: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def run_exists(self, run_id: str) -> bool: ...

    def list_runs(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include_orders: bool = True,
    ) -> list[dict[str, Any]]: ...

    def count_runs(self) -> int: ...

    def append_event(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent: ...

    def list_events(
        self, run_id: str, after_id: int = 0, limit: int | None = None
    ) -> list[RunEvent]: ...


class InMemoryRunRepository:
    """Thread-safe reference repository used by tests and local composition."""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._next_event_ids: dict[str, int] = {}
        self._lock = threading.RLock()

    def create_run(self, run: dict[str, Any]) -> None:
        with self._lock:
            run_id = str(run["id"])
            if run_id in self._runs:
                raise ValueError(f"run already exists: {run_id}")
            self._runs[run_id] = deepcopy(run)
            self._events[run_id] = []
            self._next_event_ids[run_id] = 1

    def save_run(
        self, run: dict[str, Any], *, include_orders: bool = True
    ) -> None:
        with self._lock:
            run_id = str(run["id"])
            if run_id not in self._runs:
                raise KeyError(run_id)
            self._store_locked(run_id, run, include_orders=include_orders)

    def save_run_with_event(
        self,
        run: dict[str, Any],
        event_type: str,
        data: dict[str, Any],
        *,
        include_orders: bool = True,
    ) -> RunEvent:
        with self._lock:
            run_id = str(run["id"])
            if run_id not in self._runs:
                raise KeyError(run_id)
            event = self._new_event_locked(run_id, event_type, data)
            self._store_locked(run_id, run, include_orders=include_orders)
            self._append_event_locked(event)
            return deepcopy(event)

    def save_order_with_event(
        self,
        run: dict[str, Any],
        order_key: str,
        event_type: str,
        data: dict[str, Any],
        *,
        envelope: str = ENVELOPE_PATCH,
    ) -> RunEvent:
        """Persist one order, the run scalars and the event atomically.

        Copies only the changed order, so the cost does not depend on the
        number of orders in the run.  ``envelope`` has no meaning here (there
        is no compatibility blob) and is accepted for interface parity.
        """
        if envelope not in ENVELOPE_MODES:
            raise ValueError(f"unknown envelope mode: {envelope}")
        with self._lock:
            run_id = str(run["id"])
            if run_id not in self._runs:
                raise KeyError(run_id)
            order = deepcopy(run["orders"][order_key])
            event = self._new_event_locked(run_id, event_type, data)
            self._store_locked(run_id, run, include_orders=False)
            self._runs[run_id]["orders"][str(order_key)] = order
            self._append_event_locked(event)
            return deepcopy(event)

    def _store_locked(
        self, run_id: str, run: dict[str, Any], *, include_orders: bool
    ) -> None:
        if include_orders:
            self._runs[run_id] = deepcopy(run)
            return
        stored = self._runs[run_id]
        orders = stored.get("orders")
        if not isinstance(orders, dict):
            orders = {}
        replacement = {
            key: deepcopy(value) for key, value in run.items() if key != "orders"
        }
        replacement["orders"] = orders
        self._runs[run_id] = replacement

    def _new_event_locked(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        event_id = self._next_event_ids[run_id]
        event = RunEvent(
            id=event_id,
            type=event_type,
            run_id=run_id,
            data=deepcopy(data),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return event

    def _append_event_locked(self, event: RunEvent) -> None:
        self._next_event_ids[event.run_id] = event.id + 1
        self._events[event.run_id].append(event)

    def run_exists(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._runs.get(run_id)
            return deepcopy(value) if value is not None else None

    def get_run_summary(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._runs.get(run_id)
            if value is None:
                return None
            summary = {
                key: deepcopy(item) for key, item in value.items() if key != "orders"
            }
            summary["orders"] = {}
            return summary

    def list_runs(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include_orders: bool = True,
    ) -> list[dict[str, Any]]:
        with self._lock:
            values = list(self._runs.values())[offset:]
            if limit is not None:
                values = values[:limit]
            result = [deepcopy(value) for value in values]
            if not include_orders:
                for value in result:
                    value["orders"] = {}
            return result

    def count_runs(self) -> int:
        with self._lock:
            return len(self._runs)

    def append_event(
        self, run_id: str, event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            event = self._new_event_locked(run_id, event_type, data)
            self._append_event_locked(event)
            return event

    def list_events(
        self, run_id: str, after_id: int = 0, limit: int | None = None
    ) -> list[RunEvent]:
        with self._lock:
            selected = [
                event for event in self._events.get(run_id, []) if event.id > after_id
            ]
            if limit is not None:
                selected = selected[: max(0, int(limit))]
            return [deepcopy(event) for event in selected]
