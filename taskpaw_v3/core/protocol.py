"""Event protocol shared by the V3 agent and Hub.

Carries forward the V2 #14 wire contract — monotonic `id`, a `{"events": [...]}`
envelope, and **clear-on-ack** — into a reusable, thread-safe queue (V2 used
module globals). Additive optional fields (`level`/`title`/`data`) are emitted
only when provided so old consumers are unaffected.
"""

from __future__ import annotations

import secrets
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from taskpaw_v3.core.state import MAX_EVENT_ID, StateError, StateSession


class EventAdmissionError(ValueError):
    pass


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
        history_size: int = 500,
        state_session: StateSession | None = None,
    ) -> None:
        self.machine = machine
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._queue: list[dict] = []
        # A bounded ring of recent events for the local UI's event log (#44), kept
        # SEPARATE from `_queue` so a Hub ack/poll that drains `_queue` doesn't
        # empty the console's Events tab. In-memory only (the Hub holds the durable
        # cross-restart history).
        self._history: "deque[dict]" = deque(maxlen=max(0, int(history_size)))
        self._next_id = max(1, int(start_id))
        self._persist_counter = persist_counter
        self._max_size = max_size
        self._on_overflow = on_overflow
        self.state_session = state_session
        self._boot_id = (
            state_session.boot_id if state_session else secrets.token_hex(16)
        )
        self._stream_id = (
            state_session.record.stream_id if state_session else secrets.token_hex(16)
        )
        self._resume_floor = self._next_id - 1
        self._offered_highwater = self._resume_floor

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
        # A stopped queue refuses without waiting behind an in-flight disk write.
        if self._closed.is_set():
            raise StateError("event_queue_closed")
        if level is not None and level not in LEVELS:
            raise ValueError(f"level must be one of {sorted(LEVELS)}")
        if data is not None and not isinstance(data, dict):
            raise ValueError("data must be a dict when provided")

        with self._lock:
            # Also cover callers that waited for this lock before close began.
            if self._closed.is_set():
                raise StateError("event_queue_closed")
            candidate_id = self._next_id
            if candidate_id > MAX_EVENT_ID:
                raise ValueError("counter_exhausted")
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
            self._history.append(evt)  # UI history — independent of ack trimming
            self._next_id = candidate_id + 1
            overflow = len(self._queue) - self._max_size
            if overflow > 0:
                del self._queue[:overflow]
                if self._on_overflow is not None:
                    self._on_overflow(overflow)
            return dict(evt)

    def recent(self, limit: int = 200, monitor: Optional[str] = None) -> list[dict]:
        """A NON-destructive snapshot of the most recent events (newest last) for
        the local UI's event log (#44). Reads the separate `_history` ring, so it
        is unaffected by Hub acks/polls that drain `_queue` — the Events tab keeps
        showing recent local activity even on a Hub-polled agent. Shallow copies.

        When `monitor` is given, only that monitor's events are returned (#130, the
        console's per-monitor inline event panel). The filter is applied BEFORE the
        `limit` slice, so a chatty neighbour can't crowd a quiet monitor's recent
        events out of the returned window."""
        limit = max(0, int(limit))
        with self._lock:
            items = list(self._history)
        if monitor is not None:
            items = [e for e in items if e.get("monitor") == monitor]
        return [dict(e) for e in (items[-limit:] if limit else [])]

    def last_event_times(self) -> dict[str, str]:
        """Map of monitor name → the `time` of its most recent event in the local
        history ring (#130). Feeds per-monitor freshness in the agent console's
        pill selector. Only monitors with at least one event in the ring appear;
        callers merge this additively onto the status monitors dict."""
        out: dict[str, str] = {}
        with self._lock:
            items = list(self._history)
        for e in items:  # newest last → the final write per monitor wins
            name = e.get("monitor")
            time = e.get("time")
            if isinstance(name, str) and isinstance(time, str):
                out[name] = time
        return out

    def payload(self, ack_id: Optional[int] = None) -> dict:
        with self._lock:
            return self._payload_locked(ack_id)

    def _payload_locked(self, ack_id: Optional[int]) -> dict:
        if ack_id is not None and (
            type(ack_id) is not int
            or ack_id < -1
            or ack_id > max(self._resume_floor, self._offered_highwater)
        ):
            raise EventAdmissionError("event_ack_unoffered")
        if ack_id is None:
            events = list(self._queue)
            self._queue.clear()
        else:
            self._queue[:] = [e for e in self._queue if int(e.get("id", -1)) > ack_id]
            events = list(self._queue)
        if events:
            self._offered_highwater = max(
                self._offered_highwater, max(e["id"] for e in events)
            )
        return {"events": events}

    def _cursor_locked(self) -> dict:
        return {
            "version": 1,
            "server_id": self.state_session.record.server_id
            if self.state_session
            else "",
            "stream_id": self._stream_id,
            "boot_id": self._boot_id,
            "resume_floor": self._resume_floor,
            "offered_highwater": self._offered_highwater,
            "next_event_id": self._next_id,
            "durable": self.state_session is not None,
        }

    def cursor_snapshot(self) -> dict:
        with self._lock:
            return self._cursor_locked()

    def network_payload(
        self, ack_id: Optional[int], stream: str | None, boot: str | None
    ) -> dict:
        with self._lock:
            if (stream is not None or boot is not None) and (
                stream != self._stream_id or boot != self._boot_id
            ):
                raise EventAdmissionError("event_cursor_mismatch")
            result = self._payload_locked(ack_id)
            return {**result, "event_cursor": self._cursor_locked()}

    def close(self) -> None:
        self._closed.set()
        if self.state_session is not None:
            self.state_session.close()

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)
