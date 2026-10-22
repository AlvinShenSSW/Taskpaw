"""P2-2 (#293): 设备 v1 on the loopback control API (R2, R5, R18).

Every operator command carries the worker's `expected_version`; the codes and
states come from the real store and the real worker API.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.worker_api_helpers import (
    Host,
    StepClock,
    beat,
    client,
    enrol,
    join,
    new_secret,
    pair_body,
    probe,
)
from taskpaw_v3.tests.workflow_route_helpers import Remote, claim_remote, route
from taskpaw_v3.workflow import models as m

BASE = "/control/workflow/devices"
SELF = "/worker/v1/self"


@pytest.fixture
def clock():
    return StepClock()


@pytest.fixture
def rt(tmp_path, clock):
    value = runtime(tmp_path / "wf", clock=clock.now)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def host(rt):
    return Host(rt)


def control(host, *, remote=True, bind="192.168.1.20"):
    config = AgentConfig(
        server_id="s1",
        machine="dev",
        bind_host=bind,
        api_token="t",
        workflow=WorkflowSettings(enabled=True, remote_workers=remote),
    )
    return TestClient(
        create_control_app(
            config,
            control_token="c",
            control_active=lambda: True,
            workflow_provider=lambda: host,
        ),
        headers={"Authorization": "Bearer c"},
    )


@pytest.fixture
def ctl(host):
    return control(host)


@pytest.fixture
def api(host):
    return client(host)


def _row(ctl, worker_id):
    data = ctl.get(BASE).json()
    return next(row for row in data["workers"] if row["worker_id"] == worker_id)


def test_without_remote_workers_nothing_can_be_added(host):
    ctl = control(host, remote=False)
    data = ctl.get(BASE).json()
    assert data["remote_workers"] is False and data["workers"] == []
    assert data["controller"] == {"name": "本机", "stages": ["修复", "识别", "翻译"]}
    response = ctl.post(BASE, json={"name": "5060 Ti-1", "address": "192.168.1.31"})
    assert response.status_code == 409


def test_add_pair_approve_and_show_online(ctl, api, rt, clock):
    added = ctl.post(BASE, json={"name": "5060 Ti-1", "address": "192.168.1.31"})
    assert added.status_code == 200
    code = added.json()["code"]
    assert code.startswith("TP1:192.168.1.20:5682:")
    worker_id = added.json()["worker_id"]
    row = _row(ctl, worker_id)
    assert (row["enrollment_state"], row["heartbeat"]["state"]) == (
        "pending_pairing",
        "waiting",
    )
    assert row["address"] == "192.168.1.31" and row["port"] == 5680
    body = pair_body(code, "00000000-0000-4000-8000-0000000000aa", new_secret())
    assert api.post("/worker/v1/pair", json=body).status_code == 200
    headers = {"Authorization": f"Bearer {body['credential_id']}.{body['secret']}"}
    api.post("/worker/v1/capabilities", json=probe("a"), headers=headers)
    row = _row(ctl, worker_id)
    assert row["enrollment_state"] == "pending_validation"
    assert row["probe"]["vram_mib"] == 16311 and row["restore_allowed"] is True
    assert {p["stage"] for p in row["probe"]["profiles"]} == {"restore", "asr"}
    assert row["approved"] is None
    stale = ctl.post(
        f"{BASE}/{worker_id}/approve",
        json={"expected_version": row["version"] - 1, "restore": True, "asr": True},
    )
    assert stale.status_code == 409
    ok = ctl.post(
        f"{BASE}/{worker_id}/approve",
        json={"expected_version": row["version"], "restore": True, "asr": True},
    )
    assert ok.status_code == 200
    boot = "00000000-0000-4000-8000-0000000000bb"
    api.post("/worker/v1/boot", json={"boot_id": boot}, headers=headers)
    clock.advance(timedelta(seconds=61))  # out of the restart grace
    assert _row(ctl, worker_id)["heartbeat"]["state"] == "lost"
    api.post("/worker/v1/heartbeat", json=beat(boot), headers=headers)
    row = _row(ctl, worker_id)
    assert (row["enrollment_state"], row["admission_state"]) == ("verified", "enabled")
    assert row["heartbeat"]["state"] == "online" and row["heartbeat"]["age_s"] == 0
    assert row["gpu"]["name"] == "NVIDIA GeForce RTX 5060 Ti"
    assert row["disk"]["cap"] == 500 * 10**9
    assert row["tiers_ready"] == ["standard", "good", "high"]
    assert row["eligibility"] == "可接单" and row["eligible"] is True
    assert row["approved"]["probe"]["vram_mib"] == 16311
    assert ctl.get(BASE).json()["controller"]["stages"] == ["翻译"]


def test_an_8_gb_worker_is_offered_asr_only(ctl, api, rt):
    joined, boot = enrol(api, rt, "5060", restore=False)
    api.post(
        "/worker/v1/capabilities",
        json=probe(joined.worker_id, vram=8151),
        headers=joined.headers,
    )
    row = _row(ctl, joined.worker_id)
    assert row["restore_allowed"] is False
    refused = ctl.post(
        f"{BASE}/{joined.worker_id}/approve",
        json={"expected_version": row["version"], "restore": True, "asr": True},
    )
    assert refused.status_code == 409


def test_drain_resume_revoke_and_retire(ctl, api, rt):
    joined, boot = enrol(api, rt)
    film_id = add_film(rt)
    route(rt, film_id, joined.worker_id)
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    remote = Remote(
        joined.worker_id,
        "5060 Ti-1",
        joined.auth(rt),
        boot,
        worker.physical_binding.resource_id,
    )
    claim_remote(rt, remote, film_id, "restore")

    def act(name, **body):
        row = _row(ctl, joined.worker_id)
        return ctl.post(
            f"{BASE}/{joined.worker_id}/{name}",
            json={"expected_version": row["version"], **body},
        )

    assert act("admission", state="draining").status_code == 200
    assert _row(ctl, joined.worker_id)["admission_state"] == "draining"
    assert act("admission", state="enabled").status_code == 200
    assert act("retire").status_code == 409  # not revoked yet
    assert act("revoke").status_code == 200
    assert api.get(SELF, headers=joined.headers).status_code == 401
    row = _row(ctl, joined.worker_id)
    assert row["admission_state"] == "revoked"
    assert row["unresolved"] == 1 and row["can_retire"] is False
    blocked = act("retire")
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "仍有未结束的任务，不能退休"


def test_repair_returns_a_new_code(ctl, api, rt):
    joined, _ = enrol(api, rt)
    row = _row(ctl, joined.worker_id)
    response = ctl.post(
        f"{BASE}/{joined.worker_id}/pairing", json={"expected_version": row["version"]}
    )
    assert response.status_code == 200
    assert response.json()["code"].startswith("TP1:192.168.1.20:5682:")


@pytest.mark.parametrize("address", ["0.0.0.0", "8.8.8.8", "http://x", "a b"])
def test_an_invalid_address_is_422(ctl, address):
    response = ctl.post(BASE, json={"name": "x", "address": address})
    assert response.status_code == 422


def test_the_controller_row_has_no_actions(ctl, rt):
    me = rt.identity.worker_id
    version = rt.worker().version
    for name in ("approve", "admission", "revoke", "retire", "pairing"):
        response = ctl.post(
            f"{BASE}/{me}/{name}",
            json={"expected_version": version, "restore": True, "asr": True}
            if name == "approve"
            else {"expected_version": version, "state": "enabled"}
            if name == "admission"
            else {"expected_version": version},
        )
        assert response.status_code == 409, name
    assert all(row["worker_id"] != me for row in ctl.get(BASE).json()["workers"])


def test_ipv6_controller_hosts_are_bracketed(host):
    ctl = control(host, bind="fd00::20")
    code = ctl.post(BASE, json={"name": "x", "address": "fd00::31"}).json()["code"]
    assert code.startswith("TP1:[fd00::20]:5682:")


def test_with_remote_workers_off_approval_and_admission_are_refused(host, api, rt):
    """IR1: the flag gates every command that would hand the GPU stages to a
    worker; IR14: no worker reads 失联 while remote work is off."""
    joined, boot = enrol(api, rt)
    api.post("/worker/v1/heartbeat", json=beat(boot), headers=joined.headers)
    api.post("/worker/v1/capabilities", json=probe("changed"), headers=joined.headers)
    off = control(host, remote=False)
    row = _row(off, joined.worker_id)
    assert row["heartbeat"]["state"] == "off"
    assert off.get(BASE).json()["controller"]["stages"] == ["修复", "识别", "翻译"]
    approve = off.post(
        f"{BASE}/{joined.worker_id}/approve",
        json={"expected_version": row["version"], "restore": True, "asr": True},
    )
    assert approve.status_code == 409
    admission = off.post(
        f"{BASE}/{joined.worker_id}/admission",
        json={"expected_version": row["version"], "state": "enabled"},
    )
    assert admission.status_code == 409
    revoke = off.post(  # cleaning up stays possible
        f"{BASE}/{joined.worker_id}/revoke", json={"expected_version": row["version"]}
    )
    assert revoke.status_code == 200


def test_a_revoked_worker_leaves_the_live_state_at_once(ctl, api, rt, host):
    """IR14: 撤销 drops the worker's FleetLive entry immediately."""
    joined, boot = enrol(api, rt)
    api.post("/worker/v1/heartbeat", json=beat(boot), headers=joined.headers)
    row = _row(ctl, joined.worker_id)
    assert row["heartbeat"]["state"] == "online"
    revoked = ctl.post(
        f"{BASE}/{joined.worker_id}/revoke", json={"expected_version": row["version"]}
    )
    assert revoked.status_code == 200
    assert host.fleet.entry(joined.worker_id) is None
    assert joined.worker_id not in host.fleet.snapshot()


