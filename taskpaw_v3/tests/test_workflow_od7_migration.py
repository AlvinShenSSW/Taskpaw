"""#298 (P2-OD7) §3: in-flight work at the upgrade, end to end.

A real controller and worker (as in test_workflow_remote_e2e). "The old
controller" is the OD7 constants put back (ASR routed, a worker's route kept
open for ASR, no local-only ASR claim); "the old worker" claims ASR with a
WhisperJAV executor and an approval that includes it. The upgrade is a
controller restart without those patches.

1. ASR claimed on a worker: never cancelled; either build settles, uploads
   and finishes it, and its route closes. Interrupted, it becomes a pending
   ASR retry only the controller claims.
2. ASR routed but unclaimed: the route is withdrawn (「识别转本机」, or the
   dispatcher's 转本机) and the worker releases the film.
3. An old worker claiming ASR: 409 `route_required`; it drops the attempt.
4. Re-核验: the worker's changed probe is 待核验, no new routes until the
   one-click 核验 approves restore; then restore routes again.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest

from taskpaw_v3.tests.test_workflow_remote_e2e import (
    LONG,
    World,
    routed_film,
    until,
    wait_status,
)
from taskpaw_v3.tests.test_workflow_remote_e2e import (
    probeable_video as probeable_video,  # noqa: F401 (autouse)
)
from taskpaw_v3.tests.workflow_exec_helpers import fake
from taskpaw_v3.tests.workflow_route_helpers import route
from taskpaw_v3.workflow import (
    devices,
    film_store,
    local_dispatch,
    remote_attempts,
    remote_dispatch,
    routing,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import scheduler as sc
from taskpaw_v3.workflow.exec_asr import AsrExecutor
from taskpaw_v3.workflow.executors import ExecOutcome
from taskpaw_v3.workflow.local_runtime import _RESOURCE_NS
from taskpaw_v3.workflow.worker_client import ApiError, WorkerClient

OLD = ("restore", "asr")


@pytest.fixture(name="world")
def world_fixture(tmp_path, monkeypatch):
    value = World(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.close()


def old_controller(patch: pytest.MonkeyPatch) -> None:
    """The constants before OD7: ASR is routed to the worker on its route."""
    patch.setattr(sc, "_ROUTED", OLD)
    patch.setattr(routing, "_GPU_STAGES", OLD)
    patch.setattr(local_dispatch, "REMOTE_STAGES", frozenset(OLD))
    patch.setattr(film_store, "_GPU_STAGES", OLD)
    patch.setattr(film_store, "_CONTROLLER_ONLY", frozenset())
    patch.setattr(remote_attempts, "ROUTED_STAGES", OLD)


class GatedAsr(AsrExecutor):
    """The old worker's WhisperJAV, held at its start until `gate` is set."""

    def __init__(self, runtime: Any, exe: str, gate: threading.Event) -> None:
        super().__init__(runtime, exe, launch_prefix=fake("whisperjav"))
        self.gate, self.reached = gate, threading.Event()

    def run(self, context):  # type: ignore[no-untyped-def]
        self.reached.set()
        while not self.gate.wait(0.02):
            if context.cancel.is_set():
                return ExecOutcome.failed("cancelled", "已停止")
        return super().run(context)


def old_worker(world, patch: pytest.MonkeyPatch, gate: threading.Event) -> list[Any]:
    """The worker build before OD7: restore and ASR (the gated one)."""
    built: list[Any] = []

    def executors(runtime):
        asr = GatedAsr(runtime, str(world.tools.whisper_exe), gate)
        if runtime is not None:
            built.append(asr)
        return [world.tools.restore(runtime), asr]

    patch.setattr(remote_dispatch, "STAGES", OLD)
    world.executors = executors
    return built


