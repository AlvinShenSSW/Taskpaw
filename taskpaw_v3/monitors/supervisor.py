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
EventObserver = Callable[[str, str, str, str, str], None]


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
    worker_launch_pending: bool = False
    stop: threading.Event = field(default_factory=threading.Event)
    failures: int = 0
    degraded: bool = False
    last_emit_window: int = 0
    emit_count: int = 0
    dropped_in_window: int = 0
    seen_dedupe: _BoundedKeySet = field(default_factory=_BoundedKeySet)
    restart_count: int = 0  # unexpected thread-death restarts (watchdog)
    last_restart: float = 0.0  # monotonic time of last restart
    initialized: threading.Event = field(default_factory=threading.Event)
    cleanup_done: threading.Event = field(default_factory=threading.Event)
    cleanup_thread: Optional[threading.Thread] = None
    cleanup_error: str | None = None
    init_error: str | None = None
    deadline: float | None = None
    retiring: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self.initialized.set()  # registered but not started needs no init wait


class Supervisor:
    def __init__(
        self,
        sink: EventSink,
        clock: Callable[[], float] = time.monotonic,
        *,
        observer: EventObserver | None = None,
    ) -> None:
        self._sink = sink
        self._observer = observer
        self._clock = clock
        self._lock = threading.RLock()  # guards _monitors + emit state
        self._life = threading.RLock()  # serializes lifecycle ops
        self._monitors: dict[str, _Managed] = {}
        self._pending: dict[str, threading.Event] = {}
        self._retired: dict[str, list[_Managed]] = {}
        self._errors: dict[str, str] = {}
        self._closed = False
        self._running = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

    # ── registration / lifecycle ──────────────────────────────────────────
    def _prune(self, iid: str) -> None:
        """Called under the short registry lock; never discards uncertain cleanup."""

        def complete(m: _Managed) -> bool:
            return bool(
                m.retiring
                and m.cleanup_done.is_set()
                and not m.cleanup_error
                and not (m.thread and m.thread.is_alive())
            )

        m = self._monitors.get(iid)
        if m is not None and complete(m):
            if m.init_error:
                self._errors[iid] = m.init_error
            del self._monitors[iid]
        held = [m for m in self._retired.get(iid, []) if not complete(m)]
        if held:
            self._retired[iid] = held
        else:
            self._retired.pop(iid, None)

    def register(
        self,
        plugin: MonitorPlugin,
        config: BaseMonitorConfig,
        instance_id: Optional[str] = None,
        *,
        cancel: threading.Event | None = None,
        deadline: float | None = None,
    ) -> str:
        iid = instance_id or config.name
        token = cancel or threading.Event()
        if not self._life.acquire(timeout=1):
            raise RuntimeError("operation_busy")
        try:
            with self._lock:
                self._prune(iid)
                if (
                    self._closed
                    or iid in self._monitors
                    or iid in self._pending
                    or iid in self._retired
                ):
                    raise ValueError("instance already registered or stopping")
                self._pending[iid] = token
                self._errors.pop(iid, None)
        finally:
            self._life.release()
        try:
            inst = plugin.create(
                iid, config
            )  # no lifecycle/state lock during plugin I/O
            m = _Managed(plugin=plugin, instance=inst, cancel=token, deadline=deadline)
            with self._lock:
                self._monitors[iid] = m
                cancelled = token.is_set() or self._closed
            if cancelled:
                self.request_stop(iid, timeout=0)
            elif self._running.is_set():
                self._start_worker(iid)
        except Exception:
            raise
        finally:
            with self._lock:
                if self._pending.get(iid) is token:
                    del self._pending[iid]
        return iid

    def start(self) -> None:
        if self._watchdog and self._watchdog.is_alive():
            return
        with self._lock:
            if self._closed:
                return
            self._running.set()
            ids = list(self._monitors)
        for iid in ids:
            self._start_worker(iid)
        self._watchdog = threading.Thread(
            target=self._watch, name="supervisor-watchdog", daemon=True
        )
        self._watchdog.start()

    def _cleanup(self, iid: str, m: _Managed, deadline: float) -> None:
        # Never stop concurrently with plugin.start(). The owner may outlive the API budget.
        m.initialized.wait()
        try:
            m.instance.stop(max(0.0, deadline - time.monotonic()))
        except Exception:
            m.cleanup_error = "cleanup_failed"
        if (
            m.thread
            and m.thread.ident is not None
            and m.thread is not threading.current_thread()
        ):
            m.thread.join(max(0.0, deadline - time.monotonic()))
        m.cleanup_done.set()
        with self._lock:
            self._prune(iid)

    def _retire(self, iid: str, m: _Managed, deadline: float) -> None:
        with self._lock:
            m.retiring = True
            m.stop.set()
            if m.cleanup_thread is not None and (
                not m.cleanup_done.is_set() or m.cleanup_thread.is_alive()
            ):
                return  # published/unstarted is already the sole cleanup owner
            if m.cleanup_done.is_set() and not m.cleanup_error:
                return  # callback succeeded; only observe remaining worker
            m.cleanup_done.clear()
            m.cleanup_error = None
            try:
                t = threading.Thread(
                    target=self._cleanup,
                    args=(iid, m, deadline),
                    name=f"cleanup-{iid}",
                    daemon=True,
                )
            except Exception:
                m.cleanup_error = "cleanup_failed"
                m.cleanup_done.set()
                return
            m.cleanup_thread = t
        try:
            t.start()
        except Exception:
            with self._lock:
                if t.ident is None:
                    # A known unstarted launch failure has no callback to finish.
                    m.cleanup_error = "cleanup_failed"
                    m.cleanup_done.set()
                # An already-started thread keeps its owner until _cleanup ends.

    def request_stop(self, iid: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            token = self._pending.get(iid)
            if token is not None:
                token.set()
            managed = (
                [self._monitors[iid]] if iid in self._monitors else []
            ) + self._retired.get(iid, [])
            for m in managed:
                m.cancel.set()
                m.stop.set()
                m.retiring = True
        for m in managed:
            self._retire(iid, m, deadline)

    def stop_result(self, iid: str, timeout: float = 0.0) -> dict:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            managed = (
                [self._monitors[iid]] if iid in self._monitors else []
            ) + self._retired.get(iid, [])
        for m in managed:
            m.cleanup_done.wait(max(0.0, deadline - time.monotonic()))
            if (
                m.thread
                and m.thread.ident is not None
                and m.thread is not threading.current_thread()
            ):
                m.thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._prune(iid)
            held = (
                [self._monitors[iid]] if iid in self._monitors else []
            ) + self._retired.get(iid, [])
            complete = not held and iid not in self._pending
            error = (
                "cleanup_failed"
                if any(m.cleanup_error for m in held)
                else (None if complete else "stop_timeout")
            )
            return {"complete": complete, "error_code": error}

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closed = True
            self._running.clear()
            ids = set(self._monitors) | set(self._pending) | set(self._retired)
        for iid in ids:
            self.request_stop(iid, max(0.0, deadline - time.monotonic()))
        for iid in ids:
            self.stop_result(iid, max(0.0, deadline - time.monotonic()))
        if self._watchdog and self._watchdog.ident is not None:
            self._watchdog.join(max(0.0, deadline - time.monotonic()))

    def reconfigure(
        self,
        instance_id: str,
        config: BaseMonitorConfig,
        stop_timeout: float = 10.0,
        *,
        cancel: threading.Event | None = None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, stop_timeout)
        token = cancel or threading.Event()
        if not self._life.acquire(timeout=max(0, min(1, deadline - time.monotonic()))):
            raise RuntimeError("operation_busy")
        try:
            with self._lock:
                self._prune(instance_id)
                old = self._monitors.get(instance_id)
                if old is None:
                    raise KeyError(instance_id)
                if (
                    old.retiring
                    or instance_id in self._pending
                    or instance_id in self._retired
                    or self._closed
                ):
                    raise RuntimeError("operation_busy")
                self._pending[instance_id] = token
        finally:
            self._life.release()
        replacement = None
        try:
            try:
                inst = old.plugin.create(instance_id, config)
            except Exception:
                raise ValueError("reconfigure rejected: create_failed") from None
            replacement = _Managed(
                plugin=old.plugin, instance=inst, cancel=token, deadline=deadline
            )
            self._retire(instance_id, old, deadline)
            if not self.stop_result(instance_id, max(0.0, deadline - time.monotonic()))[
                "complete"
            ]:
                # The reservation is ours: pending itself is not an old runtime owner.
                with self._lock:
                    self._prune(instance_id)
                    old_alive = (
                        instance_id in self._monitors or instance_id in self._retired
                    )
                if old_alive:
                    raise RuntimeError("stop_timeout")
            with self._lock:
                if token.is_set() or self._closed:
                    raise RuntimeError("operation_busy")
                self._monitors[instance_id] = replacement
            if self._running.is_set():
                self._start_worker(instance_id)
            replacement = None  # ownership transferred to registry
        finally:
            if replacement is not None:
                with self._lock:
                    self._retired.setdefault(instance_id, []).append(replacement)
                self._retire(instance_id, replacement, deadline)
            with self._lock:
                if self._pending.get(instance_id) is token:
                    del self._pending[instance_id]

    def has(self, instance_id: str) -> bool:
        with self._lock:
            self._prune(instance_id)
            return (
                instance_id in self._monitors
                or instance_id in self._pending
                or instance_id in self._retired
            )

    def activation_result(self, iid: str, timeout: float = 0) -> dict:
        with self._lock:
            m = self._monitors.get(iid)
        if m is not None:
            m.initialized.wait(max(0.0, timeout))
        with self._lock:
            if m is None or m.retiring or m.cancel.is_set():
                return {
                    "runtime": "failed",
                    "error_code": (m.init_error or m.cleanup_error)
                    if m
                    else self._errors.pop(iid, "create_failed"),
                }
            if not m.initialized.is_set():
                return {"runtime": "starting", "error_code": None}
            return {"runtime": "applied", "error_code": None}

    def config_matches(self, iid: str, config: dict) -> bool | None:
        with self._lock:
            m = self._monitors.get(iid)
            return (
                m.instance.config.model_dump() == config
                if m is not None and not m.retiring
                else None
            )

    def film_page(self, instance_id: str, page: object, size: object) -> dict | None:
        """Look up under the registry lock; read outside it like reconfigure."""
        with self._lock:
            managed = self._monitors.get(instance_id)
            if managed is None:
                return None
            instance = managed.instance
        try:
            return instance.film_page(page, size)
        except Exception as exc:
            log.warning("Monitor film page unavailable (%s)", type(exc).__name__)
            return None

    def run_films(
        self, instance_id: str, filter: object, page: object, size: object
    ) -> dict | None:
        """Registry lookup under _lock; tracker read outside it (#200)."""
        with self._lock:
            managed = self._monitors.get(instance_id)
            if managed is None or managed.stop.is_set():
                return None
            instance = managed.instance
        try:
            return instance.run_films(filter, page, size)
        except Exception as exc:
            log.warning("Monitor run films unavailable (%s)", type(exc).__name__)
            return None

    def unregister(self, instance_id: str, timeout: float = 10.0) -> dict:
        if not self.has(instance_id):
            raise KeyError(instance_id)
        self.request_stop(instance_id, timeout)
        return self.stop_result(instance_id, timeout)

    # ── worker ────────────────────────────────────────────────────────────
    def _start_worker(self, instance_id: str) -> None:
        with self._lock:
            m = self._monitors.get(instance_id)
            if (
                m is None
                or m.retiring
                or m.stop.is_set()
                or m.cancel.is_set()
                or self._closed
            ):
                return
            if m.worker_launch_pending or (m.thread and m.thread.is_alive()):
                return
            m.worker_launch_pending = True
            m.initialized.clear()
            try:
                t = threading.Thread(
                    target=self._run,
                    args=(instance_id, m),
                    name=f"mon-{instance_id}",
                    daemon=True,
                )
            except Exception:
                m.worker_launch_pending = False
                m.init_error = "start_failed"
                m.initialized.set()
                t = None
            m.thread = t
        if t is None:
            self._retire(instance_id, m, time.monotonic())
            return
        try:
            t.start()
        except Exception:
            with self._lock:
                unstarted = t.ident is None
                if unstarted:
                    m.init_error = "start_failed"
                    m.initialized.set()
            if unstarted:
                self._retire(instance_id, m, time.monotonic())
        finally:
            with self._lock:
                if m.thread is t:
                    m.worker_launch_pending = False

    def _run(self, instance_id: str, m: _Managed) -> None:
        try:
            if not m.stop.is_set() and not m.cancel.is_set():
                m.instance.start(self._emitter_for(instance_id, m))
        except Exception:
            m.init_error = "start_failed"
            m.stop.set()
        finally:
            m.initialized.set()
        if m.init_error:
            self._retire(
                instance_id,
                m,
                m.deadline if m.deadline is not None else time.monotonic() + 5,
            )
            return
        while not m.stop.is_set() and not m.cancel.is_set() and self._running.is_set():
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
                            and not m.worker_launch_pending
                            and m.thread
                            and m.thread.ident is not None
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
                    self._start_worker(iid)
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
            self._emit(instance_id, level, title, message, data, dedupe_key, expected=m)

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
            self._safe_sink(
                instance_id,
                "warn",
                folded[0],
                folded[1],
                None,
                None,
                task_type=m.plugin.type_id,
            )

    def _safe_sink(
        self,
        instance_id,
        level,
        title,
        message,
        data=None,
        dedupe_key=None,
        *,
        task_type="",
    ) -> bool:
        """Call the sink, isolating its exceptions (a bad sink must not degrade a
        healthy monitor or lose-then-suppress later events). Returns success."""
        try:
            self._sink(instance_id, level, title, message, data, dedupe_key)
        except Exception as e:
            log.error("event sink failed (%s): %s", title, e)
            return False
        if self._observer is not None:
            try:
                self._observer(instance_id, task_type, level, title, message)
            except Exception as e:
                # Observation cannot change delivery/dedupe or degrade a monitor.
                log.warning("event observer failed: %s", type(e).__name__)
        return True

    def _emit(
        self,
        instance_id,
        level,
        title,
        message,
        data=None,
        dedupe_key=None,
        *,
        expected=None,
    ) -> None:
        folded_msg = None
        deliver = False
        with self._lock:
            m = self._monitors.get(instance_id)
            if (
                m is None
                or m.retiring
                or m.stop.is_set()
                or (expected is not None and m is not expected)
            ):
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
            task_type = m.plugin.type_id
        # Sink calls happen OUTSIDE the lock (a blocking sink must not stall
        # lifecycle ops) and are exception-isolated.
        if folded_msg is not None:
            self._safe_sink(
                instance_id,
                "warn",
                folded_msg[0],
                folded_msg[1],
                None,
                None,
                task_type=task_type,
            )
        if deliver:
            if (
                self._safe_sink(
                    instance_id,
                    level,
                    title,
                    message,
                    data,
                    dedupe_key,
                    task_type=task_type,
                )
                and dedupe_key is not None
            ):
                with self._lock:
                    m = self._monitors.get(instance_id)
                    if m is not None:
                        m.seen_dedupe.add(dedupe_key)  # record only after success

    # ── introspection ──────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            for iid in list(set(self._monitors) | set(self._retired)):
                self._prune(iid)
            out = {}
            ids = set(self._monitors) | set(self._retired) | set(self._pending)
            for iid in ids:
                m = self._monitors.get(iid) or next(
                    iter(self._retired.get(iid, [])), None
                )
                if m is None:
                    out[iid] = {
                        "state": "stopped",
                        "alive": False,
                        "lifecycle": "starting",
                    }
                    continue
                snap = m.instance.snapshot()
                out[iid] = {
                    "state": snap.state,
                    "metrics": dict(snap.metrics),
                    "detail": snap.detail,
                    "alive": bool(m.thread and m.thread.is_alive()),
                    "failures": m.failures,
                    "degraded": m.degraded,
                    "dropped": m.dropped_in_window,
                    "lifecycle": "stopping"
                    if m.retiring
                    else ("starting" if not m.initialized.is_set() else "running"),
                    "runtime_error_code": m.init_error or m.cleanup_error,
                }
            return out
