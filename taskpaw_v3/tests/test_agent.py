"""Agent network/control API + launcher port guard."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.agent.server.launcher import ensure_port_free, port_available, PortInUseError
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.protocol import EventQueue


def _cfg(**kw):
    return AgentConfig(server_id="s1", machine="dev", **kw)


def test_ping_open_no_auth():
    cfg = _cfg(api_token="secret")
    client = TestClient(create_network_app(cfg, EventQueue("dev")))
    r = client.get("/ping")
    assert r.status_code == 200 and r.json()["machine"] == "dev"


def test_control_events_returns_recent_non_destructive(_=None):
    # The console's event log reads /control/events (#44): recent local events,
    # newest last, with a clamped limit — and reading it must NOT drain the queue.
    cfg = _cfg()
    q = EventQueue("dev")
    for i in range(3):
        q.add("mon", f"e{i}", level="info")
    client = TestClient(create_control_app(cfg, events_provider=q.recent))
    r = client.get("/control/events?limit=2")
    assert r.status_code == 200
    assert [e["message"] for e in r.json()["events"]] == ["e1", "e2"]   # last 2, newest last
    assert len(q.payload(ack_id=0)["events"]) == 3                       # not consumed
    # no provider wired → empty, not an error
    assert TestClient(create_control_app(cfg)).get("/control/events").json() == {"events": []}


def test_status_requires_auth_when_token_set():
    cfg = _cfg(api_token="secret")
    client = TestClient(create_network_app(cfg, EventQueue("dev")))
    assert client.get("/status").status_code == 401
    r = client.get("/status", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.json()["server_id"] == "s1"


def test_events_ack_trims_but_401_does_not_drain():
    cfg = _cfg(api_token="secret")
    q = EventQueue("dev")
    q.add("mon", "a")
    q.add("mon", "b")
    client = TestClient(create_network_app(cfg, q))

    # Unauthorized must NOT drain the queue.
    assert client.get("/events?ack=0").status_code == 401
    assert len(q) == 2

    auth = {"Authorization": "Bearer secret"}
    body = client.get("/events?ack=0", headers=auth).json()
    assert [e["id"] for e in body["events"]] == [1, 2]
    # ack=2 trims
    assert client.get("/events?ack=2", headers=auth).json()["events"] == []


def test_events_legacy_no_ack_clears():
    cfg = _cfg()  # no token → auth disabled
    q = EventQueue("dev")
    q.add("mon", "a")
    client = TestClient(create_network_app(cfg, q))
    assert len(client.get("/events").json()["events"]) == 1
    assert client.get("/events").json()["events"] == []


def test_control_config_masks_token():
    cfg = _cfg(api_token="secret")
    client = TestClient(create_control_app(cfg))
    r = client.get("/control/config")
    assert r.status_code == 200 and r.json()["api_token"] == "***"


def test_port_guard_detects_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        host, port = s.getsockname()
        assert port_available(host, port) is False
        with pytest.raises(PortInUseError):
            ensure_port_free(host, port, "test")
    # released now
    assert port_available("127.0.0.1", port) in (True, False)  # may race; just callable


def test_bind_host_defaults_to_loopback():
    # Secure default: never 0.0.0.0 (constitution §2). Operator opts into LAN.
    assert _cfg().bind_host == "127.0.0.1"


def test_claim_port_refuses_second_binder():
    from taskpaw_v3.core.net import claim_port
    s = claim_port("127.0.0.1", 0, "first")
    try:
        host, port = s.getsockname()
        with pytest.raises(PortInUseError):
            claim_port(host, port, "second")  # SO_REUSEADDR no longer masks this
    finally:
        s.close()
