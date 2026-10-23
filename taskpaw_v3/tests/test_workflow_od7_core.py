"""#298 (P2-OD7) R2–R8, R12: ASR on the controller, GPU workers restore only.

Only restore is routed: the controller skips just a restore that needs the
GPU and claims ASR itself under the process-wide `gpu_lease`. A worker's route
closes once its restore succeeds; an ASR claim needs the controller's own
local route (409 `route_required` otherwise). An 8 GB worker is eligible for
nothing. At upgrade, the pass withdraws an unclaimed worker route whose
routed stages are empty (「识别转本机」)."""

from __future__ import annotations

import random
from datetime import timedelta
from typing import cast

import pytest

from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    dispatcher as local_dispatcher,
)
from taskpaw_v3.tests.test_workflow_scheduler import (
    CONTROLLER,
    NOW,
    film,
    fleet,
    live,
    open_route,
    run,
    worker,
)
from taskpaw_v3.tests.workflow_route_helpers import (
    GPU_8,
    claim_remote,
    enrol_remote,
    resolve_remote,
    route,
)
from taskpaw_v3.workflow import film_store, routing
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import operator_actions as ops
from taskpaw_v3.workflow import scheduler as s
from taskpaw_v3.workflow.exec_asr import AsrExecutor
from taskpaw_v3.workflow.executors import ExecRuntime
from taskpaw_v3.workflow.local_dispatch import REMOTE_GPU, REMOTE_STAGES
from taskpaw_v3.workflow.local_gpu import UHD


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


class SpyLease(GpuLease):
    """The real lease, recording every acquire attempt's label."""

    def __init__(self) -> None:
        super().__init__()
        self.tries: list[str] = []

    def try_acquire(self, run, poll_interval, label=""):
        self.tries.append(label)
        return super().try_acquire(run, poll_interval, label)


def remote_restored(rt, name="5060 Ti-1"):
    """A film whose restore a worker ran on its auto route."""
    w = enrol_remote(rt, name)
    film_id = add_film(rt)
    route(rt, film_id, w.worker_id)
    resolve_remote(rt, w, claim_remote(rt, w, film_id, "restore"))
    return w, film_id


def routes_of(rt, film_id):
    return [
        (r.worker_id, r.kind, r.state, r.close_reason)
        for r in rt.store.list_routes(rt.operator, open_only=False)
        if r.film_id == film_id
    ]


# ── R2 / R3: only restore is routed; ASR under the lease on the controller ─
def test_r2_only_restore_is_routed_anywhere():
    assert s._ROUTED == ("restore",)
    assert routing._GPU_STAGES == ("restore",)
    assert REMOTE_STAGES == frozenset({"restore"})


def test_r2_in_remote_mode_the_controller_claims_asr_under_the_gpu_lease(rt):
    w, film_id = remote_restored(rt)
    lease = SpyLease()
    d = local_dispatcher(rt, lease=lease, remote_enabled=True)
    assert d.run_once() is True
    assert stage(rt, film_id, "asr").status == "succeeded"
    attempt = rt.store.list_attempts(rt.operator, stage(rt, film_id, "asr").job_id)[0]
    assert attempt.worker_id == rt.identity.worker_id
    assert lease.tries and lease.tries[0].startswith("影片工作流：")
    assert lease.holder() is None  # released after the stage
    assert routes_of(rt, film_id) == [
        (w.worker_id, "auto", "closed", "完成"),
        (rt.identity.worker_id, "local", "open", None),
    ]


def test_r2_a_skipped_restore_never_becomes_a_lease_waiter(rt):
    enrol_remote(rt, "5060 Ti-1")
    film_id = add_film(rt)
    lease = SpyLease()
    d = local_dispatcher(rt, lease=lease, remote_enabled=True)
    for _ in range(3):
        assert d.run_once() is False
    assert d.status().waiting == {film_id: f"{REMOTE_GPU}：5060 Ti-1"}
    assert lease.tries == [] and lease.waiters() == []


