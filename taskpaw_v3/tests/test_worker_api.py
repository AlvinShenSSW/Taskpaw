"""P2-2 (#293): the controller's worker API — enrollment, live state, security.

The real app over a real controller runtime (FastAPI TestClient); the
security invariants S-1..S-7 of the frozen contract §6 are each tested here or
in the launcher tests below.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from taskpaw_v3.agent.server.worker_api import PREFIX, create_worker_app
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.worker_api_helpers import (
    Host,
    StepClock,
    add,
    beat,
    boot_id,
    client,
    enrol,
    join,
    new_secret,
    pair_body,
    probe,
    profiles,
    version,
)
from taskpaw_v3.tests.workflow_route_helpers import route
from taskpaw_v3.workflow import devices
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.auth import digest_secret

# The §2 table, exactly.
ROUTES = {
    ("POST", f"{PREFIX}/pair"),
    ("GET", f"{PREFIX}/self"),
    ("POST", f"{PREFIX}/capabilities"),
    ("POST", f"{PREFIX}/boot"),
    ("POST", f"{PREFIX}/heartbeat"),
    ("GET", f"{PREFIX}/films/{{film_id}}/{{revision}}"),
    ("GET", f"{PREFIX}/films/{{film_id}}/{{revision}}/items/{{item_id}}"),
    ("POST", f"{PREFIX}/attempts/{{attempt_id}}/outputs"),
    ("GET", f"{PREFIX}/attempts/{{attempt_id}}/outputs"),
    ("PUT", f"{PREFIX}/attempts/{{attempt_id}}/files/{{path:path}}"),
    # P2-3 §2: remote execution
    ("POST", f"{PREFIX}/claim"),
    ("POST", f"{PREFIX}/attempts/{{attempt_id}}/renew"),
    ("POST", f"{PREFIX}/attempts/{{attempt_id}}/finish"),
    ("GET", f"{PREFIX}/attempts/{{attempt_id}}"),
}


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


@pytest.fixture
def api(host):
    return client(host)


def _enumerated(app) -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", ())
    }


# ── S-2: the route table and nothing else ───────────────────────────────
def test_routes_equal_the_contract_table_with_no_docs():
    app = create_worker_app(lambda: None)
    assert _enumerated(app) == ROUTES
    assert all(path.startswith(f"{PREFIX}/") for _, path in ROUTES)
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
    plain = TestClient(app)
    for path in ("/docs", "/openapi.json", "/redoc", "/control/status", "/status"):
        response = plain.get(path)
        assert response.status_code == 404 and response.json() == {"code": "not_found"}


# ── S-3: Bearer of a compute worker everywhere but /pair ─────────────────
def _calls(headers=None):
    film, attempt = uuid.uuid4(), uuid.uuid4()
    return [
        ("GET", "self", None),
        ("POST", "capabilities", probe()),
        ("POST", "boot", {"boot_id": boot_id()}),
        ("POST", "heartbeat", beat(boot_id())),
        ("GET", f"films/{film}/1", None),
        ("GET", f"films/{film}/1/items/{uuid.uuid4()}", None),
        ("POST", f"attempts/{attempt}/outputs", []),
        ("GET", f"attempts/{attempt}/outputs", None),
        ("PUT", f"attempts/{attempt}/files/{attempt}/a.mp4", None),
        ("POST", "claim", {}),
        ("POST", f"attempts/{attempt}/renew", {}),
        ("POST", f"attempts/{attempt}/finish", {"outcome": "failed"}),
        ("GET", f"attempts/{attempt}", None),
    ]


def _request(api, method, name, body, headers=None):
    return api.request(method, f"{PREFIX}/{name}", json=body, headers=headers or {})


@pytest.mark.parametrize("header", [None, "Bearer nope", "Basic abc", "Bearer a.b.c"])
def test_without_a_valid_bearer_every_route_is_401(api, header):
    headers = {"Authorization": header} if header else {}
    for method, name, body in _calls():
        response = _request(api, method, name, body, headers)
        assert response.status_code == 401, name
        assert response.json() == {"code": "unauthorized"}


def test_401_comes_before_the_host_is_running():
    api = TestClient(create_worker_app(lambda: None))
    assert api.get(f"{PREFIX}/self").status_code == 401
    bearer = {"Authorization": f"Bearer {uuid.uuid4()}.{new_secret()}"}
    response = api.get(f"{PREFIX}/self", headers=bearer)
    assert response.status_code == 503
    assert response.json() == {"code": "controller_starting"}
    starting = TestClient(create_worker_app(lambda: Host(None, state="starting")))
    assert starting.get(f"{PREFIX}/self", headers=bearer).status_code == 503


def _component_bearer(rt) -> dict[str, str]:
    secret = new_secret()
    grant = m.ComponentGrant(
        component_id=str(uuid.uuid4()),
        principal_id=str(uuid.uuid4()),
        credential_id=str(uuid.uuid4()),
        boot_id=rt.boot_id,
        generation=1,
        role="intake_adapter",
        secret_digest=digest_secret(SecretStr(secret)),
        expires_at=rt.clock.now() + timedelta(days=1),
    )
    rt.store.grant_component(rt.operator, rt.meta(), grant)
    return {"Authorization": f"Bearer {grant.credential_id}.{secret}"}


def test_operator_component_and_controller_credentials_are_403(api, rt):
    ident = rt.identity
    bearers = [
        {
            "Authorization": f"Bearer {ident.operator_credential_id}.{ident.operator_secret}"
        },
        _component_bearer(rt),
        # 本机 is a compute worker too, but never a remote one
        {"Authorization": f"Bearer {ident.worker_credential_id}.{ident.worker_secret}"},
    ]
    for headers in bearers:
        for method, name, body in _calls():
            response = _request(api, method, name, body, headers)
            assert response.status_code == 403, name
            assert response.json() == {"code": "forbidden"}


def test_a_revoked_then_retired_worker_gets_401(api, rt):
    joined, _ = enrol(api, rt)
    assert api.get(f"{PREFIX}/self", headers=joined.headers).status_code == 200
    devices.revoke(rt, joined.worker_id, version(rt, joined.worker_id))
    for method, name, body in _calls():
        assert _request(api, method, name, body, joined.headers).status_code == 401
    devices.retire(rt, joined.worker_id, version(rt, joined.worker_id))
    assert api.get(f"{PREFIX}/self", headers=joined.headers).status_code == 401


def test_pair_ignores_any_bearer(api, rt):
    joined = join(api, rt, "first")
    issued = add(rt, "second")
    body = pair_body(issued["code"], str(uuid.uuid4()), new_secret())
    response = api.post(f"{PREFIX}/pair", json=body, headers=joined.headers)
    assert response.status_code == 200
    assert response.json()["worker_id"] == issued["worker_id"]


# ── R2/R3: enrollment ───────────────────────────────────────────────────
def test_pairing_fills_identity_and_a_lost_reply_recovers_through_self(api, rt):
    issued = add(rt)
    assert issued["code"].startswith("TP1:127.0.0.1:5682:")
    credential_id, secret = str(uuid.uuid4()), new_secret()
    first = api.post(
        f"{PREFIX}/pair", json=pair_body(issued["code"], credential_id, secret)
    )
    assert first.status_code == 200
    assert first.json()["generation"] == 1
    # the reply was lost: the same ticket is consumed, /self proves the pairing
    again = api.post(
        f"{PREFIX}/pair", json=pair_body(issued["code"], credential_id, secret)
    )
    assert again.status_code == 401
    bearer = {"Authorization": f"Bearer {credential_id}.{secret}"}
    me = api.get(f"{PREFIX}/self", headers=bearer)
    assert me.status_code == 200
    worker = me.json()["worker"]
    assert worker["worker_id"] == issued["worker_id"]
    assert worker["enrollment_state"] == "pending_validation"
    assert me.json()["boot_reconciled"] is False


def test_wrong_reused_and_expired_tickets_are_401(api, rt, clock):
    issued = add(rt)
    body = pair_body(issued["code"], str(uuid.uuid4()), new_secret())
    wrong = dict(body, ticket=new_secret())
    assert api.post(f"{PREFIX}/pair", json=wrong).status_code == 401
    unknown = dict(body, pairing_id=str(uuid.uuid4()))
    assert api.post(f"{PREFIX}/pair", json=unknown).status_code == 401
    clock.advance(timedelta(minutes=15, seconds=1))
    expired = api.post(f"{PREFIX}/pair", json=body)
    assert expired.status_code == 401 and expired.json() == {"code": "unauthorized"}
    fresh = add(rt, "other")
    used = pair_body(fresh["code"], str(uuid.uuid4()), new_secret())
    assert api.post(f"{PREFIX}/pair", json=used).status_code == 200
    reused = dict(used, credential_id=str(uuid.uuid4()), secret=new_secret())
    assert api.post(f"{PREFIX}/pair", json=reused).status_code == 401


def test_a_revoked_worker_cannot_pair_again(api, rt):
    joined = join(api, rt)
    issued = devices.repair(
        rt, joined.worker_id, version(rt, joined.worker_id), host="127.0.0.1", port=5682
    )
    devices.revoke(rt, joined.worker_id, version(rt, joined.worker_id))
    body = pair_body(issued["code"], str(uuid.uuid4()), new_secret())
    response = api.post(f"{PREFIX}/pair", json=body)
    assert response.status_code == 409
    assert response.json() == {"code": "precondition_failed"}


def test_repairing_bumps_the_generation_and_revokes_the_old_credential(api, rt):
    joined, _ = enrol(api, rt)
    issued = devices.repair(
        rt, joined.worker_id, version(rt, joined.worker_id), host="127.0.0.1", port=5682
    )
    credential_id, secret = str(uuid.uuid4()), new_secret()
    response = api.post(
        f"{PREFIX}/pair", json=pair_body(issued["code"], credential_id, secret)
    )
    assert response.json()["generation"] == 2
    assert api.get(f"{PREFIX}/self", headers=joined.headers).status_code == 401
    me = api.get(
        f"{PREFIX}/self", headers={"Authorization": f"Bearer {credential_id}.{secret}"}
    ).json()["worker"]
    assert (me["enrollment_state"], me["admission_state"]) == (
        "pending_validation",
        "disabled",
    )


def test_malformed_pairs_are_422_without_echo(api, rt):
    issued = add(rt)
    body = pair_body(issued["code"], "not-a-uuid", "short")
    response = api.post(f"{PREFIX}/pair", json=body)
    assert response.status_code == 422 and response.json() == {
        "code": "invalid_request"
    }
    assert "short" not in response.text and issued["code"] not in response.text


# ── R4/R5: probe, 核验, re-probe (OD4, OD5) ──────────────────────────────
def test_probe_then_approval_then_a_changed_probe_needs_a_new_approval(api, rt):
    joined = join(api, rt)
    posted = api.post(f"{PREFIX}/capabilities", json=probe(), headers=joined.headers)
    assert posted.status_code == 200 and posted.json()["revision"] == 1
    me = api.get(f"{PREFIX}/self", headers=joined.headers).json()["worker"]
    assert me["capability"]["probed"]["profiles"][0]["allowed_stages"] == ["restore"]
    assert me["capability"]["approved"] is None
    devices.approve(rt, joined.worker_id, version(rt, joined.worker_id), restore=True)
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    assert (worker.enrollment_state, worker.admission_state) == ("verified", "enabled")
    assert worker.capability.revision == 2
    assert worker.capability.approved.probe_revision == 1
    assert len(worker.capability.approved.profiles) == 1  # OD7: restore only
    # a changed tool profile: a new probe drops the worker back to 待核验
    changed = probe(profiles=[dict(p, tool_version="2") for p in probe()["profiles"]])
    api.post(f"{PREFIX}/capabilities", json=changed, headers=joined.headers)
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    assert (worker.enrollment_state, worker.admission_state) == (
        "pending_validation",
        "disabled",
    )
    last = rt.store.last_approval(rt.operator, joined.worker_id)
    assert last is not None and last[1].revision == 1
    devices.approve(rt, joined.worker_id, worker.version, restore=True)
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    assert worker.enrollment_state == "verified"
    # OD7 R10: an older build's ASR profile is never approved
    assert [p.allowed_stages for p in worker.capability.approved.profiles] == [
        ("restore",)
    ]


def test_a_higher_max_alone_drops_an_approved_worker_back_to_validation(api, rt):
    """P2-5 R3: approved at 1080p, then the same worker proposes 4K (nothing else
    changed): the existing re-probe path de-verifies it, so 4K needs 核验 again."""
    joined = join(api, rt)
    api.post(
        f"{PREFIX}/capabilities",
        json=probe(profiles=profiles(("restore",))),
        headers=joined.headers,
    )
    devices.approve(rt, joined.worker_id, version(rt, joined.worker_id), restore=True)
    old = rt.store.last_approval(rt.operator, joined.worker_id)
    assert old is not None and old[0].profiles[0].max_height == 1080
    api.post(
        f"{PREFIX}/capabilities",
        json=probe(profiles=profiles(("restore",), limit=(3840, 2160))),
        headers=joined.headers,
    )
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    assert (worker.enrollment_state, worker.admission_state) == (
        "pending_validation",
        "disabled",
    )
    assert worker.capability.approved is None
    devices.approve(rt, joined.worker_id, worker.version, restore=True)
    now = rt.store.get_worker(rt.operator, joined.worker_id).worker
    assert now.capability.approved.profiles[0].max_height == 2160


def test_an_approved_section_from_a_worker_is_403(api, rt):
    joined = join(api, rt)
    response = api.post(
        f"{PREFIX}/capabilities", json=probe(approved=None), headers=joined.headers
    )
    assert response.status_code == 403 and response.json() == {"code": "forbidden"}


def test_restore_is_never_approved_under_16_gb(api, rt):
    joined = join(api, rt)
    api.post(f"{PREFIX}/capabilities", json=probe(vram=8151), headers=joined.headers)
    with pytest.raises(m.WorkflowError) as exc:
        devices.approve(
            rt, joined.worker_id, version(rt, joined.worker_id), restore=True
        )
    assert exc.value.code == "precondition_failed"
    with pytest.raises(m.WorkflowError) as exc:  # OD7: nothing else to approve
        devices.approve(
            rt, joined.worker_id, version(rt, joined.worker_id), restore=False
        )
    assert exc.value.code == "precondition_failed"


# ── R6: boot until reconciled ───────────────────────────────────────────
def test_boot_is_reposted_with_the_same_id(api, rt):
    joined, boot = enrol(api, rt)
    again = api.post(f"{PREFIX}/boot", json={"boot_id": boot}, headers=joined.headers)
    assert {k: again.json()[k] for k in ("status", "reconciled")} == {
        "status": "reconciled",
        "reconciled": True,
    }
    me = api.get(f"{PREFIX}/self", headers=joined.headers).json()
    assert me["worker"]["boot_id"] == boot and me["boot_reconciled"] is True


def test_boot_reporting_a_foreign_assignment_is_refused(api, rt):
    joined, boot = enrol(api, rt)
    body = {"boot_id": boot, "assignment_ids": [str(uuid.uuid4())]}
    response = api.post(f"{PREFIX}/boot", json=body, headers=joined.headers)
    assert response.status_code == 409


# ── R7–R10: heartbeat into FleetLive ────────────────────────────────────
def test_heartbeat_marks_online_and_replies_routes(api, rt, host):
    joined, boot = enrol(api, rt)
    film_id = add_film(rt)
    route(rt, film_id, joined.worker_id)
    reply = api.post(f"{PREFIX}/heartbeat", json=beat(boot), headers=joined.headers)
    assert reply.status_code == 200
    data = reply.json()
    assert data["routes"] == [
        {
            "route_id": data["routes"][0]["route_id"],
            "film": film_id,
            "rev": 1,
            "position": 0,
        }
    ]
    assert data["cancel"] == [] and data["release"] == []
    assert data["paused"] is False and data["reconcile_needed"] is False
    assert host.fleet.state(joined.worker_id, rt.clock.now()) == "online"
    live = host.fleet.snapshot()[joined.worker_id]
    assert live.version_ok is True


def test_heartbeat_makes_no_store_write(api, rt):
    joined, boot = enrol(api, rt)
    import sqlite3

    def counts():
        with sqlite3.connect(rt.db_path) as conn:
            return [
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("audit", "request_results", "assignments")
            ]

    before = counts()
    for _ in range(3):
        api.post(f"{PREFIX}/heartbeat", json=beat(boot), headers=joined.headers)
    assert counts() == before


def test_release_names_staged_films_without_a_route(api, rt):
    """P2-3 R10 narrows P2-2's rule: a staged film without a route is
    released only once it no longer needs this worker's GPU work — here its
    revision is not current (its restore still pending keeps it)."""
    joined, boot = enrol(api, rt)
    routed, idle = add_film(rt), add_film(rt)
    route(rt, routed, joined.worker_id)
    staged = [
        {"film": routed, "rev": 1},
        {"film": idle, "rev": 1},
        {"film": idle, "rev": 2},
    ]
    data = api.post(
        f"{PREFIX}/heartbeat", json=beat(boot, staged=staged), headers=joined.headers
    ).json()
    assert data["release"] == [{"film": idle, "rev": 2}]


def test_a_new_boot_needs_reconciliation_but_build_difference_is_compatible(
    api, rt, host
):
    joined, boot = enrol(api, rt)
    other = api.post(
        f"{PREFIX}/heartbeat",
        json=beat(boot_id(), version="1.0.0"),
        headers=joined.headers,
    ).json()
    assert other["reconcile_needed"] is True
    assert host.fleet.snapshot()[joined.worker_id].version_ok is True


def test_heartbeat_over_64_kib_is_413_and_malformed_is_422(api, rt):
    joined, boot = enrol(api, rt)
    big = beat(boot, gpu={"name": "x" * (70 << 10), "hold": "none"})
    response = api.post(f"{PREFIX}/heartbeat", json=big, headers=joined.headers)
    assert response.status_code == 413 and response.json() == {"code": "too_large"}
    bad = api.post(
        f"{PREFIX}/heartbeat", json=beat(boot, protocol="1"), headers=joined.headers
    )
    assert bad.status_code == 422 and bad.json() == {"code": "invalid_request"}


def test_json_over_1_mib_is_413(api, rt):
    joined = join(api, rt)
    big = probe(source="x" * (1 << 20))
    response = api.post(f"{PREFIX}/capabilities", json=big, headers=joined.headers)
    assert response.status_code == 413


# ── S-6: no secret or ticket in logs or error bodies ─────────────────────
def test_no_secret_or_ticket_reaches_logs_or_errors(api, rt, caplog):
    caplog.set_level(logging.DEBUG)
    issued = add(rt)
    credential_id, secret = str(uuid.uuid4()), new_secret()
    body = pair_body(issued["code"], credential_id, secret)
    bad = api.post(f"{PREFIX}/pair", json=dict(body, ticket=new_secret()))
    good = api.post(f"{PREFIX}/pair", json=body)
    wrong = api.get(
        f"{PREFIX}/self",
        headers={"Authorization": f"Bearer {credential_id}.{new_secret()}"},
    )
    ticket = body["ticket"]
    for response in (bad, good, wrong):
        assert secret not in response.text and ticket not in response.text
    assert secret not in caplog.text and ticket not in caplog.text
    from taskpaw_v3.agent.server.worker_api import PairBody

    assert secret not in repr(PairBody.model_validate(body))


# ── S-1, S-7: launcher wiring ───────────────────────────────────────────
def _free_ports(n):
    socks = [socket.socket() for _ in range(n)]
    try:
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in socks]
    finally:
        for sock in socks:
            sock.close()


def _agent_config(**workflow):
    from taskpaw_v3.core.config import AgentConfig

    read, control, worker = _free_ports(3)
    return AgentConfig(
        server_id="s",
        machine="m",
        host_metrics=False,
        bind_port=read,
        control_port=control,
        workflow={"worker_api_port": worker, **workflow},
    )


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "8.8.8.8"])
def test_a_wildcard_or_public_worker_api_is_refused_before_any_claim(monkeypatch, bind):
    from taskpaw_v3.agent.server import launcher

    claimed = []
    monkeypatch.setattr(launcher, "guard_bind_exposure", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "claim_port", lambda *a: claimed.append(a))
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: claimed.append(a)
    )
    config = _agent_config(enabled=True, remote_workers=True).model_copy(
        update={"bind_host": bind}
    )
    with pytest.raises(ValueError):
        launcher.run_agent(config, block=False)
    assert claimed == []


def test_default_config_claims_no_worker_api_port(monkeypatch):
    from taskpaw_v3.agent.server import launcher

    seen = []
    monkeypatch.setattr(
        launcher,
        "reclaim_ports_from_stale_instance",
        lambda ports, **k: seen.append([p[2] for p in ports]),
    )

    def claim(host, port, what):
        raise launcher.PortInUseError(what)

    monkeypatch.setattr(launcher, "claim_port", claim)
    for workflow in (
        {},
        {"enabled": True},
        {"enabled": True, "role": "worker", "remote_workers": True},
    ):
        with pytest.raises(launcher.PortInUseError):
            launcher.run_agent(_agent_config(**workflow), block=False)
    assert seen == [["agent network API", "agent control API"]] * 3


def _rebind(port: int) -> None:
    """Bind `port` to prove no listener holds it. On POSIX the port may sit in
    TIME_WAIT after the test's own request, so the probe sets SO_REUSEADDR as
    uvicorn's listener does (a live listener still refuses it). Never on
    Windows, where SO_REUSEADDR would steal a port in use."""
    probe = socket.socket()
    try:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_worker_api_collision_fails_loudly_and_releases_the_others(monkeypatch):
    from taskpaw_v3.agent.server import launcher

    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    config = _agent_config(enabled=True, remote_workers=True)
    taken = socket.socket()
    taken.bind(("127.0.0.1", config.workflow.worker_api_port))
    taken.listen()
    try:
        with pytest.raises(launcher.PortInUseError):
            launcher.run_agent(config, block=False)
    finally:
        taken.close()
    for port in (config.bind_port, config.control_port):
        _rebind(port)  # released after the failure


def test_the_worker_api_serves_and_its_socket_closes_on_shutdown(monkeypatch, tmp_path):
    import json
    import urllib.error
    import urllib.request

    from taskpaw_v3.agent.server import launcher

    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    # enabled without an INBOX: no workflow host, so the API answers 503
    config = _agent_config(enabled=True, remote_workers=True)
    shutdown = launcher.run_agent(
        config, config_path=tmp_path / "agent.yaml", block=False
    )
    port = config.workflow.worker_api_port
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{PREFIX}/self",
            headers={"Authorization": f"Bearer {uuid.uuid4()}.{new_secret()}"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=10
            )
        assert exc.value.code == 503
        assert json.loads(exc.value.read()) == {"code": "controller_starting"}
    finally:
        shutdown.shutdown()
        assert shutdown.stopped.wait(30)
    _rebind(port)  # S-7: the 5682 socket is closed


def test_release_covers_an_incomplete_download(api, rt):
    """§11 CX8: a half-fetched revision is reported (flagged) and the same
    release rule names it; one still routed to this worker is kept."""
    joined, boot = enrol(api, rt)
    routed, idle = add_film(rt), add_film(rt)
    route(rt, routed, joined.worker_id)
    staged = [
        {"film": routed, "rev": 1, "incomplete": True},
        {"film": idle, "rev": 2, "incomplete": True},
    ]
    data = api.post(
        f"{PREFIX}/heartbeat", json=beat(boot, staged=staged), headers=joined.headers
    ).json()
    assert data["release"] == [{"film": idle, "rev": 2}]
