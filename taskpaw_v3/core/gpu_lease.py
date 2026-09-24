"""Process-wide GPU lease with a fair per-file hand-off (#179, AC1).

Both GPU task types (`jasna`, `avsubs`) live in one agent process (A2) and
must never run GPU children at the same time on the 8 GB card. This module is
the single arbiter:

- `try_acquire(run, poll_interval, label)` never blocks: True grants (or
  re-confirms) the lease, False registers/updates `run` as a waiter;
- fairness is a **reservation**, never a callback: when the lease becomes free
  while someone waits, it is reserved for the earliest waiter for
  `max(RESERVE_MIN_S, 2 × that waiter's poll_interval)`; only that waiter can
  take it until the window expires (then the waiter is dropped and the next one
  gets the turn);
- a waiter that has not tried for `max(STALE_MIN_S, STALE_FACTOR ×
  poll_interval)` is pruned (a stopped task that forgot to `withdraw`);
- `blocking_label()` names who blocks: the holder, else the reserved waiter.

A run is keyed by `RunId = (instance_id, generation)`, so a late release from
an old generation never frees the new run's hold.

Leaf lock: nothing else is acquired while `_lock` is held and nothing is logged
under it (warnings are logged after it is released), so callers may hold their
own locks. Every public method is O(#waiters) (C10: ≤ the number of GPU tasks),
never blocks beyond that lock, and never raises. Behaviour is a pure function of
the call sequence and the injected clock.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("taskpaw.gpu_lease")

RunId = tuple[str, int]

RESERVE_MIN_S = 30.0
STALE_MIN_S = 30.0
STALE_FACTOR = 3.0


@dataclass
class _Waiter:
    poll_interval: float
    last_try: float
    label: str


def _interval(poll_interval: object) -> float:
    """A usable poll interval: finite and ≥ 0 (the floors do the rest)."""
    try:
        value = float(poll_interval)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


class GpuLease:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._holder: Optional[RunId] = None
        self._label = ""
        # Insertion order = registration order; an update keeps the position.
        self._waiters: dict[RunId, _Waiter] = {}
        self._reserved: Optional[tuple[RunId, float]] = None  # (run, deadline)

    # ── internals (called with _lock held) ──────────────────────────────
    def _prune(self, now: float) -> None:
        for run, w in list(self._waiters.items()):
            if now - w.last_try > max(STALE_MIN_S, STALE_FACTOR * w.poll_interval):
                del self._waiters[run]
        if self._reserved is not None:
            run, deadline = self._reserved
            if now >= deadline:
                self._reserved = None
                self._waiters.pop(run, None)
            elif run not in self._waiters:
                self._reserved = None
        if self._holder is None and self._reserved is None and self._waiters:
            first, w = next(iter(self._waiters.items()))
            window = max(RESERVE_MIN_S, 2.0 * w.poll_interval)
            self._reserved = (first, now + window)

    # ── API ──────────────────────────────────────────────────────────────
    def try_acquire(self, run: RunId, poll_interval: float, label: str = "") -> bool:
        pi = _interval(poll_interval)
        with self._lock:
            now = self._clock()
            self._prune(now)
            if self._holder == run:
                self._label = label
                return True
            if self._holder is None and (
                self._reserved is None or self._reserved[0] == run
            ):
                self._holder = run
                self._label = label
                self._waiters.pop(run, None)
                self._reserved = None
                return True
            w = self._waiters.get(run)
            if w is None:
                self._waiters[run] = _Waiter(pi, now, label)
            else:
                w.poll_interval, w.last_try, w.label = pi, now, label
            return False

    def release(self, run: RunId) -> bool:
        with self._lock:
            holder = self._holder
            if holder == run:
                self._holder = None
                self._label = ""
                self._prune(self._clock())
                return True
        log.warning("gpu lease: release by %s refused (holder is %s)", run, holder)
        return False

    def withdraw(self, run: RunId) -> None:
        with self._lock:
            self._waiters.pop(run, None)
            if self._reserved is not None and self._reserved[0] == run:
                self._reserved = None
            self._prune(self._clock())

    def holder(self) -> Optional[RunId]:
        with self._lock:
            return self._holder

    def blocking_label(self) -> str:
        with self._lock:
            self._prune(self._clock())
            if self._holder is not None:
                return self._label
            if self._reserved is not None:
                w = self._waiters.get(self._reserved[0])
                return w.label if w is not None else ""
            return ""

    def reserved_for(self) -> Optional[RunId]:
        with self._lock:
            self._prune(self._clock())
            return self._reserved[0] if self._reserved is not None else None

    def waiters(self) -> list[RunId]:
        with self._lock:
            self._prune(self._clock())
            return list(self._waiters)


_LEASE = GpuLease()


def try_acquire(run: RunId, poll_interval: float, label: str = "") -> bool:
    return _LEASE.try_acquire(run, poll_interval, label)


def release(run: RunId) -> bool:
    return _LEASE.release(run)


def withdraw(run: RunId) -> None:
    _LEASE.withdraw(run)


def holder() -> Optional[RunId]:
    return _LEASE.holder()


def blocking_label() -> str:
    return _LEASE.blocking_label()


def reserved_for() -> Optional[RunId]:
    return _LEASE.reserved_for()


def waiters() -> list[RunId]:
    return _LEASE.waiters()


def _reset_for_tests(clock: Callable[[], float] = time.monotonic) -> None:
    """Replace the process lease with a fresh one on `clock` (tests only)."""
    global _LEASE
    _LEASE = GpuLease(clock=clock)
