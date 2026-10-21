"""Controller-local self-enrolment, dispatch and restart settlement (T03-P1)."""

import errno
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_films import STAGES, input_manifest
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import (
    ExecOutcome,
    JobContext,
    SyntheticExecutor,
    failed_kind,
)
from taskpaw_v3.workflow.local_dispatch import NO_4K_DEVICE, LocalDispatcher
from taskpaw_v3.workflow.local_gpu import LocalGpu
from taskpaw_v3.workflow.local_runtime import LocalRuntime

GPU_8GB = LocalGpu("NVIDIA GeForce RTX 5060", "GPU-synthetic-8gb", 8151)
GPU_16GB = LocalGpu("NVIDIA GeForce RTX 5060 Ti", "GPU-synthetic-8gb", 16311)
SLEEP = "import time; print('started', flush=True); time.sleep(60)"


def executors(script="pass", **kw):
    return [SyntheticExecutor(s, script, **kw) for s in ("restore", "asr", "translate")]


def runtime(path, *, gpu=GPU_8GB, execs=None, clock=None):
    value = LocalRuntime(
        path,
        executors() if execs is None else execs,
        gpu=lambda: gpu,
        machine=lambda: "synthetic-machine",
        clock_source=clock,
    )
    value.open()
    value.ensure_worker()
    value.start_boot()
    return value


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def plan_film(rt, film_id, *, revision=1, tier="standard"):
    """Confirm the film's plan so its restore may be claimed (U1 P1')."""
    rt.store.confirm_plan(
        rt.operator,
        rt.meta(),
        m.FilmPlanConfirm(
            film_id=film_id,
            input_revision=revision,
            ltx_model=tier,
            machine=rt.identity.worker_id,
        ),
    )


def add_film(rt, *, width=1920, height=1080, plan=True):
    """A film with one input revision; planned (standard) unless `plan` is off."""
    _, auth = rt.grant_intake_adapter()
    film_id = identity()
    rt.store.create_film(
        auth,
        rt.meta(),
        m.FilmCreate(
            film_id=film_id,
            intake_session_id=identity(),
            display_name=f"Film {width}x{height}",
            policy=m.FilmPolicy(
                policy_revision=1,
                required_stages=STAGES,
                subtitle_requirement="chinese",
                required_image_roles=("poster",),
                folder_cover_required=False,
            ),
        ),
    )

    def size(data):
        for probe in data["readiness"]["media_probes"]:
            probe.update(width=width, height=height)

    rt.store.put_input_manifest(
        auth,
        rt.meta(),
        m.InputManifestPut(
            film_id=film_id,
            expected_version=1,
            manifest=input_manifest(film_id, change=size),
        ),
    )
    if plan:
        plan_film(rt, film_id)
    return film_id


def stage(rt, film_id, name):
    return next(
        s for s in rt.store.get_film(rt.operator, film_id).stages if s.stage == name
    )


def open_assignments(rt):
    return [a for a in rt.worker_view().assignments if a.status != "resolved"]


def dispatcher(rt, lease=None, poll=0.05, **kw):
    return LocalDispatcher(rt, lease=lease or GpuLease(), poll=poll, **kw)


# ── enrolment ───────────────────────────────────────────────────────────


def test_first_and_second_start_enrol_one_local_worker(tmp_path):
    first = runtime(tmp_path / "wf")
    worker = first.worker()
    assert (worker.enrollment_state, worker.admission_state) == ("verified", "enabled")
    assert worker.workflow_participation and first.reconciled
    profiles = worker.capability.approved.profiles
    assert {(p.allowed_stages, p.max_width, p.max_height) for p in profiles} == {
        (("restore",), 1920, 1080),
        (("asr",), 1920, 1080),
        (("translate",), 1920, 1080),
    }
    first.close()
    second = runtime(tmp_path / "wf")
    try:
        again = second.worker()
        assert again.worker_id == worker.worker_id
        assert again.capability.revision == worker.capability.revision
        assert again.physical_binding.resource_id == worker.physical_binding.resource_id
        assert second.boot_id != first.boot_id
    finally:
        second.close()


