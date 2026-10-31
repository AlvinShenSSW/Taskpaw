"""Stage executor contract for the local dispatcher (T03-P1 §3.2).

T04 registers the real LTX / ASR / translation executors. Here: the contract and
a synthetic executor used by tests.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .local_gpu import LocalGpu, max_resolution
from .models import InputManifest, PendingComputeJob, RestoreTiming, ToolVersions
from .owned_process import OwnedJob

if TYPE_CHECKING:
    from .stage_results import StageResult

# `machine`: the controller's own class for a remote failure caused by the
# worker's machine, not the film; that worker is put on 停领 and the attempt is a
# free retry elsewhere (never sent by a worker).
ErrorClass = Literal[
    "transient", "permission", "needs_attention", "cancelled", "machine"
]
ComputeStage = Literal["restore", "asr", "translate"]

# The error class survives restarts in the attempt's evidence kind (D18).
SUCCEEDED_KIND = "exec_succeeded"
INTERRUPTED_KIND = "exec_interrupted"
# The success notes of restore and ASR (their evidence `note`). The store reads
# the per-volume decisions back so that only attempts whose tool ran on every
# volume become GPU rate samples (P2-1 CX3).
RESTORE_DONE = "修复完成：{applied} 卷修复，{reused} 卷复用"
ASR_DONE = "字幕来源：{summary}"


def failed_kind(error_class: ErrorClass) -> str:
    return f"exec_failed:{error_class}"


def classify_exception(exc: BaseException) -> ErrorClass:
    """The class of an unexpected stage error (H1-2).

    Windows reports a file briefly held by antivirus or the indexer (a sharing
    violation) as `PermissionError`; the automatic retry outlasts it. Anything
    else is for the operator.
    """
    return "transient" if isinstance(exc, PermissionError) else "needs_attention"


@dataclass(frozen=True)
class ExecOutcome:
    error_class: ErrorClass | None = None
    message: str = ""
    # sha256 of the stage's stage-result.json, kept in the success evidence (T04 E4)
    result_digest: str | None = None
    # U1e: a restore's film timing, kept in the success evidence's summary
    timing: RestoreTiming | None = None

    @property
    def ok(self) -> bool:
        return self.error_class is None

    @classmethod
    def succeeded(
        cls,
        message: str = "",
        result_digest: str | None = None,
        timing: RestoreTiming | None = None,
    ) -> ExecOutcome:
        return cls(None, message, result_digest, timing)

    @classmethod
    def failed(cls, error_class: ErrorClass, message: str) -> ExecOutcome:
        return cls(error_class, message)


class Progress(Protocol):
    """A stage's short status text; restore adds its parsed LTX state (H3 M3)."""

    def __call__(self, text: str, ltx: dict[str, Any] | None = None) -> None: ...


def _no_progress(text: str, ltx: dict[str, Any] | None = None) -> None:
    pass


@dataclass
class JobContext:
    job: PendingComputeJob
    manifest: InputManifest
    workdir: Path
    cancel: threading.Event
    processes: OwnedJob
    progress: Progress = field(default=_no_progress)


class ExecRuntime(Protocol):
    """What an executor and `exec_common` may use of the machine it runs on
    (P2-3 §4): the controller's `LocalRuntime` or a worker's `WorkerRuntime`."""

    @property
    def work_dir(self) -> Path:
        """The root of the work copies and of every attempt's work folder."""
        ...

    @property
    def gpu(self) -> LocalGpu | None: ...

    def stage_result(self, film_id: str, stage: str) -> StageResult | None:
        """The verified ledger of the film's current `stage`; None when the
        film has no such stage. `StageResultError` when it cannot be vouched
        for (not succeeded, missing, altered)."""
        ...


class StageExecutor(Protocol):
    stage: ComputeStage
    uses_gpu: bool  # default answer of needs_gpu(); executors may decide per film

    def tools(self, job: PendingComputeJob | None = None) -> ToolVersions:
        """The claim's record; `job` is the job to claim (None: the profile)."""
        ...

    def max_resolution(self, vram_mib: int) -> tuple[int, int]: ...

    def run(self, context: JobContext) -> ExecOutcome: ...


def needs_gpu(executor: StageExecutor | None, manifest: InputManifest) -> bool:
    """Whether the stage runs a GPU tool for this film: the executor's own
    `needs_gpu(manifest)` when it has one, else its `uses_gpu`; no executor
    assumes the GPU. The one rule of the controller's dispatcher and of the
    scheduler's pass (P2-1 IR3)."""
    if executor is None:
        return True
    decide = getattr(executor, "needs_gpu", None)
    return bool(decide(manifest)) if callable(decide) else executor.uses_gpu


# Exit codes of the synthetic child (sysexits-style).
_EXIT_CLASSES: dict[int, ErrorClass] = {75: "transient", 77: "permission"}


class SyntheticExecutor:
    """Runs one Python child; its exit code decides the outcome. Tests only."""

    def __init__(
        self,
        stage: ComputeStage,
        script: str = "",
        *,
        uses_gpu: bool = True,
        revision: int = 1,
    ) -> None:
        self.stage, self.script, self.uses_gpu = stage, script, uses_gpu
        self.revision = revision
        self.runs: list[str] = []

    def tools(self, job: PendingComputeJob | None = None) -> ToolVersions:
        return ToolVersions(
            tool="synthetic",
            tool_version="1",
            model="synthetic",
            model_version="1",
            profile=f"synthetic-{self.stage}",
            profile_revision=self.revision,
            config_digest="c" * 64,
        )

    def max_resolution(self, vram_mib: int) -> tuple[int, int]:
        return max_resolution(vram_mib)

    def argv(self) -> Sequence[str]:
        return [sys.executable, "-c", self.script or "pass"]

    def run(self, context: JobContext) -> ExecOutcome:
        self.runs.append(context.job.job_id)
        child = context.processes.launch(
            self.argv(),
            cwd=str(context.workdir),
            on_line=lambda _, text: context.progress(text),
        )
        code = child.wait(context.cancel)
        if code is None:
            return ExecOutcome.failed("cancelled", "stopped")
        if code == 0:
            return ExecOutcome.succeeded()
        return ExecOutcome.failed(
            _EXIT_CLASSES.get(code, "needs_attention"), f"exit code {code}"
        )
