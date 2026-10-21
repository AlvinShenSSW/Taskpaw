"""Local dispatch recovers from rare timing and environment faults (T08-P1a R1–R5)."""

import threading
import time

import pytest

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    Crash,
    add_film,
    crash_mid_stage,
    dispatcher,
    executors,
    open_assignments,
    runtime,
    stage,
)
from taskpaw_v3.workflow import local_dispatch
from taskpaw_v3.workflow.executors import ExecOutcome, failed_kind
from taskpaw_v3.workflow.local_host import LocalWorkflowHost


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def kinds(rt, film, name="restore"):
    job = stage(rt, film, name)
    return [a.evidence[0].kind for a in rt.store.list_attempts(rt.operator, job.job_id)]


# ── R1 ──────────────────────────────────────────────────────────────────


class WaitsForRenewal:
    """Finishes only once a renewal is in flight, so the two would race."""

    stage, uses_gpu = "restore", True

    def __init__(self, base, in_flight):
        self.base, self.in_flight = base, in_flight

    def tools(self, job=None):
        return self.base.tools(job)

    def max_resolution(self, vram):
        return self.base.max_resolution(vram)

    def run(self, context):
        assert self.in_flight.wait(10)
        return ExecOutcome.succeeded()


def test_resolution_waits_for_an_in_flight_renewal(rt):
    in_flight = threading.Event()
    d = dispatcher(rt, renew_every=0.01)
    real = d._renew
    calls = []

    def slow_renew(*args):
        calls.append(threading.current_thread().name)
        if len(calls) > 1:  # a periodic renewal, not the initial owner record
            in_flight.set()
            time.sleep(0.3)
        real(*args)

    d._renew = slow_renew
    resolve = rt.store.resolve_assignment

    def delayed_resolve(*args, **kwargs):
        time.sleep(0.5)  # an in-flight renewal would land between read and write
        return resolve(*args, **kwargs)

    rt.store.resolve_assignment = delayed_resolve
    rt.executors["restore"] = WaitsForRenewal(rt.executors["restore"], in_flight)
    film = add_film(rt)
    assert d.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    assert open_assignments(rt) == []


# ── R2 ──────────────────────────────────────────────────────────────────


def _assert_visible_failure(rt, film, lease):
    assert stage(rt, film, "restore").status == "failed"
    assert kinds(rt, film) == [failed_kind("needs_attention")]
    assert open_assignments(rt) == []
    assert lease.holder() is None


def test_unwritable_work_directory_is_a_resolved_visible_failure(rt, tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    rt.work_dir = blocker
    film = add_film(rt)
    lease = GpuLease()
    assert dispatcher(rt, lease).run_once() is True
    _assert_visible_failure(rt, film, lease)


def test_job_creation_failure_is_a_resolved_visible_failure(rt, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("CreateJobObject failed")

    monkeypatch.setattr(local_dispatch, "OwnedJob", broken)
    film = add_film(rt)
    lease = GpuLease()
    assert dispatcher(rt, lease).run_once() is True
    _assert_visible_failure(rt, film, lease)


# ── R3 ──────────────────────────────────────────────────────────────────


def _crash_on_retry(target, monkeypatch):
    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(target.store, "retry_stage", crash)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("exec_interrupted", True),
        (failed_kind("cancelled"), True),
        (failed_kind("transient"), True),
        (failed_kind("permission"), False),
        (failed_kind("needs_attention"), False),
    ],
)
def test_retry_rule_is_decided_by_the_latest_evidence_kind(kind, expected):
    from taskpaw_v3.workflow.local_dispatch import should_retry

    class Evidence:
        def __init__(self, value):
            self.kind = value

    class Attempt:
        def __init__(self, value):
            self.evidence = (Evidence(value),)

    assert should_retry([Attempt(kind)]) is expected


def test_over_limit_transient_is_not_retried():
    from taskpaw_v3.workflow.local_dispatch import should_retry

    class Attempt:
        def __init__(self):
            self.evidence = (type("E", (), {"kind": failed_kind("transient")})(),)

    # the initial run plus MAX_AUTO_RETRIES retries, then it stops
    assert should_retry([Attempt()] * local_dispatch.MAX_AUTO_RETRIES) is True
    assert should_retry([Attempt()] * (local_dispatch.MAX_AUTO_RETRIES + 1)) is False


