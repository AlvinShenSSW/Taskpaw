"""CPU / GPU temperature and fan RPM from a resident LibreHardwareMonitor (#336).

LibreHardwareMonitor's Remote Web Server serves Prometheus text at the fixed
`LHM_URL`; samples are selected by their `sensorName` only, never by the
hardware-type segment of the metric name. Without LHM the GPU temperature
comes from the shared `gpu_sample()` query (no second nvidia-smi process).
Non-finite values are dropped, so nothing non-finite reaches a JSON response.
The launcher owns one `SensorSampler`; HTTP reads only its cached snapshot.
"""

from __future__ import annotations

import math
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

from taskpaw_v3.core import gpu_sample

SAMPLE_SECONDS = 5.0
LHM_URL = "http://127.0.0.1:8085/metrics"
LHM_TIMEOUT = 2.0
# Step 0 (#336), verified on the fleet: Intel reports "CPU Package", AMD Ryzen
# reports "Core (Tctl/Tdie)"; NVIDIA reports "GPU Core". The single point of change.
CPU_SENSORS = ("CPU Package", "Core (Tctl/Tdie)")
GPU_SENSOR = "GPU Core"

_LINE = re.compile(
    r"^lhm_[a-z0-9_]+_(?P<kind>temperature_celsius|fan_rpm)"
    r"\s*\{(?P<labels>[^}]*)\}\s+(?P<value>\S+)"
)
_NAME = re.compile(r'"?sensorName"?="((?:[^"\\]|\\.)*)"')
_ESCAPE = re.compile(r"\\(.)")


def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda m: "\n" if m[1] == "n" else m[1], text)


def parse_metrics(text: str) -> dict[str, Any]:
    """`cpu_c`, `gpu_c` (the hottest `GPU_SENSOR`) and the spinning `fans`."""
    cpus: list[float] = []
    gpus: list[float] = []
    fans: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        name = _NAME.search(match["labels"]) if match else None
        if match is None or name is None:
            continue
        try:
            value = float(match["value"])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        sensor = _unescape(name[1])
        if match["kind"] == "fan_rpm":
            if value > 0:
                fans.append({"name": sensor, "rpm": round(value)})
        elif sensor in CPU_SENSORS:
            cpus.append(value)
        elif sensor == GPU_SENSOR:
            gpus.append(value)
    return {
        "cpu_c": round(max(cpus), 1) if cpus else None,
        "gpu_c": round(max(gpus), 1) if gpus else None,
        "fans": fans,
    }


def read_lhm() -> Optional[dict[str, Any]]:
    """One bounded loopback read; None when LHM does not answer usefully."""
    # The empty ProxyHandler keeps a loopback request away from a system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(LHM_URL, timeout=LHM_TIMEOUT) as response:
            if response.status != 200:
                return None
            return parse_metrics(response.read().decode("utf-8"))
    except Exception:
        return None


def read_sensors() -> dict[str, Any]:
    """LHM when it answers, else the GPU temperature from nvidia-smi."""
    lhm = read_lhm()
    if lhm is not None:
        return {"source": "lhm", **lhm}
    sample = gpu_sample.gpu_sample()
    gpu = sample.get("temp_c") if sample else None
    return {
        "source": None if gpu is None else "nvidia-smi",
        "cpu_c": None,
        "gpu_c": None if gpu is None else round(gpu, 1),
        "fans": [],
    }


class SensorSampler:
    """Single-owner background sampling, with a short-lock cache for readers."""

    def __init__(
        self,
        *,
        reader: Optional[Callable[[], dict[str, Any]]] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        interval: float = SAMPLE_SECONDS,
    ) -> None:
        self._reader = reader
        self._clock, self._wall, self._interval = clock, wall, interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sample: dict[str, Any] = {
            "source": None,
            "cpu_c": None,
            "gpu_c": None,
            "fans": [],
        }
        self._at: Optional[float] = None
        self._mono: Optional[float] = None

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            return
        self._thread = threading.Thread(
            target=self._loop, name="sensor-sampler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 9.0) -> bool:
        """Above the worst tick: a 2 s LHM read plus a 5 s nvidia-smi query."""
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
                # Skip missed slots instead of replaying a backlog of reads.
                deadline += (
                    math.ceil((now - deadline) / self._interval) * self._interval
                )
            self._stop.wait(max(0.0, deadline - self._clock()))

    def tick(self) -> None:
        """Replace the snapshot; its time is taken before the read."""
        mono, at = self._clock(), self._wall()
        # Resolved at call time, so tests can stub the module's reader.
        sample = (self._reader or read_sensors)()
        with self._lock:
            self._sample, self._at, self._mono = sample, at, mono

    def snapshot(self) -> dict[str, Any]:
        """A copy of the last sample, without IO; `at` is null before the first."""
        with self._lock:
            sample, at = self._sample, self._at
        return {
            "at": at,
            "source": sample["source"],
            "cpu_c": sample["cpu_c"],
            "gpu_c": sample["gpu_c"],
            "fans": [dict(fan) for fan in sample["fans"]],
        }

    def age_s(self) -> Optional[float]:
        """Seconds since the last sample on this machine's monotonic clock."""
        with self._lock:
            mono = self._mono
        return None if mono is None else max(0.0, self._clock() - mono)
