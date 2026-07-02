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


# ── auth-disabled visibility (#145) ─────────────────────────────────────────
def test_auth_disabled_helper_is_exact_negation_of_token_ok_shortcircuit():
    from taskpaw_v3.core.auth import auth_disabled, token_ok

    assert auth_disabled("") is True
    assert auth_disabled("   ") is True
    assert auth_disabled("tok") is False
    # Whenever auth_disabled is True, token_ok accepts any request; and vice versa.
    assert token_ok("", None) is True and auth_disabled("") is True
    assert token_ok("tok", None) is False and auth_disabled("tok") is False


def test_control_config_reports_auth_disabled_and_still_masks_token():
    # #145: the console reads the auth posture from /control/config to show a banner.
    open_cr = TestClient(
        create_control_app(AgentConfig(server_id="s", machine="m", api_token=""))
    ).get("/control/config")
    assert open_cr.status_code == 200 and open_cr.json()["auth_disabled"] is True

    tok_cr = TestClient(
        create_control_app(AgentConfig(server_id="s", machine="m", api_token=SECRET))
    ).get("/control/config")
    body = tok_cr.json()
    assert body["auth_disabled"] is False
    assert body["api_token"] == "***" and SECRET not in tok_cr.text  # never leaked


def test_run_agent_warns_loudly_when_auth_disabled(caplog):
    # #145: starting with no token must emit a loud warning (the bind guard keeps
    # this loopback-only, but the operator should know auth is off).
    from taskpaw_v3.agent.server.launcher import run_agent
    from taskpaw_v3.core.net import claim_port

    a = claim_port("127.0.0.1", 0, "p-net")
    b = claim_port("127.0.0.1", 0, "p-ctl")
    net_port, ctl_port = a.getsockname()[1], b.getsockname()[1]
    a.close()
    b.close()
    cfg = AgentConfig(server_id="s", machine="m", host_metrics=False, api_token="",
                      bind_port=net_port, control_port=ctl_port)
    with caplog.at_level(logging.WARNING):
        sd = run_agent(cfg, block=False)
        try:
            assert any("auth is DISABLED" in r.message for r in caplog.records)
        finally:
            sd.shutdown()
            sd.stopped.wait(timeout=10)


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


def test_announce_ready_emits_one_json_line(capsys):
    # The §3.1 readiness handshake the Tauri shell parses (#48).
    import json as _json
    from taskpaw_v3.core.net import announce_ready
    announce_ready("agent", "http://127.0.0.1:5681")
    out = capsys.readouterr().out.strip()
    assert _json.loads(out) == {"taskpaw_ready": True, "role": "agent",
                                "base_url": "http://127.0.0.1:5681"}


def test_loopback_url_maps_wildcard_and_brackets_ipv6():
    # The readiness base_url must reach the socket that's actually bound (#48):
    # wildcard → loopback, IPv6 bracketed (Codex).
    from taskpaw_v3.core.net import loopback_url
    assert loopback_url("127.0.0.1", 5681) == "http://127.0.0.1:5681"
    assert loopback_url("0.0.0.0", 5690) == "http://127.0.0.1:5690"   # UI is local
    assert loopback_url("::1", 5681) == "http://[::1]:5681"           # IPv6 bracketed
    assert loopback_url("::", 5690) == "http://[::1]:5690"            # IPv6 wildcard


def test_run_agent_announces_readiness_with_real_control_port(capsys):
    # run_agent must print the readiness line (role + the ACTUAL loopback control
    # base_url) once the servers are up, so the shell can inject the real port (#48).
    import json as _json
    from taskpaw_v3.core.config import AgentConfig
    from taskpaw_v3.core.net import claim_port
    from taskpaw_v3.agent.server.launcher import run_agent

    # Reserve two distinct free ports, then release them for run_agent to claim.
    a = claim_port("127.0.0.1", 0, "p-net")
    b = claim_port("127.0.0.1", 0, "p-ctl")
    net_port, ctl_port = a.getsockname()[1], b.getsockname()[1]
    a.close(); b.close()

    cfg = AgentConfig(server_id="s", machine="m", host_metrics=False,
                      bind_port=net_port, control_port=ctl_port)
    sd = run_agent(cfg, block=False)
    try:
        line = next(l for l in capsys.readouterr().out.splitlines() if "taskpaw_ready" in l)
        assert _json.loads(line) == {"taskpaw_ready": True, "role": "agent",
                                     "base_url": f"http://127.0.0.1:{ctl_port}"}
    finally:
        sd.shutdown()
        sd.stopped.wait(timeout=10)


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


# ── UI CORS (design §3.2): control + hub APIs allow the UI origins ──────────
def test_control_api_has_cors_for_ui_origin():
    cfg = AgentConfig(server_id="s", machine="dev")
    client = TestClient(create_control_app(cfg))
    r = client.get("/control/ping", headers={"Origin": "tauri://localhost"})
    assert r.headers.get("access-control-allow-origin") == "tauri://localhost"


def test_control_status_served_from_provider():
    cfg = AgentConfig(server_id="s", machine="dev")
    client = TestClient(create_control_app(cfg, status_provider=lambda: {"machine": "dev", "monitors": {"x": {"state": "ok"}}}))
    r = client.get("/control/status")
    assert r.status_code == 200 and r.json()["monitors"]["x"]["state"] == "ok"


def test_network_api_has_NO_cors(  ):
    # The agent network API must stay CORS-free (design §3.2: agent doesn't open CORS).
    cfg = AgentConfig(server_id="s", machine="dev")
    client = TestClient(create_network_app(cfg, EventQueue("dev")))
    r = client.get("/ping", headers={"Origin": "tauri://localhost"})
    assert "access-control-allow-origin" not in r.headers


def test_hub_api_has_cors_for_ui_origin(tmp_path):
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.hub.server.store import HubStore
    store = HubStore(tmp_path / "hub.db")
    try:
        app, _svc = create_hub_app(HubConfig(machine="hub"), store)
        r = TestClient(app).get("/ping", headers={"Origin": "http://localhost:5173"})
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    finally:
        store.close()


def test_cors_allows_windows_tauri_origin():
    cfg = AgentConfig(server_id="s", machine="dev")
    client = TestClient(create_control_app(cfg))
    r = client.get("/control/ping", headers={"Origin": "http://tauri.localhost"})
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"