def test_card_change_writes_a_new_capability_revision(tmp_path):
    runtime(tmp_path / "wf").close()
    bigger = runtime(tmp_path / "wf", gpu=GPU_16GB)
    try:
        worker = bigger.worker()
        assert worker.capability.revision == 2
        assert {
            (p.max_width, p.max_height) for p in worker.capability.approved.profiles
        } == {(3840, 2160)}
        assert worker.admission_state == "enabled"
    finally:
        bigger.close()


@pytest.mark.parametrize("missing", ["gpu", "executors"])
def test_without_gpu_or_executor_the_worker_is_not_approved(tmp_path, missing):
    value = runtime(
        tmp_path / "wf",
        gpu=None if missing == "gpu" else GPU_8GB,
        execs=[] if missing == "executors" else None,
    )
    try:
        worker = value.worker()
        assert worker.enrollment_state == "pending_validation"
        assert value.reason in ("无法读取本机 GPU（nvidia-smi）", "没有可用的执行器")
        assert dispatcher(value).run_once() is False
    finally:
        value.close()


def test_enrolment_resumes_after_a_crash_between_steps(tmp_path, monkeypatch):
    from taskpaw_v3.workflow.store import WorkflowStore

    real = WorkflowStore.complete_pairing

    def crash(*args, **kwargs):
        raise KeyboardInterrupt("simulated crash after pairing issue")

    monkeypatch.setattr(WorkflowStore, "complete_pairing", crash)
    with pytest.raises(KeyboardInterrupt):
        runtime(tmp_path / "wf")
    monkeypatch.setattr(WorkflowStore, "complete_pairing", real)
    value = LocalRuntime(
        tmp_path / "wf",
        executors(),
        gpu=lambda: GPU_8GB,
        machine=lambda: "synthetic-machine",
    )
    try:
        value.open()
        assert value.ensure_worker().enrollment_state == "verified"
    finally:
        value.close()


# ── dispatch ────────────────────────────────────────────────────────────


def test_film_runs_every_compute_stage_and_releases_owner_and_gpu(rt):
    film = add_film(rt)
    lease = GpuLease()
    d = dispatcher(rt, lease)
    assert [d.run_once() for _ in range(4)] == [True, True, True, False]
    assert [stage(rt, film, s).status for s in ("restore", "asr", "translate")] == [
        "succeeded"
    ] * 3
    assert open_assignments(rt) == []
    assert lease.holder() is None and lease.waiters() == []
    assert not any(rt.attempt_dir.glob("*.json"))


def test_8gb_controller_never_claims_4k_and_says_why(tmp_path):
    small = runtime(tmp_path / "wf")
    try:
        film = add_film(small, width=3840, height=2160)
        d = dispatcher(small)
        assert d.run_once() is False
        assert d.status().waiting == {film: NO_4K_DEVICE}
        assert stage(small, film, "restore").status == "pending"
    finally:
        small.close()
    big = runtime(tmp_path / "wf", gpu=GPU_16GB)
    try:
        assert dispatcher(big).run_once() is True
        assert stage(big, film, "restore").status == "succeeded"
    finally:
        big.close()


def test_manual_gpu_task_blocks_until_released_and_idle_dispatch_withdraws(rt):
    lease = GpuLease()
    manual = ("jasna", 1)
    assert lease.try_acquire(manual, 5, "手动 Jasna")
    film = add_film(rt)
    d = dispatcher(rt, lease)
    assert d.run_once() is False
    assert d.status().waiting[film].startswith("等待 GPU：手动 Jasna")
    assert lease.release(manual)
    assert d.run_once() is True
    assert [d.run_once() for _ in range(3)] == [True, True, False]
    assert lease.holder() is None and d.run_id not in lease.waiters()


