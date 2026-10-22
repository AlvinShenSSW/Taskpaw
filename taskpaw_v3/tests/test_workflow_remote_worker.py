"""P2-2 (#293): the worker side over a real loopback server (§7 fake worker).

A real `WorkerClient`/`WorkerHost` against the real worker API on a loopback
uvicorn server bound to an ephemeral port: it joins with the 设备 page's code,
probes, is approved, boots and shows online on 设备; no claim is possible.
Then the client-side transfer resumes, the code and staging rules, identity
hygiene, revoke and shutdown.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.agent.server.worker_api import create_worker_app
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.core.net import claim_port
from taskpaw_v3.tests.test_workflow_local_dispatch import runtime
from taskpaw_v3.tests.test_workflow_transfer import (
    OUTPUT,
    VIDEO,
    Free,
    real_film,
    sha,
    transfers,
)
from taskpaw_v3.tests.worker_api_helpers import (
    VERSION,
    Host,
    StepClock,
    probe,
    profiles,
)
from taskpaw_v3.tests.workflow_route_helpers import (
    Remote,
    boot,
    claim_remote,
    resolve_open,
    route,
)
from taskpaw_v3.workflow import devices
from taskpaw_v3.workflow import staging as st
from taskpaw_v3.workflow import transfer as tr
from taskpaw_v3.workflow.remote_worker import (
    IDENTITY_NAME,
    PENDING_NAME,
    JoinError,
    Probe,
    WorkerHost,
    WorkerIdentity,
    parse_code,
    probe_changed,
)
from taskpaw_v3.workflow.transfer import EMPTY_SHA256
from taskpaw_v3.workflow.worker_client import ApiError, WorkerClient, backoff

TICKET = "A" * 43
PAIRING = "00000000-0000-4000-8000-000000000001"


# ── the loopback controller ─────────────────────────────────────────────
IDLE = 1.0  # the test server drops a stream after 1 s without progress (IR4)


class Server:
    def __init__(self, host: Host, **app: object) -> None:
        self.sock = claim_port("127.0.0.1", 0, "test worker API")
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_worker_app(lambda: host, idle_seconds=IDLE, **app),
                log_level="warning",
                access_log=False,
            )
        )
        self.thread = threading.Thread(
            target=lambda: self.server.run(sockets=[self.sock]), daemon=True
        )
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.01)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.sock.close()
        assert not self.thread.is_alive()


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
def free():
    return Free()


@pytest.fixture
def host(rt, free):
    return Host(rt, transfers=transfers(rt, free))


@pytest.fixture
def server(host):
    value = Server(host)
    try:
        yield value
    finally:
        value.stop()


def fake_probe(vram=16311, worker="fake"):
    def run(settings, staging):
        return Probe(probe(worker, vram=vram), ("standard", "good"), True)

    return run


def worker_host(tmp_path, *, probe=None, **kw) -> WorkerHost:
    settings = WorkflowSettings(enabled=True, role="worker", staging_cap_gb=500)
    return WorkerHost(
        settings,
        tmp_path / "worker",
        probe=probe or fake_probe(),
        gpu_live=lambda: {
            "used_mib": 512,
            "util": 1.0,
            "power_w": 18.0,
            "hold": "none",
        },
        version=VERSION,
        active_seconds=0.05,
        idle_seconds=0.05,
        backoff_base=0.02,
        **{"staging_free": lambda path: 2_000 * 10**9, **kw},  # never the host disk
    )


def until(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def code_for(rt, server, name="5060 Ti-1"):
    return devices.add_device(
        rt, name, "192.168.1.31", host="127.0.0.1", port=server.port
    )


def row(rt, host, worker_id):
    view = devices.device_view(rt, host.fleet, remote_workers=True)
    return next(r for r in view["workers"] if r["worker_id"] == worker_id)


# ── §7: the fake worker ─────────────────────────────────────────────────
def test_a_fake_worker_pairs_probes_is_approved_boots_and_shows_online(
    tmp_path, rt, host, server
):
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    try:
        status = worker.join(issued["code"], str(tmp_path / "staging"))
        assert status["worker_id"] == issued["worker_id"]
        assert status["credential"] == "valid"
        assert issued["code"] not in json.dumps(status)
        until(lambda: worker.state == "running")
        assert (
            row(rt, host, issued["worker_id"])["enrollment_state"]
            == "pending_validation"
        )
        version = row(rt, host, issued["worker_id"])["version"]
        devices.approve(rt, issued["worker_id"], version, restore=True, asr=True)
        until(
            lambda: row(rt, host, issued["worker_id"])["heartbeat"]["state"] == "online"
        )
        shown = row(rt, host, issued["worker_id"])
        assert (shown["enrollment_state"], shown["admission_state"]) == (
            "verified",
            "enabled",
        )
        assert shown["eligibility"] == "可接单"
        assert shown["gpu"]["power_w"] == 18.0 and shown["tiers_ready"] == [
            "standard",
            "good",
        ]
        assert shown["agent_version"] == VERSION and shown["boot_reconciled"] is True
        # no claim is possible: there is no claim route in P2-2
        bearer = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/worker/v1/claim",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {bearer.credential_id}.{bearer.secret.get_secret_value()}"
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                request, timeout=10
            )
        assert exc.value.code == 404
    finally:
        assert worker.stop()
    # a restart: a new boot, the same probe is not posted again (stays verified)
    again = worker_host(tmp_path)
    try:
        again.start()
        until(lambda: again.state == "running")
        assert again.boot_id != worker.boot_id
        shown = row(rt, host, issued["worker_id"])
        assert shown["enrollment_state"] == "verified"
        assert (
            rt.store.get_worker(rt.operator, issued["worker_id"]).worker.boot_id
            == again.boot_id
        )
    finally:
        assert again.stop()


def test_a_changed_probe_is_posted_and_needs_a_new_approval(tmp_path, rt, host, server):
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    worker.join(issued["code"], str(tmp_path / "staging"))
    until(lambda: worker.state == "running")
    devices.approve(
        rt,
        issued["worker_id"],
        row(rt, host, issued["worker_id"])["version"],
        restore=True,
        asr=True,
    )
    assert worker.stop()
    changed = worker_host(tmp_path, probe=fake_probe(vram=16303, worker="other"))
    try:
        changed.start()
        until(lambda: changed.state == "running")
        assert (
            row(rt, host, issued["worker_id"])["enrollment_state"]
            == "pending_validation"
        )
    finally:
        assert changed.stop()


def test_a_lost_pair_reply_recovers_through_self(tmp_path, rt, host, server):
    issued = code_for(rt, server)

    class LossyClient(WorkerClient):
        def pair(self, *args, **kw):
            super().pair(*args, **kw)
            raise ApiError(0, "unreachable")  # the reply never arrived

    worker = worker_host(tmp_path, client_factory=LossyClient)
    try:
        status = worker.join(issued["code"], str(tmp_path / "staging"))
        assert status["worker_id"] == issued["worker_id"]
        until(lambda: worker.state == "running")
    finally:
        worker.stop()


def test_the_same_code_after_an_unreachable_reply_reuses_the_credential(
    tmp_path, rt, host, server
):
    issued = code_for(rt, server)

    class Blind(WorkerClient):
        def pair(self, *args, **kw):
            super().pair(*args, **kw)
            raise ApiError(0, "unreachable")

        def self_view(self):
            raise ApiError(0, "unreachable")  # still down: the join fails

    first = worker_host(tmp_path, client_factory=Blind)
    with pytest.raises(JoinError):
        first.join(issued["code"], str(tmp_path / "staging"))
    # IR7: the unconfirmed credential waits beside the (absent) identity
    assert not (tmp_path / "worker" / IDENTITY_NAME).exists()
    stored = WorkerIdentity.load(tmp_path / "worker" / PENDING_NAME)
    assert stored is not None and stored.worker_id is None
    assert first.status()["pending"]  # CX16: held and checked by the loop
    assert first.stop()
    second = worker_host(tmp_path)
    try:
        status = second.join(issued["code"], str(tmp_path / "staging"))
        assert status["worker_id"] == issued["worker_id"]
        loaded = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
        assert loaded.credential_id == stored.credential_id and loaded.ticket is None
    finally:
        second.stop()


def test_a_used_or_wrong_code_is_refused_without_echo(tmp_path, rt, server):
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    worker.join(issued["code"], str(tmp_path / "staging"))
    worker.stop()
    other = worker_host(tmp_path / "b")
    with pytest.raises(JoinError) as exc:
        other.join(issued["code"], str(tmp_path / "staging-b"))
    assert "已使用" in str(exc.value) and issued["code"] not in str(exc.value)


def test_revocation_stops_the_worker(tmp_path, rt, host, server):
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    try:
        worker.join(issued["code"], str(tmp_path / "staging"))
        until(lambda: worker.state == "running")
        devices.revoke(
            rt, issued["worker_id"], row(rt, host, issued["worker_id"])["version"]
        )
        until(lambda: worker.state == "revoked")
        assert worker.status()["credential"] == "revoked"
    finally:
        assert worker.stop()


def test_an_unreachable_controller_backs_off_and_stops_promptly(tmp_path):
    sock = claim_port("127.0.0.1", 0, "closed")
    port = sock.getsockname()[1]
    sock.close()
    ident = WorkerIdentity(
        controller_host="127.0.0.1",
        controller_port=port,
        pairing_id=PAIRING,
        credential_id="00000000-0000-4000-8000-000000000002",
        secret=SecretStr("s" * 43),
        staging_dir=str(tmp_path / "staging"),
        worker_id="00000000-0000-4000-8000-000000000003",
    )
    (tmp_path / "worker").mkdir()
    ident.save(tmp_path / "worker" / IDENTITY_NAME)
    worker = worker_host(tmp_path)
    worker.start()
    until(lambda: "重试" in worker.reason)
    started = time.monotonic()
    assert worker.stop(timeout=10)
    assert time.monotonic() - started < 5
    assert worker._thread is not None and not worker._thread.is_alive()


# ── codes, identity, staging ────────────────────────────────────────────
@pytest.mark.parametrize(
    "code,host",
    [
        (f"TP1:192.168.1.20:5682:{PAIRING}:{TICKET}", "192.168.1.20"),
        (f"TP1:[fd00::20]:5682:{PAIRING}:{TICKET}", "fd00::20"),
        (f"  TP1:controller.lan:5682:{PAIRING}:{TICKET}\n", "controller.lan"),
    ],
)
def test_codes_parse(code, host):
    parsed = parse_code(code)
    assert (parsed.host, parsed.port, parsed.pairing_id) == (host, 5682, PAIRING)
    assert parsed.ticket.get_secret_value() == TICKET
    assert TICKET not in repr(parsed)


@pytest.mark.parametrize(
    "code",
    [
        "",
        f"TP2:192.168.1.20:5682:{PAIRING}:{TICKET}",
        f"TP1:192.168.1.20:99999:{PAIRING}:{TICKET}",
        f"TP1:192.168.1.20:5682:not-a-uuid:{TICKET}",
        f"TP1:192.168.1.20:5682:{PAIRING}:short",
        f"TP1:fd00::20:5682:{PAIRING}:{TICKET}",
        f"TP1:0.0.0.0:5682:{PAIRING}:{TICKET}",
        f"TP1:[::]:5682:{PAIRING}:{TICKET}",
        f"TP1:8.8.8.8:5682:{PAIRING}:{TICKET}",
    ],
)
def test_bad_codes_are_refused_without_echo(code):
    with pytest.raises(JoinError) as exc:
        parse_code(code)
    assert TICKET not in str(exc.value)


def test_the_identity_file_is_atomic_and_repr_hides_secrets(tmp_path):
    ident = WorkerIdentity(
        controller_host="192.168.1.20",
        controller_port=5682,
        pairing_id=PAIRING,
        credential_id="00000000-0000-4000-8000-000000000002",
        secret=SecretStr("S" * 43),
        staging_dir=str(tmp_path),
        ticket=SecretStr(TICKET),
    )
    path = tmp_path / IDENTITY_NAME
    ident.save(path)
    assert "S" * 43 not in repr(ident) and TICKET not in repr(ident)
    loaded = WorkerIdentity.load(path)
    assert loaded.secret.get_secret_value() == "S" * 43
    assert [p.name for p in tmp_path.iterdir()] == [IDENTITY_NAME]  # no temp left
    path.write_text("{", encoding="utf-8")
    assert WorkerIdentity.load(path) is None


@pytest.mark.parametrize(
    "value", ["", "relative/dir", "\\\\nas\\share\\staging", "//nas/share", "staging"]
)
def test_staging_must_be_a_local_absolute_dir(value):
    with pytest.raises(st.StagingError):
        st.validate_dir(value)


def test_a_network_drive_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "_network_drive", lambda path: True)
    with pytest.raises(st.StagingError):
        st.validate_dir(str(tmp_path))


def test_staging_space_rules():
    gb = 10**9
    assert st.need(100 * gb) == 222 * gb
    assert st.admits(0, 222 * gb, 500 * gb, 400 * gb)
    assert not st.admits(300 * gb, 222 * gb, 500 * gb, 2000 * gb)  # over the cap
    assert not st.admits(0, 222 * gb, 500 * gb, 271 * gb)  # under 50 GB left


def test_probe_changes_compare_identity_vram_and_profiles():
    body = probe("w")
    record = {"capability": {"probed": {**body, "revision": 1, "profiles": profiles()}}}
    assert not probe_changed(body, record)
    assert probe_changed(dict(body, vram_mib=8151), record)
    assert probe_changed(dict(body, gpu_identity="other"), record)
    assert probe_changed(dict(body, profiles=profiles(("asr",))), record)
    assert probe_changed(body, {"capability": None})


# ── client transfers over the real server ──────────────────────────────
@pytest.fixture
def enrolled(tmp_path, rt, host, server):
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    worker.join(issued["code"], str(tmp_path / "staging"))
    until(lambda: worker.state == "running")
    devices.approve(
        rt,
        issued["worker_id"],
        row(rt, host, issued["worker_id"])["version"],
        restore=True,
        asr=True,
    )
    worker.stop()
    until(lambda: rt.store.get_worker(rt.operator, issued["worker_id"]).worker.boot_id)
    ident = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
    client = WorkerClient("127.0.0.1", server.port, ident.credential, timeout=10)
    film_id, manifest = real_film(rt)
    route(rt, film_id, issued["worker_id"])
    staging = st.Staging(tmp_path / "staging", 500, free_bytes=lambda p: 2_000 * 10**9)
    return client, staging, film_id, manifest, issued["worker_id"], worker


def test_download_resumes_and_verifies(enrolled):
    client, staging, film_id, manifest, _, _ = enrolled
    envelope = client.envelope(film_id, 1)
    video = next(c for c in envelope["workcopy"] if c["relative_path"].endswith(".mkv"))
    target = staging.work_dir / video["relative_path"]
    target.parent.mkdir(parents=True)
    cut = random.Random(9).randrange(1, len(VIDEO))
    (target.parent / (target.name + ".part")).write_bytes(VIDEO[:cut])
    seen = []
    paths = client.download_film(
        envelope, staging, on_progress=lambda d, t: seen.append(d)
    )
    assert target.read_bytes() == VIDEO and target in paths
    assert not (target.parent / (target.name + ".part")).exists()
    assert seen and seen[-1] == sum(c["size"] for c in envelope["workcopy"])
    # a corrupt prefix: whole-file mismatch, deleted and fetched again
    target.unlink()
    (target.parent / (target.name + ".part")).write_bytes(b"\x00" * cut)
    client.download_film(envelope, staging)
    assert target.read_bytes() == VIDEO
    # an existing verified file is kept; a changed one is replaced
    target.write_bytes(b"x" * len(VIDEO))
    client.download_film(envelope, staging)
    assert target.read_bytes() == VIDEO


def test_download_refuses_when_staging_is_full(enrolled):
    client, staging, film_id, _, _, _ = enrolled
    full = st.Staging(staging.root, 500, free_bytes=lambda p: 10 * 10**9)
    with pytest.raises(st.StagingError) as exc:
        client.download_film(client.envelope(film_id, 1), full)
    assert str(exc.value) == "空间不足"


def test_a_traversing_envelope_is_refused(enrolled):
    client, staging, film_id, _, _, _ = enrolled
    envelope = client.envelope(film_id, 1)
    envelope["workcopy"][0]["relative_path"] = "../escape.mkv"
    with pytest.raises(ApiError) as exc:
        client.download_film(envelope, staging)
    assert exc.value.code == "invalid_envelope"


def test_upload_resumes_across_a_controller_restart(enrolled, rt, host, free, tmp_path):
    client, staging, film_id, _, worker_id, worker = enrolled
    resource = rt.store.get_worker(
        rt.operator, worker_id
    ).worker.physical_binding.resource_id
    ident = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
    from taskpaw_v3.workflow.auth import authenticate

    auth = authenticate(rt.store, ident.credential_id, ident.secret)
    remote = Remote(worker_id, "5060 Ti-1", auth, worker.boot_id, resource)
    _, command = claim_remote(rt, remote, film_id, "restore")
    a = command.attempt_id
    path = f"{a}/movie-破解.mp4"
    local = tmp_path / "out.mp4"
    local.write_bytes(OUTPUT)
    other = f"{a}/b.srt"
    client.plan_outputs(
        a,
        [
            {"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)},
            {"path": other, "size": 100, "sha256": sha(b"z" * 100)},
        ],
    )
    # a first stream cut short, then a new controller process
    request = urllib.request.Request(
        f"{client.base}/attempts/{a}/files/{urllib.request.quote(path)}",
        data=OUTPUT[:12345],
        method="PUT",
        headers={
            **client._headers(True),
            "Content-Range": f"bytes 0-{len(OUTPUT) - 1}/{len(OUTPUT)}",
        },
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=10
        )
    host.transfers = transfers(rt, free)
    assert client.outputs(a)["files"][0]["received"] == 12345
    done = client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
    assert done["done"] is True and (rt.work_dir / path).read_bytes() == OUTPUT
    assert client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))[
        "done"
    ]
    # a local file that does not match its plan is re-sent whole, then refused
    bad = tmp_path / "bad.srt"
    bad.write_bytes(b"y" * 100)
    with pytest.raises(ApiError) as exc:
        client.upload_file(a, other, bad, size=100, sha256=sha(b"z" * 100))
    assert exc.value.code == "hash_mismatch"
    entry = next(f for f in client.outputs(a)["files"] if f["path"] == other)
    assert (entry["mismatches"], entry["received"], entry["done"]) == (3, 0, False)


# ── the worker console routes ───────────────────────────────────────────
def _control(settings, provider):
    config = AgentConfig(server_id="s", machine="m", workflow=settings)
    return TestClient(
        create_control_app(
            config,
            control_token="c",
            control_active=lambda: True,
            worker_provider=provider,
        ),
        headers={"Authorization": "Bearer c"},
    )


def test_the_console_is_off_on_a_controller():
    ctl = _control(WorkflowSettings(enabled=True), lambda: None)
    data = ctl.get("/control/workflow/worker").json()
    assert data["state"] == "off" and data["role"] == "controller"
    join = {"code": f"TP1:192.168.1.20:5682:{PAIRING}:{TICKET}", "staging_dir": "D:/S"}
    assert ctl.post("/control/workflow/worker/join", json=join).status_code == 409


def test_join_through_the_console(tmp_path, rt, host, server):
    worker = worker_host(tmp_path)
    settings = WorkflowSettings(enabled=True, role="worker", staging_dir="E:/staging")
    ctl = _control(settings, lambda: worker)
    try:
        assert ctl.get("/control/workflow/worker").json()["state"] == "unjoined"
        bad = ctl.post(
            "/control/workflow/worker/join",
            json={
                "code": f"TP1:8.8.8.8:5682:{PAIRING}:{TICKET}",
                "staging_dir": str(tmp_path),
            },
        )
        assert bad.status_code == 422 and TICKET not in bad.text
        relative = ctl.post(
            "/control/workflow/worker/join",
            json={"code": code_for(rt, server)["code"], "staging_dir": "relative"},
        )
        assert relative.status_code == 422
        secret_code = "TP1:" + "x" * 2000
        for body in (
            {"code": secret_code, "staging_dir": "D:/S"},
            {"code": 5},
            [1],
            None,
        ):
            refused = ctl.post("/control/workflow/worker/join", json=body)
            assert refused.status_code == 422 and "xxxx" not in refused.text
        issued = code_for(rt, server, "second")
        joined = ctl.post(
            "/control/workflow/worker/join",
            json={"code": issued["code"], "staging_dir": str(tmp_path / "s")},
        )
        assert joined.status_code == 200
        body = joined.json()
        assert body["worker_id"] == issued["worker_id"] and body["role"] == "worker"
        assert issued["code"].rsplit(":", 1)[1] not in joined.text
        assert Path(body["staging_dir"]) == tmp_path / "s"
    finally:
        worker.stop()


# ── launcher: role=worker (S-5, S-7) ────────────────────────────────────
def test_a_worker_agent_starts_the_worker_host_and_joins_it_on_shutdown(
    monkeypatch, tmp_path
):
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.tests.test_worker_api import _agent_config
    from taskpaw_v3.workflow import local_host, remote_worker

    started = {}

    def fake_start(settings, config_dir):
        host = WorkerHost(settings, config_dir / "workflow", probe=fake_probe())
        host.start()
        started["host"] = host
        return host

    monkeypatch.setattr(remote_worker, "start_worker_host", fake_start)
    monkeypatch.setattr(
        local_host,
        "start_local_workflow",
        lambda *a, **k: pytest.fail("a worker runs no controller workflow"),
    )
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    config = _agent_config(enabled=True, role="worker", remote_workers=True)
    shutdown = launcher.run_agent(
        config, config_path=tmp_path / "agent.yaml", block=False
    )
    try:
        host = started["host"]
        assert host.state == "unjoined"
    finally:
        shutdown.shutdown()
        assert shutdown.stopped.wait(30)
    assert host._stop.is_set()


# ── review fixes: the client (IR2, IR3, IR4, IR11, IR12, IR16, CX1, CX5) ──
def _attempt(enrolled, rt, tmp_path):
    client, _, film_id, _, worker_id, worker = enrolled
    resource = rt.store.get_worker(
        rt.operator, worker_id
    ).worker.physical_binding.resource_id
    ident = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
    from taskpaw_v3.workflow.auth import authenticate

    auth = authenticate(rt.store, ident.credential_id, ident.secret)
    remote = Remote(worker_id, "5060 Ti-1", auth, worker.boot_id, resource)
    claimed, command = claim_remote(rt, remote, film_id, "restore")
    a = command.attempt_id
    path = f"{a}/movie.mp4"
    local = tmp_path / "out.mp4"
    local.write_bytes(OUTPUT)
    client.plan_outputs(a, [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}])
    return client, remote, claimed, a, path, local


def test_backoff_never_overflows():
    assert backoff(1024) == 30.0 and backoff(10**9, base=0.02) == 30.0  # CX1


def test_an_empty_work_copy_is_staged(enrolled):
    """CX5: nothing to fetch, still renamed into place."""
    client, staging, film_id, _, _, _ = enrolled
    envelope = {
        "film": film_id,
        "rev": 1,
        "workcopy": [
            {
                "item_id": "00000000-0000-4000-8000-0000000000e1",
                "relative_path": "intake/x/empty.srt",
                "size": 0,
                "sha256": EMPTY_SHA256,
            }
        ],
    }
    [path] = client.download_film(envelope, staging)
    assert path.read_bytes() == b"" and not path.with_name("empty.srt.part").exists()


def test_already_staged_bytes_are_not_needed_twice(tmp_path, monkeypatch):
    """IR16: 90 GB of a 100 GB film is staged; the rest fits the free space."""
    gb = 10**9
    staging = st.Staging(tmp_path, 500, free_bytes=lambda p: 250 * gb)
    monkeypatch.setattr(staging, "used", lambda: 90 * gb)
    staging.admit(100 * gb, 90 * gb)
    with pytest.raises(st.StagingError):
        staging.admit(100 * gb, 0)


def test_a_received_but_uncommitted_upload_is_not_done(
    enrolled, rt, tmp_path, monkeypatch
):
    """IR2: a refused rename is `commit_pending` (transient), never success;
    the next call commits it."""
    client, _, _, a, path, local = _attempt(enrolled, rt, tmp_path)
    replace = tr.os.replace

    def refusing(src, dst):
        if str(src).endswith(".part"):
            raise PermissionError(13, "in use")
        return replace(src, dst)

    monkeypatch.setattr(tr.os, "replace", refusing)
    with pytest.raises(ApiError) as exc:
        client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
    assert exc.value.code == "commit_pending" and exc.value.transient
    with pytest.raises(ApiError) as again:  # all received, still not committed
        client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
    assert again.value.code == "commit_pending"
    assert not (rt.work_dir / path).exists()
    monkeypatch.setattr(tr.os, "replace", replace)
    done = client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
    assert done["done"] is True and (rt.work_dir / path).read_bytes() == OUTPUT


def test_a_refusal_before_the_body_is_read_surfaces_its_code(enrolled, rt, tmp_path):
    """IR12: the server refused and reset the stream; GET outputs reads why."""
    client, remote, claimed, a, path, local = _attempt(enrolled, rt, tmp_path)

    class Reset(WorkerClient):
        def open(self, method, path, **kw):
            if method == "PUT":
                boot(rt, remote, "00000000-0000-4000-8000-0000000000b2")
                resolve_open(rt, claimed.assignment_id, "failed")
                raise ApiError(0, "unreachable")  # the reset hid the 409
            return super().open(method, path, **kw)

    reset = Reset("127.0.0.1", client.port, client._credential, timeout=10)
    with pytest.raises(ApiError) as exc:
        reset.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
    assert exc.value.code == "stale_attempt"


def _cut(client, a, path, raw, total):
    """A first stream cut short: the server keeps what arrived."""
    request = urllib.request.Request(
        f"{client.base}/attempts/{a}/files/{urllib.request.quote(path)}",
        data=raw,
        method="PUT",
        headers={
            **client._headers(True),
            "Content-Range": f"bytes 0-{total - 1}/{total}",
        },
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=10
        )


def test_a_large_prefix_resumes_after_a_real_restart_without_a_put_timeout(
    enrolled, rt, free, server, tmp_path, monkeypatch
):
    """IR3/IR18: a new app instance on the same dirs, a prefix whose re-hash
    takes longer than the client's socket timeout. GET outputs (long
    timeout) rebuilds the hash; the PUT streams at once."""
    client, _, _, a, path, local = _attempt(enrolled, rt, tmp_path)
    _cut(client, a, path, OUTPUT[:150_000], len(OUTPUT))
    server.stop()
    restarted = Server(Host(rt, transfers=transfers(rt, free)))
    real = tr.hash_prefix
    calls = []

    def slow(part, size):
        calls.append(size)
        time.sleep(1.5)  # a 40 GB prefix
        return real(part, size)

    monkeypatch.setattr(tr, "hash_prefix", slow)
    try:
        quick = WorkerClient(
            "127.0.0.1",
            restarted.port,
            client._credential,
            timeout=0.5,
            verify_timeout=30,
        )
        done = quick.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
        assert done["done"] is True and (rt.work_dir / path).read_bytes() == OUTPUT
        assert calls == [150_000]  # once, in GET outputs
    finally:
        restarted.stop()


def _raw_put(port, client, a, path, total, first):
    import socket

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    head = (
        f"PUT {PREFIX}/attempts/{a}/files/{urllib.request.quote(path)} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Authorization: {client._headers(True)['Authorization']}\r\n"
        f"Content-Range: bytes 0-{total - 1}/{total}\r\n"
        f"Content-Length: {total}\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    )
    sock.sendall(head.encode() + first)
    return sock


PREFIX = "/worker/v1"


def _reply(sock):
    """The status and body of one HTTP/1.1 reply read off a raw socket."""
    crlf = b"\r\n"
    data = b""
    while crlf * 2 not in data:
        chunk = sock.recv(4096)
        assert chunk, "the server closed without a reply"
        data += chunk
    head, _, body = data.partition(crlf * 2)
    lines = head.decode("latin-1").split("\r\n")
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.lower() == "content-length":
            length = int(value)
    while len(body) < length:
        chunk = sock.recv(4096)
        assert chunk, "the server closed mid-reply"
        body += chunk
    return int(lines[0].split()[1]), body


class _Errors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_a_client_disconnect_keeps_its_bytes_and_logs_no_error(
    enrolled, rt, host, server, tmp_path
):
    """IR11: a worker that vanishes mid-PUT: no 500, no traceback, and every
    byte already received is kept for the resume."""
    client, _, _, a, path, local = _attempt(enrolled, rt, tmp_path)
    errors = _Errors()
    for name in ("uvicorn.error", "taskpaw"):
        logging.getLogger(name).addHandler(errors)
    try:
        sock = _raw_put(server.port, client, a, path, len(OUTPUT), OUTPUT[:50_000])
        time.sleep(0.2)
        sock.close()
        until(lambda: not host.transfers._streams)
        assert client.outputs(a)["files"][0]["received"] == 50_000
    finally:
        for name in ("uvicorn.error", "taskpaw"):
            logging.getLogger(name).removeHandler(errors)
    assert errors.records == []
    done = client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
    assert done["done"] is True


def test_a_stalled_upload_frees_its_slot_and_a_concurrent_put_is_429(
    enrolled, rt, host, tmp_path
):
    """IR4/IR18: a second PUT while the first streams is 429; a stream with no
    progress for the idle time is dropped, keeping what arrived.

    The idle timeout is the app's `bound` seam, tripped by an event rather than
    a wall-clock wait: the stall lasts exactly until the 429 is observed, so
    no scheduler delay can free the slot early or hold it late."""
    import asyncio

    idle = threading.Event()  # set = the idle time has now elapsed
    waits = []  # one entry per body read the stalled PUT has started

    async def bound(read, seconds):
        assert seconds == IDLE
        waits.append(seconds)
        task = asyncio.ensure_future(read)
        try:
            while not task.done():
                if idle.is_set():
                    raise asyncio.TimeoutError
                await asyncio.wait({task}, timeout=0.005)
            return task.result()
        finally:
            task.cancel()

    stalled = Server(host, bound=bound)
    client, _, _, a, path, local = _attempt(enrolled, rt, tmp_path)
    sock = _raw_put(stalled.port, client, a, path, len(OUTPUT), OUTPUT[:1000])
    try:
        until(lambda: host.transfers._streams and len(waits) >= 2)  # body consumed
        # a head with no body: the 429 is read whole (a full upload would race
        # the server closing on its refusal and surface as a reset instead)
        second = _raw_put(stalled.port, client, a, path, len(OUTPUT), b"")
        try:
            status, body = _reply(second)
        finally:
            second.close()
        assert status == 429 and b'"busy"' in body
        assert host.transfers._streams  # still held until the idle time elapses
        idle.set()
        until(lambda: not host.transfers._streams)
        assert client.outputs(a)["files"][0]["received"] == 1000
        done = client.upload_file(a, path, local, size=len(OUTPUT), sha256=sha(OUTPUT))
        assert done["done"] is True and (rt.work_dir / path).read_bytes() == OUTPUT
    finally:
        sock.close()
        stalled.stop()


def test_a_stalled_download_frees_its_slot(tmp_path):
    """IR4: a worker that stops reading never holds the download slot."""
    import asyncio

    from taskpaw_v3.agent.server.worker_api import _ReleasingFile

    data = tmp_path / "x.bin"
    data.write_bytes(b"x" * 100)
    closed = []

    async def run():
        never = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.body":
                await never.wait()

        async def receive():
            await never.wait()

        scope = {"type": "http", "method": "GET", "headers": [], "path": "/"}
        response = _ReleasingFile(data, on_close=lambda: closed.append(1), idle=0.1)
        await response(scope, receive, send)

    asyncio.run(asyncio.wait_for(run(), 60))  # a hang guard; idle trips at 0.1 s
    assert closed == [1]


def test_re_join_with_an_expired_code_keeps_the_working_identity(
    tmp_path, rt, host, server, clock
):
    """IR7: the old identity stays until the new pairing succeeds."""
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    try:
        worker.join(issued["code"], str(tmp_path / "staging"))
        until(lambda: worker.state == "running")
        before = (tmp_path / "worker" / IDENTITY_NAME).read_bytes()
        again = devices.repair(
            rt,
            issued["worker_id"],
            row(rt, host, issued["worker_id"])["version"],
            host="127.0.0.1",
            port=server.port,
        )
        clock.advance(timedelta(minutes=16))
        with pytest.raises(JoinError):
            worker.join(again["code"], str(tmp_path / "staging"))
        assert (tmp_path / "worker" / IDENTITY_NAME).read_bytes() == before
        until(lambda: worker.state == "running")
        assert worker.status()["worker_id"] == issued["worker_id"]
    finally:
        assert worker.stop()


# ── review fixes: the worker loop (IR6, IR8, IR15, CX4) ──────────────────
WORKER = "00000000-0000-4000-8000-000000000003"


class FakeController:
    """A scripted controller for the loop: each method pops a scripted reply
    or error, then repeats its default."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.beats: list[dict] = []
        self.record: dict = {"worker_id": WORKER, "capability": None}
        self.script: dict[str, list] = {}

    def __call__(self, host, port, credential, **kw):
        return self

    def _next(self, name, default):
        self.calls.append(name)
        queue = self.script.get(name) or []
        value = queue.pop(0) if queue else default
        if isinstance(value, Exception):
            raise value
        return value

    def self_view(self):
        return self._next("self", {"worker": self.record})

    def capabilities(self, body):
        reply = self._next("capabilities", {"revision": 1, "status": "applied"})
        self.record = {
            "worker_id": WORKER,
            "capability": {"probed": dict(body)},
        }
        return reply

    def boot(self, boot_id):
        return self._next("boot", {"status": "reconciled", "reconciled": True})

    def heartbeat(self, beat):
        self.beats.append(beat)
        return self._next("heartbeat", {"routes": [], "reconcile_needed": False})


