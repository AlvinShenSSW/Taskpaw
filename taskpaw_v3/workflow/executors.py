"""Stage executor contract for the local dispatcher (T03-P1 §3.2).

T04 registers the real LTX / ASR / translation executors. Here: the contract and
a synthetic executor used by tests.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

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


@dataclass(frozen=True)
class ExecOutcome:
    error_class: ErrorClass | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.error_class is None

    @classmethod
    def succeeded(cls, message: str = "") -> ExecOutcome:
        return cls(None, message)

    @classmethod
    def failed(cls, error_class: ErrorClass, message: str) -> ExecOutcome:
        return cls(error_class, message)


@dataclass
class JobContext:
    job: PendingComputeJob
    manifest: InputManifest
    workdir: Path
    cancel: threading.Event
    processes: OwnedJob
    progress: Callable[[str], None] = field(default=lambda text: None)


class StageExecutor(Protocol):
    stage: ComputeStage
    uses_gpu: bool

    def tools(self) -> ToolVersions: ...

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

    def tools(self) -> ToolVersions:
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
