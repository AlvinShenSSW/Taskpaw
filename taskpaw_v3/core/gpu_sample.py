"""One shared nvidia-smi query for GPU %, VRAM and power (PWR-1 PW02, #287).

`gpu_sample()` runs a single `nvidia-smi --query-gpu=utilization.gpu,
memory.used,memory.total,power.draw` and caches the result for 4 s behind a
lock, so the power sampler (every 5 s) and the GPU% / VRAM consumers
(`host_metrics.read_gpu`, used by host_metrics, Jasna, avsubs and Lada) share
one child process instead of each starting their own.

`power.draw` is parsed on its own: `[N/A]` / `[Not Supported]` gives a None
power while utilisation and VRAM stay. Windows only (the V2 method, #21); on
any other platform, without nvidia-smi or without an NVIDIA GPU the sample is
None.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Callable, Optional

CACHE_SECONDS = 4.0
QUERY = "utilization.gpu,memory.used,memory.total,power.draw"

GpuSample = dict  # {util_pct, mem_used_mb, mem_total_mb, power_w (float | None)}

_lock = threading.Lock()
_clock: Callable[[], float] = time.monotonic
_cached: Optional[tuple[float, Optional[GpuSample]]] = None  # (taken_at, sample)


def _number(text: str) -> Optional[float]:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value >= 0 else None


def parse(stdout: str) -> Optional[GpuSample]:
    """Sum VRAM and power over the GPUs, average the utilisation."""
    utils: list[float] = []
    used = total = 0.0
    powers: list[float] = []
    for line in stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        utils.append(float(parts[0]))  # a garbled row fails the whole sample
        used += float(parts[1])
        total += float(parts[2])
        watts = _number(parts[3]) if len(parts) >= 4 else None
        if watts is not None:
            powers.append(watts)
    if not utils:
        return None
    return {
        "util_pct": round(sum(utils) / len(utils), 1),
        "mem_used_mb": round(used),
        "mem_total_mb": round(total),
        "power_w": round(sum(powers), 2) if powers else None,
    }


def _query() -> Optional[GpuSample]:
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return parse(out.stdout)
    except Exception:
        return None


def gpu_sample() -> Optional[GpuSample]:
    """The current GPU sample (a copy), at most CACHE_SECONDS old; None when
    there is no NVIDIA GPU to read. Concurrent callers share one query."""
    global _cached
    with _lock:
        now = _clock()
        if _cached is not None and 0 <= now - _cached[0] < CACHE_SECONDS:
            sample = _cached[1]
        else:
            sample = _query()
            _cached = (_clock(), sample)
    return dict(sample) if sample is not None else None


def _reset_for_tests(clock: Callable[[], float] = time.monotonic) -> None:
    """Drop the cache and use `clock` (tests only)."""
    global _cached, _clock
    with _lock:
        _cached = None
        _clock = clock