def test_transient_retry_lost_in_a_crash_is_made_on_the_next_start(
    tmp_path, monkeypatch
):
    first = runtime(tmp_path / "wf")
    first.executors = {e.stage: e for e in executors("import sys; sys.exit(75)")}
    film = add_film(first)
    _crash_on_retry(first, monkeypatch)
    with pytest.raises(Crash):
        dispatcher(first).run_once()
    assert stage(first, film, "restore").status == "failed"
    first.close()
    second = runtime(tmp_path / "wf")
    try:
        d = dispatcher(second)
        d.reconcile_retries()
        assert stage(second, film, "restore").status == "pending"
        assert d.run_once() is True
        assert stage(second, film, "restore").status == "succeeded"
    finally:
        second.close()


def test_interrupted_retry_lost_in_a_crash_is_made_on_the_next_start(
    tmp_path, monkeypatch
):
    first = runtime(tmp_path / "wf")
    film, crashing = crash_mid_stage(first)
    crashing.processes.terminate()
    assert crashing.processes.settle(15)
    from taskpaw_v3.workflow.store import WorkflowStore

    real = WorkflowStore.retry_stage

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(WorkflowStore, "retry_stage", crash)
    with pytest.raises(Crash):
        runtime(tmp_path / "wf")
    monkeypatch.setattr(WorkflowStore, "retry_stage", real)
    third = runtime(tmp_path / "wf")
    try:
        assert stage(third, film, "restore").status == "failed"
        d = dispatcher(third)
        d.reconcile_retries()
        assert stage(third, film, "restore").status == "pending"
        assert d.run_once() is True
    finally:
        third.close()


def test_reconcile_never_escapes(rt, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("store hiccup")

    monkeypatch.setattr(rt.store, "list_compute_jobs", broken)
    dispatcher(rt).reconcile_retries()  # logged, not raised


# ── R4 ──────────────────────────────────────────────────────────────────


def test_torn_attempt_file_does_not_block_startup(tmp_path):
    first = runtime(tmp_path / "wf")
    film, crashing = crash_mid_stage(first)
    crashing.processes.terminate()
    assert crashing.processes.settle(15)
    (torn,) = (tmp_path / "wf" / "attempts").glob("*.json")
    torn.write_text('{"attempt_id": "', encoding="utf-8")
    second = runtime(tmp_path / "wf")
    try:
        assert second.reconciled and open_assignments(second) == []
        assert stage(second, film, "restore").status == "pending"
    finally:
        second.close()


# ── R5 ──────────────────────────────────────────────────────────────────


class FakeIntake:
    def __init__(self, result):
        self.result = result

    def stop(self, timeout):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDispatcher:
    def __init__(self, stopped):
        self.stopped = stopped

    def stop(self, timeout):
        return self.stopped


@pytest.mark.parametrize(
    ("intake", "dispatch"),
    [
        (FakeIntake(False), FakeDispatcher(True)),
        (FakeIntake(OSError("stop failed")), FakeDispatcher(True)),
        (FakeIntake(True), FakeDispatcher(False)),
    ],
)
def test_unconfirmed_shutdown_keeps_the_store_open(tmp_path, intake, dispatch):
    host = LocalWorkflowHost(WorkflowSettings(enabled=True), tmp_path / "wf")
    host.runtime = runtime(tmp_path / "wf")
    host.intake, host.dispatcher = intake, dispatch
    try:
        host.stop(timeout=1)
        assert host.state == "stop_unconfirmed"
        assert host.runtime._store is not None
    finally:
        host.runtime.close()


def test_confirmed_shutdown_closes_the_store(tmp_path):
    host = LocalWorkflowHost(WorkflowSettings(enabled=True), tmp_path / "wf")
    host.runtime = runtime(tmp_path / "wf")
    host.intake, host.dispatcher = FakeIntake(True), FakeDispatcher(True)
    host.stop(timeout=1)
    assert host.state == "stopped" and host.runtime._store is None


def test_dispatcher_that_never_started_reports_stopped(rt):
    assert dispatcher(rt).stop(timeout=1) is True
