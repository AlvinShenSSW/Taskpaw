"""P2-3 (#294) §4: `run_stage()`, the `ExecRuntime` protocol and translate's
resolution (R2, R3, R4)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    GPU_8GB,
    add_film,
    executors,
    runtime,
    stage,
)
from taskpaw_v3.tests.workflow_exec_helpers import translate
from taskpaw_v3.tests.workflow_route_helpers import (
    GPU_16,
    claim_remote,
    enrol_remote,
    resolve_remote,
    route,
)
from taskpaw_v3.workflow import exec_common, owned_process
from taskpaw_v3.workflow import local_dispatch as ld
from taskpaw_v3.workflow.executors import (
    ExecOutcome,
    ExecRuntime,
    SyntheticExecutor,
)
from taskpaw_v3.workflow.local_dispatch import LocalDispatcher, StageRun, run_stage
from taskpaw_v3.workflow.local_gpu import FULL_HD, UHD, max_resolution
from taskpaw_v3.workflow.local_runtime import LocalRuntime
from taskpaw_v3.workflow.stage_results import StageResultError, load_stage_result


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def _job(rt, film_id, name="restore"):
    return next(
        j
        for j in rt.store.list_jobs(rt.operator, "pending", (name,))
        if j.film_id == film_id
    )


def _run(rt, film_id, executor, cancel=None, **kw) -> StageRun:
    job = _job(rt, film_id)
    launched: list[str] = []
    seen: list[object] = []
    kw.setdefault("stop_settle", 10.0)
    result = run_stage(
        executor,
        job,
        rt.store.get_input_manifest(rt.operator, film_id, 1),
        rt.work_dir / "attempt-x",
        cancel or threading.Event(),
        on_launch=launched.append,
        progress=lambda text, ltx=None: None,
        on_processes=seen.append,
        **kw,
    )
    assert seen == [result.processes]
    return result


# ── R2: run_stage ───────────────────────────────────────────────────────
def test_run_stage_runs_the_executor_in_its_own_job_and_settles(rt):
    film = add_film(rt)
    executor = SyntheticExecutor("restore", "print('hi')")
    result = _run(rt, film, executor)
    assert result.outcome.ok and result.settled
    assert result.processes is not None
    assert (rt.work_dir / "attempt-x").is_dir()
    assert executor.runs == [_job(rt, film).job_id]


def test_run_stage_turns_an_executor_bug_into_a_classified_failure(rt):
    film = add_film(rt)

    class Broken(SyntheticExecutor):
        def run(self, context):
            raise PermissionError("held")

    result = _run(rt, film, Broken("restore"))
    assert result.outcome.error_class == "transient" and result.settled
    assert result.outcome.message.startswith("执行器异常：")


def test_run_stage_reports_a_stop_as_cancelled(rt):
    film = add_film(rt)
    cancel = threading.Event()

    class Stopping(SyntheticExecutor):
        def run(self, context):
            cancel.set()
            return ExecOutcome.failed("needs_attention", "boom")

    result = _run(rt, film, Stopping("restore"), cancel)
    assert result.outcome == ExecOutcome.failed("cancelled", "已停止")


def test_run_stage_reports_an_unsettled_job(rt, monkeypatch):
    film = add_film(rt)
    monkeypatch.setattr(owned_process.OwnedJob, "settle", lambda self, timeout: False)
    result = _run(rt, film, SyntheticExecutor("restore"))
    assert result.outcome.ok and result.settled is False


def test_run_stage_without_a_job_object_settles_nothing(rt, monkeypatch):
    film = add_film(rt)

    def broken(*args, **kwargs):
        raise OSError("CreateJobObject failed")

    monkeypatch.setattr(ld, "OwnedJob", broken)
    job = _job(rt, film)
    result = run_stage(
        SyntheticExecutor("restore"),
        job,
        rt.store.get_input_manifest(rt.operator, film, 1),
        rt.work_dir / "a",
        threading.Event(),
        on_launch=lambda identity: None,
        progress=lambda text, ltx=None: None,
        stop_settle=1.0,
    )
    assert result.processes is None and result.settled
    assert result.outcome.error_class == "needs_attention"


# ── R3: ExecRuntime ─────────────────────────────────────────────────────
def test_the_local_runtime_is_an_exec_runtime(rt):
    value: ExecRuntime = rt  # the protocol's members
    assert value.work_dir == rt.work_dir and value.gpu == rt.gpu
    assert isinstance(rt, LocalRuntime)


def test_stage_result_is_none_without_the_stage_and_unverified_otherwise(rt):
    film = add_film(rt)
    with pytest.raises(StageResultError):  # a stage that has not succeeded
        rt.stage_result(film, "restore")
    with pytest.raises(StageResultError) as exc:
        load_stage_result(rt, film, "asr")
    assert "尚未成功" in str(exc.value)
    gone = SimpleNamespace(stage_result=lambda film_id, stage: None, work_dir=None)
    with pytest.raises(StageResultError) as exc:
        load_stage_result(gone, film, "restore")  # type: ignore[arg-type]
    assert str(exc.value) == "restore 阶段尚未成功"


def test_output_stems_fall_back_to_the_input_without_a_restore_stage(rt):
    film = add_film(rt)
    manifest = rt.store.get_input_manifest(rt.operator, film, 1)
    stems = exec_common.output_stems(
        SimpleNamespace(stage_result=lambda film_id, stage: None),  # type: ignore[arg-type]
        manifest,
    )
    assert stems and all(stems.values())


# ── R4: translate is LLM-only ───────────────────────────────────────────
@pytest.mark.parametrize("vram", [0, 8151, 16311])
def test_translate_takes_every_resolution(rt, vram):
    assert translate(rt).max_resolution(vram) == UHD


class _OldTranslate:
    """Translate as shipped before P2-3: capped at the card's resolution."""

    def __init__(self, base):
        self.base = base
        self.stage, self.uses_gpu = base.stage, base.uses_gpu

    def tools(self, job=None):
        return self.base.tools(job)

    def max_resolution(self, vram_mib):
        return max_resolution(vram_mib)

    def run(self, context):
        return self.base.run(context)


