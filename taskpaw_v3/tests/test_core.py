"""Core: protocol (clear-on-ack), auth, lifecycle, config."""

from __future__ import annotations

import subprocess
import sys

import pytest

from taskpaw_v3.core.auth import token_ok
from taskpaw_v3.core.config import AgentConfig, HubConfig, load_yaml, save_yaml
from taskpaw_v3.core.lifecycle import GracefulShutdown
from taskpaw_v3.core.protocol import EventQueue


# ── protocol ──────────────────────────────────────────────────────────────
def test_eventqueue_monotonic_and_additive_fields():
    q = EventQueue(machine="m")
    e1 = q.add("mon", "first")
    assert e1["id"] == 1 and set(e1) == {"id", "time", "machine", "monitor", "message"}
    data = {"k": 1}
    e2 = q.add("mon", "rich", level="alert", title="T", data=data)
    data["k"] = 2  # caller mutation must not leak into the queued event
    assert e2["id"] == 2 and e2["level"] == "alert" and e2["title"] == "T"
    assert e2["data"] == {"k": 1}


def test_eventqueue_clear_on_ack_vs_legacy():
    q = EventQueue(machine="m")
    q.add("mon", "a")
    q.add("mon", "b")
    # ack=0 returns both without clearing (survives a Hub crash before persist)
    assert [e["id"] for e in q.payload(ack_id=0)["events"]] == [1, 2]
    assert [e["id"] for e in q.payload(ack_id=0)["events"]] == [1, 2]
    # ack=2 trims both
    assert q.payload(ack_id=2)["events"] == []
    # legacy (no ack) clears on read
    q.add("mon", "c")
    assert len(q.payload()["events"]) == 1
    assert q.payload()["events"] == []


def test_eventqueue_persists_counter_before_visible():
    saved = []
    q = EventQueue(machine="m", persist_counter=saved.append)
    q.add("mon", "x")
    assert saved == [2]  # next_id persisted during add()


def test_eventqueue_persist_failure_keeps_event_invisible():
    """If the counter persist raises, nothing is mutated: the event is not
    appended and the id is not advanced (durable-before-visible)."""
    def boom(_next_id):
        raise OSError("disk full")

    q = EventQueue(machine="m", persist_counter=boom)
    with pytest.raises(OSError):
        q.add("mon", "x")
    assert len(q) == 0
    assert q.next_id == 1  # not advanced
    # A working persist afterwards still starts at id 1 (no gap/reuse).
    saved = []
    q._persist_counter = saved.append
    assert q.add("mon", "y")["id"] == 1
    assert saved == [2]


def test_eventqueue_cap_drops_oldest():
    dropped = []
    q = EventQueue(machine="m", max_size=3, on_overflow=dropped.append)
    for i in range(5):
        q.add("mon", f"e{i}")
    ids = [e["id"] for e in q.payload(ack_id=0)["events"]]
    assert ids == [3, 4, 5] and dropped == [1, 1]


def test_eventqueue_rejects_bad_level_and_data():
    q = EventQueue(machine="m")
    with pytest.raises(ValueError):
        q.add("mon", "x", level="nope")
    with pytest.raises(ValueError):
        q.add("mon", "x", data=["not", "a", "dict"])


# ── auth ────────────────────────────────────────────────────────────────--
def test_auth_disabled_when_token_empty():
    assert token_ok("", None) is True
    assert token_ok("   ", "anything") is True


def test_auth_requires_exact_bearer():
    assert token_ok("secret", "Bearer secret") is True
    assert token_ok("secret", "Bearer wrong") is False
    assert token_ok("secret", None) is False
    assert token_ok("secret", "secret") is False


# ── lifecycle ───────────────────────────────────────────────────────────--
def test_graceful_shutdown_runs_lifo_and_idempotent():
    gs = GracefulShutdown()
    order = []
    gs.register("a", lambda: order.append("a"))
    gs.register("b", lambda: order.append("b"))
    gs.shutdown()
    gs.shutdown()  # idempotent
    assert order == ["b", "a"]  # LIFO
    assert gs.stopped.is_set()


def test_graceful_shutdown_terminates_child():
    gs = GracefulShutdown(child_timeout=5)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    gs.register_child("sleeper", proc)
    gs.shutdown()
    assert proc.poll() is not None  # terminated


def test_graceful_shutdown_continues_if_callback_raises():
    gs = GracefulShutdown()
    ran = []
    gs.register("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    gs.register("ok", lambda: ran.append(1))
    gs.shutdown()  # must not propagate
    assert ran == [1]  # the other callback still ran


# ── config ──────────────────────────────────────────────────────────────--
def test_agent_config_roundtrip_and_secret_masking(tmp_path):
    cfg = AgentConfig(server_id="s1", machine="dev", api_token="secret")
    path = tmp_path / "agent.yaml"
    save_yaml(cfg, path)
    loaded = load_yaml(AgentConfig, path)
    assert loaded.server_id == "s1" and loaded.bind_port == 5680


def test_agent_config_rejects_bad_port_and_nonloopback_control():
    with pytest.raises(Exception):
        AgentConfig(server_id="s", machine="m", bind_port=70000)
    with pytest.raises(Exception):
        AgentConfig(server_id="s", machine="m", control_host="0.0.0.0")


def test_hub_config_defaults():
    h = HubConfig()
    assert h.poll_interval == 60 and h.openclaw_enabled is False


def test_agent_state_persists_event_id(tmp_path):
    from taskpaw_v3.core.state import load_next_id, save_next_id

    p = tmp_path / "agent.state.json"
    assert load_next_id(p) == 1
    save_next_id(p, 42)
    assert load_next_id(p) == 42


def test_build_queue_persists_across_restart(tmp_path):
    from taskpaw_v3.agent.server.launcher import build_queue

    cfg = AgentConfig(server_id="s", machine="dev")
    state = tmp_path / "agent.state.json"
    q1 = build_queue(cfg, state)
    assert q1.add("mon", "a")["id"] == 1
    assert q1.add("mon", "b")["id"] == 2
    # New queue (simulated restart) resumes from the persisted counter.
    q2 = build_queue(cfg, state)
    assert q2.add("mon", "c")["id"] == 3
