"""#306: worker-reported agent ports reach automatic power polling (all fakes)."""

from types import SimpleNamespace

import pytest

from taskpaw_v3.core import power_fleet as pf
from taskpaw_v3.tests.test_workflow_local_dispatch import runtime
from taskpaw_v3.tests.test_workflow_power_peers import Clock, Fetch
from taskpaw_v3.tests.worker_api_helpers import (
    Host,
    boot_id,
    client,
    join,
    new_secret,
    pair_body,
)
from taskpaw_v3.workflow import devices


@pytest.fixture
def controller(tmp_path):
    rt = runtime(tmp_path / "controller")
    host = Host(rt)
    peers = SimpleNamespace(
        runtime=rt, state="running", settings=SimpleNamespace(remote_workers=True)
    )
    fetch = Fetch()
    fleet = pf.PowerFleet(
        lambda: devices.power_peers(peers),
        lambda: "test-token",
        fetch=fetch,
        clock=Clock(),
        wall=lambda: 1_790_000_000.0,
    )
    try:
        with client(host) as api:
            yield SimpleNamespace(rt=rt, api=api, fetch=fetch, fleet=fleet)
    finally:
        fleet.stop()
        rt.close()


def assert_polled(controller, worker_id, port):
    worker = controller.rt.store.get_worker(controller.rt.operator, worker_id).worker
    assert worker.address == "192.0.2.31"
    assert worker.port == port
    controller.fetch.urls.clear()
    controller.fleet.refresh_once()
    assert controller.fetch.urls == [f"http://192.0.2.31:{port}/status"]


def test_join_reports_agent_port_for_power_polling(controller):
    rt, api = controller.rt, controller.api
    issued = devices.add_device(
        rt, "DarkBluePig", "192.0.2.31", host="127.0.0.1", port=5682
    )
    body = pair_body(issued["code"], boot_id(), new_secret())
    response = api.post("/worker/v1/pair", json={**body, "agent_port": 5678})
    assert response.status_code == 200, response.text
    assert_polled(controller, issued["worker_id"], 5678)


def test_later_boot_report_updates_the_polled_port(controller):
    joined = join(controller.api, controller.rt)
    boot = boot_id()
    for port in (5678, 5680):
        response = controller.api.post(
            "/worker/v1/boot",
            json={"boot_id": boot, "agent_port": port},
            headers=joined.headers,
        )
        assert response.status_code == 200, response.text
        assert_polled(controller, joined.worker_id, port)


def test_older_worker_reports_keep_the_stored_port(controller):
    rt, api = controller.rt, controller.api
    issued = devices.add_device(
        rt, "DarkBluePig", "192.0.2.31:5678", host="127.0.0.1", port=5682
    )
    credential_id, secret = boot_id(), new_secret()
    response = api.post(
        "/worker/v1/pair", json=pair_body(issued["code"], credential_id, secret)
    )
    assert response.status_code == 200, response.text
    assert_polled(controller, issued["worker_id"], 5678)
    response = api.post(
        "/worker/v1/boot",
        json={"boot_id": boot_id()},
        headers={"Authorization": f"Bearer {credential_id}.{secret}"},
    )
    assert response.status_code == 200, response.text
    assert_polled(controller, issued["worker_id"], 5678)


def test_worker_sends_its_configured_agent_port_on_join_and_boot(tmp_path, monkeypatch):
    from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
    from taskpaw_v3.workflow.remote_worker import WorkerHost, start_worker_host
    from taskpaw_v3.workflow.worker_client import WorkerClient

    sent = {}
    worker_id = boot_id()

    def call(self, method, path, body=None, **kwargs):
        sent[path] = body
        return {"worker_id": worker_id, "reconciled": True}

    monkeypatch.setattr(WorkerClient, "call", call)
    monkeypatch.setattr(WorkerHost, "_spawn", lambda self: self._stop.clear())
    config = AgentConfig(
        server_id="test-worker",
        machine="DarkBluePig",
        bind_port=5678,
        workflow=WorkflowSettings(enabled=True, role="worker"),
    )
    worker = start_worker_host(config.workflow, tmp_path, agent_port=config.bind_port)
    assert worker is not None
    try:
        code = f"TP1:127.0.0.1:5682:{boot_id()}:{'A' * 43}"
        worker.join(code, str(tmp_path / "staging"))
        assert sent["/pair"]["agent_port"] == 5678
        assert worker._boot_once(WorkerClient("127.0.0.1", 5682)) is True
        assert sent["/boot"]["agent_port"] == 5678
    finally:
        assert worker.stop()
