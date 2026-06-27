"""`host_metrics` monitor — host health: CPU / mem / GPU / net / disk (§5b).

Runs on every agent AND on the Hub (self-monitor). Reports basics each cycle and
alerts on thresholds. GPU per operator decision (#21): **Windows collects GPU**
via `nvidia-smi` (reusing the V2 method); **macOS ignores GPU** (field `n/a`).
CPU/mem/disk/net are psutil (cross-platform). The "machine alive" sign needs no
probe here — a reachable agent running this monitor is itself the liveness proof.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Optional

from pydantic import Field

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class HostMetricsConfig(BaseMonitorConfig):
    cpu_alert_pct: float = Field(90.0, ge=0, le=100)
    mem_alert_pct: float = Field(90.0, ge=0, le=100)
    disk_alert_pct: float = Field(90.0, ge=0, le=100)
    disk_path: str = "/"
    # CPU must stay over the threshold this many consecutive cycles before alert
    # (a single spike shouldn't page anyone).
    cpu_sustained_cycles: int = Field(3, ge=1)
    collect_gpu: bool = True  # honored only on Windows (else n/a)


def read_gpu_percent() -> Optional[float]:
    """Windows GPU utilization via nvidia-smi (V2 method). None when unavailable
    (non-Windows, no nvidia-smi, or no NVIDIA GPU)."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        # average across GPUs
        vals = [float(x) for x in out.stdout.strip().splitlines() if x.strip()]
        return round(sum(vals) / len(vals), 1) if vals else None
    except Exception:
        return None


class HostMetricsInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: HostMetricsConfig) -> None:
        super().__init__(instance_id, config)
        self._prev_net: Optional[tuple[float, int, int]] = None  # (ts, bytes_sent, bytes_recv)
        self._cpu_breaches = 0
        self._alerted: set[str] = set()  # which metrics are currently in alert

    def _emit_threshold(self, emit, metric: str, breached: bool, msg: str) -> None:
        """Edge-triggered alert/recovery per metric (dedupe by metric name)."""
        if breached and metric not in self._alerted:
            self._alerted.add(metric)
            emit("alert", f"{self.config.name}: {metric} high", msg, dedupe_key=None)
        elif not breached and metric in self._alerted:
            self._alerted.discard(metric)
            emit("done", f"{self.config.name}: {metric} recovered", msg, dedupe_key=None)

    def check(self, emit: EventEmitter) -> MonitorStatus:
        if psutil is None:
            raise RuntimeError("psutil not available")
        cfg: HostMetricsConfig = self.config  # type: ignore[assignment]

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(cfg.disk_path).percent

        # network throughput (bytes/s) from the delta since the last check
        now = time.monotonic()
        net = psutil.net_io_counters()
        net_in = net_out = 0.0
        if self._prev_net is not None:
            dt = max(1e-6, now - self._prev_net[0])
            net_out = max(0.0, (net.bytes_sent - self._prev_net[1]) / dt)
            net_in = max(0.0, (net.bytes_recv - self._prev_net[2]) / dt)
        self._prev_net = (now, net.bytes_sent, net.bytes_recv)

        gpu = read_gpu_percent() if cfg.collect_gpu else None

        # sustained-CPU alerting
        if cpu >= cfg.cpu_alert_pct:
            self._cpu_breaches += 1
        else:
            self._cpu_breaches = 0
        self._emit_threshold(emit, "cpu", self._cpu_breaches >= cfg.cpu_sustained_cycles,
                             f"CPU {cpu:.0f}% ≥ {cfg.cpu_alert_pct:.0f}% for {self._cpu_breaches} cycles")
        self._emit_threshold(emit, "memory", mem >= cfg.mem_alert_pct,
                             f"memory {mem:.0f}% ≥ {cfg.mem_alert_pct:.0f}%")
        self._emit_threshold(emit, "disk", disk >= cfg.disk_alert_pct,
                             f"disk {disk:.0f}% ≥ {cfg.disk_alert_pct:.0f}%")

        state = "warn" if self._alerted else "ok"
        metrics = {
            "cpu_pct": round(cpu, 1),
            "mem_pct": round(mem, 1),
            "disk_pct": round(disk, 1),
            "net_in_bps": round(net_in),
            "net_out_bps": round(net_out),
            "gpu_pct": gpu if gpu is not None else "n/a",
        }
        return MonitorStatus(state=state, detail=",".join(sorted(self._alerted)) or "ok", metrics=metrics)


class HostMetricsPlugin(MonitorPlugin):
    type_id = "host_metrics"
    display_name = "Host metrics"
    category = "service"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return HostMetricsConfig

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return HostMetricsInstance(instance_id, config)  # type: ignore[arg-type]
