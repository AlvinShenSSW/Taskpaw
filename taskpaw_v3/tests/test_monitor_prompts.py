from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.supervisor import Supervisor


def test_prompt_api_auth_typed_body_conflict_and_no_lan_route():
    calls = []
    cfg = AgentConfig(server_id="s", machine="m")

    def response(name, request, approved):
        calls.append((name, request, approved))
        return request == "current"

    app = create_control_app(
        cfg,
        control_token="test-control-token",
        control_active=lambda: True,
        prompt_responder=response,
    )
    client = TestClient(app)
    url = "/control/monitors/respond?name=jasna"
    body = dict(request_id="current", approved=True)
    assert client.post(url, json=body).status_code == 401
    assert calls == []
    client.headers["Authorization"] = "Bearer test-control-token"
    assert client.post(url, json=body).status_code == 200
    assert calls == [("jasna", "current", True)]
    for bad in (
        {**body, "url": "https://example.com"},
        {**body, "approved": "true"},
        {**body, "request_id": ""},
    ):
        assert client.post(url, json=bad).status_code == 422
    assert len(calls) == 1
    assert client.post(url, json={**body, "request_id": "stale"}).status_code == 409
    network = TestClient(create_network_app(cfg, EventQueue("m")))
    assert network.post(url, json=body).status_code == 404


def test_supervisor_refuses_stopped_or_retiring_prompt_without_holding_registry_lock():
    import threading
    from types import SimpleNamespace

    sup = Supervisor(lambda *a: None)

    class Instance:
        def respond_prompt(self, request_id, approved):
            assert sup._lock.acquire(blocking=False)
            sup._lock.release()
            return True

    managed = SimpleNamespace(
        instance=Instance(), stop=threading.Event(), retiring=False
    )
    sup._monitors["j"] = managed
    assert sup.respond_prompt("j", "r", True)
    managed.stop.set()
    assert not sup.respond_prompt("j", "r", True)
    managed.stop.clear()
    managed.retiring = True
    assert not sup.respond_prompt("j", "r", True)
    assert not sup.respond_prompt("missing", "r", True)