def test_transient_failure_retries_twice_then_stops(rt):
    execs = executors("import sys; sys.exit(75)")
    rt.executors = {e.stage: e for e in execs}
    film = add_film(rt)
    d = dispatcher(rt)
    while d.run_once():
        pass
    job = stage(rt, film, "restore")
    assert job.status == "failed"
    attempts = rt.store.list_attempts(rt.operator, job.job_id)
    assert len(attempts) == 3
    assert all(a.evidence[0].kind == failed_kind("transient") for a in attempts)


def test_permission_failure_is_not_retried(rt):
    rt.executors = {e.stage: e for e in executors("import sys; sys.exit(77)")}
    film = add_film(rt)
    d = dispatcher(rt)
    assert d.run_once() is True and d.run_once() is False
    job = stage(rt, film, "restore")
    assert job.status == "failed"
    assert len(rt.store.list_attempts(rt.operator, job.job_id)) == 1


class RaisingExecutor(SyntheticExecutor):
    def __init__(self, stage, error):
        super().__init__(stage)
        self.error = error

    def run(self, context):
        raise self.error()


def test_a_held_file_is_transient_and_retried(rt):
    # A file held by antivirus or the indexer: Windows reports the sharing
    # violation on open/copy as errno 13 without a winerror (H1-2).
    rt.executors["restore"] = RaisingExecutor(
        "restore", lambda: PermissionError(errno.EACCES, "Permission denied")
    )
    film = add_film(rt)
    d = dispatcher(rt)
    while d.run_once():
        pass
    job = stage(rt, film, "restore")
    assert job.status == "failed"
    attempts = rt.store.list_attempts(rt.operator, job.job_id)
    assert len(attempts) == 3
    assert all(a.evidence[0].kind == failed_kind("transient") for a in attempts)


def test_transient_retries_wait_a_poll_between_attempts(rt):
    """H1 internal review IR01: back-to-back retries cannot outlast an AV hold."""
    import time

    times: list[float] = []

    def held():
        times.append(time.monotonic())
        return PermissionError(errno.EACCES, "Permission denied")

    rt.executors["restore"] = RaisingExecutor("restore", held)
    film = add_film(rt)
    d = dispatcher(rt)
    d.poll = 0.3
    d.start()
    try:
        deadline = time.monotonic() + 10
        while stage(rt, film, "restore").status != "failed" or len(times) < 3:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        assert d.stop(timeout=10)
    assert len(times) == 3
    assert min(b - a for a, b in zip(times, times[1:])) >= 0.25


def test_other_executor_errors_still_need_attention(rt):
    rt.executors["restore"] = RaisingExecutor(
        "restore", lambda: FileNotFoundError(errno.ENOENT, "No such file")
    )
    film = add_film(rt)
    d = dispatcher(rt)
    assert d.run_once() is True and d.run_once() is False
    job = stage(rt, film, "restore")
    (attempt,) = rt.store.list_attempts(rt.operator, job.job_id)
    assert attempt.evidence[0].kind == failed_kind("needs_attention")


# ── stop and restart ────────────────────────────────────────────────────


class Crash(BaseException):
    """The agent process dying mid-stage (nothing after it runs)."""


class CrashingExecutor(SyntheticExecutor):
    def run(self, context):
        self.processes = context.processes
        self.child = context.processes.launch(self.argv(), cwd=str(context.workdir))
        time.sleep(0.5)
        raise Crash()


def crash_mid_stage(rt):
    film = add_film(rt)
    crashing = CrashingExecutor("restore", SLEEP)
    rt.executors["restore"] = crashing
    with pytest.raises(Crash):
        dispatcher(rt).run_once()
    rt.close()  # the process is gone; its Job is not yet closed in this test
    return film, crashing


