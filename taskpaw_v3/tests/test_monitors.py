"""Plugin system + supervisor + built-in plugins (#17)."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.monitors import supervisor as sup_mod
from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)
from taskpaw_v3.monitors.plugins.heartbeat import (
    HeartbeatConfig,
    evaluate_heartbeat,
)
from taskpaw_v3.monitors.plugins.process import (
    ProcessConfig,
    ProcessPlugin,
    process_matches,
)
from taskpaw_v3.monitors.plugins.tcp_check import (
    TcpCheckConfig,
    TcpCheckPlugin,
    tcp_listening,
)
from taskpaw_v3.monitors.registry import PluginRegistry, default_registry
from taskpaw_v3.monitors.supervisor import Supervisor


def test_tasklog_agent_observer_mirrors_delivered_and_folded_only():
    from taskpaw_v3.core.protocol import EventQueue
    from taskpaw_v3.core.tasklog import get_task_log
    from taskpaw_v3.monitors.runtime import build_supervisor

    queue = EventQueue("m")
    sup = build_supervisor(
        default_registry(),
        [
            {
                "type_id": "process",
                "config": {
                    "name": "p",
                    "pattern": "unused",
                    "max_events_per_minute": 1,
                },
            }
        ],
        queue,
        "m",
    )
    clock = [60.0]
    sup._clock = lambda: clock[0]
    sup._emit("p", "alert", "title", "message", {"argv": "PLANTED_SECRET"}, "key")
    sup._emit("p", "alert", "title", "message", None, "key")  # deduped
    sup._emit("p", "info", "dropped", "dropped")
    clock[0] += 60
    sup._flush_folded("p")
    rows = get_task_log().query()["entries"]
    assert len(rows) == 2
    assert all(
        r["kind"] == "event.mirrored" and r["task_type"] == "process" for r in rows
    )
    assert rows[-1]["severity"] == "error"
    assert rows[-1]["data"] == {
        "level": "alert",
        "title": "title",
        "message": "message",
    }
    assert "PLANTED" not in json.dumps(rows)
    assert len(queue.recent()) == 2
    # The Hub constructs Supervisor directly and must remain unaffected.
    hub = Supervisor(lambda *a: None)
    hub.register(ProcessPlugin(), ProcessConfig(name="hub", pattern="unused"))
    hub._emit("hub", "info", "hub", "hub")
    assert get_task_log().query()["entries"] == rows


@contextlib.contextmanager
def _expect_thread_death():
    """Capture (and swallow) exceptions raised in worker threads for the duration
    of the block. Used by tests that intentionally kill a worker so the death does
    not leak out as a PytestUnhandledThreadExceptionWarning. Yields the list of
    captured ``threading.ExceptHookArgs`` for assertions."""
    caught: list = []
    prev_hook = threading.excepthook
    threading.excepthook = caught.append
    try:
        yield caught
    finally:
        threading.excepthook = prev_hook


# ── registry ────────────────────────────────────────────────────────────--
def test_default_registry_has_builtin_plugins():
    reg = default_registry()
    assert {"process", "heartbeat", "tcp_check", "host_metrics"} <= set(reg.types())
    assert reg.get("process").type_id == "process"


def test_registry_rejects_duplicate():
    reg = PluginRegistry()
    reg.register(ProcessPlugin())
    with pytest.raises(ValueError):
        reg.register(ProcessPlugin())


def test_plugin_config_validation_and_json_schema():
    plugin = TcpCheckPlugin()
    cfg = plugin.validate_config({"name": "opend", "port": 11111})
    assert isinstance(cfg, TcpCheckConfig) and cfg.port == 11111
    schema = plugin.json_schema()
    assert "properties" in schema and "port" in schema["properties"]
    with pytest.raises(Exception):
        plugin.validate_config({"name": "x"})  # missing required port


def test_base_config_resource_caps_validation():
    with pytest.raises(Exception):
        TcpCheckConfig(name="x", port=1, poll_interval=0.0)  # min 1s


# ── heartbeat (status-aware) ────────────────────────────────────────────--
def _write(tmp_path, obj):
    p = tmp_path / "hb.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_heartbeat_hibernating_is_not_stale(tmp_path):
    # due far in the future + hibernating → OK (the #13 finding)
    p = _write(
        tmp_path,
        {"status": "hibernating", "next_check_due_utc": "2099-01-01T00:00:00+00:00"},
    )
    cfg = HeartbeatConfig(name="hb", path=str(p))
    assert evaluate_heartbeat(cfg).state == "ok"


def test_heartbeat_overdue_is_hung(tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    p = _write(tmp_path, {"status": "cycling", "next_check_due_utc": past})
    cfg = HeartbeatConfig(name="hb", path=str(p), grace_seconds=60)
    st = evaluate_heartbeat(cfg)
    assert st.state == "error" and "HUNG" in st.detail


def test_heartbeat_fresh_is_ok(tmp_path):
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    p = _write(tmp_path, {"status": "cycling", "next_check_due_utc": future})
    assert evaluate_heartbeat(HeartbeatConfig(name="hb", path=str(p))).state == "ok"


def test_heartbeat_missing_file_is_error(tmp_path):
    cfg = HeartbeatConfig(name="hb", path=str(tmp_path / "nope.json"))
    assert evaluate_heartbeat(cfg).state == "error"


def test_heartbeat_mtime_fallback(tmp_path):
    p = _write(tmp_path, {"status": "cycling"})  # no due field
    cfg = HeartbeatConfig(name="hb", path=str(p), grace_seconds=3600)
    assert evaluate_heartbeat(cfg).state == "ok"  # just written → fresh


# ── tcp_check ─────────────────────────────────────────────────────────---
def test_tcp_listening_true_and_false():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        host, port = s.getsockname()
        assert tcp_listening(host, port, 1.0) is True
    # socket closed → port free → not listening
    assert tcp_listening("127.0.0.1", port, 0.2) is False


# ── process ───────────────────────────────────────────────────────────---
def test_process_matches_current_interpreter():
    assert process_matches("python", search_cmdline=True) is True
    assert process_matches("nonexistent-zzz-proc-xyz", search_cmdline=True) is False


def test_process_instance_emits_on_transition(monkeypatch):
    plugin = ProcessPlugin()
    inst = plugin.create("p", ProcessConfig(name="p", pattern="x"))
    events = []

    def emit(*a, **k):
        events.append((a, k))

    monkeypatch.setattr(
        "taskpaw_v3.monitors.plugins.process._scan", lambda *a, **k: True
    )
    inst.check(emit)  # first observation, prev=None → no emit
    monkeypatch.setattr(
        "taskpaw_v3.monitors.plugins.process._scan", lambda *a, **k: False
    )
    st = inst.check(emit)  # transition alive→down → alert
    assert st.state == "error" and events and events[-1][0][0] == "alert"


# ── supervisor ────────────────────────────────────────────────────────---
class _FakeConfig(BaseMonitorConfig):
    pass


class _FakeInstance(MonitorInstance):
    def __init__(self, instance_id, config, behavior):
        super().__init__(instance_id, config)
        self.behavior = behavior  # callable(emit) -> MonitorStatus or raises

    def check(self, emit):
        return self.behavior(emit)


class _FakePlugin(MonitorPlugin):
    type_id = "fake"

    def __init__(self, behavior):
        self.behavior = behavior

    @classmethod
    def config_model(cls):
        return _FakeConfig

    def create(self, instance_id, config):
        return _FakeInstance(instance_id, config, self.behavior)


def test_supervisor_film_page_unknown_stopped_base_and_raising(monkeypatch):
    sup = Supervisor(lambda *a: None)
    assert sup.film_page("unknown", None, 10) is None
    sup.register(_FakePlugin(lambda emit: MonitorStatus()), _FakeConfig(name="films"))
    inst = sup._monitors["films"].instance
    assert inst.film_page(None, 10) is None
    assert sup.film_page("films", None, 10) is None
    calls = []

    def read(page, size):
        calls.append((page, size))
        # A non-reentrant probe catches holding _lock during the call.
        assert sup._lock.acquire(blocking=False)
        sup._lock.release()
        return {"page": page, "size": size}

    monkeypatch.setattr(sup, "_lock", threading.Lock())
    monkeypatch.setattr(inst, "film_page", read)
    assert sup.film_page("films", 2, 10) == {"page": 2, "size": 10}
    assert calls == [(2, 10)]

    def broken(*args):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(inst, "film_page", broken)
    assert sup.film_page("films", None, 10) is None
    sup.unregister("films")
    assert sup.film_page("films", None, 10) is None


def test_supervisor_run_films_unknown_stopped_base_and_unlocked(monkeypatch):
    sup = Supervisor(lambda *a: None)
    assert sup.run_films("unknown", "done", 1, 10) is None
    sup.register(_FakePlugin(lambda emit: MonitorStatus()), _FakeConfig(name="films"))
    inst = sup._monitors["films"].instance
    assert inst.run_films("done", 1, 10) is None
    assert sup.run_films("films", "done", 1, 10) is None

    def read(filter, page, size):
        assert sup._lock.acquire(blocking=False)
        sup._lock.release()
        return {"filter": filter, "page": page, "size": size}

    monkeypatch.setattr(sup, "_lock", threading.Lock())
    monkeypatch.setattr(inst, "run_films", read)
    assert sup.run_films("films", "open", 2, 10) == {
        "filter": "open",
        "page": 2,
        "size": 10,
    }
    sup._monitors["films"].stop.set()
    assert sup.run_films("films", "open", 2, 10) is None
    sup._monitors["films"].stop.clear()

    def broken(*a):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(inst, "run_films", broken)
    assert sup.run_films("films", "done", 1, 10) is None
    sup.unregister("films")
    assert sup.run_films("films", "done", 1, 10) is None


def test_supervisor_emit_throttle_and_dedupe():
    sink = []
    clock = [0.0]
    sup = Supervisor(sink=lambda *a: sink.append(a), clock=lambda: clock[0])
    sup.register(
        _FakePlugin(lambda e: MonitorStatus(state="ok")),
        _FakeConfig(name="f", max_events_per_minute=2),
    )
    # dedupe: same key emitted twice → one delivery
    sup._emit("f", "info", "t", "m", None, "k1")
    sup._emit("f", "info", "t", "m", None, "k1")
    assert len(sink) == 1
    # throttle: cap is 2/min; 3rd (new keys) is folded
    sup._emit("f", "info", "t", "m", None, "k2")  # 2nd delivery
    sup._emit("f", "info", "t", "m", None, "k3")  # folded (over cap)
    assert len(sink) == 2
    clock[0] = 120.0  # next window flushes a folded summary
    sup._emit("f", "info", "t", "m", None, "k4")
    assert any("suppressed" in s[2] for s in sink)


def test_supervisor_runs_check_and_snapshot():
    sink = []
    sup = Supervisor(sink=lambda *a: sink.append(a))
    calls = []

    def behavior(emit):
        calls.append(1)
        emit("done", "t", "m")
        return MonitorStatus(state="ok")

    sup.register(_FakePlugin(behavior), _FakeConfig(name="f", poll_interval=1))
    sup.start()
    try:
        time.sleep(0.2)  # immediate first check
        assert calls  # ran at least once
        assert sup.snapshot()["f"]["alive"] is True
        assert any(s[1] == "done" for s in sink)
    finally:
        sup.stop()


def test_supervisor_degrades_after_failures(monkeypatch):
    monkeypatch.setattr(sup_mod, "BACKOFF_MIN", 0.01)
    monkeypatch.setattr(sup_mod, "BACKOFF_MAX", 0.02)
    monkeypatch.setattr(sup_mod, "DEGRADE_AFTER", 2)
    sink = []
    sup = Supervisor(sink=lambda *a: sink.append(a))

    def boom(emit):
        raise RuntimeError("nope")

    sup.register(_FakePlugin(boom), _FakeConfig(name="f", poll_interval=1))
    sup.start()
    try:
        # Wait for the ALERT to reach the sink (it's emitted just after the
        # degraded flag is set, so waiting on the sink avoids a flag-vs-emit race).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any("degraded" in s[2] for s in sink):
            time.sleep(0.05)
        assert any("degraded" in s[2] for s in sink)
        assert sup.snapshot()["f"]["degraded"] is True
    finally:
        sup.stop()


def test_supervisor_throttled_keyed_event_not_permanently_suppressed():
    """A keyed alert dropped by the rate limit must NOT be recorded as seen, so a
    later window can still deliver it (Codex 外门 P2)."""
    sink = []
    clock = [0.0]
    sup = Supervisor(sink=lambda *a: sink.append(a), clock=lambda: clock[0])
    sup.register(
        _FakePlugin(lambda e: MonitorStatus(state="ok")),
        _FakeConfig(name="f", max_events_per_minute=1),
    )
    sup._emit("f", "info", "first", "m", None, "kA")  # delivered (cap=1)
    sup._emit("f", "alert", "boom", "m", None, "kB")  # over cap → dropped, not recorded
    assert [s[3] for s in sink] == ["m"]  # only first delivered
    clock[0] = 120.0
    sup._emit("f", "alert", "boom", "m", None, "kB")  # new window → now delivered
    assert any(s[2] == "boom" for s in sink)  # the alert eventually got through


def test_supervisor_reconfigure_stops_old_instance():
    stopped = []

    class _StopInstance(MonitorInstance):
        def check(self, emit):
            return MonitorStatus(state="ok")

        def stop(self, timeout=5.0):
            stopped.append(self.instance_id)

    class _StopPlugin(MonitorPlugin):
        type_id = "stoppy"

        @classmethod
        def config_model(cls):
            return _FakeConfig

        def create(self, instance_id, config):
            return _StopInstance(instance_id, config)

    sup = Supervisor(sink=lambda *a: None)
    sup.register(_StopPlugin(), _FakeConfig(name="f", poll_interval=1))
    sup.reconfigure("f", _FakeConfig(name="f", poll_interval=2))
    assert stopped == ["f"]  # old instance was cleaned up before replacement
    sup.stop()


def test_process_config_rejects_invalid_regex():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProcessConfig(name="p", pattern="(unclosed[")  # invalid regex at config time


def test_supervisor_degraded_key_cleared_on_recovery():
    """After recovery, a later re-degrade must alert again (dedupe key cleared)."""
    sink = []
    sup = Supervisor(sink=lambda *a: sink.append(a))
    # emit a degraded alert (keyed), then simulate recovery clearing the key
    sup.register(
        _FakePlugin(lambda e: MonitorStatus(state="ok")), _FakeConfig(name="f")
    )
    sup._emit("f", "alert", "f degraded", "x", None, "f:degraded")
    sup._emit("f", "alert", "f degraded", "x", None, "f:degraded")  # deduped
    assert sum(1 for s in sink if s[2] == "f degraded") == 1
    sup._monitors["f"].seen_dedupe.discard("f:degraded")  # recovery clears it
    sup._emit("f", "alert", "f degraded", "x", None, "f:degraded")  # re-alert allowed
    assert sum(1 for s in sink if s[2] == "f degraded") == 2


def test_bounded_key_set_evicts_oldest():
    from taskpaw_v3.monitors.supervisor import _BoundedKeySet

    s = _BoundedKeySet(cap=3)
    for k in ["a", "b", "c", "d"]:
        s.add(k)
    assert "a" not in s and "d" in s and "b" in s


def test_supervisor_sink_exception_isolated_and_not_recorded():
    """A throwing sink must not propagate (no false degrade) and a failed delivery
    must not record the dedupe key (so it can be retried)."""
    calls = []

    def bad_sink(*a):
        calls.append(a)
        raise RuntimeError("sink down")

    sup = Supervisor(sink=bad_sink)
    sup.register(
        _FakePlugin(lambda e: MonitorStatus(state="ok")), _FakeConfig(name="f")
    )
    sup._emit("f", "alert", "t", "m", None, "k1")  # must not raise
    assert "k1" not in sup._monitors["f"].seen_dedupe  # not recorded → retryable
    assert len(calls) == 1


def test_start_is_idempotent():
    sup = Supervisor(sink=lambda *a: None)
    sup.register(
        _FakePlugin(lambda e: MonitorStatus(state="ok")),
        _FakeConfig(name="f", poll_interval=1),
    )
    sup.start()
    wd1 = sup._watchdog
    sup.start()  # second call must not spawn a new watchdog
    try:
        assert sup._watchdog is wd1
    finally:
        sup.stop()


def test_process_emits_alert_on_unhealthy_startup(monkeypatch):
    monkeypatch.setattr(
        "taskpaw_v3.monitors.plugins.process._scan", lambda *a, **k: False
    )
    inst = ProcessPlugin().create("p", ProcessConfig(name="p", pattern="x"))
    events = []
    inst.check(lambda *a, **k: events.append(a))  # first check, already down → alert
    assert events and events[0][0] == "alert"


def test_tcp_emits_alert_on_unhealthy_startup():
    inst = TcpCheckPlugin().create(
        "t", TcpCheckConfig(name="t", host="127.0.0.1", port=1, timeout=0.2)
    )
    events = []
    inst.check(lambda *a, **k: events.append(a))  # nothing listening on :1 → alert
    assert events and events[0][0] == "alert"


def test_heartbeat_non_dict_json_is_clean_error(tmp_path):
    p = tmp_path / "hb.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")  # a list, not an object
    assert evaluate_heartbeat(HeartbeatConfig(name="hb", path=str(p))).state == "error"


def test_config_forbids_unknown_keys():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TcpCheckConfig(name="t", port=1, typoo_field=123)


def test_supervisor_watchdog_restarts_dead_worker():
    """A worker that dies unexpectedly is restarted by the watchdog."""
    starts = []

    def behavior(emit):
        starts.append(1)
        if len(starts) == 1:
            raise SystemExit("simulate unexpected thread death")  # kills the worker
        return MonitorStatus(state="ok")

    # SystemExit isn't caught by the check try/except (BaseException) → thread dies.
    # The death is intentional, so swallow it via threading.excepthook to keep the
    # suite warning-clean (an unhandled thread exception otherwise surfaces as a
    # PytestUnhandledThreadExceptionWarning).
    sup = Supervisor(sink=lambda *a: None)
    sup.register(_FakePlugin(behavior), _FakeConfig(name="f", poll_interval=1))
    with _expect_thread_death() as caught:
        sup.start()
        try:
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and len(starts) < 2:
                time.sleep(0.1)
            assert len(starts) >= 2  # watchdog restarted it
        finally:
            sup.stop()
    # exactly the simulated death was swallowed, nothing unexpected
    assert [e.exc_type for e in caught] == [SystemExit]


def test_config_validators_grace_and_pattern():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        HeartbeatConfig(name="h", path="/x", grace_seconds=-1)
    with pytest.raises(ValidationError):
        HeartbeatConfig(name="h", path="")  # empty path
    with pytest.raises(ValidationError):
        ProcessConfig(name="p", pattern="")  # empty pattern


def test_reconfigure_timeout_retains_stopping_old_owner():
    """Cleanup has begun: a stuck old worker stays owned and cannot be resumed."""
    release = threading.Event()
    entered = threading.Event()

    def blocking_check(emit):
        entered.set()
        release.wait(timeout=5)  # simulate a long check
        return MonitorStatus(state="ok")

    prepared_cleaned = threading.Event()
    created = []

    class Tracked(_FakeInstance):
        stop_calls = 0

        def stop(self, timeout=5):
            self.stop_calls += 1
            if self.config.poll_interval == 2:
                prepared_cleaned.set()

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            item = Tracked(iid, cfg, blocking_check)
            created.append(item)
            return item

    sup = Supervisor(sink=lambda *a: None)
    sup.register(Plugin(blocking_check), _FakeConfig(name="f", poll_interval=1))
    sup.start()
    try:
        assert entered.wait(timeout=3)  # worker is inside the blocking check
        old = sup._monitors["f"]
        with pytest.raises(RuntimeError):
            sup.reconfigure(
                "f", _FakeConfig(name="f", poll_interval=2), stop_timeout=0.3
            )
        # Old entry preserved with stop intent; no replacement may start.
        assert sup._monitors["f"] is old
        assert old.stop.is_set()
        assert old.thread.is_alive()
        assert sup.snapshot()["f"]["lifecycle"] == "stopping"
        assert prepared_cleaned.wait(1)
        assert len(created) == 2 and created[1].stop_calls == 1
        with pytest.raises(RuntimeError, match="operation_busy"):
            sup.reconfigure(
                "f", _FakeConfig(name="f", poll_interval=3), stop_timeout=0.01
            )
        assert len(created) == 2  # no third prepared object while old is retained
    finally:
        release.set()
        sup.stop()


def test_folded_summary_flushed_when_quiet_after_burst():
    """Burst over cap then silence: the folded summary must still be delivered
    by the periodic flush, not only by a later _emit."""
    sink = []
    clock = [0.0]
    sup = Supervisor(sink=lambda *a: sink.append(a), clock=lambda: clock[0])
    sup.register(
        _FakePlugin(lambda e: MonitorStatus(state="ok")),
        _FakeConfig(name="f", max_events_per_minute=1),
    )
    sup._emit("f", "info", "t", "m")  # delivered
    sup._emit("f", "info", "t", "m")  # dropped (over cap)
    assert not any("suppressed" in s[2] for s in sink)  # not yet
    clock[0] = 120.0
    sup._flush_folded("f")  # periodic flush in a new, quiet window
    assert any("suppressed" in s[2] for s in sink)


def test_reconfigure_bad_config_preserves_old_monitor():
    """If building the replacement fails (bad config), the old monitor must stay
    installed and running — a failed update must not kill it."""

    class _PickyPlugin(MonitorPlugin):
        type_id = "picky"

        @classmethod
        def config_model(cls):
            return _FakeConfig

        def create(self, instance_id, config):
            if config.name == "bad":
                raise ValueError("nope")
            return _FakeInstance(
                instance_id, config, lambda e: MonitorStatus(state="ok")
            )

    sup = Supervisor(sink=lambda *a: None)
    sup.register(_PickyPlugin(), _FakeConfig(name="ok-cfg"), instance_id="f")
    sup.start()
    try:
        old = sup._monitors["f"]
        with pytest.raises(ValueError):
            sup.reconfigure("f", _FakeConfig(name="bad"))
        assert sup._monitors["f"] is old  # old entry preserved
        assert not old.stop.is_set()  # old worker untouched
        assert old.thread.is_alive()
    finally:
        sup.stop()


def test_heartbeat_expands_user_path(tmp_path, monkeypatch):
    """A ~/... heartbeat path must be expanded (Path() alone doesn't)."""
    # expanduser reads HOME on POSIX but USERPROFILE on Windows — set both so ~
    # resolves to tmp_path on every OS.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "hb.json").write_text(
        json.dumps(
            {
                "status": "cycling",
                "next_check_due_utc": (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    st = evaluate_heartbeat(HeartbeatConfig(name="hb", path="~/hb.json"))
    assert st.state == "ok"  # resolved under HOME, not reported missing


def test_r07_unregister_retains_owned_worker_and_refuses_duplicate():
    entered, release = threading.Event(), threading.Event()

    def block(emit):
        entered.set()
        release.wait(3)
        return MonitorStatus(state="ok")

    plugin = _FakePlugin(block)
    sup = Supervisor(lambda *a: None)
    sup.register(plugin, _FakeConfig(name="owned", poll_interval=1))
    sup.start()
    try:
        assert entered.wait(1)
        old = sup._monitors["owned"]
        result = sup.unregister("owned", timeout=0.02)
        assert not result["complete"]
        assert sup.has("owned") and sup._monitors["owned"] is old
        assert sup.snapshot()["owned"]["lifecycle"] == "stopping"
        with pytest.raises(ValueError):
            sup.register(plugin, _FakeConfig(name="owned"))
        release.set()
        old.thread.join(1)
        assert sup.stop_result("owned", timeout=1)["complete"]
        assert not sup.has("owned")
    finally:
        release.set()
        sup.stop(timeout=1)


def test_r07_pending_create_cancelled_no_late_worker_or_duplicate():
    entered, release = threading.Event(), threading.Event()

    class BlockPlugin(_FakePlugin):
        creates = 0
        starts = 0
        stops = 0

        def create(self, iid, cfg):
            self.creates += 1
            entered.set()
            assert release.wait(3)
            outer = self

            class Owned(_FakeInstance):
                def start(self, emit):
                    outer.starts += 1

                def stop(self, timeout=5):
                    outer.stops += 1

            return Owned(iid, cfg, lambda emit: MonitorStatus())

    plugin = BlockPlugin(lambda emit: MonitorStatus())
    sup = Supervisor(lambda *a: None)
    sup.start()
    creator = threading.Thread(
        target=lambda: sup.register(plugin, _FakeConfig(name="owned"))
    )
    creator.start()
    try:
        assert entered.wait(1)
        sup.request_stop("owned", timeout=0.02)
        assert not sup.stop_result("owned", timeout=0.02)["complete"]
        with pytest.raises(ValueError):
            sup.register(plugin, _FakeConfig(name="owned"))
        assert plugin.creates == 1
        release.set()
        creator.join(1)
        assert not creator.is_alive()
        assert sup.stop_result("owned", timeout=1)["complete"]
        assert plugin.starts == 0 and plugin.stops == 1
    finally:
        release.set()
        creator.join(1)
        sup.stop(timeout=1)


def test_r07_blocked_init_cleanup_single_owner_and_late_emit():
    entered, release_init = threading.Event(), threading.Event()
    cleaning, release_cleanup = threading.Event(), threading.Event()
    emitted, calls = [], []

    class Owned(_FakeInstance):
        def start(self, emit):
            self.late_emit = emit
            entered.set()
            assert release_init.wait(3)
            emit("info", "late", "late init")

        def stop(self, timeout=5):
            calls.append("stop")
            cleaning.set()
            assert release_cleanup.wait(3)

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg, lambda emit: calls.append("check"))

    sup = Supervisor(lambda *args: emitted.append(args))
    plugin = Plugin(None)
    sup.register(plugin, _FakeConfig(name="owned"))
    old = sup._monitors["owned"]
    sup.start()
    try:
        assert entered.wait(1)
        sup.request_stop("owned", timeout=0.02)
        cleanup_owner = old.cleanup_thread
        assert not sup.stop_result("owned", timeout=0.02)["complete"]
        # Global shutdown shares the existing retirement owner; start and stop
        # never run concurrently on the object whose start has not returned.
        sup.stop(timeout=0.02)
        assert old.cleanup_thread is cleanup_owner and calls == []
        assert sup.snapshot()["owned"]["lifecycle"] == "stopping"
        release_init.set()
        assert cleaning.wait(1)
        assert emitted == [] and calls == ["stop"]
        sup.request_stop("owned", timeout=0.02)
        assert old.cleanup_thread is cleanup_owner
        assert not sup.stop_result("owned", timeout=0.02)["complete"]
        release_cleanup.set()
        assert sup.stop_result("owned", timeout=1)["complete"]
        assert not sup.has("owned") and calls == ["stop"]
    finally:
        release_init.set()
        release_cleanup.set()
        sup.stop(timeout=1)


def test_r07_stop_ignores_lifecycle_lock_and_old_emitter_cannot_hit_replacement():
    emits = []
    sup = Supervisor(lambda *args: emits.append(args))
    plugin = _FakePlugin(lambda emit: MonitorStatus(state="idle"))
    sup.register(plugin, _FakeConfig(name="owned"))
    old = sup._monitors["owned"]
    locked, release = threading.Event(), threading.Event()

    def holder():
        with sup._life:
            locked.set()
            assert release.wait(3)

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    try:
        assert locked.wait(1)
        before = time.monotonic()
        sup.request_stop("owned", timeout=0.05)
        assert sup.stop_result("owned", timeout=0.05)["complete"]
        assert time.monotonic() - before < 0.3
    finally:
        release.set()
        holder_thread.join(1)
    sup.register(plugin, _FakeConfig(name="owned"))
    sup._emit("owned", "info", "old", "old generation", expected=old)
    assert emits == [] and sup.has("owned")
    sup._emit(
        "owned", "info", "new", "current generation", expected=sup._monitors["owned"]
    )
    assert len(emits) == 1
    sup.stop(timeout=1)


@pytest.mark.parametrize("second", ["stop", "shutdown"])
def test_r07_sr001_cleanup_publication_reserves_single_owner(monkeypatch, second):
    published, allow_start = threading.Event(), threading.Event()
    entered, release = threading.Event(), threading.Event()
    owners, calls = [], []
    first = [True]

    class Owned(_FakeInstance):
        def stop(self, timeout=5):
            calls.append(threading.current_thread().ident)
            entered.set()
            assert release.wait(3)

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg, self.behavior)

    sup = Supervisor(lambda *a: None)
    sup.register(Plugin(None), _FakeConfig(name="owned-publication"))
    managed = sup._monitors["owned-publication"]
    original = threading.Thread.start

    def start(t):
        if t.name == "cleanup-owned-publication":
            owners.append(t)
            if first[0]:
                first[0] = False
                published.set()
                assert allow_start.wait(3)
        return original(t)

    caller = threading.Thread(
        target=lambda: sup.request_stop("owned-publication", 0.03)
    )
    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        caller.start()
        assert published.wait(1)
        before = time.monotonic()
        if second == "stop":
            sup.request_stop("owned-publication", 0.03)
            assert not sup.stop_result("owned-publication", 0.03)["complete"]
        else:
            sup.stop(0.03)
        assert time.monotonic() - before < 0.3
        assert len(owners) == 1 and managed.cleanup_thread is owners[0]
        assert sup.snapshot()["owned-publication"]["lifecycle"] == "stopping"
        allow_start.set()
        caller.join(1)
        assert entered.wait(1) and len(calls) == 1
        release.set()
        assert sup.stop_result("owned-publication", 1)["complete"]
    finally:
        allow_start.set()
        release.set()
        caller.join(1)
        for owner in owners:
            if owner.ident is not None:
                owner.join(1)
        sup.stop(1)
        assert not caller.is_alive() and all(not t.is_alive() for t in owners)


def test_r07_sr001_actual_watchdog_ignores_published_unstarted_worker(monkeypatch):
    published, allow_start = threading.Event(), threading.Event()
    entered, release = threading.Event(), threading.Event()
    owners, calls = [], []
    first = [True]

    class Owned(_FakeInstance):
        def start(self, emit):
            calls.append(threading.current_thread().ident)
            entered.set()
            assert release.wait(3)

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg, lambda emit: MonitorStatus(state="idle"))

    sup = Supervisor(lambda *a: None)
    sup._running.set()
    original = threading.Thread.start

    def start(t):
        if t.name == "mon-owned-publication":
            owners.append(t)
            if first[0]:
                first[0] = False
                published.set()
                assert allow_start.wait(3)
        return original(t)

    creator = threading.Thread(
        target=lambda: sup.register(Plugin(None), _FakeConfig(name="owned-publication"))
    )
    watcher = threading.Thread(target=sup._watch)
    monkeypatch.setattr(threading.Thread, "start", start)
    # One real production watchdog iteration, with only this fake's Event reset.
    monkeypatch.setattr(sup_mod.time, "sleep", lambda seconds: sup._running.clear())
    try:
        creator.start()
        assert published.wait(1)
        watcher.start()
        watcher.join(1)
        assert not watcher.is_alive()
        assert len(owners) == 1
        assert sup._monitors["owned-publication"].restart_count == 0
        allow_start.set()
        creator.join(1)
        assert entered.wait(1) and len(calls) == 1
        assert sup._monitors["owned-publication"].thread is owners[0]
    finally:
        allow_start.set()
        release.set()
        sup._running.clear()
        creator.join(1)
        if watcher.ident is not None:
            watcher.join(1)
        for owner in owners:
            if owner.ident is not None:
                owner.join(1)
        sup.stop(1)
        assert all(not t.is_alive() for t in owners)


def test_r07_sr001_known_cleanup_launch_failure_allows_one_retry(monkeypatch):
    calls = []
    sup = Supervisor(lambda *a: None)
    sup.register(_FakePlugin(None), _FakeConfig(name="owned-launch"))
    managed = sup._monitors["owned-launch"]
    monkeypatch.setattr(managed.instance, "stop", lambda timeout: calls.append(1))
    original = threading.Thread.start

    def failed(t):
        if t.name == "cleanup-owned-launch":
            raise RuntimeError("PLANTED_LAUNCH_SECRET")
        return original(t)

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", failed)
        sup.request_stop("owned-launch", 0.03)
        assert sup.stop_result("owned-launch", 0.03) == {
            "complete": False,
            "error_code": "cleanup_failed",
        }
        assert calls == [] and sup.has("owned-launch")
    sup.request_stop("owned-launch", 1)
    assert sup.stop_result("owned-launch", 1)["complete"]
    assert calls == [1] and not sup.has("owned-launch")
    sup.stop(1)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_r07_sr001_known_worker_launch_failure_retires_before_explicit_retry(
    monkeypatch, phase
):
    callbacks = []

    class Owned(_FakeInstance):
        def stop(self, timeout=5):
            callbacks.append(self)

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg, lambda emit: MonitorStatus(state="idle"))

    sup = Supervisor(lambda *a: None)
    plugin = Plugin(None)
    sup.register(plugin, _FakeConfig(name="owned-launch"))
    old = sup._monitors["owned-launch"]
    original_thread, original_start = threading.Thread, threading.Thread.start

    def construct(*args, **kwargs):
        if kwargs.get("name") == "mon-owned-launch":
            raise RuntimeError("PLANTED_CONSTRUCTOR_SECRET")
        return original_thread(*args, **kwargs)

    def start(t):
        if t.name == "mon-owned-launch":
            raise RuntimeError("PLANTED_START_SECRET")
        return original_start(t)

    try:
        with monkeypatch.context() as patch:
            if phase == "construct":
                patch.setattr(sup_mod.threading, "Thread", construct)
            else:
                patch.setattr(threading.Thread, "start", start)
            sup.start()
            assert sup.stop_result("owned-launch", 1)["complete"]
            assert not old.worker_launch_pending and old.init_error == "start_failed"
            assert callbacks == [old.instance] and not sup.has("owned-launch")
            assert sup.activation_result("owned-launch")["error_code"] == "start_failed"
        sup.register(plugin, _FakeConfig(name="owned-launch"))
        replacement = sup._monitors["owned-launch"]
        assert (
            replacement is not old
            and sup.activation_result("owned-launch", 1)["runtime"] == "applied"
        )
        assert replacement.thread.is_alive()
    finally:
        sup.stop(1)