def reasons(worker) -> list[str]:
    """The reason shown at each wait: every back-off and every beat."""
    seen: list[str] = []
    wait = worker._wait

    def recording(seconds):
        seen.append(worker.reason)
        return wait(seconds)

    worker._wait = recording
    return seen


def loop_worker(tmp_path, fake, probe=None, **kw):
    ident = WorkerIdentity(
        controller_host="127.0.0.1",
        controller_port=1,
        pairing_id=PAIRING,
        credential_id="00000000-0000-4000-8000-000000000002",
        secret=SecretStr("s" * 43),
        staging_dir=str(tmp_path / "staging"),
        worker_id=WORKER,
    )
    (tmp_path / "worker").mkdir(exist_ok=True)
    ident.save(tmp_path / "worker" / IDENTITY_NAME)
    return worker_host(tmp_path, probe=probe, client_factory=fake, **kw)


def test_heartbeats_run_while_the_boot_waits_for_reconcile(tmp_path):
    """IR6: 等待对账 is a state of a live worker, not a loop without beats."""
    fake = FakeController()
    waiting = {"status": "unreconciled", "reconciled": False}
    fake.script["boot"] = [waiting, waiting, waiting]
    fake.script["heartbeat"] = [{"routes": [], "reconcile_needed": True}] * 3
    worker = loop_worker(tmp_path, fake)
    seen = reasons(worker)
    try:
        worker.start()
        until(lambda: worker.reconciled)
        boots = [i for i, name in enumerate(fake.calls) if name == "boot"]
        assert fake.calls.index("heartbeat") < boots[3]  # beats before reconcile
        assert "等待对账" in seen
        until(lambda: worker.reason == "在线")
    finally:
        assert worker.stop()


