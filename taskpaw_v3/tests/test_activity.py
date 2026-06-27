"""state_file plugin + activity_writer (#22 dev-agent activity monitor)."""

from __future__ import annotations

import json
import time

import pytest

from taskpaw_v3.integrations import activity_writer as aw
from taskpaw_v3.monitors.plugins.state_file import (
    StateFileConfig,
    StateFileInstance,
    StateFilePlugin,
)
from taskpaw_v3.monitors.registry import default_registry


def _collector():
    events: list = []
    return events, (lambda *a, **k: events.append((a, k)))


def _write(path, state, ts=None, tool="claude"):
    obj = {"tool": tool, "state": state, "session": "s1"}
    if ts is not None:
        obj["ts"] = ts
    path.write_text(json.dumps(obj), encoding="utf-8")


# ── registry ─────────────────────────────────────────────────────────────--
def test_state_file_registered():
    reg = default_registry()
    assert "state_file" in set(reg.types())
    assert reg.get("state_file").type_id == "state_file"


# ── state mapping + transitions ──────────────────────────────────────────--
def test_busy_idle_waiting_states(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    events, emit = _collector()

    _write(f, "busy", ts=time.time())
    assert inst.check(emit).state == "running"   # busy → running (baseline, no emit)
    _write(f, "waiting", ts=time.time())
    st = inst.check(emit)
    assert st.state == "idle" and "waiting" in st.detail
    _write(f, "idle", ts=time.time())
    assert inst.check(emit).state == "idle"

    kinds = [a[0] for a, _ in events]
    # first check is baseline (no event); then waiting (info) + idle (done)
    assert "info" in kinds and "done" in kinds


def test_transition_busy_to_idle_emits_done(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    events, emit = _collector()
    _write(f, "busy", ts=time.time())
    inst.check(emit)            # baseline busy
    _write(f, "idle", ts=time.time())
    inst.check(emit)            # busy → idle
    done = [a for a, _ in events if a[0] == "done"]
    assert len(done) == 1


def test_first_observation_no_event(tmp_path):
    f = tmp_path / "act.json"
    _write(f, "busy", ts=time.time())
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    events, emit = _collector()
    inst.check(emit)
    assert not events  # baseline only


# ── watchdogs ────────────────────────────────────────────────────────────--
def test_busy_too_long_alerts_once(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(
        name="agent", path=str(f), busy_alert_seconds=60))
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 120)   # busy started 2 min ago
    inst.check(emit)
    inst.check(emit)
    alerts = [a for a, _ in events if a[0] == "alert"]
    assert len(alerts) == 1 and "busy too long" in alerts[0][1]


def test_stale_file_degrades(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(
        name="agent", path=str(f), stale_seconds=30))
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 300)   # not updated for 5 min
    st = inst.check(emit)
    assert st.state == "degraded"
    assert any(a[0] == "alert" for a, _ in events)


def test_busy_watchdog_resets_after_idle(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(
        name="agent", path=str(f), busy_alert_seconds=60))
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 120)
    inst.check(emit)                          # alert 1
    _write(f, "idle", ts=time.time())
    inst.check(emit)                          # reset
    _write(f, "busy", ts=time.time() - 120)
    inst.check(emit)                          # alert 2 (new busy episode)
    assert len([a for a, _ in events if a[0] == "alert"]) == 2


# ── missing / malformed ──────────────────────────────────────────────────--
def test_missing_file_is_idle_by_default(tmp_path):
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(tmp_path / "none.json")))
    _, emit = _collector()
    assert inst.check(emit).state == "idle"


def test_missing_file_unknown_when_configured(tmp_path):
    inst = StateFileInstance("a1", StateFileConfig(
        name="agent", path=str(tmp_path / "none.json"), missing_is_idle=False))
    _, emit = _collector()
    assert inst.check(emit).state == "unknown"


def test_malformed_file_is_error(tmp_path):
    f = tmp_path / "act.json"
    f.write_text("not json", encoding="utf-8")
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    _, emit = _collector()
    assert inst.check(emit).state == "error"


# ── activity_writer ──────────────────────────────────────────────────────--
def test_writer_explicit_state(tmp_path):
    out = tmp_path / "a.json"
    aw.write_activity(str(out), "codex", "idle", session="x")
    data = json.loads(out.read_text())
    assert data["tool"] == "codex" and data["state"] == "idle"
    assert data["session"] == "x" and isinstance(data["ts"], float)


def test_writer_atomic_replace(tmp_path):
    out = tmp_path / "a.json"
    aw.write_activity(str(out), "claude", "busy")
    aw.write_activity(str(out), "claude", "idle")   # overwrite
    assert json.loads(out.read_text())["state"] == "idle"
    # no leftover temp files
    assert list(tmp_path.glob(".*.tmp")) == []


def test_writer_creates_parent_dir(tmp_path):
    out = tmp_path / "nested" / "deep" / "a.json"
    aw.write_activity(str(out), "claude", "busy")
    assert out.exists()


@pytest.mark.parametrize("event,expected", [
    ("UserPromptSubmit", "busy"),
    ("SessionStart", "busy"),
    ("Notification", "waiting"),
    ("Stop", "idle"),
    ("SubagentStop", "idle"),
    ("UnknownEvent", None),
])
def test_writer_maps_claude_hook_events(event, expected):
    state, session = aw.state_from_stdin(json.dumps(
        {"hook_event_name": event, "session_id": "sess1"}))
    assert state == expected
    if expected:
        assert session == "sess1"


def test_writer_stdin_garbage_is_none():
    assert aw.state_from_stdin("not json") == (None, None)


# ── end-to-end: writer → plugin reads it ─────────────────────────────────--
def test_writer_then_plugin_reads_state(tmp_path):
    out = tmp_path / "a.json"
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(out)))
    events, emit = _collector()
    aw.write_activity(str(out), "claude", "busy")
    assert inst.check(emit).state == "running"
    aw.write_activity(str(out), "claude", "idle")
    assert inst.check(emit).state == "idle"
    assert any(a[0] == "done" for a, _ in events)
