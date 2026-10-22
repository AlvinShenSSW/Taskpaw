"""P2-2 (#293): the worker side over a real loopback server (§7 fake worker).

A real `WorkerClient`/`WorkerHost` against the real worker API on a loopback
uvicorn server bound to an ephemeral port: it joins with the 设备 page's code,
probes, is approved, boots and shows online on 设备; no claim is possible.
Then the client-side transfer resumes, the code and staging rules, identity
hygiene, revoke and shutdown.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.request
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
from taskpaw_v3.tests.test_workflow_transfer import OUTPUT, VIDEO, Free, real_film, sha
from taskpaw_v3.tests.worker_api_helpers import (
    VERSION,
    Host,
    StepClock,
    probe,
    profiles,
)
from taskpaw_v3.tests.workflow_route_helpers import Remote, claim_remote, route
from taskpaw_v3.workflow import devices
from taskpaw_v3.workflow import staging as st
from taskpaw_v3.workflow.remote_worker import (
    IDENTITY_NAME,
    JoinError,
    Probe,
    WorkerHost,
    WorkerIdentity,
    parse_code,
    probe_changed,
)
from taskpaw_v3.workflow.transfer import Transfers
from taskpaw_v3.workflow.worker_client import ApiError, WorkerClient

TICKET = "A" * 43
PAIRING = "00000000-0000-4000-8000-000000000001"


# ── the loopback controller ─────────────────────────────────────────────
class Server:
    def __init__(self, host: Host) -> None:
        self.sock = claim_port("127.0.0.1", 0, "test worker API")
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(
            uvicorn.Config(
                create_worker_app(lambda: host), log_level="warning", access_log=False
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
    return Host(rt, transfers=Transfers(rt.work_dir, free_bytes=free))


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
        **kw,
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
    stored = WorkerIdentity.load(tmp_path / "worker" / IDENTITY_NAME)
    assert stored is not None and stored.worker_id is None
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
    host.transfers = Transfers(rt.work_dir, free_bytes=free)
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
