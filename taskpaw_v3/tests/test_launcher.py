"""agent launcher build_queue (#115).

ensure_port_free / port_available / claim_port are covered in test_agent.py;
run_agent is integration (binds real sockets). This covers build_queue: the
EventQueue wiring + the persisted monotonic id contract (constitution §3)."""

from __future__ import annotations

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
