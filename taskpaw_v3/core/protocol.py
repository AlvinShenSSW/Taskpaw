"""Event protocol shared by the V3 agent and Hub.

Carries forward the V2 #14 wire contract — monotonic `id`, a `{"events": [...]}`
envelope, and **clear-on-ack** — into a reusable, thread-safe queue (V2 used
module globals). Additive optional fields (`level`/`title`/`data`) are emitted
only when provided so old consumers are unaffected.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# Optional richness on top of the required {id,time,machine,monitor,message}.
LEVELS = {"info", "warn", "alert", "done"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EventQueue:
    """Thread-safe, monotonic-id event queue with clear-on-ack semantics.

    - `add(...)` appends an event with the next id and persists the counter
      (via `persist_counter`) *while the lock is held*, so the id is durable
      before the event is visible to a poll — preventing id reuse → dedup loss
      after a crash (V2 #14 终审 finding).
    - `payload(ack_id=None)` builds the `/events` response. With `ack_id` it
      trims events `id <= ack` and returns `id > ack` WITHOUT clearing (so an
      un-acked batch survives a Hub crash). Without it, legacy clear-on-read.
    - `max_size` is an OOM backstop: past it the oldest are dropped with a loud
      callback (durable spill-to-disk is deferred — see design).
    """

    def __init__(
        self,
        machine: str,
        start_id: int = 1,
        persist_counter: Optional[Callable[[int], None]] = None,
        max_size: int = 10000,
        on_overflow: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.machine = machine
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._next_id = max(1, int(start_id))
        self._persist_counter = persist_counter
        self._max_size = max_size
        self._on_overflow = on_overflow

    @property
    def next_id(self) -> int:
        with self._lock:
            return self._next_id

    def add(
        self,
        monitor: str,
        message: str,
        level: Optional[str] = None,
        title: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> dict:
        if level is not None and level not in LEVELS:
            raise ValueError(f"level must be one of {sorted(LEVELS)}")
        if data is not None and not isinstance(data, dict):
            raise ValueError("data must be a dict when provided")

        with self._lock:
            candidate_id = self._next_id
            evt: dict[str, Any] = {
                "id": candidate_id,
                "time": _now_iso(),
                "machine": self.machine,
                "monitor": monitor,
                "message": message,
            }
            if level is not None:
                evt["level"] = level
            if title is not None:
                evt["title"] = title
            if data is not None:
                evt["data"] = dict(data)  # shallow copy: caller may mutate theirs

            # Durable BEFORE visible: persist the post-event counter first. If it
            # raises, nothing is mutated (id not advanced, event not appended) so
            # the caller can retry and we never expose an event whose id wasn't
            # durably reserved (which would let a restart reuse it → dedup loss).
            if self._persist_counter is not None:
                self._persist_counter(candidate_id + 1)

            self._queue.append(evt)
            self._next_id = candidate_id + 1
            overflow = len(self._queue) - self._max_size
            if overflow > 0:
                del self._queue[:overflow]
                if self._on_overflow is not None:
                    self._on_overflow(overflow)
            return dict(evt)

    def payload(self, ack_id: Optional[int] = None) -> dict:
        with self._lock:
            if ack_id is None:
                events = list(self._queue)
                self._queue.clear()
            else:
                self._queue[:] = [
                    e for e in self._queue if int(e.get("id", -1)) > ack_id
                ]
                events = list(self._queue)
            return {"events": events}

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)