def test_restart_settles_the_interrupted_attempt_without_counting_a_failure(tmp_path):
    first = runtime(tmp_path / "wf")
    film, crashing = crash_mid_stage(first)
    crashing.processes.terminate()  # what the OS does when the agent's Job closes
    assert crashing.processes.settle(15)
    second = runtime(tmp_path / "wf")
    try:
        assert second.reconciled and open_assignments(second) == []
        job = stage(second, film, "restore")
        assert job.status == "pending"
        (old,) = second.store.list_attempts(second.operator, job.job_id)
        assert (old.status, old.evidence[0].kind) == ("failed", "exec_interrupted")
        assert dispatcher(second).run_once() is True
        assert stage(second, film, "restore").status == "succeeded"
    finally:
        second.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Native process identity")
def test_restart_keeps_the_owner_while_the_recorded_child_still_runs(tmp_path):
    first = runtime(tmp_path / "wf")
    film, crashing = crash_mid_stage(first)
    second = LocalRuntime(
        tmp_path / "wf",
        executors(),
        gpu=lambda: GPU_8GB,
        machine=lambda: "synthetic-machine",
    )
    try:
        second.open()
        second.ensure_worker()
        second.boot_id = identity()
        second._record_boot()
        assert second.settle_previous(wait=0.5) is False
        assert second.reason == "上次的任务进程仍在运行"
        d = dispatcher(second)
        assert d.run_once() is False
        assert len(open_assignments(second)) == 1
        crashing.processes.terminate()
        assert crashing.processes.settle(15)
        assert d.run_once() is True
        assert stage(second, film, "restore").status == "succeeded"
    finally:
        crashing.processes.terminate()
        second.close()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows Job-object stop of a running stage"
)
def test_stop_during_a_stage_ends_the_child_and_requeues_without_counting(rt):
    rt.executors["restore"] = SyntheticExecutor("restore", SLEEP)
    film = add_film(rt)
    lease = GpuLease()
    d = dispatcher(rt, lease)
    d.start()
    deadline = time.monotonic() + 20
    while (
        d.status().state != "running" or "started" not in d.status().current["progress"]
    ):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    d.stop(timeout=30)
    job = stage(rt, film, "restore")
    assert job.status == "pending"
    (attempt,) = rt.store.list_attempts(rt.operator, job.job_id)
    assert attempt.evidence[0].kind == failed_kind("cancelled")
    assert open_assignments(rt) == [] and lease.holder() is None


def test_clock_step_back_does_not_disable_the_workflow(tmp_path):
    now = [datetime.now(timezone.utc)]
    value = runtime(tmp_path / "wf", clock=lambda: now[0])
    try:
        film = add_film(value)
        now[0] -= timedelta(hours=1)
        assert dispatcher(value).run_once() is True
        now[0] -= timedelta(seconds=1)
        value.close()
        value = runtime(tmp_path / "wf", clock=lambda: now[0])
        assert dispatcher(value).run_once() is True
        assert stage(value, film, "asr").status == "succeeded"
    finally:
        value.close()


def test_stop_without_work_returns_promptly(rt):
    d = dispatcher(rt)
    d.start()
    started = time.monotonic()
    d.stop(timeout=10)
    assert time.monotonic() - started < 5
    assert not d._thread.is_alive()


def test_dispatch_thread_finishes_a_film_and_wakes_on_demand(rt):
    d = dispatcher(rt, poll=60.0)  # only wake() can start the work promptly
    d.start()
    try:
        film = add_film(rt)
        d.wake()
        deadline = time.monotonic() + 30
        while stage(rt, film, "translate").status != "succeeded":
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        d.stop(timeout=10)


