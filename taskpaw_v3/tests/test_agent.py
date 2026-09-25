"""Agent network/control API + launcher port guard."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.agent.server.launcher import (
    PortInUseError,
    ensure_port_free,
    port_available,
)
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.protocol import EventQueue


def _cfg(**kw):
    return AgentConfig(server_id="s1", machine="dev", **kw)


def test_ping_open_no_auth():
    from taskpaw_v3 import __version__

    cfg = _cfg(api_token="secret")
    client = TestClient(create_network_app(cfg, EventQueue("dev")))
    r = client.get("/ping")
    assert r.status_code == 200 and r.json()["machine"] == "dev"
    # /ping reports the real app version (single source), not a stale hardcoded string.
    assert r.json()["version"] == __version__


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
    assert [e["message"] for e in r.json()["events"]] == [
        "e1",
        "e2",
    ]  # last 2, newest last
    assert len(q.payload(ack_id=0)["events"]) == 3  # not consumed
    # no provider wired → empty, not an error
    assert TestClient(create_control_app(cfg)).get("/control/events").json() == {
        "events": []
    }


def test_control_events_filters_by_monitor():
    # #130: an optional `monitor` query param scopes the console's inline panel to
    # one monitor's events; without it the whole agent's stream is returned.
    cfg = _cfg()
    q = EventQueue("dev")
    q.add("lada", "l0")
    q.add("folder", "f0")
    q.add("lada", "l1")
    client = TestClient(create_control_app(cfg, events_provider=q.recent))
    both = client.get("/control/events").json()["events"]
    assert [e["message"] for e in both] == ["l0", "f0", "l1"]
    only = client.get("/control/events?monitor=lada").json()["events"]
    assert [e["message"] for e in only] == ["l0", "l1"]  # folder filtered out
    # an unknown monitor is simply empty, not an error
    assert client.get("/control/events?monitor=nope").json() == {"events": []}


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
    # #178: the LLM key fields are always present (masked; none configured here).
    assert r.json()["llm_api_key"] == "" and r.json()["llm_api_key_source"] == "none"
    assert "secret" not in r.text


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


def test_run_agent_guards_bind_exposure_before_binding():
    """run_agent runs the shared exposure guard BEFORE claiming any socket, so a
    hand-edited agent.yaml binding wildcard/public/non-loopback-without-token is
    refused at startup — not just from the UI (#114)."""
    from taskpaw_v3.agent.server.launcher import run_agent

    with pytest.raises(ValueError, match="all interfaces"):
        run_agent(_cfg(bind_host="0.0.0.0"), block=False)
    with pytest.raises(ValueError, match="public/WAN"):
        run_agent(_cfg(bind_host="8.8.8.8", api_token="tok"), block=False)
    with pytest.raises(ValueError, match="requires an api_token"):
        run_agent(_cfg(bind_host="192.168.1.9"), block=False)


# ── global LLM API settings (#178) ─────────────────────────────────────────
_LLM_KEY = "sk-ROUTEKEY-7b21"


def _control_clients(cfg):
    """Both construction paths: plain running config, and with a MonitorAdmin
    (config_view = desired scalars)."""
    from taskpaw_v3.agent.server.admin import MonitorAdmin
    from taskpaw_v3.monitors.registry import PluginRegistry

    admin = MonitorAdmin(cfg, None, PluginRegistry(), None)
    return [
        TestClient(create_control_app(cfg)),
        TestClient(create_control_app(cfg, admin=admin)),
    ]


@pytest.mark.parametrize(
    "stored,env,shown,source",
    [
        (_LLM_KEY, None, "***", "config"),
        ("", "sk-ENV-3c3c", "***", "env"),
        (_LLM_KEY, "sk-ENV-3c3c", "***", "env"),
        ("", "   ", "", "none"),
        ("", None, "", "none"),
    ],
)
def test_control_config_masks_llm_key_and_reports_source(
    monkeypatch, stored, env, shown, source
):
    # T-G1: GET masks the LLM key to *** whenever one is in effect and reports
    # where it comes from, via the same resolver chat() uses (D1).
    from taskpaw_v3.core.llm import LLM_KEY_ENV

    if env is not None:
        monkeypatch.setenv(LLM_KEY_ENV, env)
    for client in _control_clients(_cfg(llm_api_key=stored)):
        r = client.get("/control/config")
        assert r.status_code == 200
        data = r.json()
        assert data["llm_api_key"] == shown
        assert data["llm_api_key_source"] == source
        assert data["llm_api_base"] == "https://api.x.ai/v1"
        assert data["llm_model"] == "grok-4.3"
        assert _LLM_KEY not in r.text and "sk-ENV-3c3c" not in r.text


def test_control_config_whitespace_stored_key_reports_none():
    # A whitespace-only stored key (only reachable by bypassing the validator)
    # resolves to no key at all → "" + none, never "***".
    cfg = _cfg()
    cfg.llm_api_key = "   "  # no validate_assignment → stays unstripped
    for client in _control_clients(cfg):
        data = client.get("/control/config").json()
        assert (data["llm_api_key"], data["llm_api_key_source"]) == ("", "none")


def test_control_llm_test_route(monkeypatch):
    # T-G2: POST /control/llm-test → the admin's dict; bad base → 400 whose
    # detail carries no key.
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.agent.server.admin import MonitorAdmin
    from taskpaw_v3.core.llm import ChatResult
    from taskpaw_v3.monitors.registry import PluginRegistry

    calls = []

    def fake_chat(settings, messages, **kw):
        calls.append(settings)
        return ChatResult('{"1": "你好"}', "stop", "served/m", 5)

    monkeypatch.setattr(adminmod, "chat", fake_chat)
    cfg = _cfg(llm_api_key=_LLM_KEY)
    admin = MonitorAdmin(cfg, None, PluginRegistry(), None)
    client = TestClient(create_control_app(cfg, admin=admin))
    r = client.post("/control/llm-test", json={"llm_model": "cand/m"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "served/m", "latency_ms": 5}
    assert calls[0].model == "cand/m"
    r = client.post(
        "/control/llm-test", json={"llm_api_base": "ftp://x", "llm_api_key": _LLM_KEY}
    )
    assert r.status_code == 400
    assert "llm_api_base" in r.json()["detail"] and _LLM_KEY not in r.text
    assert len(calls) == 1
    # Not mounted without an admin (like the other mutation routes).
    plain = TestClient(create_control_app(cfg))
    assert plain.post("/control/llm-test", json={}).status_code in (404, 405)


# ── #190/#192 AC11: fallback providers over the control API ────────────────
_FB_KEYS = {
    "primary": "sk-STORED-P-1a1a",
    "fallback1": "sk-STORED-F1-2b2b",
    "fallback2": "sk-STORED-F2-3c3c",
}
_FB_ENV_KEYS = {
    "primary": "sk-ENV-P-4d4d",
    "fallback1": "sk-ENV-F1-5e5e",
    "fallback2": "sk-ENV-F2-6f6f",
}


@pytest.mark.parametrize("slot", ["fallback1", "fallback2"])
@pytest.mark.parametrize(
    "stored,env,shown,source",
    [
        (True, False, "***", "config"),
        (False, True, "***", "env"),
        (True, True, "***", "env"),
        (False, False, "", "none"),
    ],
)
def test_control_config_masks_fallback_keys_and_reports_source(
    monkeypatch, slot, stored, env, shown, source
):
    from taskpaw_v3.core.llm import LLM_SLOT_KEY_ENV

    key_field = f"llm_{slot}_api_key"
    if env:
        monkeypatch.setenv(LLM_SLOT_KEY_ENV[slot], _FB_ENV_KEYS[slot])
    cfg = _cfg(**({key_field: _FB_KEYS[slot]} if stored else {}))
    for client in _control_clients(cfg):
        r = client.get("/control/config")
        data = r.json()
        assert data[key_field] == shown
        assert data[f"{key_field}_source"] == source
        # The other slots are unaffected (by name, never positional).
        for other in {"primary", "fallback1", "fallback2"} - {slot}:
            prefix = "llm_" if other == "primary" else f"llm_{other}_"
            assert data[f"{prefix}api_key"] == ""
            assert data[f"{prefix}api_key_source"] == "none"
        assert _FB_KEYS[slot] not in r.text and _FB_ENV_KEYS[slot] not in r.text


def test_control_config_never_returns_a_stored_or_env_key_value(monkeypatch):
    # AC11: no stored key VALUE — primary or fallback, stored or env — nor the
    # token appears anywhere in GET /control/config.
    from taskpaw_v3.core.llm import LLM_SLOT_KEY_ENV

    cfg = _cfg(
        api_token="tok-SECRET-7a7a",
        llm_api_key=_FB_KEYS["primary"],
        llm_fallback1_api_base="https://api.deepseek.com/v1",
        llm_fallback1_model="deepseek-chat",
        llm_fallback1_api_key=_FB_KEYS["fallback1"],
        llm_fallback2_api_base="https://mimo.example/v1",
        llm_fallback2_model="mimo",
        llm_fallback2_api_key=_FB_KEYS["fallback2"],
    )
    for with_env in (False, True):
        if with_env:
            for slot, name in LLM_SLOT_KEY_ENV.items():
                monkeypatch.setenv(name, _FB_ENV_KEYS[slot])
        for client in _control_clients(cfg):
            r = client.get("/control/config")
            assert r.status_code == 200
            for secret in (*_FB_KEYS.values(), *_FB_ENV_KEYS.values(), "tok-SECRET"):
                assert secret not in r.text
            data = r.json()
            for prefix in ("llm_", "llm_fallback1_", "llm_fallback2_"):
                assert data[f"{prefix}api_key"] == "***"
                assert data[f"{prefix}api_key_source"] == (
                    "env" if with_env else "config"
                )
            assert data["llm_fallback1_api_base"] == "https://api.deepseek.com/v1"
            assert data["llm_fallback2_model"] == "mimo"
            assert data["llm_failover"] is True


def test_control_config_emits_every_fallback_field_and_failover():
    for client in _control_clients(_cfg()):
        data = client.get("/control/config").json()
        for n in (1, 2):
            for f in ("api_base", "model", "api_key", "api_key_source"):
                assert f"llm_fallback{n}_{f}" in data
            assert data[f"llm_fallback{n}_api_key_source"] == "none"
        assert data["llm_failover"] is True


def _admin_client(cfg, tmp_path=None):
    from taskpaw_v3.agent.server.admin import MonitorAdmin
    from taskpaw_v3.monitors.registry import PluginRegistry

    path = None if tmp_path is None else tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, PluginRegistry(), path)
    return TestClient(create_control_app(cfg, admin=admin))


def test_patch_config_fallback_shapes_from_the_settings_ui(tmp_path):
    # The UI's exact PATCH shapes: Save = base + model (+ the key only when
    # typed); Clear = key null; turning a fallback off = blank base + model;
    # the failover switch alone on every toggle.
    from taskpaw_v3.core.llm import get_llm_chain, get_llm_failover

    cfg = _cfg(llm_api_key=_FB_KEYS["primary"])
    client = _admin_client(cfg, tmp_path)

    def patch(body):
        r = client.patch("/control/config", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    patch(
        {
            "llm_fallback1_api_base": "https://api.deepseek.com/v1",
            "llm_fallback1_model": "deepseek-chat",
            "llm_fallback1_api_key": _FB_KEYS["fallback1"],
        }
    )
    assert [s.model for s in get_llm_chain()] == ["grok-4.3", "deepseek-chat"]
    # Save without a typed key keeps the stored one (absent, blank or ***).
    for body_key in (
        {},
        {"llm_fallback1_api_key": ""},
        {"llm_fallback1_api_key": "***"},
    ):
        patch(
            {
                "llm_fallback1_api_base": "https://api.deepseek.com/v1",
                "llm_fallback1_model": "deepseek-reasoner",
                **body_key,
            }
        )
        assert cfg.llm_fallback1_api_key == _FB_KEYS["fallback1"]
    assert get_llm_chain()[1].model == "deepseek-reasoner"
    # The failover switch alone.
    assert patch({"llm_failover": False}) == {"ok": True, "restart_required": False}
    assert get_llm_failover() is False and cfg.llm_failover is False
    assert patch({"llm_failover": True})["ok"] is True
    assert get_llm_failover() is True
    # Clear.
    patch({"llm_fallback1_api_key": None})
    assert cfg.llm_fallback1_api_key == ""
    assert [s.model for s in get_llm_chain()] == ["grok-4.3"]
    # Off: blank base + model are accepted (the validators allow "").
    patch({"llm_fallback1_api_base": "", "llm_fallback1_model": ""})
    assert (cfg.llm_fallback1_api_base, cfg.llm_fallback1_model) == ("", "")
    data = client.get("/control/config").json()
    assert (data["llm_fallback1_api_base"], data["llm_fallback1_model"]) == ("", "")
    assert data["llm_fallback1_api_key_source"] == "none"
    text = (tmp_path / "agent.yaml").read_text(encoding="utf-8")
    assert _FB_KEYS["fallback1"] not in text


@pytest.mark.parametrize(
    "slot,body",
    [
        (
            "primary",
            {
                "llm_api_base": "https://cand.example/v1",
                "llm_model": "cand/p",
                "slot": "primary",
            },
        ),
        (
            "fallback1",
            {
                "llm_fallback1_api_base": "https://cand.example/v1",
                "llm_fallback1_model": "cand/f1",
                "llm_fallback1_api_key": "***",
                "slot": "fallback1",
            },
        ),
        (
            "fallback2",
            {
                "llm_fallback2_api_base": "https://cand.example/v1",
                "llm_fallback2_model": "cand/f2",
                "llm_fallback2_api_key": "",
                "slot": "fallback2",
            },
        ),
    ],
)
def test_control_llm_test_route_per_slot(monkeypatch, slot, body):
    # The UI's llm-test body per slot: that slot's OWN field names + `slot`; a
    # missing / blank / *** key uses the slot's stored key.
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import ChatResult

    calls = []

    def fake_chat(settings, messages, **kw):
        calls.append(settings)
        return ChatResult('{"1": "你好"}', "stop", "served/m", 5)

    monkeypatch.setattr(adminmod, "chat", fake_chat)
    cfg = _cfg(
        llm_api_key=_FB_KEYS["primary"],
        llm_fallback1_api_key=_FB_KEYS["fallback1"],
        llm_fallback2_api_key=_FB_KEYS["fallback2"],
    )
    client = _admin_client(cfg)
    prefix = "llm_" if slot == "primary" else f"llm_{slot}_"
    r = client.post("/control/llm-test", json=body)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "served/m", "latency_ms": 5}
    ((s,),) = [calls]
    assert (s.api_base, s.model) == ("https://cand.example/v1", body[f"{prefix}model"])
    assert (s.api_key, s.key_source) == (_FB_KEYS[slot], "config")


def test_control_llm_test_route_rejects_an_unknown_slot(monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod

    monkeypatch.setattr(
        adminmod, "chat", lambda *a, **k: pytest.fail("chat must not be called")
    )
    client = _admin_client(_cfg())
    r = client.post("/control/llm-test", json={"slot": "fallback9"})
    assert r.status_code == 400 and "slot" in r.json()["detail"]