def test_ordinary_errors_never_end_the_loop(tmp_path, monkeypatch):
    """IR8: staging trouble, a refused probe post and a missing GPU reading
    are reported, backed off and retried."""
    fake = FakeController()
    fake.script["capabilities"] = [ApiError(409, "cas_conflict")]
    readings = [None]

    def flaky_probe(settings, staging):
        if readings:
            return readings.pop()
        return Probe(probe("fake"), ("standard",), True)

    worker = loop_worker(tmp_path, fake, probe=flaky_probe)
    real = worker.staging
    failures = [st.StagingError("暂存目录必须是完整路径"), OSError("gone")]

    def staging():
        if failures:
            raise failures.pop(0)
        return real()

    monkeypatch.setattr(worker, "staging", staging)
    seen = reasons(worker)
    try:
        worker.start()
        until(lambda: fake.record["capability"] is not None and fake.beats)
        assert any("暂存目录不可用" in reason for reason in seen)
        assert any("主控拒绝" in reason for reason in seen)
        assert any("GPU" in reason for reason in seen)
        assert fake.calls.count("capabilities") == 2
        until(lambda: worker.state == "running")
    finally:
        assert worker.stop()


def test_a_refused_heartbeat_shows_its_reason(tmp_path):
    """IR15: a 422 is not 「无法连接主控」."""
    fake = FakeController()
    fake.script["heartbeat"] = [ApiError(422, "invalid_request")] * 50
    worker = loop_worker(tmp_path, fake)
    try:
        worker.start()
        until(lambda: "invalid_request" in worker.reason)
        assert "心跳" in worker.reason
    finally:
        assert worker.stop()