def test_r3_a_manual_task_holding_the_gpu_makes_controller_asr_wait(rt):
    _, film_id = remote_restored(rt)
    lease = GpuLease()
    manual = ("jasna", 1)
    assert lease.try_acquire(manual, 1.0, "Jasna：手动任务")
    d = local_dispatcher(rt, lease=lease, remote_enabled=True)
    assert d.run_once() is False
    assert d.status().waiting == {film_id: "等待 GPU：Jasna：手动任务"}
    assert stage(rt, film_id, "asr").status == "pending"
    assert lease.release(manual)
    assert d.run_once() is True
    assert stage(rt, film_id, "asr").status == "succeeded"


# ── R4: a worker's route closes once its restore is done ─────────────────
def test_r4_a_worker_route_closes_when_restore_succeeds(rt):
    w, film_id = remote_restored(rt)
    assert routes_of(rt, film_id) == [(w.worker_id, "auto", "closed", "完成")]


def test_r4_a_worker_route_closes_failed_when_restore_fails(rt):
    w = enrol_remote(rt, "5060 Ti-1")
    film_id = add_film(rt)
    route(rt, film_id, w.worker_id)
    resolve_remote(rt, w, claim_remote(rt, w, film_id, "restore"), "failed")
    assert routes_of(rt, film_id) == [(w.worker_id, "auto", "closed", "失败")]


def test_r4_the_controllers_local_route_still_covers_asr_and_translate(rt):
    d = local_dispatcher(rt)  # single machine: every stage here
    film_id = add_film(rt)
    assert d.run_once() is True
    assert stage(rt, film_id, "restore").status == "succeeded"
    local = (rt.identity.worker_id, "local", "open", None)
    assert routes_of(rt, film_id) == [local]
    assert d.run_once() is True  # asr
    assert routes_of(rt, film_id) == [local]
    assert d.run_once() is True  # translate
    assert routes_of(rt, film_id) == [
        (rt.identity.worker_id, "local", "closed", "完成")
    ]


# ── R5: an ASR claim needs the controller's own local route ──────────────
def test_r5_a_worker_cannot_claim_asr_on_its_route(rt):
    w, film_id = remote_restored(rt)
    route(rt, film_id, w.worker_id)  # an old controller left one open
    with pytest.raises(m.WorkflowError) as refused:
        claim_remote(rt, w, film_id, "asr")
    assert refused.value.code == "route_required"
    assert stage(rt, film_id, "asr").status == "pending"


def test_r5_an_asr_claimed_before_the_upgrade_finishes_and_closes_its_route(
    rt, monkeypatch
):
    """§3.1: an in-flight worker ASR is never cancelled; on resolve its route
    closes (restore finished → 完成) and the controller takes the next stage."""
    w, film_id = remote_restored(rt)
    route(rt, film_id, w.worker_id)  # the old build kept it open for ASR
    with monkeypatch.context() as old:  # the controller before the upgrade
        old.setattr(film_store, "_CONTROLLER_ONLY", frozenset())
        claimed = claim_remote(rt, w, film_id, "asr")
    assert claimed[0].status == "running"
    d = local_dispatcher(rt, remote_enabled=True)
    assert d.run_once() is False  # nothing of the film is the controller's yet
    assert stage(rt, film_id, "asr").status == "running"
    resolve_remote(rt, w, claimed)
    assert routes_of(rt, film_id)[-1] == (w.worker_id, "auto", "closed", "完成")
    assert d.run_once() is True
    assert stage(rt, film_id, "translate").status == "succeeded"


def test_r5_the_controller_claims_asr_on_its_local_route(rt):
    d = local_dispatcher(rt)
    film_id = add_film(rt)
    assert d.run_once() is True and d.run_once() is True
    assert stage(rt, film_id, "asr").status == "succeeded"


