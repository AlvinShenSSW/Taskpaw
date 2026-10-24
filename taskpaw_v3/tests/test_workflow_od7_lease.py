"""#298 (P2-OD7) R3: the controller's ASR shares the process-wide `gpu_lease`
with manual Jasna / avsubs tasks, fair both ways, on an injected clock.

A manual task holds the GPU → the ASR waits 「等待 GPU：<holder>」; the
release reserves the GPU for the waiting ASR (a manual retry inside the
window is refused); while ASR holds it a manual task waits 「影片工作流：
<name>」, and after the ASR the GPU is reserved for that manual task. A
remote-mode restore never queues on the lease."""

from __future__ import annotations

import threading

import pytest

from taskpaw_v3.core.gpu_lease import RESERVE_MIN_S, GpuLease
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    dispatcher as local_dispatcher,
)
from taskpaw_v3.tests.workflow_route_helpers import (
    claim_remote,
    enrol_remote,
    resolve_remote,
    route,
)
from taskpaw_v3.workflow.executors import ExecOutcome, SyntheticExecutor

MANUAL = ("jasna", 7)
LONG = 30.0  # a condition wait's ceiling; never a timing budget


class Clock:
    """The lease's monotonic clock, moved only by the test."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class HeldAsr(SyntheticExecutor):
    """An ASR that runs until the test releases it (events, no wall clock)."""

    def __init__(self) -> None:
        super().__init__("asr")
        self.started, self.release = threading.Event(), threading.Event()

    def run(self, context):  # type: ignore[no-untyped-def]
        self.started.set()
        self.release.wait(LONG)
        return ExecOutcome.succeeded()


@pytest.fixture
def world(tmp_path):
    rt = runtime(tmp_path / "wf")
    try:
        yield rt
    finally:
        rt.close()


def restored(rt, n=1):
    w = enrol_remote(rt, "5060 Ti-1")
    films = []
    for _ in range(n):
        film_id = add_film(rt)
        route(rt, film_id, w.worker_id)
        resolve_remote(rt, w, claim_remote(rt, w, film_id, "restore"))
        films.append(film_id)
    return films


def test_manual_and_controller_asr_take_turns_on_the_gpu(world):
    rt = world
    (film_id,) = restored(rt)
    clock = Clock()
    lease = GpuLease(clock=clock)
    asr = HeldAsr()
    rt.executors["asr"] = asr
    d = local_dispatcher(rt, lease=lease, remote_enabled=True, poll=1.0)

    # 1. a manual task holds the GPU: the ASR waits and names it
    assert lease.try_acquire(MANUAL, 1.0, "Jasna：手动修复")
    assert d.run_once() is False
    assert d.status().waiting == {film_id: "等待 GPU：Jasna：手动修复"}
    assert lease.waiters() == [d.run_id]

    # 2. the manual task ends: the GPU is reserved for the waiting ASR
    assert lease.release(MANUAL)
    assert lease.reserved_for() == d.run_id
    clock.value += RESERVE_MIN_S / 2
    assert lease.try_acquire(MANUAL, 1.0, "Jasna：手动修复") is False  # fair

    # 3. the ASR runs; a manual task waits naming the film
    runner = threading.Thread(target=d.run_once)
    runner.start()
    try:
        assert asr.started.wait(LONG)
        assert lease.holder() == d.run_id
        assert lease.try_acquire(MANUAL, 1.0, "Jasna：手动修复") is False
        assert lease.blocking_label().startswith("影片工作流：")
    finally:
        asr.release.set()
        runner.join(LONG)
    assert stage(rt, film_id, "asr").status == "succeeded"

    # 4. the ASR released the GPU: it is the manual task's turn
    assert lease.holder() is None
    assert lease.reserved_for() == MANUAL
    assert lease.try_acquire(MANUAL, 1.0, "Jasna：手动修复")


def test_a_remote_mode_restore_never_queues_on_the_lease(world):
    rt = world
    enrol_remote(rt, "5060 Ti-1")
    film_id = add_film(rt)
    clock = Clock()
    lease = GpuLease(clock=clock)
    assert lease.try_acquire(MANUAL, 1.0, "Jasna：手动修复")
    d = local_dispatcher(rt, lease=lease, remote_enabled=True)
    for _ in range(3):
        assert d.run_once() is False
        clock.value += 1.0
    assert stage(rt, film_id, "restore").status == "pending"
    assert lease.waiters() == []  # never a waiter
    assert lease.release(MANUAL)
    assert lease.reserved_for() is None  # nothing was waiting for it
