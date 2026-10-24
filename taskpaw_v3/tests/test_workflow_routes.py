"""P2-1 (#292): schema v4, pins, routes, the claim route check and 判定已停止."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    dispatcher,
    open_assignments,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import (
    ATTESTED,
    GPU_16,
    GPU_16_FAST,
    StepClock,
    attest,
    boot,
    claim_remote,
    enrol_remote,
    finish_remote,
    pin_version,
    resolve_remote,
    route,
    swap_gpu,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import routing, scheduler, schema
from taskpaw_v3.workflow.auth import require_role
from taskpaw_v3.workflow.executors import (
    ASR_DONE,
    RESTORE_DONE,
    failed_kind,
)
from taskpaw_v3.workflow.local_dispatch import should_retry


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


def confirm(rt, film_id, machine, *, tier="standard", revision=1):
    return rt.store.confirm_plan(
        rt.operator,
        rt.meta(),
        m.FilmPlanConfirm(
            film_id=film_id, input_revision=revision, ltx_model=tier, machine=machine
        ),
    )


def set_pin(rt, film_id, worker_id, *, revision=1, version=None):
    return rt.store.set_pin(
        rt.operator,
        rt.meta(),
        m.PinSet(
            film_id=film_id,
            input_revision=revision,
            expected_version=version or pin_version(rt, film_id, revision),
            worker_id=worker_id,
            reason="改派",
        ),
    )


def open_route(rt, film_id, revision=1):
    return rt.store.film_route(rt.operator, film_id, revision)


def withdraw(rt, route_value, reason="测试撤回"):
    return rt.store.withdraw_route(
        rt.operator,
        rt.meta(),
        m.RouteWithdraw(
            route_id=route_value.route_id,
            expected_version=route_value.version,
            reason=reason,
        ),
    )


def auto_film(rt, **kw):
    film = add_film(rt, plan=False, **kw)
    confirm(rt, film, None)
    return film


# ── plans and pins ───────────────────────────────────────────────────────
def test_new_databases_are_v4_with_routes_and_pins(rt):
    assert schema.SCHEMA_VERSION == 4
    assert {"film_plans", "film_pins", "film_routes"} <= set(schema.TABLES)
    assert "one_open_route_per_film" in schema.INDEXES


def test_a_plan_may_leave_the_machine_to_auto(rt):
    film = auto_film(rt)
    (plan,) = rt.store.list_plans(rt.operator)
    assert (plan.film_id, plan.machine) == (film, None)
    pin = rt.store.get_pin(rt.operator, film, 1)
    assert (pin.worker_id, pin.version) == (None, 1)


def test_a_given_machine_is_the_first_pin(rt):
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt, plan=False)
    confirm(rt, film, w.worker_id)
    assert rt.store.get_pin(rt.operator, film, 1).worker_id == w.worker_id
    with pytest.raises(m.WorkflowError, match="plan_exists"):
        confirm(rt, film, None)


def test_pin_changes_are_cas_and_current_revision_only(rt):
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    set_pin(rt, film, w.worker_id)
    pin = rt.store.get_pin(rt.operator, film, 1)
    assert (pin.worker_id, pin.version) == (w.worker_id, 2)
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        set_pin(rt, film, None, version=1)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        set_pin(rt, film, identity())  # not an enrolled worker
    (plan,) = rt.store.list_plans(rt.operator)
    assert plan.machine is None  # the plan itself never changes (C2)


def test_a_plan_on_the_controller_pins_auto(rt):
    """IR9: one encoding of 自动 (NULL), as the v4 migration writes it."""
    film = add_film(rt)  # 待处理 confirms with 本机
    (plan,) = rt.store.list_plans(rt.operator)
    assert plan.machine == rt.identity.worker_id  # history keeps the choice
    assert rt.store.get_pin(rt.operator, film, 1).worker_id is None
    set_pin(rt, film, rt.identity.worker_id)
    pin = rt.store.get_pin(rt.operator, film, 1)
    assert (pin.worker_id, pin.version) == (None, 2)


def test_repin_to_auto_keeps_a_healthy_route_and_moves_its_version(rt):
    """IR8: 改派 to 自动 keeps the route; CX2: every kept route's version moves,
    so a pass's withdrawal decided before the 改派 loses its CAS."""
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    before = open_route(rt, film)
    set_pin(rt, film, None)
    kept = open_route(rt, film)
    assert (kept.route_id, kept.version) == (before.route_id, 2)
    set_pin(rt, film, w.worker_id)
    assert open_route(rt, film).version == 3
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        withdraw(rt, before)
    assert open_route(rt, film).route_id == before.route_id