# ── R6: eligibility is restore only; < 16 GB is eligible for nothing ─────
def test_r6_an_8gb_worker_is_eligible_for_nothing():
    small = worker("5060", gpu="NVIDIA GeForce RTX 5060", vram=8151)
    beats = live([small])[small.worker_id]
    for stages in (("restore",), ()):
        assert (
            s.eligibility(small, film(), stages, beats, NOW) == "显存不足，不参与修复"
        )
    big = fleet()[0]
    assert (
        s.eligibility(big, film(), ("restore",), live([big])[big.worker_id], NOW)
        is None
    )
    assert not hasattr(s.WorkerLive(last_seen=NOW), "asr_ready")


def test_r6_a_restored_film_needing_asr_is_never_routed():
    ws = fleet()
    asr_only = film(0, restore="succeeded", asr="pending")
    result = run([asr_only], ws)
    assert result.routes == () and asr_only.film_id not in result.waiting


def test_r6_reassign_options_and_pin_look_at_restore_only(rt):
    w, film_id = remote_restored(rt)
    assert ops.reassign_options(rt, None, film_id) == []
    view = rt.store.film_board(rt.operator, film_id=film_id)[0]
    assert ops.can_reassign(view, False) is False
    with pytest.raises(m.WorkflowError) as refused:
        ops.reassign(
            rt,
            fleet=None,
            film_id=film_id,
            input_revision=1,
            expected_version=1,
            worker_id=w.worker_id,
        )
    assert refused.value.code == "precondition_failed"
    from taskpaw_v3.agent.server import workflow_reads as reads

    assert reads._pin(rt, view, {}) is None
    fresh = add_film(rt)
    board = rt.store.film_board(rt.operator, film_id=fresh)[0]
    assert ops.can_reassign(board, False) is True
    assert reads._pin(rt, board, {}) == {
        "worker_id": None,
        "name": "自动",
        "version": 1,
    }


# ── R7: preview of a film with no routed stage ───────────────────────────
def test_r7_a_restored_film_previews_no_device_and_no_gpu_text():
    ws = fleet()
    restored = film(planned=False, confirmed_at=None, restore="succeeded")
    view = s.preview(
        m.RoutingSnapshot(films=(restored,), routes=(), workers=tuple(ws), samples=()),
        restored.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
    )["standard"]
    assert view.machine is None and view.waiting is None and view.devices == ()
    assert view.reason == s.NO_GPU == "无需修复，本机完成"


# ── R8: WhisperJAV reads audio only — 4K ASR on the 8 GB controller ──────
def test_r8_asr_takes_any_resolution_on_an_8gb_card(tmp_path):
    executor = AsrExecutor(cast(ExecRuntime, None), str(tmp_path / "whisperjav.exe"))
    assert executor.max_resolution(GPU_8[1]) == UHD


def test_r8_the_controller_records_a_uhd_asr_profile_on_its_8gb_card(tmp_path):
    """As P2-3 R4, the controller approves its own profiles at start; on the
    8 GB 5060 the ASR profile now reaches 4K."""
    unbound = cast(ExecRuntime, None)  # tools()/max_resolution() never read it
    controller = runtime(
        tmp_path / "wf", execs=[AsrExecutor(unbound, str(tmp_path / "w.exe"))]
    )
    try:
        cap = controller.worker().capability
        assert cap is not None and cap.approved is not None
        (profile,) = cap.approved.profiles
        assert profile.allowed_stages == ("asr",)
        assert (profile.max_width, profile.max_height) == UHD
    finally:
        controller.close()


