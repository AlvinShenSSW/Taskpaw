"""Supervisor: owns monitor instance lifecycles (V3 design §4.1).

- One worker thread per instance runs its `check()` every `poll_interval`.
- A `check()` exception → exponential backoff (5s..5min), failure counter; 5
  consecutive failures → DEGRADED state + one alert.
- A watchdog restarts any worker thread that died unexpectedly (is_alive()).
- `emit` is throttled per instance to `max_events_per_minute` (storm → one
  folded summary), de-duplicated by `dedupe_key` (bounded LRU — no leak), and
  the key is recorded only on actual delivery.
- Lifecycle ops (register/start/stop/reconfigure) are serialized per instance so
  a reconfigure can't race a still-running worker; observability (snapshot/emit)
  uses a separate lock and never blocks on a thread join.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
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
DEDUPE_MAX = 10_000

# (instance_id, level, title, message, data, dedupe_key) — instance_id is the
# STABLE monitor name (used as the event's `monitor` field), title is display text.
EventSink = Callable[[str, str, str, str, Optional[dict], Optional[str]], None]


class _BoundedKeySet:
    """FIFO-bounded set of dedupe keys — no unbounded memory growth."""

    def __init__(self, cap: int = DEDUPE_MAX) -> None:
        self._d: "OrderedDict[str, None]" = OrderedDict()
        self._cap = cap

    def __contains__(self, k: str) -> bool:
        return k in self._d

    def add(self, k: str) -> None:
        self._d[k] = None
        self._d.move_to_end(k)
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def discard(self, k: str) -> None:
        self._d.pop(k, None)


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
    seen_dedupe: _BoundedKeySet = field(default_factory=_BoundedKeySet)
    restart_count: int = 0  # unexpected thread-death restarts (watchdog)
    last_restart: float = 0.0  # monotonic time of last restart


class Supervisor:
    def __init__(
        self, sink: EventSink, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._lock = threading.RLock()  # guards _monitors + emit state
        self._life = threading.RLock()  # serializes lifecycle ops
        self._monitors: dict[str, _Managed] = {}
        self._running = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

    # ── registration / lifecycle ──────────────────────────────────────────
    def register(
        self,
        plugin: MonitorPlugin,
        config: BaseMonitorConfig,
        instance_id: Optional[str] = None,
    ) -> str:
        instance_id = instance_id or config.name
        with self._life:
            inst = plugin.create(instance_id, config)
            with self._lock:
                if instance_id in self._monitors:
                    raise ValueError(f"instance already registered: {instance_id}")
                self._monitors[instance_id] = _Managed(plugin=plugin, instance=inst)
            if self._running.is_set():
                self._start_worker(instance_id)
        return instance_id

    def start(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return  # idempotent — don't leak a second watchdog
        self._running.set()
        with self._lock:
            ids = list(self._monitors)
        for iid in ids:
            with self._life:
                self._start_worker(iid)
        self._watchdog = threading.Thread(
            target=self._watch, name="supervisor-watchdog", daemon=True
        )
        self._watchdog.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._running.clear()
        deadline = time.monotonic() + timeout  # one budget for the whole shutdown
        # Serialize against register/reconfigure (same lifecycle lock) so a
        # concurrent op can't add/replace a monitor mid-shutdown. Timed acquire
        # so a stuck lifecycle op can't block shutdown indefinitely.
        acquired = self._life.acquire(timeout=max(0.0, timeout))
        if not acquired:
            log.error(
                "stop(): could not acquire lifecycle lock in time; proceeding best-effort"
            )
        try:
            with self._lock:
                managed = list(self._monitors.values())
            for m in managed:
                m.stop.set()
            for m in managed:
                # Call instance.stop() FIRST: for a monitor whose stop() closes a
                # socket/file/subprocess to unblock a running check(), this lets
                # the worker break out of a blocking check; THEN we join it. Doing
                # it the other way could burn the budget joining a still-blocked
                # worker and return with the thread alive + resources held.
                self._cleanup_instance(
                    m.instance, max(0.0, deadline - time.monotonic())
                )
                if m.thread:
                    m.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        finally:
            if acquired:
                self._life.release()
        if self._watchdog:
            self._watchdog.join(timeout=max(0.0, deadline - time.monotonic()))

    def reconfigure(
        self, instance_id: str, config: BaseMonitorConfig, stop_timeout: float = 10.0
    ) -> None:
        # Whole sequence serialized so a worker can't run against a half-swapped
        # entry and a concurrent reconfigure can't interleave.
        with self._life:
            with self._lock:
                m = self._monitors.get(instance_id)
                if m is None:
                    raise KeyError(instance_id)
                plugin, old_instance, old_thread, old_stop = (
                    m.plugin,
                    m.instance,
                    m.thread,
                    m.stop,
                )
            # Build the replacement FIRST: if the new config is bad and create()
            # raises, fail without having touched the old monitor (a failed config
            # update must not turn a healthy monitor into a dead one).
            try:
                new_instance = plugin.create(instance_id, config)
            except Exception as e:
                raise ValueError(
                    f"reconfigure of {instance_id} rejected (bad config): {e}"
                ) from e
            old_stop.set()
            if old_thread:
                old_thread.join(timeout=stop_timeout)
                if old_thread.is_alive():
                    # The old worker is stuck (e.g. a long check). Abort WITHOUT
                    # killing it: clear the stop flag so it keeps running on the
                    # OLD config (resources untouched — we have NOT called its
                    # stop() yet), and surface the failure for the caller to retry.
                    old_stop.clear()
                    raise RuntimeError(
                        f"reconfigure of {instance_id} aborted: old worker did not stop"
                    )
            self._cleanup_instance(old_instance, 5.0)
            new = _Managed(plugin=plugin, instance=new_instance)
            with self._lock:
                self._monitors[instance_id] = new
            if self._running.is_set():
                self._start_worker(instance_id)

    def has(self, instance_id: str) -> bool:
        with self._lock:
            return instance_id in self._monitors

    def unregister(self, instance_id: str, timeout: float = 10.0) -> None:
        """Stop + remove ONE monitor live (no agent restart) — used by the control
        API's remove/disable. Serialized against register/reconfigure/stop via the
        lifecycle lock. Raises KeyError if the instance isn't registered."""
        with self._life:
            with self._lock:
                m = self._monitors.pop(instance_id, None)
            if m is None:
                raise KeyError(instance_id)
            # Signal the worker, release the instance's resources first (this can
            # unblock a running check() so the join doesn't burn the budget), then
            # join. The worker also exits via its `_monitors.get(id) is not m`
            # guard now that the entry is gone — and the watchdog won't restart a
            # popped instance (its `m is None` makes `dead` False).
            m.stop.set()
            self._cleanup_instance(m.instance, timeout)
            if m.thread:
                m.thread.join(timeout=timeout)

    @staticmethod
    def _cleanup_instance(instance: MonitorInstance, timeout: float) -> None:
        try:
            instance.stop(timeout)
        except Exception as e:
            log.error("instance %s stop() failed: %s", instance.instance_id, e)

    # ── worker ────────────────────────────────────────────────────────────
    def _start_worker(self, instance_id: str) -> None:
        with self._lock:
            m = self._monitors[instance_id]
            if m.thread and m.thread.is_alive():
                return  # never run two workers for one instance
            m.stop.clear()
            m.thread = threading.Thread(
                target=self._run,
                args=(instance_id, m),
                name=f"mon-{instance_id}",
                daemon=True,
            )
            m.thread.start()

    def _run(self, instance_id: str, m: _Managed) -> None:
        # One-time init hook (open tails/subprocesses). A failure kills the
        # worker → the watchdog restarts it with backoff (and degrades).
        try:
            m.instance.start(self._emitter_for(instance_id, m))
        except Exception as e:
            log.error("monitor %s start() failed: %s", instance_id, e)
            return
        while not m.stop.is_set() and self._running.is_set():
            # Exit if this _Managed is no longer the current one (reconfigured).
            with self._lock:
                if self._monitors.get(instance_id) is not m:
                    return
            # Flush a pending rate-limit summary even in the burst-then-quiet case
            # (where no later _emit would otherwise roll the window).
            self._flush_folded(instance_id)
            # Wallclock cadence (constitution §4): deadline set BEFORE check, so
            # the interval is poll_interval, not poll_interval + check duration.
            iter_start = time.monotonic()
            interval = max(1.0, m.instance.config.poll_interval)
            try:
                # check() runs OUTSIDE the lock (it may be slow / blocking)…
                status = m.instance.check(self._emitter_for(instance_id, m))
                # …then mutate shared state briefly UNDER the lock so snapshot()
                # and _emit() observe consistent failures/degraded/_status.
                with self._lock:
                    m.instance._status = status or m.instance.snapshot()
                    if m.failures or m.degraded:
                        m.failures = 0
                        m.degraded = False
                        m.seen_dedupe.discard(
                            f"{instance_id}:degraded"
                        )  # allow re-alert
                    m.restart_count = 0  # healthy → reset thread-death backoff
                # min 0.1s pause so a check slower than poll_interval can't tight-loop.
                m.stop.wait(timeout=max(0.1, iter_start + interval - time.monotonic()))
            except Exception as e:  # check() failure → backoff, not thread death
                with self._lock:
                    m.failures += 1
                    failures = m.failures
                    degrade_now = failures >= DEGRADE_AFTER and not m.degraded
                    if degrade_now:
                        m.degraded = True
                        m.instance._status = MonitorStatus(
                            state="degraded", detail=str(e)
                        )
                log.warning(
                    "monitor %s check failed (%d): %s", instance_id, failures, e
                )
                self.on_instance_error(instance_id, e)
                if degrade_now:
                    self._emit(
                        instance_id,
                        "alert",
                        f"{instance_id} degraded",
                        f"{DEGRADE_AFTER} consecutive failures: {e}",
                        dedupe_key=f"{instance_id}:degraded",
                    )
                backoff = min(BACKOFF_MAX, BACKOFF_MIN * (2 ** (failures - 1)))
                m.stop.wait(timeout=max(0.1, iter_start + backoff - time.monotonic()))

    def on_instance_error(self, instance_id: str, exc: Exception) -> None:
        """Hook for instance errors (overridable). Default: already logged."""

    def _watch(self) -> None:
        """Restart worker threads that died unexpectedly (is_alive()), with
        exponential backoff so a plugin that crashes on start doesn't spin —
        repeated thread deaths transition it to DEGRADED.

        Lock-order invariant: whenever BOTH locks are held it is always
        _life → _lock. (Listing ids briefly takes _lock alone and releases it
        before _life is acquired, so the invariant holds.)
        """
        while self._running.is_set():
            with self._lock:
                ids = list(self._monitors)
            for iid in ids:
                now = time.monotonic()
                do_restart = False
                do_emit = False
                restarts = 0
                with self._life:  # outer lock first, consistently
                    with self._lock:  # all _Managed mutations stay under _lock
                        m = self._monitors.get(iid)
                        dead = bool(
                            m
                            and not m.stop.is_set()
                            and m.thread
                            and not m.thread.is_alive()
                        )
                        if dead:
                            assert m is not None  # `dead` is only True when m exists
                            backoff = (
                                min(BACKOFF_MAX, BACKOFF_MIN * (2**m.restart_count))
                                if m.restart_count
                                else 0
                            )
                            if (
                                now - m.last_restart
                            ) >= backoff or m.restart_count == 0:
                                m.restart_count += 1
                                m.last_restart = now
                                restarts = m.restart_count
                                do_restart = True
                                if restarts >= DEGRADE_AFTER and not m.degraded:
                                    m.degraded = True
                                    m.instance._status = MonitorStatus(
                                        state="degraded", detail="worker keeps dying"
                                    )
                                    do_emit = True
                    if do_restart:
                        log.error("monitor %s thread died; restart #%d", iid, restarts)
                        self._start_worker(iid)  # takes _lock, nested under _life
                # _emit OUTSIDE _life — the sink must not block lifecycle ops.
                if do_emit:
                    self._emit(
                        iid,
                        "alert",
                        f"{iid} degraded",
                        f"worker thread died {restarts} times",
                        dedupe_key=f"{iid}:degraded",
                    )
            time.sleep(2)

    # ── emit (throttle + dedupe) ───────────────────────────────────────────
    def _emitter_for(self, instance_id: str, m: _Managed) -> EventEmitter:
        def emit(level, title, message, data=None, dedupe_key=None):
            self._emit(instance_id, level, title, message, data, dedupe_key)

        return emit

    def _flush_folded(self, instance_id) -> None:
        """If the rate-limit window rolled over with suppressed events pending,
        emit the folded summary now (even with no new event to trigger it)."""
        folded = None
        with self._lock:
            m = self._monitors.get(instance_id)
            if m is None:
                return
            window = int(self._clock() // 60)
            if window != m.last_emit_window:
                if m.dropped_in_window:
                    folded = (
                        f"{instance_id}: {m.dropped_in_window} suppressed",
                        f"{m.dropped_in_window} events folded (rate limit)",
                    )
                    m.dropped_in_window = 0
                m.last_emit_window = window
                m.emit_count = 0
        if folded is not None:
            self._safe_sink(instance_id, "warn", folded[0], folded[1], None, None)

    def _safe_sink(
        self, instance_id, level, title, message, data=None, dedupe_key=None
    ) -> bool:
        """Call the sink, isolating its exceptions (a bad sink must not degrade a
        healthy monitor or lose-then-suppress later events). Returns success."""
        try:
            self._sink(instance_id, level, title, message, data, dedupe_key)
            return True
        except Exception as e:
            log.error("event sink failed (%s): %s", title, e)
            return False

    def _emit(
        self, instance_id, level, title, message, data=None, dedupe_key=None
    ) -> None:
        folded_msg = None
        deliver = False
        with self._lock:
            m = self._monitors.get(instance_id)
            if m is None:
                return
            if dedupe_key is not None and dedupe_key in m.seen_dedupe:
                return
            window = int(self._clock() // 60)
            if window != m.last_emit_window:
                if m.dropped_in_window:
                    folded_msg = (
                        f"{instance_id}: {m.dropped_in_window} suppressed",
                        f"{m.dropped_in_window} events folded (rate limit)",
                    )
                    m.dropped_in_window = 0
                m.last_emit_window = window
                m.emit_count = 0
            if m.emit_count >= m.instance.config.max_events_per_minute:
                m.dropped_in_window += 1  # dropped → do NOT record the key
            else:
                m.emit_count += 1
                deliver = True
        # Sink calls happen OUTSIDE the lock (a blocking sink must not stall
        # lifecycle ops) and are exception-isolated.
        if folded_msg is not None:
            self._safe_sink(
                instance_id, "warn", folded_msg[0], folded_msg[1], None, None
            )
        if deliver:
            if (
                self._safe_sink(instance_id, level, title, message, data, dedupe_key)
                and dedupe_key is not None
            ):
                with self._lock:
                    m = self._monitors.get(instance_id)
                    if m is not None:
                        m.seen_dedupe.add(dedupe_key)  # record only after success

    # ── introspection ──────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            return {
                iid: {
                    "state": m.instance.snapshot().state,
                    # The actual measured values (CPU/mem/GPU/net for host_metrics,
                    # queue depth, etc.) — the whole point of /status.
                    "metrics": dict(m.instance.snapshot().metrics),
                    "detail": m.instance.snapshot().detail,
                    "alive": bool(m.thread and m.thread.is_alive()),
                    "failures": m.failures,
                    "degraded": m.degraded,
                    "dropped": m.dropped_in_window,
                }
                for iid, m in self._monitors.items()
            }
