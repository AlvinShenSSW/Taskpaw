"""Bundled-backend entry dispatch (#40/#41)."""

from __future__ import annotations

from taskpaw_v3.packaging import backend_main


def test_dispatch_agent(monkeypatch):
    called = {}
    import taskpaw_v3.agent.server.service as agent_service

    def fake():
        called["role"] = "agent"
        return 0
    monkeypatch.setattr(agent_service, "main", fake)
    assert backend_main.main(["agent"]) == 0
    assert called["role"] == "agent"


def test_dispatch_hub(monkeypatch):
    called = {}
    import taskpaw_v3.hub.server.service as hub_service

    def fake():
        called["role"] = "hub"
        return 0
    monkeypatch.setattr(hub_service, "main", fake)
    assert backend_main.main(["hub"]) == 0
    assert called["role"] == "hub"


def test_dispatch_defaults_to_agent(monkeypatch):
    called = {}
    import taskpaw_v3.agent.server.service as agent_service

    def fake():
        called["role"] = "agent"
        return 0
    monkeypatch.setattr(agent_service, "main", fake)
    assert backend_main.main([]) == 0          # no arg → agent
    assert called["role"] == "agent"


def test_dispatch_unknown_role():
    assert backend_main.main(["bogus"]) == 2   # clean exit code, no crash