# ── R12: the pass withdraws an unclaimed route with nothing left to route ─
def test_r12_the_pass_withdraws_an_unclaimed_restored_worker_route():
    ws = fleet()
    auto = film(0, restore="succeeded", asr="pending")
    pinned = film(1, restore="succeeded", asr="pending", pin=ws[1].worker_id)
    claimed = film(
        2,
        restore="succeeded",
        asr="running",
        claimed_by=ws[2].worker_id,
        claimed_stage="asr",
        claimed_at=NOW,
    )
    routes = [
        open_route(auto, ws[0]),
        open_route(pinned, ws[1], kind="pinned"),
        open_route(claimed, ws[2]),
    ]
    result = run([auto, pinned, claimed], ws, routes=routes)
    assert {(x.film_id, x.reason) for x in result.withdrawals} == {
        (auto.film_id, s.ASR_LOCAL),
        (pinned.film_id, s.ASR_LOCAL),
    }
    assert s.ASR_LOCAL == "识别转本机"
    assert result.routes == ()


def test_r12_a_withdrawn_route_releases_the_film_on_the_worker(rt):
    """§3.2: once withdrawn, the worker gets the film in `release` (restore
    done is all a worker waits for)."""
    from taskpaw_v3.workflow.remote_attempts import release_list

    w, film_id = remote_restored(rt)
    assert release_list(rt, w.worker_id, [(film_id, 1)]) == [
        {"film": film_id, "rev": 1}
    ]


# ── property: the pass over random states ────────────────────────────────
_STATUSES = ("pending", "running", "succeeded", "failed", "skipped")


def _random_state(rng: random.Random):
    ws = fleet()
    films, routes = [], []
    for i in range(rng.randint(1, 12)):
        restore = rng.choice(_STATUSES)
        asr = (
            rng.choice(_STATUSES) if restore in ("succeeded", "skipped") else "pending"
        )
        claim = None
        if restore == "running" or asr == "running":
            claim = rng.choice([w.worker_id for w in ws] + [CONTROLLER])
        pin = rng.choice([None, None, ws[rng.randrange(len(ws))].worker_id])
        f = film(
            i,
            restore=restore,
            asr=asr,
            pin=pin,
            claimed_by=claim,
            claimed_stage=None
            if claim is None
            else ("restore" if restore == "running" else "asr"),
            claimed_at=None if claim is None else NOW - timedelta(minutes=5),
        )
        films.append(f)
        if claim is not None and claim != CONTROLLER:
            routes.append(open_route(f, next(w for w in ws if w.worker_id == claim)))
        elif rng.random() < 0.4:
            target = rng.choice(ws)
            routes.append(open_route(f, target, kind="pinned" if pin else "auto"))
    return ws, films, routes


@pytest.mark.parametrize("seed", range(40))
def test_the_pass_routes_restore_only_and_never_withdraws_a_claim(seed):
    rng = random.Random(seed)
    ws, films, routes = _random_state(rng)
    result = run(films, ws, routes=routes)
    by_id = {f.film_id: f for f in films}
    routed_ids = [d.film_id for d in result.routes]
    for decision in result.routes:
        f = by_id[decision.film_id]
        assert f.restore == "pending" and f.claimed_by is None
        assert decision.worker_id != CONTROLLER
    assert len(routed_ids) == len(set(routed_ids))
    withdrawn = {x.route_id for x in result.withdrawals}
    for item in routes:
        if by_id[item.film_id].claimed_by is not None:
            assert item.route_id not in withdrawn
    for f in films:  # a film with no restore left is never routed
        if f.restore in ("succeeded", "skipped"):
            assert f.film_id not in routed_ids
            assert all(
                x.reason == s.ASR_LOCAL
                for x in result.withdrawals
                if x.film_id == f.film_id
            )
    # preview 自动 is the pass's own decision for every film it routed
    snapshot = m.RoutingSnapshot(
        films=tuple(films), routes=tuple(routes), workers=tuple(ws), samples=()
    )
    for decision in result.routes:
        f = by_id[decision.film_id]
        if f.pin_worker_id is not None:
            continue
        view = s.preview(
            snapshot,
            f.film_id,
            (f.ltx_model,),
            live=live(ws),
            now=NOW,
            controller_id=CONTROLLER,
            keep_confirmed=True,
        )[f.ltx_model]
        assert view.machine == decision.worker_id
