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
from typing import Any, Literal, Protocol

from .local_gpu import max_resolution
from .models import InputManifest, PendingComputeJob, ToolVersions
from .owned_process import OwnedJob

ErrorClass = Literal["transient", "permission", "needs_attention", "cancelled"]
ComputeStage = Literal["restore", "asr", "translate"]

# The error class survives restarts in the attempt's evidence kind (D18).
SUCCEEDED_KIND = "exec_succeeded"
INTERRUPTED_KIND = "exec_interrupted"


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

    @property
    def ok(self) -> bool:
        return self.error_class is None

    @classmethod
    def succeeded(
        cls, message: str = "", result_digest: str | None = None
    ) -> ExecOutcome:
        return cls(None, message, result_digest)

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


class StageExecutor(Protocol):
    stage: ComputeStage
    uses_gpu: bool  # default answer of needs_gpu(); executors may decide per film

    def tools(self, job: PendingComputeJob | None = None) -> ToolVersions:
        """The claim's record; `job` is the job to claim (None: the profile)."""
        ...

    def max_resolution(self, vram_mib: int) -> tuple[int, int]: ...

    def run(self, context: JobContext) -> ExecOutcome: ...


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