def _translate_limit(rt) -> tuple[int, int]:
    profile = next(
        p
        for p in rt.worker().capability.approved.profiles
        if p.allowed_stages == ("translate",)
    )
    return profile.max_width, profile.max_height


def test_a_restart_re_records_the_controllers_translate_profile(tmp_path):
    first = LocalRuntime(tmp_path / "wf", [], gpu=lambda: GPU_8GB, machine=lambda: "m")
    first.executors = {
        **{e.stage: e for e in executors()[:2]},
        "translate": _OldTranslate(translate(first)),
    }
    first.open()
    first.ensure_worker()
    assert _translate_limit(first) == FULL_HD
    revision = first.worker().capability.revision
    first.close()
    second = LocalRuntime(tmp_path / "wf", [], gpu=lambda: GPU_8GB, machine=lambda: "m")
    second.executors = {
        **{e.stage: e for e in executors()[:2]},
        "translate": translate(second),
    }
    try:
        second.open()
        worker = second.ensure_worker()
        assert _translate_limit(second) == UHD
        assert worker.capability.revision == revision + 1
        assert (worker.enrollment_state, worker.admission_state) == (
            "verified",
            "enabled",
        )
    finally:
        second.close()


def test_a_4k_film_restored_remotely_translates_on_the_8gb_controller(tmp_path):
    rt = LocalRuntime(tmp_path / "wf", [], gpu=lambda: GPU_8GB, machine=lambda: "m")
    rt.executors = {
        **{e.stage: e for e in executors()[:2]},
        "translate": translate(rt),
    }
    rt.open()
    rt.ensure_worker()
    rt.start_boot()
    try:
        remote = enrol_remote(rt, "5070 Ti", gpu=GPU_16, limit=UHD)
        film = add_film(rt, width=3840, height=2160)
        route(rt, film, remote.worker_id)
        for name in ("restore", "asr"):
            resolve_remote(rt, remote, claim_remote(rt, remote, film, name))
        d = LocalDispatcher(rt, lease=GpuLease(), poll=0.05, remote_enabled=True)
        assert d.run_once() is True  # claimed: no 「没有可接 4K 的设备」
        assert stage(rt, film, "translate").attempt_id is not None
        assert d.status().waiting.get(film) != ld.NO_4K_DEVICE
    finally:
        rt.close()
