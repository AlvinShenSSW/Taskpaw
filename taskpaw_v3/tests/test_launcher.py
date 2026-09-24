"""agent launcher build_queue (#115).

ensure_port_free / port_available / claim_port are covered in test_agent.py;
run_agent is integration (binds real sockets). This covers build_queue: the
EventQueue wiring + the persisted monotonic id contract (constitution §3)."""

from __future__ import annotations

import pytest

from taskpaw_v3.agent.server.launcher import build_queue
from taskpaw_v3.core.config import AgentConfig


def _cfg() -> AgentConfig:
    return AgentConfig(server_id="s1", machine="box1")


def test_build_queue_without_state_is_in_memory():
    q = build_queue(_cfg(), None)
    assert q.machine == "box1"
    # An in-memory queue still issues monotonically increasing ids.
    e1 = q.add("mon", "m", level="info")
    e2 = q.add("mon", "m", level="info")
    assert e2["id"] > e1["id"]


def test_build_queue_persists_and_resumes_id_across_restart(tmp_path):
    state = tmp_path / "next_id.json"
    q1 = build_queue(_cfg(), state)
    first = q1.add("mon", "m", level="info")["id"]

    # A fresh queue from the same state file must NOT reissue an already-used id —
    # the persisted counter resumes past it (no duplicate ids after a restart).
    q2 = build_queue(_cfg(), state)
    nxt = q2.add("mon", "m", level="info")["id"]
    assert nxt > first


# ── LLM settings holder initialised at boot (#178 AC5) ─────────────────────
# Hermetic (D7): non-default ports, and run_agent never reaches the real stale-
# port reclaim or a real socket bind.
def _llm_cfg() -> AgentConfig:
    return AgentConfig(
        server_id="s1",
        machine="box1",
        bind_port=15680,
        control_port=15681,
        host_metrics=False,
        llm_model="cfg/model",
        llm_api_key="sk-BOOTKEY-2e2e",
    )


class _Sentinel(Exception):
    pass


def test_run_agent_sets_llm_holder_before_port_reclaim(monkeypatch):
    # T-L1: the holder is initialised BEFORE the stale-port reclaim — hence before
    # every socket claim and the supervisor.
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.llm import get_llm_settings

    seen = {}

    def reclaim(*a, **k):
        seen["settings"] = get_llm_settings()
        raise _Sentinel

    def no_claim(*a, **k):
        raise AssertionError("must not claim a port")

    monkeypatch.setattr(launcher, "reclaim_ports_from_stale_instance", reclaim)
    monkeypatch.setattr(launcher, "claim_port", no_claim)
    with pytest.raises(_Sentinel):
        launcher.run_agent(_llm_cfg(), block=False)
    s = seen["settings"]
    assert (s.model, s.api_key, s.key_source) == (
        "cfg/model",
        "sk-BOOTKEY-2e2e",
        "config",
    )
    assert get_llm_settings() == s


def test_run_agent_supervisor_starts_with_llm_settings(monkeypatch):
    # T-L2 (D13): ordering by observation — the supervisor's start() (the first
    # point any monitor can check()) already sees the configured settings.
    import socket

    import uvicorn

    import taskpaw_v3.monitors.runtime as runtime
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.core.llm import get_llm_settings

    started = {}

    class _FakeSupervisor:
        def start(self):
            started["settings"] = get_llm_settings()

        def stop(self):
            started["stopped"] = True

        def snapshot(self):
            return {}

    class _FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self, sockets=None):
            return None

    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(launcher, "claim_port", lambda *a, **k: socket.socket())
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **k: _FakeSupervisor())
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    shutdown = GracefulShutdown()
    # Keep pytest's own SIGINT/SIGTERM handlers.
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    try:
        launcher.run_agent(_llm_cfg(), shutdown=shutdown, block=False)
        s = started["settings"]
        assert (s.model, s.api_key, s.key_source) == (
            "cfg/model",
            "sk-BOOTKEY-2e2e",
            "config",
        )
    finally:
        shutdown.shutdown()
    assert started.get("stopped") is True