def test_readiness_is_refreshed_without_reposting_an_unchanged_probe(tmp_path):
    """CX4: tiers and ASR readiness follow the disk; the probe is posted
    again only when it changed (R4)."""
    fake = FakeController()
    tiers = [("standard",)]

    def growing(settings, staging):
        return Probe(probe("fake"), tiers[-1], True)

    worker = loop_worker(tmp_path, fake, probe=growing, probe_seconds=0.1)
    try:
        worker.start()
        until(lambda: fake.beats and fake.beats[-1]["tiers_ready"] == ["standard"])
        tiers.append(("standard", "good", "high"))
        until(lambda: fake.beats[-1]["tiers_ready"] == ["standard", "good", "high"])
        assert fake.calls.count("capabilities") == 1
    finally:
        assert worker.stop()


# ── Codex round 2 (CX6, CX7, CX8) ───────────────────────────────────────
class CutServer:
    """A raw loopback server: each request gets the next scripted reply (a
    status line, headers and a body of which only `send` bytes are written
    before the connection is cut). Records every request's headers."""

    def __init__(self, replies) -> None:
        import socket

        self.replies, self.requests = list(replies), []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        for status, headers, body, send in self.replies:
            try:
                conn, _ = self.sock.accept()
            except OSError:  # stopped with replies left
                return
            with conn:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                self.requests.append(raw.decode("latin-1"))
                head = f"HTTP/1.1 {status}\r\n" + "".join(
                    f"{k}: {v}\r\n" for k, v in headers.items()
                )
                conn.sendall(head.encode() + b"\r\n" + body[:send])

    def stop(self) -> None:
        self.sock.close()


