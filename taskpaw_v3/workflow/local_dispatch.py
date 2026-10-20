"""Single-node dispatch loop on the controller's own GPU (T03-P1 §3.4).

One thread claims ready compute stages, runs them through their executor inside
a per-attempt kill-on-close Job, and resolves the attempt only after the Job has
settled. GPU stages share the agent's process-wide `gpu_lease` with manual
Jasna / subtitle tasks (C5).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core.generation import next_generation

from . import models as m
from .executors import (
    INTERRUPTED_KIND,
    SUCCEEDED_KIND,
    ExecOutcome,
    JobContext,
    StageExecutor,
    failed_kind,
)
from .local_runtime import AttemptFile, LocalRuntime, _new_id
from .owned_process import OwnedJob

log = logging.getLogger("taskpaw.workflow.dispatch")

MAX_AUTO_RETRIES = 2
POLL_SECONDS = 5.0
STOP_SETTLE_SECONDS = 30.0
RENEW_SECONDS = 3600.0
NO_4K_DEVICE = "没有可接 4K 的设备"


class Lease(Protocol):
    def try_acquire(
        self, run: gpu_lease.RunId, poll_interval: float, label: str = ""
    ) -> bool: ...
    def release(self, run: gpu_lease.RunId) -> bool: ...
    def withdraw(self, run: gpu_lease.RunId) -> None: ...


@dataclass
class _Running:
    job: m.PendingComputeJob
    attempt: AttemptFile
    assignment_id: str
    owner: m.LocalOwnerEvidence
    processes: OwnedJob | None
    uses_gpu: bool
    progress: str = ""


@dataclass
class _Unsettled:
    """A Job that did not settle in time: owner and GPU stay held (C4)."""

    running: _Running
    outcome: ExecOutcome


@dataclass
class DispatchStatus:
    state: str
    reason: str
    current: dict[str, str] | None
    waiting: dict[str, str] = field(default_factory=dict)


def _limits(manifest: m.InputManifest) -> tuple[int, int]:
    width = height = 0
    for probe in manifest.readiness.media_probes:
        width = max(width, probe.width or 0)
        height = max(height, probe.height or 0)
    return width, height


def fits(manifest: m.InputManifest, limit: tuple[int, int]) -> bool:
    width, height = _limits(manifest)
    return 0 < width <= limit[0] and 0 < height <= limit[1]


_FREE_RETRY = frozenset({INTERRUPTED_KIND, failed_kind("cancelled")})
_TRANSIENT = failed_kind("transient")


def _kinds(attempt: Any) -> set[str]:
    return {evidence.kind for evidence in attempt.evidence}


def should_retry(attempts: Sequence[Any]) -> bool:
    """C6 / T08-P1a R3: decided only by persisted evidence kinds, oldest first.

    Interruptions and stops are retried without counting; a transient failure
    is retried while at most MAX_AUTO_RETRIES transient attempts have failed;
    permission and needs-attention failures are never retried.
    """
    if not attempts:
        return False
    latest = _kinds(attempts[-1])
    if latest & _FREE_RETRY:
        return True
    if _TRANSIENT in latest:
        failures = sum(1 for attempt in attempts if _TRANSIENT in _kinds(attempt))
        return failures <= MAX_AUTO_RETRIES
    return False


class LocalDispatcher:
    def __init__(
        self,
        runtime: LocalRuntime,
        *,
        lease: Lease | Any = gpu_lease,
        poll: float = POLL_SECONDS,
        stop_settle: float = STOP_SETTLE_SECONDS,
        renew_every: float = RENEW_SECONDS,
    ) -> None:
        self.runtime, self.lease = runtime, lease
        self.poll, self.stop_settle, self.renew_every = poll, stop_settle, renew_every
        # A process-unique generation: never equal to a manual task's RunId (C5).
        self.run_id: gpu_lease.RunId = ("workflow", next_generation())
        self.cancel = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running: _Running | None = None
        self._unsettled: _Unsettled | None = None
        self._waiting: dict[str, str] = {}
        self._reason = "idle"
        self._holding = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="workflow-dispatch", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 60.0) -> bool:
        """True when the dispatch thread has exited (or never started)."""
        self.cancel.set()
        self._wake.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def status(self) -> DispatchStatus:
        with self._lock:
            current = None
            if self._running is not None:
                current = {
                    "film_id": self._running.job.film_id,
                    "film": self._running.job.display_name,
                    "stage": self._running.job.stage,
                    "progress": self._running.progress,
                }
            state = (
                "stopping_unconfirmed"
                if self._unsettled is not None
                else "running"
                if self._running is not None
                else "idle"
            )
            return DispatchStatus(state, self._reason, current, dict(self._waiting))

    def reconcile_retries(self) -> None:
        """R3: make retries a crash lost between resolution and requeue."""
        rt = self.runtime
        try:
            for job in rt.store.list_compute_jobs(rt.operator, "failed"):
                try:
                    self._retry_if_due(job.job_id)
                except m.WorkflowError as exc:
                    log.warning(
                        "workflow retry reconcile skipped %s: %s", job.job_id, exc
                    )
        except Exception:
            log.exception("workflow retry reconcile failed")

    def _loop(self) -> None:
        self.reconcile_retries()
        while not self.cancel.is_set():
            try:
                worked = self.run_once()
            except m.WorkflowError as exc:
                self._reason = str(exc)
                if str(exc) == "authority_unavailable":
                    log.error("workflow store unavailable; dispatch stopped")
                    return
                worked = False
            except Exception:
                log.exception("workflow dispatch tick failed")
                worked = False
            if not worked:
                self._wake.wait(self.poll)
                self._wake.clear()
        if self._unsettled is None:  # a still-running tree keeps the GPU (C4)
            self._release()

    # ── one tick ────────────────────────────────────────────────────────
    def run_once(self) -> bool:
        """Claim and run at most one stage; True when a stage ran."""
        rt = self.runtime
        if self._unsettled is not None and not self._finish_unsettled():
            return False
        if not rt.reconciled and not rt.settle_previous(wait=0):
            self._reason = rt.reason
            return False
        jobs = rt.store.list_pending_compute_jobs(rt.operator)
        waiting: dict[str, str] = {}
        vram = rt.gpu.vram_mib if rt.gpu else 0
        for job in jobs:
            executor = rt.executors.get(job.stage)
            if executor is None or rt.gpu is None:
                waiting[job.film_id] = (
                    rt.reason if rt.gpu is None else "没有可用的执行器"
                )
                continue
            manifest = rt.store.get_input_manifest(
                rt.operator, job.film_id, job.input_revision
            )
            if not fits(manifest, executor.max_resolution(vram)):
                waiting[job.film_id] = NO_4K_DEVICE
                continue
            if executor.uses_gpu and not self._acquire(job):
                waiting[job.film_id] = "等待 GPU：" + (
                    self.lease.blocking_label()
                    if hasattr(self.lease, "blocking_label")
                    else ""
                )
                self._set_waiting(waiting)
                return False
            if self._execute(job, executor, manifest):
                self._set_waiting(waiting)
                return True
            waiting[job.film_id] = self._reason  # claim refused: try the next film
        self._set_waiting(waiting)
        self._release()
        return False

    def _set_waiting(self, waiting: dict[str, str]) -> None:
        with self._lock:
            self._waiting = waiting

    def _acquire(self, job: m.PendingComputeJob) -> bool:
        if self.lease.try_acquire(
            self.run_id, self.poll, f"影片工作流：{job.display_name}"
        ):
            self._holding = True
            return True
        return False

    def _release(self) -> None:
        if self._holding:
            self.lease.release(self.run_id)
            self._holding = False
        else:
            self.lease.withdraw(self.run_id)

    # ── execution ───────────────────────────────────────────────────────
    def _execute(
        self,
        job: m.PendingComputeJob,
        executor: StageExecutor,
        manifest: m.InputManifest,
    ) -> bool:
        """Claim and run one stage; False (nothing ran) when the claim is refused."""
        rt = self.runtime
        worker = rt.worker()
        if worker.physical_binding is None:
            self._release()
            self._reason = "本机 GPU 尚未登记"
            return False
        tools = executor.tools()
        attempt = AttemptFile(
            attempt_id=_new_id(), operation_id=_new_id(), job_id=job.job_id
        )
        rt.write_attempt(attempt)
        try:
            claimed = rt.store.claim(
                rt.worker_auth,
                rt.meta(),
                m.ClaimRequest(
                    job_id=job.job_id,
                    expected_version=job.version,
                    attempt_id=attempt.attempt_id,
                    operation_id=attempt.operation_id,
                    worker_id=worker.worker_id,
                    boot_id=rt.boot_id,
                    profile_revision=tools.profile_revision,
                    config_digest=tools.config_digest,
                    resource_id=worker.physical_binding.resource_id,
                    tool_versions=tools,
                ),
            )
        except m.WorkflowError as exc:
            rt.drop_attempt(attempt.attempt_id)
            self._release()
            self._reason = f"领取失败：{exc}"
            return False
        assignment_id = claimed.assignment_id
        if assignment_id is None:  # T01 always reports it for a claim
            raise m.WorkflowError("precondition_failed")
        owner = m.LocalOwnerEvidence(
            owner_id=_new_id(),
            generation=1,
            boot_id=rt.boot_id,
            process_identity=rt.agent_identity,
            state="running",
            evidence=rt.evidence("native_owner"),
        )
        self._renew(assignment_id, attempt.attempt_id, owner)
        children: list[str] = []

        def launched(identity: str) -> None:
            children.append(identity)
            rt.write_attempt(replace(attempt, children=tuple(children)))

        running = _Running(job, attempt, assignment_id, owner, None, executor.uses_gpu)
        with self._lock:
            self._running = running

        def progress(text: str) -> None:
            running.progress = text[-200:]

        stop_renew = threading.Event()
        renewer = threading.Thread(
            target=self._renew_loop,
            args=(stop_renew, assignment_id, attempt.attempt_id, owner),
            daemon=True,
        )
        renewer.start()
        try:
            # Setup after the claim fails like a stage, never leaving it claimed (R2).
            running.processes = OwnedJob(on_launch=launched)
            workdir = rt.work_dir / attempt.attempt_id
            workdir.mkdir(parents=True, exist_ok=True)
            outcome = executor.run(
                JobContext(
                    job, manifest, workdir, self.cancel, running.processes, progress
                )
            )
        except Exception as exc:  # setup error or executor bug: visible needs-attention
            log.exception("workflow stage %s failed", job.stage)
            outcome = ExecOutcome.failed("needs_attention", f"执行器异常：{exc}")
        finally:
            stop_renew.set()
            renewer.join()  # no renewal may land after resolution reads the version (R1)
        if self.cancel.is_set() and not outcome.ok:
            outcome = ExecOutcome.failed("cancelled", "已停止")
        processes = running.processes
        if processes is not None:  # None: nothing was launched, nothing to settle
            processes.terminate()  # descendants left behind by a finished primary
            if not processes.settle(self.stop_settle):
                self._unsettled = _Unsettled(running, outcome)
                self._reason = "停止未确认：任务进程仍在运行"
                return True
        self._resolve(running, outcome)
        return True

    def _finish_unsettled(self) -> bool:
        pending = self._unsettled
        assert pending is not None
        processes = pending.running.processes
        if processes is not None and not processes.settle(0):
            return False
        self._unsettled = None
        self._resolve(pending.running, pending.outcome)
        return True

    def _renew(
        self, assignment_id: str, attempt_id: str, owner: m.LocalOwnerEvidence
    ) -> None:
        rt = self.runtime
        try:
            current = next(
                a
                for a in rt.worker_view().assignments
                if a.assignment_id == assignment_id
            )
            rt.store.renew(
                rt.worker_auth,
                rt.meta(),
                m.AssignmentRenewal(
                    assignment_id=assignment_id,
                    expected_version=current.version,
                    worker_id=current.worker_id,
                    boot_id=rt.boot_id,
                    attempt_id=attempt_id,
                    authority_epoch=current.authority_epoch,
                    fencing_token=current.fencing_token,
                    local_owner=owner,
                ),
            )
        except (m.WorkflowError, StopIteration) as exc:
            # Renewal only extends the lease; a failure never stops the Job (D19).
            log.warning("workflow lease renewal failed: %s", exc)

    def _renew_loop(
        self,
        stop: threading.Event,
        assignment_id: str,
        attempt_id: str,
        owner: m.LocalOwnerEvidence,
    ) -> None:
        while not stop.wait(self.renew_every):
            self._renew(assignment_id, attempt_id, owner)

    def _resolve(self, running: _Running, outcome: ExecOutcome) -> None:
        rt = self.runtime
        try:
            current = next(
                a
                for a in rt.worker_view().assignments
                if a.assignment_id == running.assignment_id
            )
            kind = (
                SUCCEEDED_KIND if outcome.ok else failed_kind(outcome.error_class)  # type: ignore[arg-type]
            )
            rt.store.resolve_assignment(
                rt.operator,
                rt.meta(),
                m.AssignmentResolution(
                    assignment_id=running.assignment_id,
                    attempt_id=running.attempt.attempt_id,
                    expected_version=current.version,
                    reason=outcome.message or ("完成" if outcome.ok else "失败"),
                    operation_id=running.attempt.operation_id,
                    local_owner=running.owner.model_copy(
                        update={
                            "state": "stopped",
                            "evidence": rt.evidence("native_owner_stopped"),
                        }
                    ),
                    evidence=(rt.evidence(kind, outcome.message or None),),
                    outcome="succeeded" if outcome.ok else "failed",
                ),
            )
            rt.drop_attempt(running.attempt.attempt_id)
            if not outcome.ok:
                self._retry_if_due(running.job.job_id)  # C6
                self._reason = outcome.message
        finally:
            with self._lock:
                self._running = None
            if running.uses_gpu:
                self._release()

    def _retry_if_due(self, job_id: str) -> None:
        """C6: bounded automatic retry by `should_retry`; interruptions never count."""
        rt = self.runtime
        if not should_retry(rt.store.list_attempts(rt.operator, job_id)):
            return
        stage = rt.store.get_stage(rt.operator, job_id)
        if stage.status == "failed":
            rt.store.retry_stage(
                rt.operator,
                rt.meta(),
                m.StageRetry(
                    job_id=job_id, expected_version=stage.version, reason="自动重试"
                ),
            )
