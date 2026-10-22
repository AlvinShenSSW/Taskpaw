"""Single-node dispatch loop on the controller's own GPU (T03-P1 §3.4).

One thread claims ready compute stages, runs them through their executor inside
a per-attempt kill-on-close Job, and resolves the attempt only after the Job has
settled. GPU stages share the agent's process-wide `gpu_lease` with manual
Jasna / subtitle tasks (C5).
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, cast

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core import power as power_estimate
from taskpaw_v3.core.generation import next_generation

from . import board
from . import models as m
from .auth import AuthProof
from .executors import (
    INTERRUPTED_KIND,
    SUCCEEDED_KIND,
    ComputeStage,
    ExecOutcome,
    JobContext,
    StageExecutor,
    classify_exception,
    failed_kind,
)
from .local_runtime import AttemptFile, LocalRuntime, _new_id
from .owned_process import OwnedJob
from .timing import WorkflowLog, film_timing

if TYPE_CHECKING:
    from .finalize import Finalizer

log = logging.getLogger("taskpaw.workflow.dispatch")

_COMPUTE: tuple[str, ...] = ("restore", "asr", "translate")
_LOCAL: tuple[str, ...] = (*_COMPUTE, "return", "scrape")
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
    owner: m.LocalOwnerEvidence | None  # None: a return claim holds no resource
    processes: OwnedJob | None
    uses_gpu: bool
    name: str = ""  # the film's shown name (H3 D06)
    progress: str = ""
    ltx: dict[str, Any] | None = None  # restore's parsed LTX state (H3 M3)
    # U1c: when this stage started (UTC ISO); the UI ticks 已用 from it
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # PWR-2 (PW04): the film's power counter at the claim; None without a sampler
    energy_start: Any = None


class ComponentStep(Protocol):
    """A non-GPU stage claimed by a component principal: return (T04), scrape (M01)."""

    def tools(self) -> m.ToolVersions: ...

    def run(self, context: ReturnContext) -> ExecOutcome: ...


ReturnStep = ComponentStep


@dataclass
class ReturnContext:
    job: m.PendingComputeJob
    manifest: m.InputManifest
    attempt_id: str
    operation_id: str
    runtime: LocalRuntime
    cancel: threading.Event
    # the attempt's kill-on-close Job, for steps that run a child (M01 scrape)
    processes: OwnedJob | None = None


def _needs_gpu(executor: StageExecutor, manifest: m.InputManifest) -> bool:
    decide = getattr(executor, "needs_gpu", None)
    return bool(decide(manifest)) if callable(decide) else executor.uses_gpu


@dataclass
class _Unsettled:
    """A Job that did not settle in time: owner and GPU stay held (C4)."""

    running: _Running
    outcome: ExecOutcome


@dataclass
class DispatchStatus:
    state: str
    reason: str
    current: dict[str, Any] | None
    waiting: dict[str, str] = field(default_factory=dict)
    paused: bool = False


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
        return_step: ReturnStep | None = None,
        scrape_step: ComponentStep | None = None,
        off_reasons: dict[str, str] | None = None,
        finalizer: Finalizer | None = None,
        workflow_log: WorkflowLog | None = None,
        power: Callable[[], Any] | None = None,
    ) -> None:
        self.runtime, self.lease = runtime, lease
        # PWR-2: the agent's power sampler, read late (it starts after the host)
        self.power = power or power_estimate.get_sampler
        self.workflow_log = workflow_log or WorkflowLog(None)  # U1e: 日志 rows
        self.finalizer = finalizer
        self.return_step, self.scrape_step = return_step, scrape_step
        # unconfigured component stages, shown as the waiting reason (M01 §4.4)
        configured = {
            stage
            for stage, step in (("return", return_step), ("scrape", scrape_step))
            if step is not None
        }
        self.off_reasons = {
            stage: reason
            for stage, reason in (off_reasons or {}).items()
            if stage not in configured
        }
        self.poll, self.stop_settle, self.renew_every = poll, stop_settle, renew_every
        # A process-unique generation: never equal to a manual task's RunId (C5).
        self.run_id: gpu_lease.RunId = ("workflow", next_generation())
        self.cancel = threading.Event()
        if finalizer is not None:  # shutdown stops finalization too (constitution §4)
            finalizer.cancel = self.cancel
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running: _Running | None = None
        self._unsettled: _Unsettled | None = None
        self._waiting: dict[str, str] = {}
        self._reason = "idle"
        self._backoff = False  # wait a poll before retrying a transient failure
        self._holding = False
        # V3 §7.5: no new claims while paused; in-flight work and the finalizer
        # carry on. In memory: a restart resumes dispatch.
        self.paused = False
        # Admission window: held by the dispatch thread from the paused check through
        # the claim (never while a stage runs), so an acknowledged pause cannot be
        # followed by a new claim.
        self._admission = threading.Lock()
        self._admitting = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="workflow-dispatch", daemon=True
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._wake.set()
        if self.paused:
            # Return only once any admission in progress has finished: it either saw
            # the flag and claimed nothing, or its claim is already in flight.
            with self._admission:
                pass

    def stop(self, timeout: float = 60.0) -> bool:
        """True when the dispatch thread has exited (or never started)."""
        self.cancel.set()
        self._wake.set()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def current_stage(self) -> tuple[str, str] | None:
        """(film_id, stage) of the running stage, else None. A pure read for
        the power sampler's activity and owner (PWR-1 PW03/PW04)."""
        with self._lock:
            running = self._running
            return None if running is None else (running.job.film_id, running.job.stage)

    def status(self) -> DispatchStatus:
        with self._lock:
            current: dict[str, Any] | None = None
            running = self._running
            if running is not None:
                current = {
                    "film_id": running.job.film_id,
                    "film": running.name or running.job.display_name,
                    "stage": running.job.stage,
                    "progress": running.progress,
                    "started_at": running.started_at,
                    # PWR-2: whether the stage owns the GPU (and so the machine)
                    "uses_gpu": running.uses_gpu,
                }
                if running.ltx is not None:
                    current["ltx"] = running.ltx
            state = (
                "stopping_unconfirmed"
                if self._unsettled is not None
                else "running"
                if self._running is not None
                else "paused"
                if self.paused
                else "idle"
            )
            waiting = dict(self._waiting)
            if self.finalizer is not None:
                waiting.update(self.finalizer.waiting)
        if current is not None and running is not None:
            # PWR-2, read outside the dispatch lock (the sampler reads
            # `current_stage` under its own): the energy so far and, only on
            # the GPU-owning stage, the machine's live estimate (PW12)
            current["energy"] = self._energy_dict(running)
            current["power"] = self._live_power() if running.uses_gpu else None
        return DispatchStatus(state, self._reason, current, waiting, self.paused)

    # ── power (PWR-2) ───────────────────────────────────────────────────
    def _sampler(self) -> Any:
        try:
            return self.power()
        except Exception:
            log.exception("workflow power sampler unavailable")
            return None

    def _energy_reading(self, film_id: str) -> Any:
        """The film's counter at the claim; None without a sampler."""
        sampler = self._sampler()
        if sampler is None:
            return None
        try:
            return sampler.reading(film_id)
        except Exception:
            log.exception("workflow power reading failed")
            return None

    def _energy(self, running: _Running) -> m.AttemptEnergy | None:
        """The film's energy from the claim to now; None when there is no
        value (no sampler, sampling off or restarted, another counter, an
        interval the counter did not cover — IR02: missing, never a zero)."""
        start = running.energy_start
        sampler = self._sampler() if start is not None else None
        if sampler is None:
            return None
        try:
            delta = sampler.energy_between(start, sampler.reading(start.owner))
            if delta is None:
                return None
            return m.AttemptEnergy.model_validate(delta.to_dict())
        except Exception:
            log.exception("workflow attempt energy unavailable")
            return None

    def _energy_dict(self, running: _Running) -> dict[str, Any] | None:
        energy = self._energy(running)
        if energy is None:
            return None
        value = energy.model_dump(mode="json")
        value.pop("schema_version", None)
        return value

    def _live_power(self) -> dict[str, Any] | None:
        sampler = self._sampler()
        if sampler is None:
            return None
        try:
            return sampler.live()
        except Exception:
            log.exception("workflow live power unavailable")
            return None

    def reconcile_retries(self) -> None:
        """R3: make retries a crash lost between resolution and requeue."""
        rt = self.runtime
        try:
            for job in rt.store.list_jobs(rt.operator, "failed", _LOCAL):
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
            if not self.cancel.is_set():
                self._finalize()
            if not worked or self._backoff:
                # a transient failure waits a poll before its retry: an antivirus or
                # indexer hold outlasts three back-to-back attempts (H1 IR01)
                self._backoff = False
                self._wake.wait(self.poll)
                self._wake.clear()
        if self._unsettled is None:  # a still-running tree keeps the GPU (C4)
            self._release()

    def _finalize(self) -> None:
        """Archive and clean approved films (F02-P1); never holds the GPU, never spins."""
        if self.finalizer is None:
            return
        try:
            self.finalizer.run_once()
        except Exception:
            log.exception("workflow finalize tick failed")

    # ── one tick ────────────────────────────────────────────────────────
    def run_once(self) -> bool:
        """Claim and run at most one stage; True when a stage ran."""
        rt = self.runtime
        if self._unsettled is not None and not self._finish_unsettled():
            return False
        if not rt.reconciled and not rt.settle_previous(wait=0):
            self._reason = rt.reason
            return False
        self._admission.acquire()
        self._admitting = True
        try:
            return self._admit_and_run()
        finally:
            self._end_admission()

    def _end_admission(self) -> None:
        """Close the admission window (dispatch thread only); idempotent."""
        if self._admitting:
            self._admitting = False
            self._admission.release()

    def _paused_exit(self) -> bool:
        self._reason = "paused"
        self._set_waiting({})
        self._release()  # give up the GPU (or its queue place) while paused
        return False

    def _admit_and_run(self) -> bool:
        rt = self.runtime
        if self.paused:
            return self._paused_exit()
        steps = self._steps()
        stages = _COMPUTE + tuple(steps) + tuple(self.off_reasons)
        jobs = rt.store.list_jobs(rt.operator, "pending", stages)
        waiting: dict[str, str] = {}
        vram = rt.gpu.vram_mib if rt.gpu else 0
        gpu_waited = False
        for job in jobs:
            if self.paused:  # a pause arrived while admission was blocked
                return self._paused_exit()
            manifest = rt.store.get_input_manifest(
                rt.operator, job.film_id, job.input_revision
            )
            if (
                job.stage in self.off_reasons
            ):  # visible, never silently stuck (M01 §4.4)
                waiting[job.film_id] = self.off_reasons[job.stage]
                continue
            if job.stage in steps:
                self._leave_queue()  # about to run a stage without the GPU (E7)
                step, auth = steps[job.stage]
                ran = self._execute_step(job, manifest, step, auth)
            else:  # only compute stages are listed besides the component steps
                executor = rt.executors.get(cast(ComputeStage, job.stage))
                if executor is None or rt.gpu is None:
                    waiting[job.film_id] = (
                        rt.reason if rt.gpu is None else "没有可用的执行器"
                    )
                    continue
                if not fits(manifest, executor.max_resolution(vram)):
                    waiting[job.film_id] = NO_4K_DEVICE
                    continue
                gpu = _needs_gpu(executor, manifest)
                name = self._name(job, manifest)
                if gpu and not self._acquire(job, name):
                    gpu_waited = True  # keep the queue place, look further (E7)
                    waiting[job.film_id] = "等待 GPU：" + (
                        self.lease.blocking_label()
                        if hasattr(self.lease, "blocking_label")
                        else ""
                    )
                    continue
                if not gpu:
                    self._leave_queue()
                ran = self._execute(job, executor, manifest, gpu, name)
            if ran:
                self._set_waiting(waiting)
                return True
            waiting[job.film_id] = self._reason  # claim refused: try the next film
            if self.paused:
                return self._paused_exit()
        self._set_waiting(waiting)
        if self._holding:
            self._release()
        elif not gpu_waited:
            self._leave_queue()
        return False

    def _set_waiting(self, waiting: dict[str, str]) -> None:
        with self._lock:
            self._waiting = waiting

    def _name(self, job: m.PendingComputeJob, manifest: m.InputManifest) -> str:
        """The film's shown name (H3 D06); a failed read never stops dispatch."""
        try:
            return board.name_of(self.runtime, job.film_id, manifest) or (
                job.display_name
            )
        except Exception:
            log.exception("workflow film name unavailable for %s", job.film_id)
            return job.display_name

    def _acquire(self, job: m.PendingComputeJob, name: str) -> bool:
        if self.lease.try_acquire(self.run_id, self.poll, f"影片工作流：{name}"):
            self._holding = True
            return True
        return False

    def _release(self) -> None:
        """Give the GPU back if held, else leave the queue (stop and resolution)."""
        if self._holding:
            self.lease.release(self.run_id)
            self._holding = False
        else:
            self._leave_queue()

    def _release_if_holding(self) -> None:
        if self._holding:
            self.lease.release(self.run_id)
            self._holding = False

    def _leave_queue(self) -> None:
        """The only place the workflow gives up its gpu_lease queue place (E7)."""
        if not self._holding:
            self.lease.withdraw(self.run_id)

    # ── execution ───────────────────────────────────────────────────────
    def _execute(
        self,
        job: m.PendingComputeJob,
        executor: StageExecutor,
        manifest: m.InputManifest,
        uses_gpu: bool,
        name: str = "",
    ) -> bool:
        """Claim and run one stage; False (nothing ran) when the claim is refused."""
        rt = self.runtime
        worker = rt.worker()
        if worker.physical_binding is None:
            self._release_if_holding()
            self._reason = "本机 GPU 尚未登记"
            return False
        if self.paused:  # re-checked at the claim, after any blocking lease wait
            self._release_if_holding()
            self._reason = "paused"
            return False
        tools = executor.tools(job)  # U1 P4': the planned tier, read once
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
            self._release_if_holding()
            self._reason = f"领取失败：{exc}"
            return False
        assignment_id = claimed.assignment_id
        if assignment_id is None:  # T01 always reports it for a claim
            raise m.WorkflowError("precondition_failed")
        attempt = replace(attempt, assignment_id=assignment_id)
        rt.write_attempt(attempt)
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

        running = _Running(job, attempt, assignment_id, owner, None, uses_gpu, name)
        running.energy_start = self._energy_reading(job.film_id)
        with self._lock:
            self._running = running
        self._end_admission()  # the claim is visible as running: admission is over

        def progress(text: str, ltx: dict[str, Any] | None = None) -> None:
            running.progress = text[-200:]
            running.ltx = ltx  # a plain update ends the previous LTX observation

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
        except (
            Exception
        ) as exc:  # setup error / executor bug: needs-attention; a held file: transient
            log.exception("workflow stage %s failed", job.stage)
            outcome = ExecOutcome.failed(classify_exception(exc), f"执行器异常：{exc}")
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
            current = rt.open_assignment(assignment_id)
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
        except m.WorkflowError as exc:
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
            current = rt.open_assignment(running.assignment_id)
            kind = (
                SUCCEEDED_KIND if outcome.ok else failed_kind(outcome.error_class)  # type: ignore[arg-type]
            )
            owner = running.owner
            rt.store.resolve_assignment(
                rt.operator,
                rt.meta(),
                m.AssignmentResolution(
                    assignment_id=running.assignment_id,
                    attempt_id=running.attempt.attempt_id,
                    expected_version=current.version,
                    reason=outcome.message or ("完成" if outcome.ok else "失败"),
                    operation_id=running.attempt.operation_id,
                    local_owner=None
                    if owner is None
                    else owner.model_copy(
                        update={
                            "state": "stopped",
                            "evidence": rt.evidence("native_owner_stopped"),
                        }
                    ),
                    evidence=(
                        rt.evidence(
                            kind,
                            outcome.message or None,
                            payload_digest=outcome.result_digest,
                            timing=outcome.timing,
                            energy=self._energy(running),  # PWR-2, every attempt
                        ),
                    ),
                    outcome="succeeded" if outcome.ok else "failed",
                ),
            )
            # IR03: the store now holds the attempt's energy; a board read
            # must not add it again as the running stage's
            with self._lock:
                self._running = None
            rt.drop_attempt(running.attempt.attempt_id)
            self._log_resolved(running, outcome)
            if outcome.ok:
                self._drop_other_workdirs(running)  # E4: superseded outputs
            else:
                self._retry_if_due(running.job.job_id)  # C6
                self._reason = outcome.message
                self._backoff = outcome.error_class == "transient"
        finally:
            with self._lock:
                self._running = None
            if running.uses_gpu:
                self._release()

    def _log_resolved(self, running: _Running, outcome: ExecOutcome) -> None:
        """U1e: one 日志 row per finished attempt (a stop is no failure), and
        when translate succeeds, the film's whole run from the store."""
        if outcome.error_class == "cancelled":
            return
        try:
            job, name = running.job, running.name or running.job.film_id
            started = datetime.fromisoformat(running.started_at)
            duration = (datetime.now(timezone.utc) - started).total_seconds()
            self.workflow_log.stage(
                name, job.stage, ok=outcome.ok, duration=duration, note=outcome.message
            )
            if outcome.ok and job.stage == "translate":
                rt = self.runtime
                films = rt.store.film_board(rt.operator, film_id=job.film_id)
                if films:
                    self.workflow_log.translated(name, film_timing(films[0]))
        except Exception:  # the log never changes the stage's outcome
            log.exception("workflow log row failed")

    def _drop_other_workdirs(self, running: _Running) -> None:
        rt = self.runtime
        for attempt in rt.store.list_attempts(rt.operator, running.job.job_id):
            if attempt.attempt_id != running.attempt.attempt_id:
                shutil.rmtree(rt.work_dir / attempt.attempt_id, ignore_errors=True)

    def _steps(self) -> dict[str, tuple[ComponentStep, AuthProof]]:
        """Configured component stages and the principal that claims each."""
        rt, steps = self.runtime, {}
        if self.return_step is not None:
            steps["return"] = (self.return_step, rt.receiver_auth)
        if self.scrape_step is not None:
            steps["scrape"] = (self.scrape_step, rt.scrape_auth)
        return steps

    def _execute_step(
        self,
        job: m.PendingComputeJob,
        manifest: m.InputManifest,
        step: ComponentStep,
        auth: AuthProof,
    ) -> bool:
        """Claim a component stage (return E5, scrape M01) and run it in its own Job."""
        rt = self.runtime
        if self.paused:
            self._reason = "paused"
            return False
        try:
            tools = step.tools()
        except Exception as exc:  # e.g. the Miko profile cannot be read
            self._reason = f"无法准备 {job.stage}：{exc}"
            return False
        attempt = AttemptFile(
            attempt_id=_new_id(), operation_id=_new_id(), job_id=job.job_id
        )
        rt.write_attempt(attempt)
        try:
            claimed = rt.store.claim(
                auth,
                rt.meta(),
                m.ClaimRequest(
                    job_id=job.job_id,
                    expected_version=job.version,
                    attempt_id=attempt.attempt_id,
                    operation_id=attempt.operation_id,
                    worker_id=None,
                    boot_id=rt.boot_id,
                    profile_revision=tools.profile_revision,
                    config_digest=tools.config_digest,
                    resource_id=None,
                    tool_versions=tools,
                ),
            )
        except m.WorkflowError as exc:
            rt.drop_attempt(attempt.attempt_id)
            self._reason = f"领取失败：{exc}"
            return False
        if claimed.assignment_id is None:  # T01 always reports it for a claim
            raise m.WorkflowError("precondition_failed")
        attempt = replace(attempt, assignment_id=claimed.assignment_id)
        rt.write_attempt(attempt)
        children: list[str] = []

        def launched(identity: str) -> None:
            children.append(identity)
            rt.write_attempt(replace(attempt, children=tuple(children)))

        # No renewal thread: the 24 h lease outlives these stages (D11).
        running = _Running(
            job,
            attempt,
            claimed.assignment_id,
            None,
            None,
            False,
            self._name(job, manifest),
        )
        running.energy_start = self._energy_reading(job.film_id)
        with self._lock:
            self._running = running
        self._end_admission()  # the claim is visible as running: admission is over
        try:
            running.processes = OwnedJob(on_launch=launched)
            outcome = step.run(
                ReturnContext(
                    job,
                    manifest,
                    attempt.attempt_id,
                    attempt.operation_id,
                    rt,
                    self.cancel,
                    running.processes,
                )
            )
        except Exception as exc:  # a step bug: needs-attention; a held file: transient
            log.exception("workflow %s failed", job.stage)
            outcome = ExecOutcome.failed(
                classify_exception(exc), f"{job.stage} 异常：{exc}"
            )
        if self.cancel.is_set() and not outcome.ok:
            outcome = ExecOutcome.failed("cancelled", "已停止")
        processes = running.processes
        if processes is not None:
            processes.terminate()
            if not processes.settle(self.stop_settle):
                self._unsettled = _Unsettled(running, outcome)
                self._reason = "停止未确认：任务进程仍在运行"
                return True
        self._resolve(running, outcome)
        return True

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