def test_an_unplanned_film_takes_only_a_local_route(rt):
    """IR13: the scheduler's kinds need a plan; the controller's own does not."""
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt, plan=False)
    for kind in ("auto", "pinned"):
        with pytest.raises(m.WorkflowError, match="precondition_failed"):
            route(rt, film, w.worker_id, kind=kind)
    route(rt, film, rt.identity.worker_id, kind="local")


def test_route_commands_are_operator_only():
    for command in ("route_film", "withdraw_route", "set_pin", "attest_stopped"):
        require_role("operator", command)
        with pytest.raises(m.WorkflowError, match="forbidden"):
            require_role("compute_worker", command)


# ── routes and the claim gate ────────────────────────────────────────────
def test_a_remote_claim_needs_an_open_route_to_that_worker(rt):
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    film = auto_film(rt)
    with pytest.raises(m.WorkflowError, match="route_required"):
        claim_remote(rt, w1, film, "restore")
    route(rt, film, w2.worker_id)
    with pytest.raises(m.WorkflowError, match="route_required"):
        claim_remote(rt, w1, film, "restore")
    result, _ = claim_remote(rt, w2, film, "restore")
    assert result.status == "running"
    value = open_route(rt, film)
    assert (value.worker_id, value.kind, value.state) == (w2.worker_id, "auto", "open")