def test_unconfirmed_stop_keeps_owner_and_gpu_until_the_tree_ends(rt, monkeypatch):
    from taskpaw_v3.workflow import owned_process

    real = owned_process.OwnedJob.settle
    stuck = {"value": True}

    def settle(self, timeout):
        return False if stuck["value"] else real(self, timeout)

    monkeypatch.setattr(owned_process.OwnedJob, "settle", settle)
    film = add_film(rt)
    lease = GpuLease()
    d = dispatcher(rt, lease)
    assert d.run_once() is True
    assert d.status().state == "stopping_unconfirmed"
    assert lease.holder() == d.run_id and len(open_assignments(rt)) == 1
    assert d.run_once() is False  # nothing new starts while the tree runs
    stuck["value"] = False
    assert d.run_once() is True  # settles, resolves, then runs the next stage
    assert stage(rt, film, "restore").status == "succeeded"
    assert stage(rt, film, "asr").status == "succeeded"


def test_refused_claim_does_not_spin_and_never_starves_later_films(rt, monkeypatch):
    blocked, later = add_film(rt), add_film(rt)
    refused = stage(rt, blocked, "restore").job_id
    real = rt.store.claim

    def claim(auth, meta, command):
        if command.job_id == refused:
            raise m.WorkflowError("source_not_admissible")
        return real(auth, meta, command)

    monkeypatch.setattr(rt.store, "claim", claim)
    lease = GpuLease()
    d = dispatcher(rt, lease)
    assert d.run_once() is True  # the later film runs despite the refused one
    assert stage(rt, later, "restore").status == "succeeded"
    assert d.status().waiting[blocked] == "领取失败：source_not_admissible"
    while d.run_once():
        pass
    assert stage(rt, later, "translate").status == "succeeded"
    assert d.run_once() is False  # only the refused film is left: no busy loop
    assert stage(rt, blocked, "restore").status == "pending"
    assert lease.holder() is None and not any(rt.attempt_dir.glob("*.json"))


class _Reporting(SyntheticExecutor):
    """Reports LTX progress, then plain text, reading the status after each."""

    def __init__(self) -> None:
        super().__init__("restore")
        self.dispatcher: LocalDispatcher | None = None
        self.seen: list[dict] = []
        self.blocking: list[str] = []

    def run(self, context: JobContext) -> ExecOutcome:
        assert self.dispatcher is not None
        ltx = {"ltx_stage": "2/4 encode", "current_file": "第 1/2 卷"}
        context.progress("修复 第 1/2 卷 · 阶段 2/4 encode", ltx=ltx)
        self.seen.append(dict(self.dispatcher.status().current or {}))
        self.blocking.append(self.dispatcher.lease.blocking_label())
        context.progress("修复 第 2/2 卷")
        self.seen.append(dict(self.dispatcher.status().current or {}))
        return ExecOutcome.succeeded()


def test_the_running_stage_reports_text_ltx_detail_and_the_film_name(rt):
    """H3 M3/D04: `progress` stays the text, `ltx` rides beside it; D06: the
    status and the GPU lease name the film, never its grouping key."""
    reporting = _Reporting()
    rt.executors["restore"] = reporting
    film = add_film(rt)
    d = reporting.dispatcher = dispatcher(rt)
    assert d.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    first, second = reporting.seen
    assert first == {
        "film_id": film,
        "film": "movie",  # the main video's stem: no package, no NFO
        "stage": "restore",
        "progress": "修复 第 1/2 卷 · 阶段 2/4 encode",
        "ltx": {"ltx_stage": "2/4 encode", "current_file": "第 1/2 卷"},
    }
    assert second["progress"] == "修复 第 2/2 卷" and "ltx" not in second
    assert reporting.blocking == ["影片工作流：movie"]


def test_workflow_lease_id_never_collides_with_a_manual_task_named_workflow(rt):
    from taskpaw_v3.core.generation import next_generation

    lease = GpuLease()
    manual = ("workflow", next_generation())  # a monitor instance named "workflow"
    assert lease.try_acquire(manual, 5, "手动任务")
    add_film(rt)
    d = dispatcher(rt, lease)
    assert d.run_id != manual
    assert d.run_once() is False
    assert lease.holder() == manual
