"""Core: protocol (clear-on-ack), auth, lifecycle, config."""

from __future__ import annotations

import subprocess
import sys

import pytest

from core.auth import token_ok
from core.config import AgentConfig, HubConfig, load_yaml, save_yaml
from core.lifecycle import GracefulShutdown
from core.protocol import EventQueue


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