def test_a_truncated_reply_is_transient(tmp_path):
    """CX6: IncompleteRead and a cut or partial JSON body are `unreachable`."""
    body = b'{"worker_id": "abcdefghij"}'
    cut = CutServer(
        [("200 OK", {"Content-Length": str(len(body))}, body, 10)] * 2
        + [("200 OK", {"Content-Length": "5"}, b'{"wor', 5)]
    )
    try:
        client = WorkerClient("127.0.0.1", cut.port, timeout=5)
        for _ in range(2):  # a short body (IncompleteRead), then a partial JSON
            with pytest.raises(ApiError) as exc:
                client.call("GET", "/self", auth=False)
            assert exc.value.transient and exc.value.code == "unreachable"
        with pytest.raises(ApiError) as exc:
            client.call("GET", "/self", auth=False)
        assert exc.value.transient
    finally:
        cut.stop()


def test_a_pair_reply_cut_mid_body_recovers_through_self(tmp_path, rt, host, server):
    """CX6: /pair commits, the reply body is cut, /self proves the pairing."""
    import http.client

    issued = code_for(rt, server)

    class CutClient(WorkerClient):
        def open(self, method, path, **kw):
            reply = super().open(method, path, **kw)
            if path != "/pair":
                return reply
            reply.read()  # the server committed; the client never sees it

            class Cut:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self, *args):
                    raise http.client.IncompleteRead(b"{", 20)

            return Cut()

    worker = worker_host(tmp_path, client_factory=CutClient)
    try:
        status = worker.join(issued["code"], str(tmp_path / "staging"))
        assert status["worker_id"] == issued["worker_id"]
        until(lambda: worker.state == "running")
    finally:
        worker.stop()


def test_a_download_cut_by_an_early_eof_keeps_its_part_and_resumes(tmp_path):
    """CX7: 100 KB cut after 10 KB: the part stays, the next request is a
    Range from 10240, and the file completes."""
    data = random.Random(7).randbytes(100 * 1024)
    digest = sha(data)
    envelope = {
        "film": "f",
        "rev": 1,
        "workcopy": [
            {
                "item_id": "00000000-0000-4000-8000-0000000000aa",
                "relative_path": "a.bin",
                "size": len(data),
                "sha256": digest,
            }
        ],
    }
    base = {"X-Sha256": digest, "X-Size": str(len(data))}
    cut = CutServer(
        [
            ("200 OK", {**base, "Content-Length": str(len(data))}, data, 10240),
            (
                "206 Partial Content",
                {**base, "Content-Length": str(len(data) - 10240)},
                data[10240:],
                len(data),
            ),
        ]
    )
    staging = st.Staging(tmp_path / "staging", 500, free_bytes=lambda p: 2_000 * 10**9)
    try:
        client = WorkerClient(
            "127.0.0.1", cut.port, (PAIRING, SecretStr("s" * 43)), timeout=5
        )
        with pytest.raises(ApiError) as exc:
            client.download_film(envelope, staging)
        assert exc.value.transient
        part = staging.work_dir / "a.bin.part"
        assert part.read_bytes() == data[:10240]
        paths = client.download_film(envelope, staging)
        assert paths[0].read_bytes() == data and not part.exists()
        assert "range: bytes=10240-" in cut.requests[1].lower()
        assert "range" not in cut.requests[0].lower()
    finally:
        cut.stop()


def test_a_missing_gpu_reading_withdraws_readiness(tmp_path, rt, host, server):
    """CX8: after a good probe, losing the GPU reading clears the advertised
    tiers and ASR; 设备 shows the worker ineligible until a probe returns."""
    readings = [True]

    def flaky(settings, staging):
        if readings[-1]:
            return Probe(probe("fake"), ("standard", "good"), True)
        return None

    issued = code_for(rt, server)
    # probe_seconds=0: every beat probes again, so the change shows at the
    # next beat whatever the runner's load (no refresh interval to wait out)
    worker = worker_host(tmp_path, probe=flaky, probe_seconds=0.0)
    try:
        worker.join(issued["code"], str(tmp_path / "staging"))
        until(lambda: worker.state == "running")
        version = row(rt, host, issued["worker_id"])["version"]
        devices.approve(rt, issued["worker_id"], version, restore=True, asr=True)
        until(lambda: row(rt, host, issued["worker_id"])["eligible"])
        readings.append(False)
        shown = until(
            lambda: (
                (r := row(rt, host, issued["worker_id"]))["asr_ready"] is False and r
            )
        )
        assert shown["tiers_ready"] == [] and shown["eligible"] is False
        assert shown["eligibility"] != "可接单"
        readings.append(True)
        until(lambda: row(rt, host, issued["worker_id"])["eligible"])
    finally:
        assert worker.stop()


