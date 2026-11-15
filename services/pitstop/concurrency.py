"""Bounded concurrency, exclusive retries and automatic serial fallback.

One ``PitStopGate`` guards one PitStop server.  Gates are shared by every
``PitStopService`` that uses the same transport (the application creates a
service per run and per reviewed-PDF check, so a per-service lock never
limited anything across them).

Design (a small readers-writer gate on one ``threading.Condition``):

* ``shared()`` -- an ordinary check.  At most ``effective_limit`` run at once.
* ``exclusive()`` -- a retry.  Starts only when no check is running and keeps
  all others out until it finishes.

Why this cannot deadlock: a thread never holds a shared slot while waiting for
the exclusive one (the service leaves ``shared()`` before it asks for
``exclusive()``), so the exclusive waiter only ever waits for threads that are
inside the remote command, and those always finish (the command has a
timeout).  Several threads asking for ``exclusive()`` simply take turns.
Pending exclusive requests stop new shared entries, so a retry cannot starve.

Why slots cannot leak: both context managers release in ``finally`` and the
counters change only under the condition's lock.

Degradation: failures that happened while checks overlapped are counted; when
``degrade_threshold`` of them occur within ``degrade_window_seconds`` the
effective limit drops to 1 for ``degrade_cooldown_seconds``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


logger = logging.getLogger(__name__)

MAX_CONCURRENT_ENV = "IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT"
# Keep in sync with server/settings.py (covered by a test).
MAX_CONCURRENT_DEFAULT = 4
MAX_CONCURRENT_MIN = 1
MAX_CONCURRENT_MAX = 8

# Defaults for automatic degradation: 3 failures of overlapping checks within
# 2 minutes switch PitStop to serial mode for 30 minutes.
DEGRADE_THRESHOLD_DEFAULT = 3
DEGRADE_WINDOW_SECONDS_DEFAULT = 120.0
DEGRADE_COOLDOWN_SECONDS_DEFAULT = 1800.0


def max_concurrent_from_env() -> int:
    """Read the limit the same way ``Settings.from_env`` does (clamped)."""
    raw = os.environ.get(MAX_CONCURRENT_ENV, "").strip()
    if not raw:
        return MAX_CONCURRENT_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return MAX_CONCURRENT_DEFAULT
    return max(MAX_CONCURRENT_MIN, min(MAX_CONCURRENT_MAX, value))


@dataclass(frozen=True, slots=True)
class GateConfig:
    max_concurrent: int = MAX_CONCURRENT_DEFAULT
    degrade_threshold: int = DEGRADE_THRESHOLD_DEFAULT
    degrade_window_seconds: float = DEGRADE_WINDOW_SECONDS_DEFAULT
    degrade_cooldown_seconds: float = DEGRADE_COOLDOWN_SECONDS_DEFAULT

    def __post_init__(self) -> None:
        if not MAX_CONCURRENT_MIN <= self.max_concurrent <= MAX_CONCURRENT_MAX:
            raise ValueError(
                "число параллельных проверок PitStop должно быть от "
                f"{MAX_CONCURRENT_MIN} до {MAX_CONCURRENT_MAX}"
            )
        if self.degrade_threshold < 1:
            raise ValueError("порог перехода PitStop в последовательный режим должен быть >= 1")
        if self.degrade_window_seconds <= 0 or self.degrade_cooldown_seconds <= 0:
            raise ValueError("окно и время последовательного режима PitStop должны быть положительными")


class GateTicket:
    """Handle of one running check; ``peak`` is the most checks seen at once."""

    __slots__ = ("exclusive", "peak")

    def __init__(self, *, exclusive: bool) -> None:
        self.exclusive = exclusive
        self.peak = 1

    @property
    def overlapped(self) -> bool:
        """True if another check ran at the same time as this one."""
        return self.peak > 1


class PitStopGate:
    def __init__(
        self,
        config: GateConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config or GateConfig()
        self._clock = clock
        self._condition = threading.Condition()
        self._running: list[GateTicket] = []
        self._exclusive_waiting = 0
        self._exclusive_running = False
        self._failures: deque[float] = deque()
        self._degraded_until: float | None = None

    @property
    def config(self) -> GateConfig:
        return self._config

    @property
    def effective_limit(self) -> int:
        with self._condition:
            return self._effective_limit_locked()

    @property
    def degraded(self) -> bool:
        return self.effective_limit < self._config.max_concurrent

    def _effective_limit_locked(self) -> int:
        if self._degraded_until is not None:
            if self._clock() < self._degraded_until:
                return 1
            self._degraded_until = None
            self._failures.clear()
            logger.warning(
                "PitStop: время последовательного режима истекло, возвращена "
                "параллельность: до %d проверок одновременно "
                "(IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT).",
                self._config.max_concurrent,
            )
        return self._config.max_concurrent

    @contextmanager
    def shared(self) -> Iterator[GateTicket]:
        ticket = GateTicket(exclusive=False)
        with self._condition:
            while (
                self._exclusive_running
                or self._exclusive_waiting
                or len(self._running) >= self._effective_limit_locked()
            ):
                self._condition.wait()
            self._running.append(ticket)
            self._update_peaks_locked()
        try:
            yield ticket
        finally:
            self._release(ticket)

    @contextmanager
    def exclusive(self) -> Iterator[GateTicket]:
        ticket = GateTicket(exclusive=True)
        with self._condition:
            self._exclusive_waiting += 1
            try:
                while self._exclusive_running or self._running:
                    self._condition.wait()
            finally:
                self._exclusive_waiting -= 1
                # Waiters blocked only by our pending request may go on if we
                # were interrupted while waiting.
                self._condition.notify_all()
            # No lock release between the wait and this line: nobody can enter.
            self._exclusive_running = True
            self._running.append(ticket)
        try:
            yield ticket
        finally:
            self._release(ticket)

    def _update_peaks_locked(self) -> None:
        count = len(self._running)
        for running in self._running:
            if running.peak < count:
                running.peak = count

    def _release(self, ticket: GateTicket) -> None:
        with self._condition:
            try:
                self._running.remove(ticket)
            finally:
                if ticket.exclusive:
                    self._exclusive_running = False
                self._condition.notify_all()

    def note_suspect_failure(self, *, overlapped: bool, reason: str) -> bool:
        """Count a failure of a check that ran concurrently with others.

        Returns True if this failure switched PitStop to serial mode.  A
        failure of a check that ran alone is no evidence against concurrency
        and is ignored.
        """
        if not overlapped or self._config.max_concurrent <= 1:
            return False
        with self._condition:
            if self._effective_limit_locked() <= 1:
                return False
            now = self._clock()
            self._failures.append(now)
            window = self._config.degrade_window_seconds
            while self._failures and now - self._failures[0] > window:
                self._failures.popleft()
            if len(self._failures) < self._config.degrade_threshold:
                return False
            self._degraded_until = now + self._config.degrade_cooldown_seconds
            failures = len(self._failures)
            self._failures.clear()
            self._condition.notify_all()
        logger.warning(
            "PitStop переключён в последовательный режим (1 проверка за раз) "
            "на %d мин: за %d с произошло %d сбоев при параллельных проверках "
            "(последний: %s). Лимит параллельности задаёт "
            "IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT (сейчас %d); значение 1 "
            "отключает параллельность насовсем.",
            round(self._config.degrade_cooldown_seconds / 60),
            round(window),
            failures,
            reason,
            self._config.max_concurrent,
        )
        return True


# One gate per (transport, configuration).  Weak keys: a gate disappears with
# its transport (tests create many).
_gates: "weakref.WeakKeyDictionary[object, dict[GateConfig, PitStopGate]]" = (
    weakref.WeakKeyDictionary()
)
_gates_lock = threading.Lock()


def shared_gate_for(
    transport: object,
    config: GateConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> PitStopGate:
    """Return the gate every service using ``transport`` must share."""
    with _gates_lock:
        try:
            per_transport = _gates.setdefault(transport, {})
        except TypeError:  # not weak-referenceable: private gate
            return PitStopGate(config, clock=clock)
        gate = per_transport.get(config)
        if gate is None:
            gate = per_transport[config] = PitStopGate(config, clock=clock)
        return gate
