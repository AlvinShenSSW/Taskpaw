"""P2-3 (#294) §2, §5: claim, renew, finish and the attempt read over the real
worker API; the envelope's `stages`, the typed heartbeat `current`, the
cancel and release producers and the boot settlement (R5, R8, R9, R10)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import runtime
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.test_workflow_transfer import (
    OUTPUT,
    Free,
    plan,
    put,
    real_film,
    sha,
    transfers,
    video_copy,
)
from taskpaw_v3.tests.worker_api_helpers import (
    Host,
    StepClock,
    beat,
    boot_id,
    client,
    enrol,
)
from taskpaw_v3.tests.workflow_route_helpers import attest, job, route
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.local_dispatch import should_retry
from taskpaw_v3.workflow.stage_results import (
    FileRef,
    RestoreFacts,
    StageResult,
    VideoResult,
    load_stage_result,
)

P = "/worker/v1"
QUIET = 301  # 判定已停止 needs 5 min without a renewal


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
    return Host(rt, transfers=transfers(rt, Free()))


@pytest.fixture
def api(host):
    return client(host)


@pytest.fixture
def world(api, rt):
    joined, booted = enrol(api, rt)
    response = api.post(f"{P}/heartbeat", json=beat(booted), headers=joined.headers)
    assert response.status_code == 200
    film_id, manifest = real_film(rt)
    route(rt, film_id, joined.worker_id)
    return joined, booted, film_id, manifest


def tools(stage: str = "restore") -> dict:
    return SyntheticExecutor(stage).tools().model_dump(mode="json")  # type: ignore[arg-type]


def claim_body(rt, film_id, boot, stage="restore", **kw) -> dict:
    record = job(rt, film_id, stage)
    body = {
        "attempt_id": identity(),
        "operation_id": identity(),
        "job_id": record.job_id,
        "expected_version": record.version,
        "boot_id": boot,
        "tool_versions": tools(stage),
    }
    body.update(kw)
    return body


def claim(api, joined, body):
    return api.post(f"{P}/claim", json=body, headers=joined.headers)


def owner(boot: str) -> dict:
    return {
        "owner_id": identity(),
        "generation": 1,
        "boot_id": boot,
        "process_identity": "pid:1:2",
    }


def restore_ledger(film_id, attempt_id, ref: FileRef) -> bytes:
    result = StageResult(
        film_id=film_id,
        input_revision=1,
        stage="restore",
        attempt_id=attempt_id,
        videos=(
            VideoResult(
                video_item_id=identity(),
                volume_ordinal=None,
                restore=RestoreFacts(
                    decision="applied",
                    file=ref,
                    width=1920,
                    height=1080,
                    duration_ms=1000,
                    video_streams=1,
                    audio_streams=1,
                ),
            ),
        ),
    )
    return json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    ).encode()


def finished(raw: bytes, **kw) -> dict:
    body = {
        "outcome": "succeeded",
        "message": "修复完成：1 卷修复，0 卷复用",
        "ledger": raw.decode(),
        "payload_digest": hashlib.sha256(raw).hexdigest(),
        "timing": {"scan": 1.0, "ltx": 5.0, "wall": 7.0},
        "energy": {"wh": 12.5, "gpu_wh": 10.0, "seconds": 600.0, "avg_w": 75.0},
    }
    body.update(kw)
    return body


def uploaded(api, joined, attempt_id) -> FileRef:
    path = f"{attempt_id}/movie.mp4"
    files = [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}]
    assert plan(api, joined, attempt_id, files).status_code == 200
    done = put(api, joined, attempt_id, path, OUTPUT, 0, len(OUTPUT))
    assert done.status_code == 200 and done.json()["done"] is True
    return FileRef(path=path, sha256=sha(OUTPUT), size=len(OUTPUT))


def finish(api, joined, attempt_id, body):
    return api.post(
        f"{P}/attempts/{attempt_id}/finish", json=body, headers=joined.headers
    )


def state(api, joined, attempt_id):
    return api.get(f"{P}/attempts/{attempt_id}", headers=joined.headers)


# ── envelope `stages` ───────────────────────────────────────────────────
def test_the_envelope_carries_the_compute_stages_for_the_claim(api, rt, world):
    joined, _, film_id, _ = world
    body = api.get(f"{P}/films/{film_id}/1", headers=joined.headers).json()
    restore = job(rt, film_id, "restore")
    assert body["stages"]["restore"] == {
        "job_id": restore.job_id,
        "version": restore.version,
        "status": "pending",
    }
    assert set(body["stages"]) == {"restore", "asr"}


# ── POST /claim ─────────────────────────────────────────────────────────
def test_a_claim_replays_the_same_bytes_to_one_assignment(api, rt, world):
    joined, boot, film_id, _ = world
    body = claim_body(rt, film_id, boot)
    first = claim(api, joined, body)
    assert first.status_code == 200, first.text
    again = claim(api, joined, body)
    assert again.status_code == 200 and again.json() == first.json()
    data = first.json()
    assert data["attempt_id"] == body["attempt_id"] and data["assignment_id"]
    unresolved = [
        o
        for o in rt.store.list_open_assignments(rt.operator)
        if o.assignment.job_id == body["job_id"]
    ]
    assert len(unresolved) == 1
    assert (
        unresolved[0].assignment.resource_id
        == (
            rt.store.get_worker(rt.operator, joined.worker_id).worker.physical_binding
        ).resource_id
    )
    changed = claim(api, joined, {**body, "operation_id": identity()})
    assert changed.status_code == 409


def test_a_claim_needs_the_route_and_the_current_version(api, rt, world):
    joined, boot, film_id, _ = world
    stale = claim(api, joined, claim_body(rt, film_id, boot, expected_version=99))
    assert stale.status_code == 409 and stale.json() == {"code": "cas_conflict"}
    other, _ = real_film(rt)
    unrouted = claim(api, joined, claim_body(rt, other, boot))
    assert unrouted.status_code == 409 and unrouted.json() == {"code": "route_required"}


# ── POST /attempts/{a}/renew ────────────────────────────────────────────
def test_one_renew_records_the_owner(api, rt, world):
    joined, boot, film_id, _ = world
    body = claim_body(rt, film_id, boot)
    assert claim(api, joined, body).status_code == 200
    who = owner(boot)
    renewed = api.post(
        f"{P}/attempts/{body['attempt_id']}/renew", json=who, headers=joined.headers
    )
    assert renewed.status_code == 200, renewed.text
    (open_,) = [
        o
        for o in rt.store.list_open_assignments(rt.operator)
        if o.assignment.attempt_id == body["attempt_id"]
    ]
    recorded = open_.assignment.local_owner
    assert recorded is not None and recorded.owner_id == who["owner_id"]
    assert recorded.state == "running" and recorded.evidence.status == "passed"
    unknown = api.post(
        f"{P}/attempts/{identity()}/renew", json=who, headers=joined.headers
    )
    assert unknown.status_code == 409 and unknown.json() == {"code": "stale_attempt"}


# ── POST /attempts/{a}/finish ───────────────────────────────────────────
def _claimed(api, rt, world, *, renew=True):
    joined, boot, film_id, manifest = world
    body = claim_body(rt, film_id, boot)
    assert claim(api, joined, body).status_code == 200
    if renew:
        api.post(
            f"{P}/attempts/{body['attempt_id']}/renew",
            json=owner(boot),
            headers=joined.headers,
        )
    return body["attempt_id"]


@pytest.mark.parametrize("renew", [True, False])
def test_a_verified_finish_resolves_succeeded_and_is_idempotent(
    api, rt, host, world, renew
):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world, renew=renew)
    raw = restore_ledger(film_id, a, uploaded(api, joined, a))
    done = finish(api, joined, a, finished(raw))
    assert done.status_code == 200, done.text
    assert done.json() == {"state": "resolved", "outcome": "succeeded"}
    assert job(rt, film_id, "restore").status == "succeeded"
    assert (rt.work_dir / a / "stage-result.json").read_bytes() == raw
    assert load_stage_result(rt, film_id, "restore").attempt_id == a  # digest holds
    (attempt,) = rt.store.list_attempts(rt.operator, job(rt, film_id, "restore").job_id)
    summary = attempt.evidence[0].summary
    assert summary.timing is not None and summary.timing.wall == 7.0
    assert summary.energy is not None and summary.energy.wh == 12.5
    assert finish(api, joined, a, finished(raw)).json() == done.json()  # by state
    assert state(api, joined, a).json() == {
        "state": "resolved",
        "outcome": "succeeded",
        "current": True,
    }
    assert not (rt.work_dir / ".incoming" / a).exists()  # the plan is swept
    assert host.resolved and host.resolved[-1][1] == "restore"


def test_a_finish_accepts_a_work_copy_ref_and_null_energy(api, rt, world):
    joined, _, film_id, manifest = world
    a = _claimed(api, rt, world)
    copy = video_copy(manifest)
    ref = FileRef(path=copy.relative_path, sha256=copy.sha256, size=copy.size_bytes)
    raw = restore_ledger(film_id, a, ref)
    done = finish(api, joined, a, finished(raw, energy=None))
    assert done.status_code == 200, done.text
    (attempt,) = rt.store.list_attempts(rt.operator, job(rt, film_id, "restore").job_id)
    assert attempt.evidence[0].summary.energy is None  # missing, never 0


@pytest.mark.parametrize(
    "change,code",
    [
        ({"payload_digest": "0" * 64}, "digest_mismatch"),
        ({"ledger": "{}", "payload_digest": sha(b"{}")}, "invalid_ledger"),
        ("unverified", "unverified_output"),
        ("foreign", "ledger_mismatch"),
    ],
)
def test_an_unverifiable_finish_is_422_and_resolves_nothing(
    api, rt, world, change, code
):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    ref = FileRef(path=f"{a}/never.mp4", sha256=sha(b"x"), size=1)
    raw = restore_ledger(identity() if change == "foreign" else film_id, a, ref)
    body = finished(raw, **change) if isinstance(change, dict) else finished(raw)
    response = finish(api, joined, a, body)
    assert response.status_code == 422 and response.json()["code"] == code
    assert job(rt, film_id, "restore").status == "running"


def test_a_cancelled_finish_is_a_free_retry_and_closes_the_route(api, rt, world):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    body = {"outcome": "failed", "error_class": "cancelled", "message": "已停止"}
    done = finish(api, joined, a, body)
    assert done.status_code == 200 and done.json()["outcome"] == "failed"
    assert job(rt, film_id, "restore").status == "pending"  # retried at once
    assert rt.store.film_route(rt.operator, film_id, 1) is None  # routed afresh


def test_a_stale_or_foreign_finish_is_409(api, rt, clock, world):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    second, _ = enrol(api, rt, "5060 Ti-2")
    body = {"outcome": "failed", "error_class": "transient", "message": "x"}
    assert finish(api, second, a, body).status_code == 409
    assert state(api, second, a).status_code == 404  # S-4
    clock.advance(timedelta(seconds=QUIET))
    (open_,) = [
        o
        for o in rt.store.list_open_assignments(rt.operator)
        if o.assignment.attempt_id == a
    ]
    attest(rt, open_.assignment.assignment_id)  # 判定已停止
    raw = restore_ledger(film_id, a, FileRef(path=f"{a}/x", sha256=sha(b"x"), size=1))
    stale = finish(api, joined, a, finished(raw))  # crash matrix 9: after attest
    assert stale.status_code == 409 and stale.json() == {"code": "stale_attempt"}
    record = job(rt, film_id, "restore")
    assert record.status == "failed"  # 判定已停止: a free retry when due
    assert should_retry(rt.store.list_attempts(rt.operator, record.job_id))


def test_the_attempt_read(api, rt, world):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    assert state(api, joined, a).json() == {"state": "running", "current": True}
    assert state(api, joined, identity()).status_code == 404


# ── heartbeat: typed current, cancel, release ───────────────────────────
def current(film_id, a, **kw) -> dict:
    value = {
        "film_id": film_id,
        "stage": "restore",
        "attempt_id": a,
        "phase": "running",
        "progress": "LTX 2/4",
        "elapsed_s": 90.0,
        "ltx": {"percent": 52.0, "params": {"preset": "5060Ti-16"}},
        "energy": None,
        "power": None,
        "uses_gpu": True,
    }
    value.update(kw)
    return value


def test_a_requeued_attempt_and_a_stale_current_are_cancelled(
    api, rt, host, clock, world
):
    joined, boot, film_id, _ = world
    a = _claimed(api, rt, world)
    beat_ = beat(boot, current=current(film_id, a))
    assert (
        api.post(f"{P}/heartbeat", json=beat_, headers=joined.headers).json()["cancel"]
        == []
    )
    host.cancels.add(a)  # 停止并重新排队
    assert api.post(f"{P}/heartbeat", json=beat_, headers=joined.headers).json()[
        "cancel"
    ] == [a]
    host.cancels.clear()
    stale = beat(boot, current=current(film_id, identity()))
    reply = api.post(f"{P}/heartbeat", json=stale, headers=joined.headers).json()
    assert reply["cancel"] == [stale["current"]["attempt_id"]]


def test_the_typed_current_is_bounded(api, rt, world):
    joined, boot, film_id, _ = world
    bad = beat(boot, current=current(film_id, identity(), phase="dreaming"))
    assert (
        api.post(f"{P}/heartbeat", json=bad, headers=joined.headers).status_code == 422
    )


def test_release_names_only_finished_stale_or_moved_staged_films(api, rt, world):
    joined, boot, routed, _ = world
    waiting, _ = real_film(rt)  # planned, restore pending, no route: kept (R10)
    moved, _ = real_film(rt)
    second, _ = enrol(api, rt, "5060 Ti-2")
    route(rt, moved, second.worker_id)
    staged = [{"film": f, "rev": 1} for f in (routed, waiting, moved)]
    staged.append({"film": routed, "rev": 7})  # not the current revision
    data = api.post(
        f"{P}/heartbeat", json=beat(boot, staged=staged), headers=joined.headers
    ).json()
    assert sorted((r["film"], r["rev"]) for r in data["release"]) == sorted(
        [(moved, 1), (routed, 7)]
    )


# ── boot settlement (§5) ────────────────────────────────────────────────
def report(a, **kw) -> dict:
    value = {
        "attempt_id": a,
        "children_alive": False,
        "ledger": False,
        "outputs_intact": False,
        "outcome": None,
    }
    value.update(kw)
    return value


def reboot(api, joined, boot, attempts):
    return api.post(
        f"{P}/boot",
        json={"boot_id": boot, "assignment_ids": [], "attempts": attempts},
        headers=joined.headers,
    ).json()


@pytest.mark.parametrize("reported", [False, True])
def test_a_restart_without_a_ledger_is_interrupted_and_a_free_retry(
    api, rt, world, reported
):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    reply = reboot(api, joined, boot_id(), [report(a)] if reported else [])
    assert reply["reconciled"] is True and reply["attempts"] == {a: "interrupted"}
    assert job(rt, film_id, "restore").status == "pending"
    assert reply["keep"] == []  # the failed route closed (IR2)


def test_a_live_child_is_held_and_claims_are_refused(api, rt, world):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    boot = boot_id()
    reply = reboot(api, joined, boot, [report(a, children_alive=True)])
    assert reply["reconciled"] is False and reply["attempts"] == {a: "held"}
    assert reply["keep"] == [{"film": film_id, "rev": 1}]
    other, _ = real_film(rt)
    route(rt, other, joined.worker_id)
    refused = claim(api, joined, claim_body(rt, other, boot))
    assert refused.status_code == 409 and refused.json() == {"code": "owner_unresolved"}
    again = reboot(api, joined, boot, [report(a)])  # the child is gone now
    assert again["reconciled"] is True and again["attempts"] == {a: "interrupted"}


def test_a_ledger_with_outputs_is_finished_without_a_re_run(api, rt, world):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    boot = boot_id()
    reply = reboot(api, joined, boot, [report(a, ledger=True, outputs_intact=True)])
    assert reply["reconciled"] is False and reply["attempts"] == {a: "finish"}
    assert state(api, joined, a).json()["state"] == "unknown"
    raw = restore_ledger(film_id, a, uploaded(api, joined, a))  # unknown uploads (C7)
    assert finish(api, joined, a, finished(raw)).json()["outcome"] == "succeeded"
    assert reboot(api, joined, boot, [])["reconciled"] is True


# ── 停止并重新排队 (R9a): the control route and its set ─────────────────
class ControlHost:
    def __init__(self, rt, cancels, fleet=None):
        self.state, self.reason, self.runtime = "running", "", rt
        self.cancels, self.dispatcher, self.finalizer = cancels, None, None
        self.fleet = fleet

    def requeue(self, film_id: str, attempt_id: str) -> None:
        from taskpaw_v3.workflow import remote_attempts

        remote_attempts.requeue(
            self.runtime, self.cancels, film_id, attempt_id, fleet=self.fleet
        )


def control(rt, host):
    from fastapi.testclient import TestClient

    from taskpaw_v3.agent.server.app import create_control_app
    from taskpaw_v3.core.config import AgentConfig, WorkflowSettings

    config = AgentConfig(
        server_id="s1",
        machine="dev",
        workflow=WorkflowSettings(enabled=True, remote_workers=True),
    )
    return TestClient(
        create_control_app(
            config,
            control_token="t",
            control_active=lambda: True,
            workflow_provider=lambda: host,
        ),
        headers={"Authorization": "Bearer t"},
    )


def test_requeue_marks_a_running_remote_attempt_until_its_finish(api, rt, host, world):
    joined, _, film_id, _ = world
    a = _claimed(api, rt, world)
    ctl = control(rt, ControlHost(rt, host.cancels))
    url = f"/control/workflow/films/{film_id}/requeue"
    assert ctl.post(url, json={"attempt_id": identity()}).status_code == 409
    other, _ = real_film(rt)
    wrong = ctl.post(f"/control/workflow/films/{other}/requeue", json={"attempt_id": a})
    assert wrong.status_code == 409
    done = ctl.post(url, json={"attempt_id": a})
    assert done.status_code == 200 and done.json() == {"ok": True}
    assert host.cancels == {a}
    body = {"outcome": "failed", "error_class": "cancelled", "message": "已停止"}
    assert finish(api, joined, a, body).status_code == 200
    assert host.cancels == set()  # cleared at resolve


@pytest.mark.parametrize("phase", ["uploading", "finishing"])
def test_requeue_is_refused_once_the_worker_settled_its_stage(
    api, rt, host, world, phase
):
    """§11 CX5: finished GPU work is never discarded — once the worker
    uploads or finishes, 停止并重新排队 answers 409 `already_settled`."""
    joined, boot, film_id, _ = world
    a = _claimed(api, rt, world)
    ctl = control(rt, ControlHost(rt, host.cancels, host.fleet))
    url = f"/control/workflow/films/{film_id}/requeue"
    running = beat(boot, current=current(film_id, a, phase="settling"))
    assert (
        api.post(f"{P}/heartbeat", json=running, headers=joined.headers).status_code
        == 200
    )
    assert ctl.post(url, json={"attempt_id": a}).status_code == 200  # still on
    host.cancels.clear()
    late = beat(boot, current=current(film_id, a, phase=phase))
    assert (
        api.post(f"{P}/heartbeat", json=late, headers=joined.headers).status_code == 200
    )
    refused = ctl.post(url, json={"attempt_id": a})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "修复已完成，正在回传"
    assert host.cancels == set()
