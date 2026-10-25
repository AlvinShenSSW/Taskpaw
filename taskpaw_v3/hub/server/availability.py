"""Persist one Hub-owned incident for a sustained Agent status outage."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable

from .store import HubStore


class AvailabilityTracker:
    def __init__(
        self,
        store: HubStore,
        *,
        failed_samples: int = 3,
        grace_seconds: float = 120,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.failed_samples = failed_samples
        self.grace_seconds = grace_seconds
        self.now = now
        self.monotonic = monotonic
        # A durable last-failure marker binds monotonic continuity to this exact
        # incident. Disable/re-enable can reset SQL between two poll cycles.
        self._last_samples: dict[int, tuple[str, float]] = {}

    def retain(self, server_ids: set[int]) -> None:
        self._last_samples = {
            sid: sample
            for sid, sample in self._last_samples.items()
            if sid in server_ids
        }

    def reset(self, server_id: int) -> None:
        with self.store._lock:
            try:
                self.store._conn.execute(
                    "DELETE FROM hub_availability WHERE server_id=?", (server_id,)
                )
                self.store._conn.commit()
            except Exception:
                self.store._conn.rollback()
                raise
        self._last_samples.pop(server_id, None)

    def observe(
        self, server: dict, reachable: bool, reason: str | None, active: bool
    ) -> None:
        """Commit incident state, local event and optional outbox atomically.

        Grace accumulates only between observed failures in this process. A Hub
        restart retains the incident and observed duration but adds neither
        outage time nor failed samples while the Hub was not running.
        """
        sid = server["id"]
        point = self.monotonic()
        stamp = self.now().isoformat(timespec="microseconds")
        next_sample = None
        with self.store._lock:
            conn = self.store._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT enabled,ip,port,name FROM servers WHERE id=?", (sid,)
                ).fetchone()
                if not current or not current[0]:
                    conn.execute(
                        "DELETE FROM hub_availability WHERE server_id=?", (sid,)
                    )
                elif current[1:3] != (server["ip"], server["port"]):
                    # Registration changed while its old endpoint was in flight.
                    conn.commit()
                    self._last_samples.pop(sid, None)
                    return
                else:
                    row = conn.execute(
                        "SELECT failure_count,failure_started_at,last_failed_at,"
                        "elapsed_seconds,announced,last_error "
                        "FROM hub_availability WHERE server_id=?",
                        (sid,),
                    ).fetchone() or (0, None, None, 0.0, 0, None)
                    count, started, last_failed, elapsed, announced, last_error = row
                    if reachable:
                        if announced:
                            self.store._insert_hub_event(
                                current[3],
                                "agent-availability",
                                f"Agent recovered; previous reason: {last_error}",
                                "info",
                                active,
                                sid,
                            )
                        conn.execute(
                            "DELETE FROM hub_availability WHERE server_id=?", (sid,)
                        )
                    else:
                        prior = self._last_samples.get(sid)
                        if count and prior and prior[0] == last_failed:
                            elapsed += max(0.0, point - prior[1])
                        count = min(self.failed_samples, count + 1)
                        elapsed = min(self.grace_seconds, elapsed)
                        reason = reason or "upstream_failed"
                        if (
                            not announced
                            and count >= self.failed_samples
                            and elapsed >= self.grace_seconds
                        ):
                            self.store._insert_hub_event(
                                current[3],
                                "agent-availability",
                                f"Agent unavailable; reason: {reason}",
                                "error",
                                active,
                                sid,
                            )
                            announced = 1
                        conn.execute(
                            "INSERT INTO hub_availability(server_id,failure_count,"
                            "failure_started_at,last_failed_at,elapsed_seconds,"
                            "announced,last_error) VALUES(?,?,?,?,?,?,?) "
                            "ON CONFLICT(server_id) DO UPDATE SET "
                            "failure_count=excluded.failure_count,"
                            "failure_started_at=excluded.failure_started_at,"
                            "last_failed_at=excluded.last_failed_at,"
                            "elapsed_seconds=excluded.elapsed_seconds,"
                            "announced=excluded.announced,last_error=excluded.last_error",
                            (
                                sid,
                                count,
                                started or stamp,
                                stamp,
                                elapsed,
                                announced,
                                reason,
                            ),
                        )
                        next_sample = (stamp, point)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # A failed transaction must not advance the in-memory observation clock.
        if next_sample:
            self._last_samples[sid] = next_sample
        else:
            self._last_samples.pop(sid, None)
