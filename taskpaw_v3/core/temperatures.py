"""Current NVIDIA temperatures, independent of power sampling (#323).

The launcher owns the worker; HTTP reads its memory-only snapshot. A separate
bounded query deliberately leaves the shared power/utilisation query unchanged.
CPU and board readings require a verified reader, which this release does not
have. No value is estimated and no temperature history is persisted.
"""

from __future__ import annotations

import logging
import math
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("taskpaw.temperatures")

SAMPLE_SECONDS = 5.0
QUERY_TIMEOUT = 5.0
STALE_SECONDS = 30.0
QUERY = "index,temperature.gpu"


@dataclass(frozen=True)
class GpuReading:
    """One complete inventory, or an explicit failed query (valid=False)."""

    celsius: Optional[float] = None
    readable: int = 0
    total: int = 0
    reason: Optional[str] = None
    valid: bool = True


def parse_gpu(stdout: str) -> GpuReading:
    """Keep individual missing cells, but refuse an ambiguous GPU inventory."""
    indices: set[int] = set()
    values: list[float] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        cells = [part.strip() for part in line.split(",")]
        if len(cells) != 2 or not cells[0].isascii() or not cells[0].isdigit():
            raise ValueError("invalid GPU inventory")
        index = int(cells[0])
        if index in indices:
            raise ValueError("duplicate GPU index")
        indices.add(index)
        try:
            value = float(cells[1])
        except ValueError:
            continue
        if math.isfinite(value) and -50 <= value <= 200:
            values.append(value)
    return GpuReading(
        celsius=max(values) if values else None,
        readable=len(values),
        total=len(indices),
        reason=None if values else "invalid_reading" if indices else "no_sensor",
    )


def read_gpu() -> GpuReading:
    """One fixed, bounded native call; no power-cache lock or power reader."""
    if sys.platform != "win32":
        return GpuReading(reason="unsupported_platform", valid=False)
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            timeout=QUERY_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return GpuReading(
                reason="permission_denied"
                if result.returncode == 4
                else "query_failed",
                valid=False,
            )
        return parse_gpu(result.stdout)
    except FileNotFoundError:
        reason = "provider_missing"
    except PermissionError:
        reason = "permission_denied"
    except subprocess.TimeoutExpired:
        reason = "timeout"
    except OSError:
        reason = "query_failed"
    except (ValueError, UnicodeError):
        reason = "invalid_reading"
    return GpuReading(reason=reason, valid=False)


# Tests replace only the sampler's default reader, leaving read_gpu testable.
_gpu_reader: Callable[[], GpuReading] = read_gpu


class TemperatureSampler:
    """Single-owner background sampling, with a short-lock cache for readers."""

    def __init__(
        self,
        *,
        reader: Optional[Callable[[], GpuReading]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        interval: float = SAMPLE_SECONDS,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("temperature interval must be positive and finite")
        self._reader = reader
        self._clock, self._wall, self._interval = clock, wall, interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reading = GpuReading(reason="pending", valid=False)
        self._taken: Optional[float] = None
        self._sampled_at: Optional[float] = None
        self._last_reason: Optional[str] = None

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(
            target=self._loop, name="temperature-sampler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = QUERY_TIMEOUT + 2) -> bool:
        self._stop.set()
        thread = self._thread
        if (
            thread is not None
            and thread.ident is not None
            and thread is not threading.current_thread()
        ):
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def _loop(self) -> None:
        deadline = self._clock()
        while not self._stop.is_set():
            self.tick()
            deadline += self._interval
            now = self._clock()
            if deadline < now:
                # Skip missed slots instead of replaying a backlog of queries.
                deadline += (
                    math.ceil((now - deadline) / self._interval) * self._interval
                )
            self._stop.wait(max(0.0, deadline - self._clock()))

    def tick(self) -> None:
        """Replace one snapshot; sampling time includes the whole query."""
        taken, sampled_at = self._clock(), self._wall()
        try:
            reading = (self._reader or _gpu_reader)()
        except Exception:
            # Expose/log the bounded reason below, never the provider's raw text.
            reading = GpuReading(reason="query_failed", valid=False)
        if not reading.valid:
            reading = GpuReading(reason=reading.reason or "query_failed", valid=False)
        with self._lock:
            self._reading = reading
            self._taken = taken if reading.valid else None
            self._sampled_at = sampled_at if reading.valid else None
        if reading.reason is not None and reading.reason != self._last_reason:
            log.warning("temperature provider unavailable (%s)", reading.reason)
        self._last_reason = reading.reason

    def snapshot(self) -> dict[str, Any]:
        """Copy cached state without provider IO or a cross-machine wall clock."""
        with self._lock:
            reading, taken, sampled_at = self._reading, self._taken, self._sampled_at
        age = None if taken is None else max(0.0, self._clock() - taken)
        return {
            "sampled_at": sampled_at,
            "age_s": age,
            "stale": age is None or age >= STALE_SECONDS,
            "cpu": {
                "celsius": None,
                "label": "CPU",
                "source": None,
                "reason": "no_reader",
            },
            "gpu": {
                "celsius": reading.celsius,
                "label": "NVIDIA GPU core (highest readable)",
                "source": "nvidia-smi",
                "reason": reading.reason,
                "readable": reading.readable,
                "total": reading.total,
            },
            "board": {
                "celsius": None,
                "label": "Board / chassis",
                "source": None,
                "reason": "no_reader",
            },
        }