def approve_as_before(world, worker_id: str) -> None:
    """The old 核验 (restore and ASR) of every profile the worker proposes."""
    rt = world.rt
    for _ in range(20):  # the worker's own writes may move the version
        worker = rt.store.get_worker(rt.operator, worker_id).worker
        cap = worker.capability
        assert cap is not None and cap.probed is not None
        probe, revision = cap.probed, cap.revision + 1
        try:
            rt.store.record_capabilities(
                rt.operator,
                rt.meta(),
                m.CapabilityUpdate(
                    worker_id=worker_id,
                    expected_version=worker.version,
                    capability=m.CapabilityRevision(
                        revision=revision,
                        declared=None,
                        probed=probe,
                        approved=m.ApprovedCapability(
                            revision=revision,
                            probe_revision=probe.revision,
                            approved_by=rt.operator.principal_id,
                            approved_at=rt.clock.now(),
                            profiles=probe.profiles,
                        ),
                    ),
                    physical_binding=m.PhysicalGpuBinding(
                        resource_id=str(
                            uuid.uuid5(
                                _RESOURCE_NS,
                                f"{probe.device_identity}|{probe.gpu_identity}",
                            )
                        ),
                        device_id=worker.device_id,
                        device_identity=probe.device_identity,
                        gpu_identity=probe.gpu_identity,
                        capability_revision=revision,
                        evidence=rt.evidence("physical_identity"),
                    ),
                ),
            )
        except m.WorkflowError as exc:
            if exc.code != "cas_conflict":
                raise
            continue
        devices._enable(rt, worker_id)
        return
    raise AssertionError("approval kept losing its CAS")


def asr_on_the_old_worker(world, monkeypatch, gate):
    """An old worker running ASR under the old controller."""
    built = old_worker(world, monkeypatch, gate)
    with monkeypatch.context() as before:
        old_controller(before)
        worker = world.join()
        worker_id = world.issued["w1"]["worker_id"]
        approve_as_before(world, worker_id)
        film = routed_film(world)
        until(lambda: built and built[-1].reached.is_set(), LONG)
        wait_status(world, film, "asr", "running")
        world.restart_controller()  # the upgrade: OD7 from here on
    return worker, worker_id, film


def routes_of(world, film: str) -> list[tuple[str, str, str | None]]:
    return [
        (r.worker_id, r.state, r.close_reason)
        for r in world.rt.store.list_routes(world.rt.operator, open_only=False)
        if r.film_id == film
    ]


# ── §3.1 ────────────────────────────────────────────────────────────────
def test_a_claimed_worker_asr_finishes_on_the_worker_and_its_route_closes(
    world, monkeypatch
):
    gate = threading.Event()
    worker, worker_id, film = asr_on_the_old_worker(world, monkeypatch, gate)
    assert world.status(film, "asr") == "running"  # not cancelled by the upgrade
    gate.set()
    wait_status(world, film, "scrape", "succeeded")
    (asr,) = world.attempts(film, "asr")
    assert asr.worker_id == worker_id and asr.status == "succeeded"
    (translate,) = world.attempts(film, "translate")
    assert translate.worker_id == world.rt.identity.worker_id
    assert (worker_id, "closed", "完成") in routes_of(world, film)
    assert world.jasna_runs() == 1
    world.invariants()


def test_an_interrupted_worker_asr_is_retried_by_the_controller_only(
    world, monkeypatch
):
    gate = threading.Event()
    worker, worker_id, film = asr_on_the_old_worker(world, monkeypatch, gate)
    monkeypatch.setattr(remote_dispatch, "STAGES", ("restore",))  # the new build
    del world.executors
    worker = world.restart_worker(worker)  # stops the running ASR
    wait_status(world, film, "asr", "succeeded")
    first, *rest = world.attempts(film, "asr")
    assert first.worker_id == worker_id and first.status == "failed"
    assert {a.worker_id for a in rest} == {world.rt.identity.worker_id}
    assert rest[-1].status == "succeeded"
    assert world.jasna_runs() == 1
    world.invariants()


# ── §3.2 ────────────────────────────────────────────────────────────────
def test_an_unclaimed_restored_route_is_withdrawn_and_released(world, monkeypatch):
    with monkeypatch.context() as before:
        old_controller(before)
        worker = world.join()  # the new worker build: it never claims ASR
        worker_id = world.issued["w1"]["worker_id"]
        film = routed_film(world)
        wait_status(world, film, "restore", "succeeded")
        held = world.rt.store.film_route(world.rt.operator, film, 1)
        assert held is not None and held.worker_id == worker_id  # kept for ASR
        assert world.status(film, "asr") == "pending"
        world.restart_controller()
    wait_status(world, film, "asr", "succeeded")
    (asr,) = world.attempts(film, "asr")
    assert asr.worker_id == world.rt.identity.worker_id
    closed = [r for r in routes_of(world, film) if r[0] == worker_id]
    assert closed == [(worker_id, "closed", closed[0][2])]
    assert closed[0][2] in (sc.ASR_LOCAL, local_dispatch.TAKE_OVER)
    until(lambda: not worker.dispatcher.staged(), LONG)  # released
    world.invariants()


