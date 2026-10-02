"""R01: real local control routes reject before any state mutation."""

import copy
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.core.config import AgentConfig, HubConfig, save_yaml
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.tasklog import get_task_log
from taskpaw_v3.hub.server import app as hub_app
from taskpaw_v3.hub.server.store import HubStore
from taskpaw_v3.monitors.runtime import build_supervisor
from taskpaw_v3.tests.test_admin import _registry

CONTROL = "fake-runtime-control-key"
READ = "fake-network-read-key"
POLL = "fake-outbound-poll-key"
AUTH = {"Authorization": f"Bearer {CONTROL}"}
AGENT_WRITES = [
    ("POST", "/control/command", {"command": "stop_monitor", "name": "w"}),
    (
        "POST",
        "/control/monitors",
        {"type_id": "fake", "name": "new", "config": {"name": "new"}},
    ),
    ("DELETE", "/control/monitors?name=w", None),
    ("PATCH", "/control/monitors?name=w", {"config": {"poll_interval": 19}}),
    ("POST", "/control/monitors/start?name=w", None),
    ("POST", "/control/monitors/stop?name=w", None),
    ("PATCH", "/control/config", {"machine": "changed", "api_token": "changed-read"}),
    ("POST", "/control/llm-test", {"llm_api_base": "http://127.0.0.1:12345/v1"}),
]
HUB_WRITES = [
    ("POST", "/servers", {"name": "new", "ip": "127.0.0.1"}),
    ("PATCH", "/servers/1", {"name": "changed", "enabled": False}),
    ("DELETE", "/servers/1", None),
    ("PATCH", "/config", {"polling_token": "changed-poll"}),
]
REJECT_HEADERS = [
    ({}, 401),
    ({"Authorization": "Bearer "}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Authorization": f"Bearer {READ}"}, 401),
    ({"Authorization": f"Bearer {POLL}"}, 401),
    (
        [
            ("Authorization", f"Bearer {CONTROL}"),
            ("Authorization", f"Bearer {CONTROL}"),
        ],
        401,
    ),
    ({**AUTH, "Origin": "https://evil.example"}, 403),
    ({**AUTH, "Origin": "null"}, 403),
    ({**AUTH, "Origin": ""}, 403),
    (
        [
            ("Authorization", f"Bearer {CONTROL}"),
            ("Origin", "http://localhost:5173"),
            ("Origin", "http://localhost:5173"),
        ],
        403,
    ),
]


def _agent_factory(cfg, **kwargs):
    return create_control_app(
        cfg, control_token=CONTROL, control_active=lambda: True, **kwargs
    )


def _hub_factory(cfg, store):
    network, service = hub_app.create_hub_app(cfg, store)
    control = hub_app.create_hub_control_app(
        cfg, store, service, control_token=CONTROL, control_active=lambda: True
    )
    return network, control, service


@pytest.fixture
def agent(tmp_path, monkeypatch):
    cfg = AgentConfig(
        server_id="s",
        machine="m",
        host_metrics=False,
        api_token=READ,
        monitors=[
            {"type_id": "fake", "name": "w", "config": {"name": "w"}, "enabled": True}
        ],
    )
    path = tmp_path / "agent.yaml"
    save_yaml(cfg, path)
    queue = EventQueue("m")
    queue.add("w", "pending")
    registry = _registry()
    sup = build_supervisor(registry, cfg.monitors, queue, cfg.machine)
    admin = MonitorAdmin(cfg, sup, registry, path)
    llm = Mock(return_value={"ok": True})
    monkeypatch.setattr(admin, "llm_test", llm)
    calls = []
    for name in ("handle", "add", "remove", "patch", "set_enabled", "update_config"):
        original = getattr(admin, name)

        def observed(*args, _name=name, _original=original, **kw):
            calls.append(_name)
            return _original(*args, **kw)

        monkeypatch.setattr(admin, name, observed)
    client = TestClient(
        _agent_factory(
            cfg,
            admin=admin,
            on_command=admin.handle,
            registry=registry,
            events_provider=queue.recent,
            status_provider=sup.snapshot,
        )
    )

    def snapshot():
        return (
            path.read_bytes(),
            cfg.model_dump(),
            admin.config_view(),
            copy.deepcopy(sup.snapshot()),
            queue.payload(ack_id=0),
            queue.next_id,
            queue.recent(),
            copy.deepcopy(get_task_log().query()),
            list(calls),
            llm.call_count,
        )

    yield client, snapshot, admin, sup, queue
    sup.stop()


@pytest.fixture
def hub(tmp_path):
    store = HubStore(tmp_path / "hub.db")
    store.add_server("existing", "127.0.0.1", 5680)
    store.set_config("polling_token", POLL)
    store.store_event(
        1,
        {"id": 1, "monitor": "w", "message": "pending", "time": "2026-01-01 00:00:00"},
    )
    store.enqueue_delivery(
        server_name="existing", kind="event", payload_json='{"text":"pending"}'
    )
    cfg = HubConfig(
        self_monitor=False, api_token=READ, polling_token=POLL, write_status_md=False
    )
    network, control, service = _hub_factory(cfg, store)

    def snapshot():
        return (
            list(store._conn.iterdump()),
            service.poller.snapshot_statuses(),
            service.poller.snapshot_acks(),
            service._running.is_set(),
            service._thread,
        )

    yield TestClient(control), snapshot, store, service, TestClient(network)
    store.close()


@pytest.mark.parametrize("method,path,body", AGENT_WRITES)
@pytest.mark.parametrize("headers,status", REJECT_HEADERS)
def test_agent_every_mutation_rejects_without_state_change(
    agent, method, path, body, headers, status
):
    client, snapshot, *_ = agent
    before = snapshot()
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == status
    assert snapshot() == before
    assert CONTROL not in response.text and READ not in response.text


@pytest.mark.parametrize("method,path,body", HUB_WRITES)
@pytest.mark.parametrize("headers,status", REJECT_HEADERS)
def test_hub_every_mutation_rejects_without_state_change(
    hub, method, path, body, headers, status
):
    client, snapshot, *_ = hub
    before = snapshot()
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == status
    assert snapshot() == before
    assert CONTROL not in response.text and POLL not in response.text


@pytest.mark.parametrize("method,path,body", HUB_WRITES)
def test_hub_read_listener_has_no_mutation_routes(hub, method, path, body):
    _, snapshot, _, _, network = hub
    before = snapshot()
    assert network.request(
        method, path, json=body, headers={"Authorization": f"Bearer {READ}"}
    ).status_code in (404, 405)
    assert snapshot() == before


@pytest.mark.parametrize("method,path,body", AGENT_WRITES)
def test_agent_guard_precedes_invalid_body_and_parameters(agent, method, path, body):
    client, snapshot, *_ = agent
    before = snapshot()
    response = client.request(
        method, path.replace("name=w", "missing=true"), content="not-json"
    )
    assert response.status_code == 401
    assert snapshot() == before


@pytest.mark.parametrize(
    "command",
    [
        "add_monitor",
        "remove_monitor",
        "enable_monitor",
        "start_monitor",
        "disable_monitor",
        "stop_monitor",
        "update_monitor",
        "llm_test",
    ],
)
def test_dispatcher_has_no_auth_bypass(agent, command):
    client, snapshot, *_ = agent
    before = snapshot()
    response = client.post(
        "/control/command",
        json={
            "command": command,
            "name": "w",
            "monitor": {"type_id": "fake", "name": "new", "config": {"name": "new"}},
        },
    )
    assert response.status_code == 401
    assert snapshot() == before


@pytest.mark.parametrize(
    "content_type,body",
    [
        ("application/x-www-form-urlencoded", "name=w"),
        (
            "multipart/form-data; boundary=a",
            '--a\r\nContent-Disposition: form-data; name="name"\r\n\r\nw\r\n--a--\r\n',
        ),
        ("text/plain", "stop"),
    ],
)
def test_simple_browser_post_cannot_stop(agent, content_type, body):
    client, snapshot, *_ = agent
    before = snapshot()
    response = client.post(
        "/control/monitors/stop?name=w",
        content=body,
        headers={"Content-Type": content_type, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert snapshot() == before


def test_valid_control_changes_agent_and_network_token_does_not_lock_session(agent):
    client, _, admin, _, _ = agent
    assert client.post("/control/monitors/stop?name=w", headers=AUTH).status_code == 200
    assert admin.config_view()["monitors"][0]["enabled"] is False
    assert (
        client.patch(
            "/control/config", json={"api_token": "new-network"}, headers=AUTH
        ).status_code
        == 200
    )
    assert client.get("/control/config", headers=AUTH).status_code == 200
    assert (
        client.post("/control/monitors/start?name=w", headers=AUTH).status_code == 200
    )
    assert admin.config_view()["monitors"][0]["enabled"] is True


def test_valid_control_changes_hub_once(hub):
    client, _, store, _, network = hub
    response = client.post(
        "/servers", json={"name": "added", "ip": "127.0.0.1"}, headers=AUTH
    )
    assert response.status_code == 200
    assert len(store.list_servers()) == 2
    assert (
        client.patch(
            "/config", json={"polling_token": "new-poll"}, headers=AUTH
        ).status_code
        == 200
    )
    assert store.get_config("polling_token") == "new-poll"
    assert client.get("/status", headers=AUTH).status_code == 200
    assert network.get("/status", headers=AUTH).status_code == 401


def test_local_agent_reads_leave_queue_and_config_unchanged(agent):
    client, snapshot, *_ = agent
    before = snapshot()
    for path in [
        "/control/status",
        "/control/config",
        "/control/events",
        "/control/logs",
        "/control/plugins",
    ]:
        assert client.get(path, headers=AUTH).status_code == 200
    assert snapshot() == before


def test_agent_network_ack_contract_stays_independent(tmp_path):
    from taskpaw_v3.agent.server.launcher import build_queue
    from taskpaw_v3.core.state import initialize_state

    cfg = AgentConfig(server_id="s", machine="m", api_token=READ)
    state_path = tmp_path / "agent.state.json"
    initialize_state(state_path, cfg.server_id)
    queue = build_queue(cfg, state_path)
    try:
        ev = queue.add("w", "pending")
        client = TestClient(create_network_app(cfg, queue))
        assert client.get(f"/events?ack={ev['id']}", headers=AUTH).status_code == 401
        assert len(queue) == 1
        headers = {"Authorization": f"Bearer {READ}"}
        offer = client.get("/events?ack=-1", headers=headers)
        assert offer.status_code == 200
        assert [row["id"] for row in offer.json()["events"]] == [ev["id"]]
        assert len(queue) == 1
        assert client.get(f"/events?ack={ev['id']}", headers=headers).status_code == 200
        assert len(queue) == 0
    finally:
        queue.close()


def test_rejected_stop_preserves_managed_child_and_rejected_start_never_spawns(
    tmp_path,
):
    import subprocess
    import sys
    import threading

    from taskpaw_v3.monitors.base import (
        BaseMonitorConfig,
        MonitorInstance,
        MonitorPlugin,
        MonitorStatus,
    )
    from taskpaw_v3.monitors.registry import PluginRegistry

    started = threading.Event()
    instances = []
    counts = {"start": 0, "stop": 0}

    class Instance(MonitorInstance):
        def __init__(self, iid, config):
            super().__init__(iid, config)
            self.child = None
            instances.append(self)

        def start(self, emit):
            counts["start"] += 1
            self.child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            started.set()

        def check(self, emit):
            return MonitorStatus(state="running")

        def stop(self, timeout=5):
            counts["stop"] += 1
            if self.child is not None:
                self.child.terminate()
                self.child.wait(timeout=timeout)

    class Plugin(MonitorPlugin):
        type_id = "managed-test"
        display_name = "Managed test"

        @classmethod
        def config_model(cls):
            return BaseMonitorConfig

        def create(self, iid, cfg):
            return Instance(iid, cfg)

    registry = PluginRegistry()
    registry.register(Plugin())
    cfg = AgentConfig(
        server_id="s",
        machine="m",
        host_metrics=False,
        monitors=[
            {"type_id": "managed-test", "name": "live", "config": {"name": "live"}},
            {
                "type_id": "managed-test",
                "name": "off",
                "config": {"name": "off"},
                "enabled": False,
            },
        ],
    )
    path = tmp_path / "agent.yaml"
    save_yaml(cfg, path)
    queue = EventQueue("m")
    sup = build_supervisor(registry, cfg.monitors, queue, "m")
    admin = MonitorAdmin(cfg, sup, registry, path)
    client = TestClient(_agent_factory(cfg, admin=admin, on_command=admin.handle))
    try:
        sup.start()
        assert started.wait(timeout=3)
        child = instances[0].child
        assert child.poll() is None
        before = (
            path.read_bytes(),
            dict(counts),
            sup.snapshot(),
            queue.payload(ack_id=0),
            queue.next_id,
        )
        assert client.post("/control/monitors/stop?name=live").status_code == 401
        assert client.post("/control/monitors/start?name=off").status_code == 401
        assert child.poll() is None
        assert (
            path.read_bytes(),
            counts,
            sup.snapshot(),
            queue.payload(ack_id=0),
            queue.next_id,
        ) == before
        assert (
            client.post("/control/monitors/stop?name=live", headers=AUTH).status_code
            == 200
        )
        assert child.poll() is not None
        assert counts == {"start": 1, "stop": 1}
    finally:
        sup.stop()


@pytest.mark.parametrize("method,path,body", HUB_WRITES)
def test_hub_guard_precedes_invalid_body_and_path(hub, method, path, body):
    client, snapshot, *_ = hub
    before = snapshot()
    response = client.request(
        method, path.replace("/1", "/invalid"), content="not-json"
    )
    assert response.status_code == 401
    assert snapshot() == before


def test_control_local_hub_reads_do_not_mutate_storage(hub):
    client, snapshot, *_ = hub
    before = snapshot()
    for path in ("/status", "/events", "/ping"):
        assert client.get(path, headers=AUTH).status_code == 200
    assert snapshot() == before


@pytest.mark.parametrize("resource", ["films", "run-films"])
def test_hub_control_film_uses_only_polling_token(hub, resource, monkeypatch):
    client, snapshot, _, service, _ = hub
    monkeypatch.setattr(
        service.poller, "snapshot_statuses", lambda: {1: {"online": True}}
    )
    downstream = []

    def fetch(server, kind, params, headers, timeout):
        downstream.append(headers)
        return {"films": []}

    monkeypatch.setattr(hub_app, "fetch_agent_film_page", fetch)
    before = snapshot()
    response = client.get(f"/servers/1/monitors/{resource}?name=w", headers=AUTH)
    assert response.status_code == 200
    assert downstream == [{"Authorization": f"Bearer {POLL}"}]
    assert CONTROL not in str(downstream)
    assert snapshot() == before