# ── Codex round 3 (CX9, CX10) ───────────────────────────────────────────
def test_the_config_fingerprint_is_part_of_the_probe_comparison():
    """CX9: a changed executor setting needs 核验; a stored probe without a
    fingerprint compares as changed once."""
    body = probe("w", config_digest="a" * 64)
    record = {"capability": {"probed": {**body, "revision": 1}}}
    assert not probe_changed(body, record)
    assert probe_changed(dict(body, config_digest="b" * 64), record)
    old = {"capability": {"probed": {**probe("w"), "revision": 1}}}
    assert probe_changed(body, old)


def test_a_stored_probe_without_a_fingerprint_is_reposted_once(tmp_path):
    """CX9: after an upgrade the worker posts once, then stays quiet."""
    fake = FakeController()
    fake.record = {
        "worker_id": WORKER,
        "capability": {"probed": {**probe("fake"), "revision": 1}},
    }
    digest = ["a" * 64]

    def run(settings, staging):
        return Probe(probe("fake", config_digest=digest[0]), ("standard",), True)

    worker = loop_worker(tmp_path, fake, probe=run, probe_seconds=0.05)
    try:
        worker.start()
        until(lambda: fake.calls.count("capabilities") == 1)
        time.sleep(0.4)  # several probe rounds
        assert fake.calls.count("capabilities") == 1
        digest[0] = "b" * 64
        until(lambda: fake.calls.count("capabilities") == 2)
    finally:
        assert worker.stop()


def test_the_default_probe_fingerprints_the_executor_settings(tmp_path, monkeypatch):
    """CX9: another WhisperJAV path is another fingerprint."""
    from types import SimpleNamespace

    from taskpaw_v3.workflow import local_gpu
    from taskpaw_v3.workflow.remote_worker import default_probe

    gpu = SimpleNamespace(name="GPU", vram_mib=16311, uuid="gpu-x")
    monkeypatch.setattr(local_gpu, "query_gpu", lambda: gpu)
    monkeypatch.setattr(local_gpu, "machine_identity", lambda: "device-x")
    staging = SimpleNamespace(free=lambda: 10**12)

    def digest(exe):
        settings = WorkflowSettings(
            enabled=True, role="worker", whisperjav_exe_path=str(tmp_path / exe)
        )
        return default_probe(settings, staging).body["config_digest"]

    first = digest("a.exe")
    assert len(first) == 64 and digest("a.exe") == first
    assert digest("b.exe") != first


def test_the_approve_dialog_data_carries_the_fingerprints(tmp_path, rt, host, server):
    """CX9: 设备 hands the dialog the probe's and the approved probe's digest."""
    issued = code_for(rt, server)
    first = worker_host(
        tmp_path,
        probe=lambda s, g: Probe(probe("fake", config_digest="a" * 64), (), True),
    )
    first.join(issued["code"], str(tmp_path / "staging"))
    until(lambda: first.state == "running")
    shown = row(rt, host, issued["worker_id"])
    assert shown["probe"]["config_digest"] == "a" * 64
    devices.approve(rt, issued["worker_id"], shown["version"], restore=True, asr=True)
    assert first.stop()
    second = worker_host(
        tmp_path,
        probe=lambda s, g: Probe(probe("fake", config_digest="b" * 64), (), True),
    )
    try:
        second.start()
        until(
            lambda: (
                row(rt, host, issued["worker_id"])["enrollment_state"]
                == "pending_validation"
            )
        )
        shown = row(rt, host, issued["worker_id"])
        assert shown["probe"]["config_digest"] == "b" * 64
        assert shown["approved"]["probe"]["config_digest"] == "a" * 64
    finally:
        assert second.stop()


@pytest.mark.parametrize("expires", [False, True], ids=["paired", "refused"])
def test_a_shutdown_during_a_join_leaves_no_worker_running(
    tmp_path, rt, host, server, clock, expires
):
    """CX10: stop() while /pair is in flight; the join then completes (or is
    refused and resumes the old identity). No heartbeat thread starts after
    shutdown and the identity files stay consistent."""
    issued = code_for(rt, server)
    entered, release = threading.Event(), threading.Event()
    block = threading.Event()

    class Slow(WorkerClient):
        def pair(self, *args, **kw):
            if block.is_set():
                entered.set()
                assert release.wait(10)
            return super().pair(*args, **kw)

    worker = worker_host(tmp_path, client_factory=Slow)
    code = issued["code"]
    if expires:  # a working identity first, then a re-join with an expired code
        worker.join(code, str(tmp_path / "staging"))
        until(lambda: worker.state == "running")
        code = devices.repair(
            rt,
            issued["worker_id"],
            row(rt, host, issued["worker_id"])["version"],
            host="127.0.0.1",
            port=server.port,
        )["code"]
        clock.advance(timedelta(minutes=16))
    block.set()
    outcome: list[object] = []

    def joiner():
        try:
            outcome.append(worker.join(code, str(tmp_path / "staging")))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertions
            outcome.append(exc)

    thread = threading.Thread(target=joiner)
    thread.start()
    assert entered.wait(10)
    stopped: list[bool] = []
    stopper = threading.Thread(target=lambda: stopped.append(worker.stop(10)))
    stopper.start()
    time.sleep(0.2)
    assert stopper.is_alive()  # waits for the in-flight join
    release.set()
    thread.join(10)
    stopper.join(10)
    assert not thread.is_alive() and not stopper.is_alive()
    assert stopped == [True]
    assert not any(
        t.name == "workflow-worker" and t.is_alive() for t in threading.enumerate()
    )
    assert worker._stop.is_set() and worker.state == "stopped"
    identity = tmp_path / "worker" / IDENTITY_NAME
    assert identity.exists()  # the pairing stands, or the old identity stayed
    assert (WorkerIdentity.load(identity) or pytest.fail()).worker_id == (
        issued["worker_id"]
    )
    assert not (tmp_path / "worker" / PENDING_NAME).exists() or expires
    with pytest.raises(JoinError):  # closed for good
        worker.join(code, str(tmp_path / "staging"))


# ── Codex round 4 (CX11, CX12) ──────────────────────────────────────────
def test_a_re_join_that_cannot_write_its_identity_leaves_the_worker_running(
    tmp_path, rt, host, server, monkeypatch
):
    """CX11: a disk error while writing the pending identity is a JoinError,
    not a 500, and the working loop and identity are untouched."""
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    try:
        worker.join(issued["code"], str(tmp_path / "staging"))
        until(lambda: worker.state == "running")
        before = (tmp_path / "worker" / IDENTITY_NAME).read_bytes()
        again = devices.repair(
            rt,
            issued["worker_id"],
            row(rt, host, issued["worker_id"])["version"],
            host="127.0.0.1",
            port=server.port,
        )

        def full(self, path):
            raise OSError(28, "No space left on device")

        with monkeypatch.context() as patch:
            patch.setattr(WorkerIdentity, "save", full)
            with pytest.raises(JoinError, match="无法写入身份文件"):
                worker.join(again["code"], str(tmp_path / "staging"))
        assert (tmp_path / "worker" / IDENTITY_NAME).read_bytes() == before
        assert any(
            t.name == "workflow-worker" and t.is_alive() for t in threading.enumerate()
        )
        until(lambda: worker.state == "running")
        assert worker.status()["worker_id"] == issued["worker_id"]
        # the same code still joins once the disk is back
        assert (
            worker.join(again["code"], str(tmp_path / "staging"))["worker_id"]
            == (issued["worker_id"])
        )
    finally:
        assert worker.stop()


@pytest.mark.parametrize(
    ("status", "transient"),
    [("503 Service Unavailable", True), ("401 Unauthorized", False)],
)
def test_an_error_reply_with_a_cut_body_is_still_an_api_error(status, transient):
    """CX12: a truncated error body (IncompleteRead) keeps the HTTP status."""
    body = b'{"code": "busy", "detail": "x"}'
    cut = CutServer([(status, {"Content-Length": str(len(body))}, body, 8)])
    try:
        client = WorkerClient("127.0.0.1", cut.port, timeout=5)
        with pytest.raises(ApiError) as exc:
            client.call("GET", "/self", auth=False)
        assert exc.value.status == int(status[:3])
        assert exc.value.transient is transient
        assert exc.value.code == "http_error"
    finally:
        cut.stop()


