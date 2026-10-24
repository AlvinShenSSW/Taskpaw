"""P2-1 (#292): the controller's stages next to remote workers.

(C1, translate built without WhisperJAV, is in test_workflow_pipeline_e2e.) OD7: once a remote worker
is configured the controller's own GPU takes no restore but runs ASR; C12: a
restore that needs no GPU still runs on the controller. Without remote workers
every stage runs locally, as in phase 1.
"""

from __future__ import annotations

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    dispatcher as local_dispatcher,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import (
    claim_remote,
    enrol_remote,
    finish_remote,
    resolve_remote,
    route,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import routing
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.local_dispatch import NO_GPU_DEVICE, REMOTE_GPU
from taskpaw_v3.workflow.scheduler import HAND_OVER, WorkerLive

WAIT = f"{REMOTE_GPU}：5060 Ti-1"  # IR5: the wait names the remote worker


def dispatcher(rt, **kw):
    """The controller's dispatcher with `workflow.remote_workers` on."""
    return local_dispatcher(rt, remote_enabled=True, **kw)


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def test_with_a_remote_worker_the_controller_takes_no_gpu_stage(rt):
    enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)
    d = dispatcher(rt)
    assert d.run_once() is False
    assert d.status().waiting == {film: WAIT}
    assert stage(rt, film, "restore").status == "pending"
    assert rt.store.list_routes(rt.operator, open_only=False) == ()


def test_a_stage_needing_no_gpu_still_runs_on_the_controller(rt):
    """C12: an all-reused restore needs no GPU; OD7: the ASR after it is the
    controller's too, on the same local route."""
    rt.executors["restore"] = SyntheticExecutor("restore", uses_gpu=False)
    enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)
    d = dispatcher(rt)
    assert d.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    assert d.run_once() is True
    assert stage(rt, film, "asr").status == "succeeded"
    local = rt.store.film_route(rt.operator, film, 1)
    assert (local.worker_id, local.kind) == (rt.identity.worker_id, "local")


def test_the_controller_transcribes_and_translates_after_a_remote_restore(rt):
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)
    route(rt, film, w.worker_id)
    resolve_remote(rt, w, claim_remote(rt, w, film, "restore"))
    d = dispatcher(rt)
    assert d.run_once() is True
    assert stage(rt, film, "asr").status == "succeeded"
    assert d.run_once() is True
    assert stage(rt, film, "translate").status == "succeeded"


def test_the_controller_takes_over_an_unclaimed_route_for_a_local_stage(rt):
    rt.executors["restore"] = SyntheticExecutor("restore", uses_gpu=False)
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt, plan=False)
    rt.store.confirm_plan(
        rt.operator,
        rt.meta(),
        m.FilmPlanConfirm(
            film_id=film, input_revision=1, ltx_model="standard", machine=None
        ),
    )
    route(rt, film, w.worker_id)
    d = dispatcher(rt)
    assert d.run_once() is True  # withdrew the remote route, ran it here
    routes = rt.store.list_routes(rt.operator, open_only=False)
    assert [(r.worker_id, r.state, r.close_reason) for r in routes] == [
        (w.worker_id, "closed", "转本机"),
        (rt.identity.worker_id, "open", None),
    ]


def test_a_claimed_remote_route_is_never_taken_over(rt):
    """The controller's ASR waits while the worker's restore owner is not
    settled (its route stays open, claimed), then runs."""
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)
    route(rt, film, w.worker_id)
    claimed = claim_remote(rt, w, film, "restore")
    finish_remote(rt, w, claimed)  # restore succeeded; its owner is not settled
    d = dispatcher(rt)
    assert d.run_once() is False
    assert d.status().waiting == {film: "已派往 5060 Ti-1"}
    resolve_remote(rt, w, claimed)  # settles the owner and closes the route
    assert d.run_once() is True
    assert stage(rt, film, "asr").status == "succeeded"


def test_in_remote_mode_the_controller_never_takes_a_gpu_stage(rt):
    """OD1 correction: the controller's GPU takes no restore/ASR whatever the
    verified set is. A worker still waiting for its pairing counts for
    nothing and a revoked one leaves none; the film waits and says why."""
    rt.store.register_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRegistration(
            worker_id=identity(),
            device_id=identity(),
            name="待配对",
            address="192.0.2.31",
            port=5682,
        ),
    )
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)
    d = dispatcher(rt)
    assert d.run_once() is False
    assert d.status().waiting == {film: WAIT}
    worker = rt.store.get_worker(rt.operator, w.worker_id).worker
    rt.store.revoke_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRevocation(
            worker_id=w.worker_id, expected_version=worker.version, reason="撤销"
        ),
    )
    for _ in range(3):  # no verified worker left: still no local GPU stage
        assert d.run_once() is False
        assert d.status().waiting == {film: f"{REMOTE_GPU}：{NO_GPU_DEVICE}"}
        assert stage(rt, film, "restore").status == "pending"


def test_the_pass_and_the_dispatcher_agree_on_a_reuse_only_restore(rt):
    """IR3: one GPU-need rule for both. The controller runs the all-reused
    restore and (OD7) the ASR after it; the pass routes nothing."""
    rt.executors["restore"] = SyntheticExecutor("restore", uses_gpu=False)
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)

    def run_pass():
        return routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live={w.worker_id: WorkerLive(last_seen=rt.clock.now())},
            now=rt.clock.now(),
            executors=rt.executors,
            remote_enabled=True,
        )

    assert run_pass().routed == ()  # the restore is the controller's
    d = dispatcher(rt)
    assert d.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    outcome = run_pass()
    assert (outcome.routed, outcome.withdrawn) == ((), ())
    assert HAND_OVER not in [x.reason for x in outcome.withdrawn]
    assert d.run_once() is True
    assert stage(rt, film, "asr").status == "succeeded"


def test_turning_remote_workers_off_runs_restore_and_asr_locally_again(rt):
    """IR1/OD1: the single predicate is the setting (remote mode); with
    `remote_workers: false` an approved worker no longer keeps the controller
    off its GPU, and the pass routes nothing."""
    w = enrol_remote(rt, "5060 Ti-1")
    film = add_film(rt)
    on = dispatcher(rt)
    assert on.run_once() is False
    assert on.status().waiting == {film: WAIT}
    outcome = routing.run_pass(
        rt.store,
        rt.operator,
        rt.meta,
        controller_id=rt.identity.worker_id,
        live={w.worker_id: WorkerLive(last_seen=rt.clock.now())},
        now=rt.clock.now(),
        executors=rt.executors,
        remote_enabled=False,
    )
    assert (outcome.routed, outcome.withdrawn) == ((), ())
    off = local_dispatcher(rt)  # the flag is off (the default)
    assert off.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    assert off.run_once() is True
    assert stage(rt, film, "asr").status == "succeeded"
