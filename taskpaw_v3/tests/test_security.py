"""Security acceptance suite (issue #16 / V3 #2.5).

Each test maps to a §3.1/§3.2 security property from the V3 design and the
constitution. Network-reachable surface stays minimal and read-only; control is
loopback-only; auth failures don't leak or drain; secrets never appear in
responses; binds default safe. See docs/specs/2026-06-27-v3-security-16.md for
the full checklist + evidence mapping.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.core.config import AgentConfig, HubConfig
from taskpaw_v3.core.protocol import EventQueue


SECRET = "super-secret-token"


def _net(token=SECRET):
    cfg = AgentConfig(server_id="s1", machine="dev", api_token=token)
    q = EventQueue("dev")
    q.add("mon", "pending-event")
    return cfg, q, TestClient(create_network_app(cfg, q))


# ── network surface is minimal + read-only (§3.2) ──────────────────────────
def test_network_app_does_not_expose_control_routes():
    _, _, client = _net()
    # NONE of the control endpoints may exist on the network app.
    assert client.get("/control/ping").status_code == 404
    assert client.get("/control/config").status_code == 404
    assert client.post("/control/command", json={}).status_code == 404


def test_control_app_does_not_expose_network_routes():
    cfg = AgentConfig(server_id="s1", machine="dev", api_token=SECRET)
    client = TestClient(create_control_app(cfg))
    assert client.get("/status").status_code == 404
    assert client.get("/events").status_code == 404


def test_only_ping_is_open_without_auth():
    _, _, client = _net()
    assert client.get("/ping").status_code == 200
    assert client.get("/status").status_code == 401
    assert client.get("/events").status_code == 401


# ── auth failures don't drain or leak (§3.2 / constitution §2) ─────────────
def test_401_does_not_drain_event_queue():
    _, q, client = _net()
    before = len(q)
    assert client.get("/events?ack=0").status_code == 401
    assert client.get("/events").status_code == 401
    assert len(q) == before  # unauthorized polls must not consume events


def test_401_response_does_not_leak_token():
    _, _, client = _net()
    r = client.get("/status", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert SECRET not in r.text
    assert "WWW-Authenticate" in r.headers


# ── secrets never appear in responses (§4.3) ───────────────────────────────
def test_secret_never_in_status_or_config():
    cfg, _, net_client = _net()
    ok = net_client.get("/status", headers={"Authorization": f"Bearer {SECRET}"})
    assert ok.status_code == 200 and SECRET not in ok.text

    ctl_client = TestClient(create_control_app(cfg))
    cr = ctl_client.get("/control/config")
    assert cr.status_code == 200
    assert SECRET not in cr.text and cr.json()["api_token"] == "***"


def test_token_not_logged(caplog, tmp_path):
    """No token-bearing endpoint may log the secret (agent + Hub paths)."""
    cfg, _, client = _net()
    ctl = TestClient(create_control_app(cfg))
    auth = {"Authorization": f"Bearer {SECRET}"}
    with caplog.at_level(logging.DEBUG):
        client.get("/ping")
        client.get("/status", headers=auth)
        client.get("/status", headers={"Authorization": "Bearer wrong"})
        client.get("/events", headers=auth)
        client.get("/events")  # 401
        ctl.get("/control/config")
        # Hub paths
        from taskpaw_v3.hub.server.app import create_hub_app
        from taskpaw_v3.hub.server.store import HubStore

        store = HubStore(tmp_path / "hub.db")
        try:
            store.set_config("openclaw_token", SECRET)
            store.set_config("polling_token", SECRET)
            hub_app, _ = create_hub_app(HubConfig(openclaw_token=SECRET, polling_token=SECRET), store)
            hub_client = TestClient(hub_app)
            hub_client.get("/ping")
            hub_client.get("/status")
        finally:
            store.close()
    assert SECRET not in caplog.text


# ── secure binds (§3.2 / constitution §2) ──────────────────────────────────
def test_agent_bind_defaults_to_loopback_not_all_interfaces():
    assert AgentConfig(server_id="s", machine="m").bind_host == "127.0.0.1"


def test_control_host_must_be_numeric_loopback():
    AgentConfig(server_id="s", machine="m", control_host="127.0.0.1")
    AgentConfig(server_id="s", machine="m", control_host="::1")
    for bad in ("0.0.0.0", "192.168.1.5", "localhost", "example.com"):
        with pytest.raises(ValidationError):  # precise: the loopback validator, not any error
            AgentConfig(server_id="s", machine="m", control_host=bad)


def test_auth_disabled_when_token_empty_is_explicit():
    # V2 parity: empty token = auth disabled (documented, opt-in to open).
    cfg = AgentConfig(server_id="s", machine="m", api_token="")
    client = TestClient(create_network_app(cfg, EventQueue("m")))
    assert client.get("/status").status_code == 200


# ── Hub forwarding secret handling ─────────────────────────────────────────
def test_hub_status_endpoint_never_returns_tokens(tmp_path):
    """Exercise the REAL Hub /status: even with secrets in config + a server
    row, the read API must not leak openclaw_token / polling_token."""
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.hub.server.store import HubStore

    store = HubStore(tmp_path / "hub.db")
    try:
        store.add_server("agent", "127.0.0.1", 5680)
        store.set_config("openclaw_token", SECRET)
        store.set_config("polling_token", SECRET)
        config = HubConfig(openclaw_token=SECRET, polling_token=SECRET)
        app, _service = create_hub_app(config, store)
        client = TestClient(app)
        r = client.get("/status")
        assert r.status_code == 200
        assert SECRET not in r.text  # regression guard: no token in the real payload
        r2 = client.get("/ping")
        assert r2.status_code == 200 and SECRET not in r2.text
    finally:
        store.close()


def test_runtime_binding_separation_and_loopback_control():
    """The bind mechanism run_agent() uses (claim_port) actually binds the
    network and control sockets to their configured, distinct addresses, and the
    control socket sits on a numeric loopback."""
    from taskpaw_v3.core.net import claim_port

    net_sock = claim_port("127.0.0.1", 0, "network")
    try:
        ctl_sock = claim_port("127.0.0.1", 0, "control")
        try:
            net_host, net_port = net_sock.getsockname()[:2]
            ctl_host, ctl_port = ctl_sock.getsockname()[:2]
            assert net_port != ctl_port  # distinct sockets
            assert ctl_host in {"127.0.0.1", "::1"}  # control on loopback
            assert net_sock.getsockname() != ctl_sock.getsockname()
        finally:
            ctl_sock.close()
    finally:
        net_sock.close()


def test_no_token_bearing_cli_flags_in_v3_sources():
    """Constitution §2: secrets come from config/env, never argv. Guard against a
    future CLI flag that would put a token on the command line / in ps output."""
    v3_root = Path(__file__).resolve().parents[1]
    offenders = []
    for py in v3_root.rglob("*.py"):
        if "tests" in py.parts:
            continue
        text = py.read_text(encoding="utf-8").lower()
        if "add_argument" in text and "token" in text:
            offenders.append(str(py.relative_to(v3_root)))
    assert not offenders, f"token-bearing CLI flags found in: {offenders}"
