"""A wall clock that never moves backwards, for the controller's own store (T03-P1 C8).

T01 marks the authority `recovery_required` forever when it observes time going
backwards. An automatic time sync can step the system clock back, so the local
runtime hands the store this clock instead of the raw system time.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .store_core import parse_time


def _system_now() -> datetime:
    return datetime.now(timezone.utc)


class MonotonicWallClock:
    """`max(system UTC time, last returned value)`, seeded by a persisted floor."""

    def __init__(
        self,
        floor: datetime | None = None,
        source: Callable[[], datetime] = _system_now,
    ) -> None:
        self._source = source
        self._last = floor
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            value = self._source().astimezone(timezone.utc)
            if self._last is not None and value < self._last:
                value = self._last
            self._last = value
            return value


def read_high_water(path: Path) -> datetime | None:
    """The store's persisted `max_observed_at`, read without opening the store."""
    if not path.exists():
        return None
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT max_observed_at FROM authority WHERE singleton=1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return parse_time(row[0]) if row and row[0] else None