def test_approval_survives_a_write_between_its_two_steps(ctl, api, rt, monkeypatch):
    """IR9: a write that lands between the approved revision and the enabling
    update must not leave a verified worker disabled (no button would show)."""
    joined = join(api, rt)
    api.post(
        "/worker/v1/capabilities", json=probe(joined.worker_id), headers=joined.headers
    )
    original = rt.store.record_capabilities

    def racing(*args, **kw):
        result = original(*args, **kw)
        worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
        rt.store.update_worker(  # e.g. an address edit: the version moves on
            rt.operator,
            rt.meta(),
            m.WorkerUpdate(
                worker_id=worker.worker_id,
                expected_version=worker.version,
                address=worker.address,
                port=worker.port,
                network_revision=worker.network_revision,
                workflow_participation=worker.workflow_participation,
                admission_state=worker.admission_state,
            ),
        )
        return result

    monkeypatch.setattr(rt.store, "record_capabilities", racing)
    row = _row(ctl, joined.worker_id)
    response = ctl.post(
        f"{BASE}/{joined.worker_id}/approve",
        json={"expected_version": row["version"], "restore": True, "asr": True},
    )
    assert response.status_code == 200, response.text
    row = _row(ctl, joined.worker_id)
    assert (row["enrollment_state"], row["admission_state"]) == ("verified", "enabled")