# ── identity promotion after a successful pair (CX11 follow-up) ─────────
def _break_promotion(monkeypatch, kind):
    """Make saving the confirmed identity fail (`save`) or its rename onto
    the identity file fail (`replace`) until `heal()` is called."""
    import os

    from taskpaw_v3.workflow import remote_worker as rw

    broken = threading.Event()
    broken.set()
    save, replace = WorkerIdentity.save, os.replace

    def failing_save(self, path):
        if broken.is_set() and kind == "save" and self.worker_id:
            raise OSError(28, "No space left on device")
        return save(self, path)

    def failing_replace(src, dst):
        if broken.is_set() and kind == "replace" and Path(dst).name == IDENTITY_NAME:
            raise PermissionError(13, "Access is denied")
        return replace(src, dst)

    monkeypatch.setattr(WorkerIdentity, "save", failing_save)
    monkeypatch.setattr(rw.os, "replace", failing_replace)
    return lambda: broken.clear()


@pytest.mark.parametrize("kind", ["save", "replace"])
def test_a_failed_identity_save_after_a_good_pair_keeps_the_worker_running(
    tmp_path, rt, host, server, monkeypatch, kind
):
    """The pair stands (the old credential is gone): no raw exception, the
    worker runs on the in-memory identity and promotes it on a later beat."""
    issued = code_for(rt, server)
    worker = worker_host(tmp_path)
    heal = _break_promotion(monkeypatch, kind)
    identity = tmp_path / "worker" / IDENTITY_NAME
    try:
        status = worker.join(issued["code"], str(tmp_path / "staging"))
        assert status["worker_id"] == issued["worker_id"]
        until(lambda: worker.state == "running")
        until(lambda: "身份文件未保存" in worker.reason)
        assert not identity.exists()
        heal()
        until(lambda: identity.exists() and worker.reason == "在线")
        loaded = WorkerIdentity.load(identity)
        assert loaded is not None and loaded.worker_id == issued["worker_id"]
        assert loaded.ticket is None
        assert not (tmp_path / "worker" / PENDING_NAME).exists()
    finally:
        assert worker.stop()


@pytest.mark.parametrize("kind", ["save", "replace"])
def test_a_restart_promotes_a_pending_identity_the_controller_accepts(
    tmp_path, rt, host, server, monkeypatch, kind
):
    issued = code_for(rt, server)
    first = worker_host(tmp_path)
    heal = _break_promotion(monkeypatch, kind)
    try:
        first.join(issued["code"], str(tmp_path / "staging"))
        until(lambda: "身份文件未保存" in first.reason)
    finally:
        assert first.stop()
    heal()
    identity = tmp_path / "worker" / IDENTITY_NAME
    assert not identity.exists()
    second = worker_host(tmp_path)
    second.start()
    try:
        until(lambda: identity.exists() and second.state == "running")
        assert second.status()["worker_id"] == issued["worker_id"]
        assert not (tmp_path / "worker" / PENDING_NAME).exists()
    finally:
        assert second.stop()


def test_a_restart_drops_a_pending_identity_the_controller_refuses(
    tmp_path, rt, host, server
):
    issued = code_for(rt, server)
    first = worker_host(tmp_path)
    first.join(issued["code"], str(tmp_path / "staging"))
    until(lambda: first.state == "running")
    assert first.stop()
    kept = (tmp_path / "worker" / IDENTITY_NAME).read_bytes()
    WorkerIdentity(
        controller_host="127.0.0.1",
        controller_port=server.port,
        pairing_id=PAIRING,
        credential_id="00000000-0000-4000-8000-0000000000ff",
        secret=SecretStr("x" * 43),
        staging_dir=str(tmp_path / "staging"),
        ticket=SecretStr(TICKET),
    ).save(tmp_path / "worker" / PENDING_NAME)
    second = worker_host(tmp_path)
    second.start()
    try:
        until(lambda: not (tmp_path / "worker" / PENDING_NAME).exists())
        until(lambda: second.state == "running")
        assert (tmp_path / "worker" / IDENTITY_NAME).read_bytes() == kept
        assert second.status()["worker_id"] == issued["worker_id"]
    finally:
        assert second.stop()


# ── Codex round 5 (CX13) ────────────────────────────────────────────────
class Gate:
    """A client factory whose pending controller is unreachable until opened."""

    def __init__(self, pending_id: str) -> None:
        self.pending_id, self.up = pending_id, threading.Event()
        self.calls = 0

    def __call__(self, host, port, credential=None, **kw):
        client = WorkerClient(host, port, credential, **kw)
        if credential is None or credential[0] != self.pending_id or self.up.is_set():
            return client
        self.calls += 1

        def down():
            raise ApiError(0, "unreachable")

        client.self_view = down  # type: ignore[method-assign]
        return client


def _saved_and_pending(tmp_path, rt, server, *, refused=False):
    """The working identity of device 1 saved; a pending one for device 2 (or
    a credential the controller refuses). Returns both worker ids."""
    one = code_for(rt, server, "one")
    first = worker_host(tmp_path)
    first.join(one["code"], str(tmp_path / "staging"))
    until(lambda: first.state == "running")
    assert first.stop()
    data = tmp_path / "worker"
    if refused:
        WorkerIdentity(
            controller_host="127.0.0.1",
            controller_port=server.port,
            pairing_id=PAIRING,
            credential_id="00000000-0000-4000-8000-0000000000fe",
            secret=SecretStr("y" * 43),
            staging_dir=str(tmp_path / "staging"),
            ticket=SecretStr(TICKET),
        ).save(data / PENDING_NAME)
        return one["worker_id"], None
    two = code_for(rt, server, "two")
    other = worker_host(tmp_path / "second")
    other.join(two["code"], str(tmp_path / "staging2"))
    until(lambda: other.state == "running")
    assert other.stop()
    (data / PENDING_NAME).write_bytes(
        (tmp_path / "second" / "worker" / IDENTITY_NAME).read_bytes()
    )
    return one["worker_id"], two["worker_id"]


def test_an_unreachable_pending_controller_never_blocks_the_saved_identity(
    tmp_path, rt, host, server
):
    """CX13: the single start-up check fails; the saved identity is online
    within a tick and the status says a pairing is waiting."""
    saved, pending = _saved_and_pending(tmp_path, rt, server)
    pend = WorkerIdentity.load(tmp_path / "worker" / PENDING_NAME)
    assert pend is not None
    gate = Gate(pend.credential_id)
    worker = worker_host(tmp_path, client_factory=gate, pending_seconds=3600)
    worker.start()
    try:
        until(lambda: worker.state == "running" and worker.reason == "新的配对待确认")
        status = worker.status()
        assert status["worker_id"] == saved and status["pending"] is True
        assert (tmp_path / "worker" / PENDING_NAME).exists() and gate.calls == 1
    finally:
        assert worker.stop()


def test_a_later_confirmation_of_the_pending_identity_switches_to_it(
    tmp_path, rt, host, server
):
    saved, pending = _saved_and_pending(tmp_path, rt, server)
    pend = WorkerIdentity.load(tmp_path / "worker" / PENDING_NAME)
    assert pend is not None
    gate = Gate(pend.credential_id)
    worker = worker_host(tmp_path, client_factory=gate, pending_seconds=0.05)
    worker.start()
    try:
        until(lambda: worker.state == "running" and worker.status()["pending"])
        assert worker.status()["worker_id"] == saved
        gate.up.set()
        until(lambda: worker.status()["worker_id"] == pending)
        until(lambda: worker.state == "running" and worker.reason == "在线")
        loaded = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
        assert loaded is not None and loaded.worker_id == pending
        assert not (tmp_path / "worker" / PENDING_NAME).exists()
        assert worker.status()["pending"] is False
    finally:
        assert worker.stop()


def test_a_later_refusal_of_the_pending_identity_drops_it(tmp_path, rt, host, server):
    saved, _ = _saved_and_pending(tmp_path, rt, server, refused=True)
    kept = (tmp_path / "worker" / IDENTITY_NAME).read_bytes()
    pend = WorkerIdentity.load(tmp_path / "worker" / PENDING_NAME)
    assert pend is not None
    gate = Gate(pend.credential_id)
    worker = worker_host(tmp_path, client_factory=gate, pending_seconds=0.05)
    worker.start()
    try:
        until(lambda: worker.state == "running" and worker.status()["pending"])
        gate.up.set()
        until(lambda: not (tmp_path / "worker" / PENDING_NAME).exists())
        until(lambda: worker.reason == "在线" and not worker.status()["pending"])
        assert worker.status()["worker_id"] == saved
        assert (tmp_path / "worker" / IDENTITY_NAME).read_bytes() == kept
    finally:
        assert worker.stop()