def test_one_open_route_per_film_and_pin_rules_per_kind(rt):
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    film = auto_film(rt)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        route(rt, film, w1.worker_id, kind="pinned")  # the pin is 自动
    route(rt, film, w1.worker_id)
    with pytest.raises(m.WorkflowError, match="route_exists"):
        route(rt, film, w2.worker_id)
    pinned = auto_film(rt)
    set_pin(rt, pinned, w2.worker_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        route(rt, pinned, w1.worker_id, kind="pinned")
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        route(rt, pinned, w2.worker_id, kind="auto")
    route(rt, pinned, w2.worker_id, kind="pinned")
    # `local` is the controller's own route: no pin rule
    local = auto_film(rt)
    set_pin(rt, local, w2.worker_id)
    route(rt, local, rt.identity.worker_id, kind="local")


def test_a_route_is_cas_on_the_pin_and_the_current_revision(rt):
    from taskpaw_v3.tests.test_workflow_plans import new_revision

    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    stale_pin = pin_version(rt, film)
    set_pin(rt, film, None)  # 改派 back to 自动: a new version
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        rt.store.route_film(
            rt.operator,
            rt.meta(),
            m.RouteOpen(
                route_id=identity(),
                film_id=film,
                input_revision=1,
                worker_id=w.worker_id,
                kind="auto",
                expected_pin_version=stale_pin,
                reason="旧快照",
            ),
        )
    new_revision(rt, film)
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        route(rt, film, w.worker_id)  # revision 1 is no longer current


def test_only_an_enabled_verified_worker_is_routed(rt):
    w = enrol_remote(rt, "5060 Ti-1")
    worker = rt.store.get_worker(rt.operator, w.worker_id).worker
    rt.store.update_worker(
        rt.operator,
        rt.meta(),
        m.WorkerUpdate(
            worker_id=w.worker_id,
            expected_version=worker.version,
            address=worker.address,
            port=worker.port,
            network_revision=worker.network_revision,
            workflow_participation=True,
            admission_state="draining",
        ),
    )
    film = auto_film(rt)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        route(rt, film, w.worker_id)


def test_withdraw_frees_an_unclaimed_route_and_the_claim_is_refused(rt):
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    film = auto_film(rt)
    route(rt, film, w1.worker_id)
    value = open_route(rt, film)
    withdraw(rt, value, "设备停领")
    assert open_route(rt, film) is None
    (closed,) = rt.store.list_routes(rt.operator, open_only=False)
    assert (closed.state, closed.close_reason) == ("closed", "设备停领")
    with pytest.raises(m.WorkflowError, match="route_required"):
        claim_remote(rt, w1, film, "restore")
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        withdraw(rt, value)  # already closed: its version moved on
    route(rt, film, w2.worker_id)
    claim_remote(rt, w2, film, "restore")


def test_a_claimed_route_is_never_withdrawn_or_repinned(rt):
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    film = auto_film(rt)
    route(rt, film, w1.worker_id)
    claim_remote(rt, w1, film, "restore")
    with pytest.raises(m.WorkflowError, match="route_claimed"):
        withdraw(rt, open_route(rt, film))
    with pytest.raises(m.WorkflowError, match="route_claimed"):
        set_pin(rt, film, w2.worker_id)
    assert open_route(rt, film).worker_id == w1.worker_id


def test_repin_withdraws_an_unclaimed_route_but_keeps_a_matching_one(rt):
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    film = auto_film(rt)
    route(rt, film, w1.worker_id)
    set_pin(rt, film, w1.worker_id)  # already there: the route stays
    assert open_route(rt, film).worker_id == w1.worker_id
    set_pin(rt, film, w2.worker_id)  # 改派
    assert open_route(rt, film) is None
    route(rt, film, w2.worker_id, kind="pinned")
    claim_remote(rt, w2, film, "restore")


def test_the_route_closes_after_restore_and_asr_is_the_controllers(rt):
    """OD7 R4/R5: the worker's route closes once its restore is done (the
    slot is free); ASR and translate are the controller's, on its own
    implicit route, closed when done. (The worker was approved for ASR
    before the upgrade.)"""
    w = enrol_remote(rt, "5060 Ti-1", stages=("restore", "asr"))
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    resolve_remote(rt, w, claim_remote(rt, w, film, "restore"))
    assert open_route(rt, film) is None
    (done,) = rt.store.list_routes(rt.operator, open_only=False)
    assert done.close_reason == "完成"
    route(rt, film, w.worker_id)
    with pytest.raises(m.WorkflowError, match="route_required"):
        claim_remote(rt, w, film, "asr")
    d = dispatcher(rt)
    assert d.run_once() is True  # 转本机: the worker's unclaimed route goes
    assert stage(rt, film, "asr").status == "succeeded"
    assert d.run_once() is True
    assert stage(rt, film, "translate").status == "succeeded"
    routes = rt.store.list_routes(rt.operator, open_only=False)
    assert [(r.kind, r.state) for r in routes] == [
        ("auto", "closed"),
        ("auto", "closed"),
        ("local", "closed"),
    ]


def test_single_machine_runs_on_one_local_route_closed_at_the_end(rt):
    film = add_film(rt)
    d = dispatcher(rt)
    assert [d.run_once() for _ in range(4)] == [True, True, True, False]
    (local,) = rt.store.list_routes(rt.operator, open_only=False)
    assert (local.worker_id, local.kind, local.state, local.reason) == (
        rt.identity.worker_id,
        "local",
        "closed",
        "本机",
    )
    assert open_assignments(rt) == []
    assert stage(rt, film, "translate").status == "succeeded"


def test_a_new_revision_closes_the_old_route(rt):
    from taskpaw_v3.tests.test_workflow_plans import new_revision

    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    new_revision(rt, film)
    assert open_route(rt, film) is None
    (old,) = rt.store.list_routes(rt.operator, open_only=False)
    assert (old.input_revision, old.close_reason) == (1, "新版本")


def test_a_new_revision_keeps_a_claimed_route_until_its_work_resolves(rt):
    """IR6: the old revision still runs on the worker, so its route (and the
    worker's slot) stays until that assignment resolves."""
    from taskpaw_v3.tests.test_workflow_plans import new_revision

    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    claimed = claim_remote(rt, w, film, "restore")
    new_revision(rt, film)
    old = open_route(rt, film, 1)
    assert old is not None and old.worker_id == w.worker_id
    assert old in rt.store.routing_snapshot(rt.operator).routes
    resolve_remote(rt, w, claimed)
    assert open_route(rt, film, 1) is None
    (closed,) = rt.store.list_routes(rt.operator, open_only=False)
    assert closed.close_reason == "新版本"


def test_the_snapshot_carries_an_old_revision_routes_own_facts(rt):
    """CX5: the old revision's route is accounted with the old revision's
    claim, stages and size, not the current revision's (or none at all)."""
    from taskpaw_v3.tests.test_workflow_plans import new_revision

    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    claim_remote(rt, w, film, "restore")
    new_revision(rt, film)
    snap = rt.store.routing_snapshot(rt.operator)
    (old,) = snap.older_revisions
    assert (old.film_id, old.input_revision, old.restore) == (film, 1, "running")
    assert (old.claimed_by, old.claimed_stage) == (w.worker_id, "restore")
    assert old.source_bytes > 0 and old.max_width == 1920 and old.duration_ms
    current = [f for f in snap.films if f.film_id == film]
    assert [(f.input_revision, f.claimed_by) for f in current] == [(2, None)]


def test_a_failed_gpu_stage_closes_its_route_and_its_retry_routes_again(rt):
    """IR2: a failed stage waiting for its retry holds no worker slot."""
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    film = auto_film(rt)
    route(rt, film, w1.worker_id)
    resolve_remote(rt, w1, claim_remote(rt, w1, film, "restore"), "failed")
    assert open_route(rt, film) is None
    (closed,) = rt.store.list_routes(rt.operator, open_only=False)
    assert closed.close_reason == "失败"
    job = stage(rt, film, "restore")
    rt.store.retry_stage(
        rt.operator,
        rt.meta(),
        m.StageRetry(job_id=job.job_id, expected_version=job.version, reason="重试"),
    )
    route(rt, film, w2.worker_id)
    assert claim_remote(rt, w2, film, "restore")[0].status == "running"


def test_a_route_needs_unfinished_compute_work(rt):
    film = add_film(rt)
    d = dispatcher(rt)
    while d.run_once():
        pass
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        route(rt, film, rt.identity.worker_id, kind="local")


def test_route_replay_is_idempotent(rt):
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    meta = rt.meta()
    command = m.RouteOpen(
        route_id=identity(),
        film_id=film,
        input_revision=1,
        worker_id=w.worker_id,
        kind="auto",
        expected_pin_version=1,
        reason="自动",
    )
    first = rt.store.route_film(rt.operator, meta, command)
    assert rt.store.route_film(rt.operator, meta, command) == first
    assert len(rt.store.list_routes(rt.operator)) == 1


# ── 判定已停止 ───────────────────────────────────────────────────────────
def crashed(rt, clock, *, quiet=True):
    """A remote restore whose worker restarted: assignment and GPU unknown;
    `quiet`: five minutes pass without a renewal (IR4)."""
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    claimed = claim_remote(rt, w, film, "restore")
    w = boot(rt, w, identity())
    if quiet:
        clock.advance()
    return w, film, claimed


def test_attest_waits_five_minutes_after_the_last_renewal(rt, clock):
    """IR4: a worker that renewed its claim lately may still be running it."""
    _, film, (result, _) = crashed(rt, clock, quiet=False)
    clock.advance(timedelta(minutes=4))
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        attest(rt, result.assignment_id)
    clock.advance(timedelta(minutes=1, seconds=1))
    attest(rt, result.assignment_id)
    assert stage(rt, film, "restore").status == "failed"


def test_attest_refuses_the_controllers_own_work(rt, clock):
    """IR4: the controller proves its own stop locally (restart settlement)."""
    film = add_film(rt)
    route(rt, film, rt.identity.worker_id, kind="local")
    job, worker = stage(rt, film, "restore"), rt.worker()
    tools = rt.executors["restore"].tools()
    claimed = rt.store.claim(
        rt.worker_auth,
        rt.meta(),
        m.ClaimRequest(
            job_id=job.job_id,
            expected_version=job.version,
            attempt_id=identity(),
            operation_id=identity(),
            worker_id=worker.worker_id,
            boot_id=rt.boot_id,
            profile_revision=tools.profile_revision,
            config_digest=tools.config_digest,
            resource_id=worker.physical_binding.resource_id,
            tool_versions=tools,
        ),
    )
    clock.advance()
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        attest(rt, claimed.assignment_id)


def test_attest_stopped_resolves_unknown_work_and_frees_the_gpu(rt, clock):
    w, film, (result, _) = crashed(rt, clock)
    assert stage(rt, film, "restore").status == "unknown"
    attest(rt, result.assignment_id)
    job = stage(rt, film, "restore")
    assert job.status == "failed"
    (attempt,) = rt.store.list_attempts(rt.operator, job.job_id)
    assert {e.kind for e in attempt.evidence} == {ATTESTED, failed_kind("cancelled")}
    assert attempt.status == "failed" and should_retry([attempt])  # a free retry
    assert not [o for o in rt.store.list_open_assignments(rt.operator)]
    assert open_route(rt, film) is None
    (closed,) = rt.store.list_routes(rt.operator, open_only=False)
    assert closed.close_reason == "判定已停止"
    # the worker's boot reconciles and its GPU takes the next film
    w = boot(rt, w, w.boot_id)
    rt.store.retry_stage(
        rt.operator,
        rt.meta(),
        m.StageRetry(job_id=job.job_id, expected_version=job.version, reason="重派"),
    )
    route(rt, film, w.worker_id)
    assert claim_remote(rt, w, film, "restore")[0].status == "running"


def test_attest_keeps_a_proven_success(rt, clock):
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    route(rt, film, w.worker_id)
    claimed = claim_remote(rt, w, film, "restore")
    finish_remote(rt, w, claimed)
    boot(rt, w, identity())
    clock.advance()
    attest(rt, claimed[0].assignment_id)
    assert stage(rt, film, "restore").status == "succeeded"
    assert not rt.store.list_open_assignments(rt.operator)


def test_attest_needs_operator_attestation_evidence(rt, clock):
    w, film, (result, command) = crashed(rt, clock)
    current = next(o.assignment for o in rt.store.list_open_assignments(rt.operator))
    for kinds in ((ATTESTED,), (failed_kind("cancelled"),)):
        with pytest.raises(m.WorkflowError, match="precondition_failed"):
            rt.store.attest_stopped(
                rt.operator,
                rt.meta(),
                m.StoppedAttestation(
                    assignment_id=current.assignment_id,
                    attempt_id=current.attempt_id,
                    expected_version=current.version,
                    operation_id=command.operation_id,
                    reason="缺少证明",
                    evidence=tuple(rt.evidence(kind) for kind in kinds),
                ),
            )
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        rt.store.attest_stopped(
            rt.operator,
            rt.meta(),
            m.StoppedAttestation(
                assignment_id=current.assignment_id,
                attempt_id=identity(),
                expected_version=current.version,
                operation_id=command.operation_id,
                reason="错误的尝试",
                evidence=(
                    rt.evidence(ATTESTED),
                    rt.evidence(failed_kind("cancelled")),
                ),
            ),
        )
    attest(rt, result.assignment_id)
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        attest_again = m.StoppedAttestation(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            operation_id=command.operation_id,
            reason="重复",
            evidence=(rt.evidence(ATTESTED), rt.evidence(failed_kind("cancelled"))),
        )
        rt.store.attest_stopped(rt.operator, rt.meta(), attest_again)


def test_attest_refuses_component_assignments(rt):
    """Return and scrape run on the controller and settle by restart, not attestation."""
    film = add_film(rt)
    d = dispatcher(rt)
    for _ in range(3):
        assert d.run_once() is True
    job = stage(rt, film, "return")
    tools = m.ToolVersions(
        tool="synthetic",
        tool_version="1",
        model="synthetic",
        model_version="1",
        profile="synthetic",
        profile_revision=1,
        config_digest="c" * 64,
    )
    claimed = rt.store.claim(
        rt.receiver_auth,
        rt.meta(),
        m.ClaimRequest(
            job_id=job.job_id,
            expected_version=job.version,
            attempt_id=identity(),
            operation_id=identity(),
            worker_id=None,
            boot_id=rt.boot_id,
            profile_revision=1,
            config_digest=tools.config_digest,
            resource_id=None,
            tool_versions=tools,
        ),
    )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        attest(rt, claimed.assignment_id)


def test_routing_snapshot_reads_films_routes_workers_and_samples(rt):
    w = enrol_remote(rt, "5070 Ti", gpu=GPU_16)
    film = auto_film(rt, width=3840, height=2160)
    route(rt, film, w.worker_id)
    resolve_remote(
        rt,
        w,
        claim_remote(rt, w, film, "restore"),
        note=RESTORE_DONE.format(applied=1, reused=0),
        timing=m.RestoreTiming(wall=1800.0),
    )
    snap = rt.store.routing_snapshot(rt.operator)
    (entry,) = [f for f in snap.films if f.film_id == film]
    assert (entry.restore, entry.asr, entry.max_width, entry.max_height) == (
        "succeeded",
        "pending",
        3840,
        2160,
    )
    assert entry.pin_worker_id is None and entry.pin_version == 1
    assert entry.source_bytes > 0 and entry.claimed_by is None
    assert snap.routes == ()  # OD7 R4: closed once the restore succeeded
    remote = next(x for x in snap.workers if x.worker_id == w.worker_id)
    assert (remote.gpu_model, remote.vram_mib, remote.boot_reconciled) == (
        GPU_16[0],
        GPU_16[1],
        True,
    )
    assert {(p.stage, p.max_width) for p in remote.profiles} == {("restore", 3840)}
    (sample,) = snap.samples
    assert (sample.stage, sample.worker_id, sample.tier) == (
        "restore",
        w.worker_id,
        "standard",
    )
    assert sample.wall_seconds == 1800.0


def test_rate_samples_skip_reuse_only_and_mixed_attempts(rt, monkeypatch):
    """CX3: a sample is GPU throughput only when the tool ran on every volume.
    The evidence says how many volumes ran but not their durations, so a mixed
    attempt is left out too. (Worker ASR as before OD7: in-flight work.)"""
    from taskpaw_v3.workflow import film_store

    monkeypatch.setattr(film_store, "_CONTROLLER_ONLY", frozenset())
    timing = m.RestoreTiming(wall=1800.0)
    full, reused = (
        RESTORE_DONE.format(applied=1, reused=0),
        RESTORE_DONE.format(applied=0, reused=1),
    )
    cases = {  # worker: (restore note, Jasna timing, ASR note)
        "full": (full, timing, ASR_DONE.format(summary="generated_ja 1")),
        "reused": (reused, None, ASR_DONE.format(summary="reused_zh 1")),
        "mixed": (
            RESTORE_DONE.format(applied=1, reused=1),
            timing,
            ASR_DONE.format(summary="source_ja 1，generated_ja 1"),
        ),
        "untimed": (full, None, ASR_DONE.format(summary="no_speech 1")),
    }
    names = {}
    for name, (restore_note, wall, asr_note) in cases.items():
        w = enrol_remote(rt, name, gpu=GPU_16, stages=("restore", "asr"))
        names[w.worker_id] = name
        film = auto_film(rt)
        route(rt, film, w.worker_id)
        claimed = claim_remote(rt, w, film, "restore")
        resolve_remote(rt, w, claimed, note=restore_note, timing=wall)
        route(rt, film, w.worker_id)  # the pre-OD7 route stayed open for ASR
        resolve_remote(rt, w, claim_remote(rt, w, film, "asr"), note=asr_note)
    samples = {
        (s.stage, names[s.worker_id])
        for s in rt.store.routing_snapshot(rt.operator).samples
    }
    assert samples == {("restore", "full"), ("asr", "full"), ("asr", "untimed")}


def test_rate_samples_keep_the_gpu_model_they_ran_on(rt):
    """CX6: a sample is the GPU the attempt held at claim, not the worker's
    current card: after a swap the old samples stay with the old model."""
    w = enrol_remote(rt, "5060 Ti-1", gpu=GPU_16)
    done = RESTORE_DONE.format(applied=1, reused=0)

    def restore(remote):
        film = auto_film(rt)
        route(rt, film, remote.worker_id)
        claimed = claim_remote(rt, remote, film, "restore")
        resolve_remote(
            rt, remote, claimed, note=done, timing=m.RestoreTiming(wall=1800.0)
        )

    restore(w)
    w = swap_gpu(rt, w, GPU_16_FAST)
    restore(w)
    samples = rt.store.routing_snapshot(rt.operator).samples
    assert [s.gpu_model for s in samples] == [GPU_16_FAST[0], GPU_16[0]]


# ── v3 → v4 migration ────────────────────────────────────────────────────
def as_v3(path) -> None:
    """Turn a v4 file back into the exact v3 format."""
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE film_routes")
        conn.execute("DROP TABLE film_pins")
        conn.execute("ALTER TABLE film_plans RENAME TO plans_v4")
        conn.execute(schema.V3_TABLES["film_plans"])
        conn.execute("INSERT INTO film_plans SELECT * FROM plans_v4")
        conn.execute("DROP TABLE plans_v4")
        conn.execute("PRAGMA user_version=3")


def test_v3_upgrade_keeps_every_plan_and_pins_it_to_auto(tmp_path):
    first = runtime(tmp_path / "wf")
    done = add_film(first)
    waiting = add_film(first)
    d = dispatcher(first)
    for _ in range(3):
        assert d.run_once() is True  # `done` runs all its compute stages
    before = [
        (p.film_id, p.ltx_model, p.machine, p.confirmed_at, p.backfilled)
        for p in first.store.list_plans(first.operator)
    ]
    worker = first.identity.worker_id
    first.close()
    path = tmp_path / "wf" / "workflow.db"
    as_v3(path)
    again = runtime(tmp_path / "wf")
    try:
        plans = again.store.list_plans(again.operator)
        assert [
            (p.film_id, p.ltx_model, p.machine, p.confirmed_at, p.backfilled)
            for p in plans
        ] == before
        assert {p.machine for p in plans} == {worker}  # history kept
        for film in (done, waiting):
            pin = again.store.get_pin(again.operator, film, 1)
            assert (pin.worker_id, pin.version) == (None, 1)  # D9/OD1: 自动
        assert again.store.list_routes(again.operator, open_only=False) == ()
        d = dispatcher(again)
        while d.run_once():
            pass
        assert stage(again, waiting, "translate").status == "succeeded"
    finally:
        again.close()
    backups = list((tmp_path / "wf").glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute(
            "SELECT \"notnull\" FROM pragma_table_info('film_plans') WHERE name='machine'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("point", ["rebuild", "routes", "pins", "commit"])
def test_v3_upgrade_failure_keeps_the_v3_file(tmp_path, monkeypatch, point):
    rt = runtime(tmp_path / "wf")
    add_film(rt)
    rt.close()
    path = tmp_path / "wf" / "workflow.db"
    as_v3(path)
    with sqlite3.connect(path) as conn:
        plans_before = conn.execute("SELECT * FROM film_plans").fetchall()
    real_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if point == "rebuild" and sql.startswith("ALTER TABLE film_plans"):
                raise sqlite3.OperationalError("injected rebuild failure")
            if point == "routes" and sql.startswith("CREATE TABLE film_routes"):
                raise sqlite3.OperationalError("injected routes DDL failure")
            if point == "pins" and sql.startswith("INSERT INTO film_pins"):
                raise sqlite3.OperationalError("injected pin failure")
            return super().execute(sql, *args, **kwargs)

        def commit(self):
            if point == "commit" and self.in_transaction:
                raise sqlite3.OperationalError("injected commit failure")
            return super().commit()

    def connect(database, *args, **kwargs):
        if str(database).endswith("?mode=rw"):
            kwargs["factory"] = FailingConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(schema.sqlite3, "connect", connect)
    with pytest.raises(schema.SchemaError, match="migration_failed"):
        schema.connect_existing(path)
    monkeypatch.undo()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert not {"film_pins", "film_routes", "film_plans_v3"} & names
        assert conn.execute("SELECT * FROM film_plans").fetchall() == plans_before
    schema.validate_schema(sqlite3.connect(path), version=3)
    assert len(list((tmp_path / "wf").glob("*.bak"))) == 1


def test_a_modified_route_table_is_rejected(tmp_path):
    rt = runtime(tmp_path / "wf")
    rt.close()
    path = tmp_path / "wf" / "workflow.db"
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX one_open_route_per_film")
    with pytest.raises(schema.SchemaError, match="invalid_schema"):
        schema.connect_existing(path)


# ── CX7: the store's own source admissibility gates routing ──────────────
@pytest.fixture
def changed(rt, monkeypatch):
    """Film ids whose intake source is not admissible: the one predicate the
    claim uses (`_require_source_admissible`) refuses them."""
    ids: set[str] = set()
    real = rt.store._require_source_admissible

    def guard(row):
        if row["film_id"] in ids:
            raise m.WorkflowError("source_not_admissible")
        real(row)

    monkeypatch.setattr(rt.store, "_require_source_admissible", guard)
    return ids


def snapshot_film(rt, film_id):
    return next(
        f for f in rt.store.routing_snapshot(rt.operator).films if f.film_id == film_id
    )


def test_route_film_refuses_an_inadmissible_source_until_it_recovers(rt, changed):
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    changed.add(film)
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        route(rt, film, w.worker_id)
    assert open_route(rt, film) is None
    changed.clear()
    route(rt, film, w.worker_id)
    assert open_route(rt, film) is not None


def test_the_snapshot_flags_an_inadmissible_source_and_recovers(rt, changed):
    film = auto_film(rt)
    assert snapshot_film(rt, film).source_admissible is True
    changed.add(film)
    assert snapshot_film(rt, film).source_admissible is False
    changed.clear()
    assert snapshot_film(rt, film).source_admissible is True


def run_pass(rt, clock, workers):
    return routing.run_pass(
        rt.store,
        rt.operator,
        rt.meta,
        controller_id=rt.identity.worker_id,
        live={
            w.worker_id: scheduler.WorkerLive(last_seen=clock.now()) for w in workers
        },
        now=clock.now(),
        executors=rt.executors,
        remote_enabled=True,
    )


def test_a_pass_releases_an_unclaimed_route_of_a_changed_source(rt, clock, changed):
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    assert [d.film_id for d in run_pass(rt, clock, [w]).routed] == [film]
    changed.add(film)
    outcome = run_pass(rt, clock, [w])
    assert [x.reason for x in outcome.withdrawn] == [scheduler.SOURCE_CHANGED]
    assert outcome.routed == ()
    assert outcome.planned.waiting[film] == scheduler.SOURCE_CHANGED
    assert open_route(rt, film) is None
    again = run_pass(rt, clock, [w])  # stays released, keeps saying why
    assert (again.routed, again.withdrawn) == ((), ())
    assert again.planned.waiting[film] == scheduler.SOURCE_CHANGED
    changed.clear()
    assert [d.film_id for d in run_pass(rt, clock, [w]).routed] == [film]


def test_a_pass_keeps_a_claimed_route_of_a_changed_source(rt, clock, changed):
    w = enrol_remote(rt, "5060 Ti-1")
    film = auto_film(rt)
    run_pass(rt, clock, [w])
    claim_remote(rt, w, film, "restore")
    changed.add(film)
    outcome = run_pass(rt, clock, [w])
    assert (outcome.routed, outcome.withdrawn) == ((), ())
    assert open_route(rt, film).worker_id == w.worker_id


def test_changed_sources_cannot_exhaust_a_workers_route_slots(rt, clock, changed):
    w = enrol_remote(rt, "5060 Ti-1")
    bad = [auto_film(rt) for _ in range(3)]
    changed.update(bad)
    fine = [auto_film(rt) for _ in range(2)]
    outcome = run_pass(rt, clock, [w])
    assert {d.film_id for d in outcome.routed} == {fine[0]}
    assert outcome.planned.waiting[fine[1]] == "第 1 位 · 哪台先空给哪台"
    assert all(outcome.planned.waiting[f] == scheduler.SOURCE_CHANGED for f in bad)


def test_a_source_invalidated_mid_pass_is_a_tolerated_race(
    rt, clock, changed, monkeypatch
):
    """CX8: intake dirties a source after the snapshot but before its route
    write; that film is skipped, the rest of the pass still routes."""
    w = enrol_remote(rt, "5060 Ti-1")
    other = enrol_remote(rt, "5060 Ti-2")
    first, second = auto_film(rt), auto_film(rt)
    real = rt.store.route_film

    def route_film(auth, meta, command):
        if command.film_id == first:
            changed.add(first)  # invalidated between snapshot and write
        return real(auth, meta, command)

    monkeypatch.setattr(rt.store, "route_film", route_film)
    outcome = run_pass(rt, clock, [w, other])
    assert {d.film_id for d in outcome.planned.routes} == {first, second}
    assert [d.film_id for d in outcome.routed] == [second]
    assert open_route(rt, first) is None
    assert open_route(rt, second) is not None
