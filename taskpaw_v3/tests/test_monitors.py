"""Plugin system + supervisor + built-in plugins (#17)."""

from __future__ import annotations

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
from taskpaw_v3.monitors.plugins.heartbeat import HeartbeatConfig, HeartbeatPlugin, evaluate_heartbeat
from taskpaw_v3.monitors.plugins.process import ProcessConfig, ProcessPlugin, process_matches
from taskpaw_v3.monitors.plugins.tcp_check import TcpCheckConfig, TcpCheckPlugin, tcp_listening
from taskpaw_v3.monitors.registry import PluginRegistry, default_registry
from taskpaw_v3.monitors.supervisor import Supervisor


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
    p = _write(tmp_path, {"status": "hibernating",
                          "next_check_due_utc": "2099-01-01T00:00:00+00:00"})
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
    emit = lambda *a, **k: events.append((a, k))
    monkeypatch.setattr("taskpaw_v3.monitors.plugins.process._scan", lambda *a, **k: True)
    inst.check(emit)  # first observation, prev=None → no emit
    monkeypatch.setattr("taskpaw_v3.monitors.plugins.process._scan", lambda *a, **k: False)
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


def test_supervisor_emit_throttle_and_dedupe():
    sink = []
    clock = [0.0]
    sup = Supervisor(sink=lambda *a: sink.append(a), clock=lambda: clock[0])
    sup.register(_FakePlugin(lambda e: MonitorStatus(state="ok")),
                 _FakeConfig(name="f", max_events_per_minute=2))
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
    sup.register(_FakePlugin(lambda e: MonitorStatus(state="ok")),
                 _FakeConfig(name="f", max_events_per_minute=1))
    sup._emit("f", "info", "first", "m", None, "kA")   # delivered (cap=1)
    sup._emit("f", "alert", "boom", "m", None, "kB")   # over cap → dropped, not recorded
    assert [s[3] for s in sink] == ["m"]  # only first delivered
    clock[0] = 120.0
    sup._emit("f", "alert", "boom", "m", None, "kB")   # new window → now delivered
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
    import taskpaw_v3.monitors.supervisor as sm
    sink = []
    sup = Supervisor(sink=lambda *a: sink.append(a))
    # emit a degraded alert (keyed), then simulate recovery clearing the key
    sup.register(_FakePlugin(lambda e: MonitorStatus(state="ok")), _FakeConfig(name="f"))
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
    sup.register(_FakePlugin(lambda e: MonitorStatus(state="ok")), _FakeConfig(name="f"))
    sup._emit("f", "alert", "t", "m", None, "k1")  # must not raise
    assert "k1" not in sup._monitors["f"].seen_dedupe  # not recorded → retryable
    assert len(calls) == 1


def test_start_is_idempotent():
    sup = Supervisor(sink=lambda *a: None)
    sup.register(_FakePlugin(lambda e: MonitorStatus(state="ok")), _FakeConfig(name="f", poll_interval=1))
    sup.start()
    wd1 = sup._watchdog
    sup.start()  # second call must not spawn a new watchdog
    try:
        assert sup._watchdog is wd1
    finally:
        sup.stop()


def test_process_emits_alert_on_unhealthy_startup(monkeypatch):
    monkeypatch.setattr("taskpaw_v3.monitors.plugins.process._scan", lambda *a, **k: False)
    inst = ProcessPlugin().create("p", ProcessConfig(name="p", pattern="x"))
    events = []
    inst.check(lambda *a, **k: events.append(a))  # first check, already down → alert
    assert events and events[0][0] == "alert"


def test_tcp_emits_alert_on_unhealthy_startup():
    inst = TcpCheckPlugin().create("t", TcpCheckConfig(name="t", host="127.0.0.1", port=1, timeout=0.2))
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
    sup = Supervisor(sink=lambda *a: None)
    sup.register(_FakePlugin(behavior), _FakeConfig(name="f", poll_interval=1))
    sup.start()
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and len(starts) < 2:
            time.sleep(0.1)
        assert len(starts) >= 2  # watchdog restarted it
    finally:
        sup.stop()


def test_config_validators_grace_and_pattern():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        HeartbeatConfig(name="h", path="/x", grace_seconds=-1)
    with pytest.raises(ValidationError):
        HeartbeatConfig(name="h", path="")  # empty path
    with pytest.raises(ValidationError):
        ProcessConfig(name="p", pattern="")  # empty pattern


def test_reconfigure_abort_keeps_old_monitor_running():
    """If the old worker is stuck (long check), an aborted reconfigure must NOT
    kill it — stop is cleared so it keeps running on the old config."""
    release = threading.Event()
    entered = threading.Event()

    def blocking_check(emit):
        entered.set()
        release.wait(timeout=5)  # simulate a long check
        return MonitorStatus(state="ok")

    sup = Supervisor(sink=lambda *a: None)
    sup.register(_FakePlugin(blocking_check), _FakeConfig(name="f", poll_interval=1))
    sup.start()
    try:
        assert entered.wait(timeout=3)  # worker is inside the blocking check
        old = sup._monitors["f"]
        with pytest.raises(RuntimeError):
            sup.reconfigure("f", _FakeConfig(name="f", poll_interval=2), stop_timeout=0.3)
        # Old entry preserved, stop cleared, worker still alive.
        assert sup._monitors["f"] is old
        assert not old.stop.is_set()
        assert old.thread.is_alive()
    finally:
        release.set()
        sup.stop()


def test_folded_summary_flushed_when_quiet_after_burst():
    """Burst over cap then silence: the folded summary must still be delivered
    by the periodic flush, not only by a later _emit."""
    sink = []
    clock = [0.0]
    sup = Supervisor(sink=lambda *a: sink.append(a), clock=lambda: clock[0])
    sup.register(_FakePlugin(lambda e: MonitorStatus(state="ok")),
                 _FakeConfig(name="f", max_events_per_minute=1))
    sup._emit("f", "info", "t", "m")   # delivered
    sup._emit("f", "info", "t", "m")   # dropped (over cap)
    assert not any("suppressed" in s[2] for s in sink)  # not yet
    clock[0] = 120.0
    sup._flush_folded("f")             # periodic flush in a new, quiet window
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
            return _FakeInstance(instance_id, config, lambda e: MonitorStatus(state="ok"))

    sup = Supervisor(sink=lambda *a: None)
    sup.register(_PickyPlugin(), _FakeConfig(name="ok-cfg"), instance_id="f")
    sup.start()
    try:
        old = sup._monitors["f"]
        with pytest.raises(ValueError):
            sup.reconfigure("f", _FakeConfig(name="bad"))
        assert sup._monitors["f"] is old          # old entry preserved
        assert not old.stop.is_set()              # old worker untouched
        assert old.thread.is_alive()
    finally:
        sup.stop()


def test_heartbeat_expands_user_path(tmp_path, monkeypatch):
    """A ~/... heartbeat path must be expanded (Path() alone doesn't)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "hb.json").write_text(
        json.dumps({"status": "cycling",
                    "next_check_due_utc": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}),
        encoding="utf-8",
    )
    st = evaluate_heartbeat(HeartbeatConfig(name="hb", path="~/hb.json"))
    assert st.state == "ok"  # resolved under HOME, not reported missing