# ── Codex round 6 (CX14): stop only when every identity is refused ──────
PENDING_CRED = "00000000-0000-4000-8000-0000000000fd"
PENDING_WORKER = "00000000-0000-4000-8000-000000000004"
SAVED_KINDS = ["valid", "401", "unreachable>valid"]
PENDING_KINDS = ["none", "200", "401", "unreachable>200", "unreachable>401"]


class Identities:
    """A client factory: each credential answers as its kind says — valid
    (200), refused (401), or unreachable until `up` is set, then one of
    those. Records which credentials were refused and every attempt."""

    def __init__(
        self, kinds: dict[str, str], worker_ids: dict[str, str], default=""
    ) -> None:
        self.kinds, self.worker_ids, self.default = kinds, worker_ids, default
        self.up = threading.Event()
        self.refused: set[str] = set()
        self.attempts: dict[str, int] = {}

    def __call__(self, host, port, credential, **kw):
        return _IdentityClient(self, credential[0])


class _IdentityClient:
    def __init__(self, owner: Identities, cid: str) -> None:
        self.owner, self.cid = owner, cid

    def _answer(self) -> None:
        owner = self.owner
        kind = owner.kinds.setdefault(self.cid, owner.default)
        owner.attempts[self.cid] = owner.attempts.get(self.cid, 0) + 1
        if kind.startswith("unreachable") and not owner.up.is_set():
            raise ApiError(0, "unreachable")
        if kind.endswith("401"):
            owner.refused.add(self.cid)
            raise ApiError(401, "unauthorized")

    def self_view(self):
        self._answer()
        worker_id = self.owner.worker_ids.get(self.cid, PENDING_WORKER)
        return {"worker": {"worker_id": worker_id}}

    def pair(self, pairing_id, ticket, credential_id, secret):
        for cid in list(self.owner.kinds):  # the device's old credential goes
            if cid != self.cid:
                self.owner.kinds[cid] = "401"
        raise ApiError(0, "unreachable")  # and the reply is lost (R3)

    def capabilities(self, body):
        self._answer()
        return {"revision": 1, "status": "applied"}

    def boot(self, boot_id):
        self._answer()
        return {"status": "reconciled", "reconciled": True}

    def heartbeat(self, beat):
        self._answer()
        return {"routes": [], "reconcile_needed": False}


@pytest.mark.parametrize("pending_kind", PENDING_KINDS)
@pytest.mark.parametrize("saved_kind", SAVED_KINDS)
def test_a_worker_stops_only_when_every_identity_it_holds_is_refused(
    tmp_path, saved_kind, pending_kind
):
    """CX14: a 401 refuses only the identity it was sent with; a transient
    error refuses nothing; a usable identity is never blocked. The end state
    after connectivity returns: online on the valid identity (a confirmed
    pending one wins), else 「凭据已撤销，请重新加入」 — and no file is lost."""
    saved_cred = "00000000-0000-4000-8000-000000000002"
    kinds = {saved_cred: saved_kind}
    worker_ids = {saved_cred: WORKER, PENDING_CRED: PENDING_WORKER}
    data = tmp_path / "worker"
    if pending_kind != "none":
        kinds[PENDING_CRED] = pending_kind.replace("200", "valid")
        data.mkdir()
        WorkerIdentity(
            controller_host="127.0.0.1",
            controller_port=2,
            pairing_id=PAIRING,
            credential_id=PENDING_CRED,
            secret=SecretStr("p" * 43),
            staging_dir=str(tmp_path / "staging"),
            ticket=SecretStr(TICKET),
        ).save(data / PENDING_NAME)
    fake = Identities(kinds, worker_ids)
    worker = loop_worker(tmp_path, fake, pending_seconds=0.05)
    saved_bytes = (data / IDENTITY_NAME).read_bytes()
    early: list[str] = []
    revoked = worker._revoked

    def checked_revoked():  # the invariant, at the moment the loop gives up
        held = set(kinds) - ({PENDING_CRED} if pending_kind == "none" else set())
        if not held <= fake.refused:
            early.append(f"stopped holding unrefused {held - fake.refused}")
        revoked()

    worker._revoked = checked_revoked  # type: ignore[method-assign]
    worker.start()
    try:
        unreachable = [c for c, k in kinds.items() if k.startswith("unreachable")]
        if pending_kind == "200":  # confirmed at start: the saved one is never used
            unreachable = [c for c in unreachable if c != saved_cred]
        if unreachable:
            reachable = set(kinds) - set(unreachable)
            until(
                lambda: (
                    all(fake.attempts.get(c, 0) >= 3 for c in unreachable)
                    and all(fake.attempts.get(c, 0) >= 1 for c in reachable)
                )
            )
            # an unrefused identity remains: the loop is still trying
            assert worker._thread is not None and worker._thread.is_alive()
            assert worker.state != "revoked"
            fake.up.set()
        if pending_kind.endswith("200"):
            expected = (PENDING_CRED, PENDING_WORKER)
        elif saved_kind != "401":
            expected = (saved_cred, WORKER)
        else:
            expected = None
        if expected is None:
            until(lambda: worker.state == "revoked")
            assert worker.reason == "凭据已撤销，请重新加入"
            until(lambda: not worker._thread.is_alive())
        else:
            until(
                lambda: (
                    worker.state == "running"
                    and worker.reason == "在线"
                    and worker.worker_id == expected[1]
                )
            )
        assert early == []
        until(lambda: not (data / PENDING_NAME).exists())
        loaded = WorkerIdentity.load(data / IDENTITY_NAME)
        assert loaded is not None
        if expected is not None and expected[0] == PENDING_CRED:
            assert loaded.credential_id == PENDING_CRED
            assert loaded.worker_id == PENDING_WORKER and loaded.ticket is None
        else:
            assert (data / IDENTITY_NAME).read_bytes() == saved_bytes
    finally:
        assert worker.stop()


def test_a_re_join_whose_reply_was_lost_is_held_until_it_settles(tmp_path):
    """CX14 in process: the pair landed (the old credential is revoked) but
    its reply and `/self` were lost. The worker resumes the old identity,
    whose 401 hands over to the held pending one instead of stopping."""
    saved_cred = "00000000-0000-4000-8000-000000000002"
    fake = Identities({saved_cred: "valid"}, {saved_cred: WORKER}, "unreachable>valid")
    worker = loop_worker(tmp_path, fake, pending_seconds=0.05)
    code = f"TP1:127.0.0.1:2:00000000-0000-4000-8000-000000000005:{TICKET}"
    try:
        worker.start()
        until(lambda: worker.reason == "在线")
        with pytest.raises(JoinError):
            worker.join(code, str(tmp_path / "staging"))
        until(lambda: saved_cred in fake.refused and worker.status()["pending"])
        assert worker._thread is not None and worker._thread.is_alive()
        assert worker.state != "revoked"
        fake.up.set()
        until(lambda: worker.reason == "在线" and worker.worker_id == PENDING_WORKER)
        loaded = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
        assert loaded is not None and loaded.worker_id == PENDING_WORKER
        assert not (tmp_path / "worker" / PENDING_NAME).exists()
    finally:
        assert worker.stop()


# ── P2-3 R1 / CX16: a first join whose reply and /self were both lost ───
@pytest.mark.parametrize("later", ["200", "401"])
def test_a_first_join_whose_reply_was_lost_is_held_until_it_settles(tmp_path, later):
    """CX16: no saved identity; `/pair` landed but its reply and the `/self`
    recovery were lost. The pending identity is kept and checked by the loop
    (no saved identity): a later 200 promotes it, a later 401 drops it."""
    fake = Identities({}, {}, f"unreachable>{'valid' if later == '200' else '401'}")
    worker = worker_host(tmp_path, client_factory=fake, pending_seconds=0.05)
    code = f"TP1:127.0.0.1:2:00000000-0000-4000-8000-000000000005:{TICKET}"
    data = tmp_path / "worker"
    try:
        with pytest.raises(JoinError):
            worker.join(code, str(tmp_path / "staging"))
        assert (data / PENDING_NAME).exists() and not (data / IDENTITY_NAME).exists()
        until(lambda: worker.status()["pending"] and len(fake.attempts) == 1)
        (cid,) = fake.attempts
        until(lambda: fake.attempts[cid] >= 3)  # /pair's /self, then the loop's
        assert worker._thread is not None and worker._thread.is_alive()
        fake.up.set()
        if later == "200":
            until(
                lambda: worker.reason == "在线" and worker.worker_id == PENDING_WORKER
            )
            loaded = WorkerIdentity.load(data / IDENTITY_NAME)
            assert loaded is not None and loaded.worker_id == PENDING_WORKER
            assert loaded.ticket is None
        else:
            until(lambda: worker.state == "unjoined")
            assert worker.reason == "加入失败，请重新加入"
            assert not (data / IDENTITY_NAME).exists()
        until(lambda: not (data / PENDING_NAME).exists())
    finally:
        assert worker.stop()
