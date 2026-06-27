"""Supervisor: owns monitor instance lifecycles (V3 design §4.1).

- One worker thread per instance runs its `check()` every `poll_interval`.
- A `check()` exception → exponential backoff (5s..5min), failure counter; 5
  consecutive failures → DEGRADED state + one alert.
- A watchdog restarts any worker thread that died unexpectedly (is_alive()).
- `emit` is throttled per instance to `max_events_per_minute` (storm → one
  folded summary), and de-duplicated by `dedupe_key`.
- `reconfigure` = stop → recreate → start (instances may override hot-apply).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)

log = logging.getLogger("taskpaw.supervisor")

BACKOFF_MIN = 5.0
BACKOFF_MAX = 300.0
DEGRADE_AFTER = 5

# Sink the supervisor forwards confirmed events to (e.g. the agent EventQueue).
EventSink = Callable[[str, str, str, Optional[dict], Optional[str]], None]


@dataclass
class _Managed:
    plugin: MonitorPlugin
    instance: MonitorInstance
    thread: Optional[threading.Thread] = None
    stop: threading.Event = field(default_factory=threading.Event)
    failures: int = 0
    degraded: bool = False
    last_emit_window: int = 0
    emit_count: int = 0
    dropped_in_window: int = 0
    seen_dedupe: set = field(default_factory=set)


class Supervisor:
    def __init__(self, sink: EventSink, clock: Callable[[], float] = time.monotonic) -> None:
        self._sink = sink
        self._clock = clock
        self._lock = threading.RLock()
        self._monitors: dict[str, _Managed] = {}
        self._running = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

    # ── registration / lifecycle ──────────────────────────────────────────
    def register(self, plugin: MonitorPlugin, config: BaseMonitorConfig, instance_id: Optional[str] = None) -> str:
        instance_id = instance_id or config.name
        inst = plugin.create(instance_id, config)
        with self._lock:
            if instance_id in self._monitors:
                raise ValueError(f"instance already registered: {instance_id}")
            m = _Managed(plugin=plugin, instance=inst)
            self._monitors[instance_id] = m
        if self._running.is_set():
            self._start_worker(instance_id)
        return instance_id

    def start(self) -> None:
        self._running.set()
        with self._lock:
            ids = list(self._monitors)
        for iid in ids:
            self._start_worker(iid)
        self._watchdog = threading.Thread(target=self._watch, name="supervisor-watchdog", daemon=True)
        self._watchdog.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._running.clear()
        with self._lock:
            managed = list(self._monitors.values())
        for m in managed:
            m.stop.set()
        for m in managed:
            if m.thread:
                m.thread.join(timeout=timeout)
            try:
                m.instance.stop(timeout) if hasattr(m.instance, "stop") else None
            except Exception as e:
                log.error("instance %s stop() failed: %s", m.instance.instance_id, e)
        if self._watchdog:
            self._watchdog.join(timeout=timeout)

    def reconfigure(self, instance_id: str, config: BaseMonitorConfig) -> None:
        with self._lock:
            m = self._monitors.get(instance_id)
            if m is None:
                raise KeyError(instance_id)
            plugin = m.plugin
        # Default semantics: stop → recreate → start.
        self._stop_worker(instance_id)
        with self._lock:
            self._monitors[instance_id] = _Managed(plugin=plugin, instance=plugin.create(instance_id, config))
        if self._running.is_set():
            self._start_worker(instance_id)

    # ── worker ────────────────────────────────────────────────────────────
    def _start_worker(self, instance_id: str) -> None:
        with self._lock:
            m = self._monitors[instance_id]
            m.stop.clear()
            m.thread = threading.Thread(target=self._run, args=(instance_id,), name=f"mon-{instance_id}", daemon=True)
            m.thread.start()

    def _stop_worker(self, instance_id: str) -> None:
        with self._lock:
            m = self._monitors.get(instance_id)
        if not m:
            return
        m.stop.set()
        if m.thread:
            m.thread.join(timeout=5)

    def _run(self, instance_id: str) -> None:
        m = self._monitors[instance_id]
        while not m.stop.is_set() and self._running.is_set():
            try:
                status = m.instance.check(self._emitter_for(instance_id))
                m.instance._status = status or m.instance.snapshot()
                if m.failures or m.degraded:
                    m.failures = 0
                    m.degraded = False
                m.stop.wait(timeout=max(1.0, m.instance.config.poll_interval))
            except Exception as e:  # check() failure → backoff, not thread death
                m.failures += 1
                log.warning("monitor %s check failed (%d): %s", instance_id, m.failures, e)
                self.on_instance_error(instance_id, e)
                if m.failures >= DEGRADE_AFTER and not m.degraded:
                    m.degraded = True
                    m.instance._status = MonitorStatus(state="degraded", detail=str(e))
                    self._emit(instance_id, "alert", f"{instance_id} degraded",
                               f"{DEGRADE_AFTER} consecutive failures: {e}",
                               dedupe_key=f"{instance_id}:degraded")
                backoff = min(BACKOFF_MAX, BACKOFF_MIN * (2 ** (m.failures - 1)))
                m.stop.wait(timeout=backoff)

    def on_instance_error(self, instance_id: str, exc: Exception) -> None:
        """Hook for instance errors (overridable). Default: already logged."""

    def _watch(self) -> None:
        """Restart worker threads that died unexpectedly (is_alive())."""
        while self._running.is_set():
            with self._lock:
                ids = list(self._monitors)
            for iid in ids:
                m = self._monitors.get(iid)
                if not m or m.stop.is_set():
                    continue
                if m.thread and not m.thread.is_alive():
                    log.error("monitor %s thread died; restarting", iid)
                    self._start_worker(iid)
            time.sleep(2)

    # ── emit (throttle + dedupe) ───────────────────────────────────────────
    def _emitter_for(self, instance_id: str) -> EventEmitter:
        def emit(level, title, message, data=None, dedupe_key=None):
            self._emit(instance_id, level, title, message, data, dedupe_key)
        return emit

    def _emit(self, instance_id, level, title, message, data=None, dedupe_key=None) -> None:
        m = self._monitors.get(instance_id)
        if m is None:
            return
        with self._lock:
            # de-dupe identical keyed events
            if dedupe_key is not None:
                if dedupe_key in m.seen_dedupe:
                    return
                m.seen_dedupe.add(dedupe_key)
            # per-minute throttle (storm → folded summary)
            window = int(self._clock() // 60)
            if window != m.last_emit_window:
                if m.dropped_in_window:
                    folded = m.dropped_in_window
                    m.dropped_in_window = 0
                    self._sink("warn", f"{instance_id}: {folded} suppressed",
                               f"{folded} events folded (rate limit)", None, None)
                m.last_emit_window = window
                m.emit_count = 0
            if m.emit_count >= m.instance.config.max_events_per_minute:
                m.dropped_in_window += 1
                return
            m.emit_count += 1
        self._sink(level, title, message, data, dedupe_key)

    # ── introspection ──────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                iid: {
                    "state": m.instance.snapshot().state,
                    "alive": bool(m.thread and m.thread.is_alive()),
                    "failures": m.failures,
                    "degraded": m.degraded,
                    "dropped": m.dropped_in_window,
                }
                for iid, m in self._monitors.items()
            }