# ── §3.3 ────────────────────────────────────────────────────────────────
def test_an_old_worker_claiming_asr_gets_route_required_and_drops_it(
    world, monkeypatch
):
    gate = threading.Event()
    gate.set()
    old_worker(world, monkeypatch, gate)
    refused: list[tuple[str, int, str]] = []
    real = WorkerClient.claim

    def claim(self, body):
        try:
            return real(self, body)
        except ApiError as exc:
            refused.append((body["job_id"], exc.status, exc.code))
            raise

    monkeypatch.setattr(WorkerClient, "claim", claim)
    worker = world.join()
    worker_id = world.issued["w1"]["worker_id"]
    approve_as_before(world, worker_id)
    # the controller takes no ASR until its restart (暂停派发 would pause the
    # worker too): no ASR executor here before the restore can finish
    world.rt.executors.pop("asr")
    film = routed_film(world)
    wait_status(world, film, "restore", "succeeded")
    host = world.host
    assert host is not None and host.scheduler is not None
    assert host.scheduler.stop()  # nothing withdraws the route below
    route(world.rt, film, worker_id)  # as an old controller left it
    asr_job = world.stage(film, "asr").job_id
    until(lambda: (asr_job, 409, "route_required") in refused, LONG)
    assert world.attempts(film, "asr") == []
    staging = world.tmp / "w1" / "staging" / "attempts"
    until(lambda: not list(staging.glob("*.json")), LONG)  # dropped (4xx)
    world.restart_controller()
    wait_status(world, film, "asr", "succeeded")
    (asr,) = world.attempts(film, "asr")
    assert asr.worker_id == world.rt.identity.worker_id
    del worker
    world.invariants()


# ── §3.4 ────────────────────────────────────────────────────────────────
def test_after_the_upgrade_one_click_核验_brings_restore_routes_back(
    world, monkeypatch
):
    gate = threading.Event()
    gate.set()
    old_worker(world, monkeypatch, gate)
    worker = world.join()
    worker_id = world.issued["w1"]["worker_id"]
    approve_as_before(world, worker_id)
    monkeypatch.setattr(remote_dispatch, "STAGES", ("restore",))  # upgraded
    del world.executors
    worker = world.restart_worker(worker)

    def record() -> m.WorkerRecord:
        return world.rt.store.get_worker(world.rt.operator, worker_id).worker

    until(lambda: record().enrollment_state == "pending_validation", LONG)
    film = routed_film_or_wait(world)
    assert world.rt.store.film_route(world.rt.operator, film, 1) is None

    def approved() -> bool:
        try:
            devices.approve(world.rt, worker_id, record().version, restore=True)
        except m.WorkflowError as exc:
            if exc.code in {"cas_conflict", "precondition_failed"}:
                return False
            raise
        return True

    until(approved, LONG)
    profiles = record().capability.approved.profiles  # type: ignore[union-attr]
    assert [p.allowed_stages for p in profiles] == [("restore",)]
    world.wake()
    wait_status(world, film, "asr", "succeeded")
    (restore,) = world.attempts(film, "restore")
    assert restore.worker_id == worker_id
    (asr,) = world.attempts(film, "asr")
    assert asr.worker_id == world.rt.identity.worker_id
    del worker
    world.invariants()


def routed_film_or_wait(world) -> str:
    """A planned film the pass holds: the worker is 待核验 (no new route)."""
    from taskpaw_v3.tests import test_workflow_return_step as ret

    film = ret.add_real_film(world.rt)
    seen = len(world.passes)
    world.wake()
    until(
        lambda: any(
            "未核验" in (outcome.planned.waiting.get(film) or "")
            for outcome in world.passes[seen:]
        ),
        LONG,
    )
    return film
