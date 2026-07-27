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


class RunRepository(Protocol):
    """Minimal boundary that a SQLite/SQLAlchemy implementation can satisfy."""

    def create_run(self, run: dict[str, Any]) -> None: ...

    def save_run(self, run: dict[str, Any]) -> None: ...

    def save_run_with_event(
        self, run: dict[str, Any], event_type: str, data: dict[str, Any]
    ) -> RunEvent: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

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

    def list_events(self, run_id: str, after_id: int = 0) -> list[RunEvent]: ...


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

    def save_run(self, run: dict[str, Any]) -> None:
        with self._lock:
            run_id = str(run["id"])
            if run_id not in self._runs:
                raise KeyError(run_id)
            self._runs[run_id] = deepcopy(run)

    def save_run_with_event(
        self, run: dict[str, Any], event_type: str, data: dict[str, Any]
    ) -> RunEvent:
        with self._lock:
            run_id = str(run["id"])
            if run_id not in self._runs:
                raise KeyError(run_id)
            event_id = self._next_event_ids[run_id]
            event = RunEvent(
                id=event_id,
                type=event_type,
                run_id=run_id,
                data=deepcopy(data),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._runs[run_id] = deepcopy(run)
            self._next_event_ids[run_id] = event_id + 1
            self._events[run_id].append(event)
            return deepcopy(event)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._runs.get(run_id)
            return deepcopy(value) if value is not None else None

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
            event_id = self._next_event_ids[run_id]
            self._next_event_ids[run_id] = event_id + 1
            event = RunEvent(
                id=event_id,
                type=event_type,
                run_id=run_id,
                data=deepcopy(data),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._events[run_id].append(event)
            return event

    def list_events(self, run_id: str, after_id: int = 0) -> list[RunEvent]:
        with self._lock:
            return [
                deepcopy(event)
                for event in self._events.get(run_id, [])
                if event.id > after_id
            ]
