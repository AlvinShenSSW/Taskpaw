"""The shipped local pipeline: executors built from agent.yaml (T04-P1 E9).

A missing tool path leaves its stage without an executor, which T03 reports as
"没有可用的执行器"; there is never a second, manually added Jasna monitor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from taskpaw_v3.core.config import WorkflowSettings

from .exec_asr import AsrExecutor
from .exec_restore import RestoreExecutor, resolve_gpu_preset
from .exec_translate import TranslateExecutor
from .executors import StageExecutor
from .local_gpu import LocalGpu, query_gpu
from .local_runtime import LocalRuntime
from .lookup import MetadataLookup
from .scrape_step import LocalScrapeStep

log = logging.getLogger("taskpaw.workflow.pipeline")


def default_executors(
    settings: WorkflowSettings,
    gpu: Callable[[], LocalGpu | None] = query_gpu,
) -> Callable[[LocalRuntime], list[StageExecutor]]:
    def build(runtime: LocalRuntime) -> list[StageExecutor]:
        executors: list[StageExecutor] = []
        if settings.jasna_exe_path:
            # U1d: an unset `ltx_gpu` is decided once, here, so the approved
            # capability profile keeps matching for the runtime's life
            auto = not settings.ltx_gpu
            preset = settings.ltx_gpu or resolve_gpu_preset(gpu())
            try:
                executors.append(
                    RestoreExecutor(
                        runtime,
                        settings.jasna_exe_path,
                        preset,
                        preset_auto=auto,
                        ffprobe=settings.ffprobe_path,
                    )
                )
            except ValueError:
                log.exception("LTX restore disabled: invalid settings")
        if settings.whisperjav_exe_path:
            try:
                executors.append(AsrExecutor(runtime, settings.whisperjav_exe_path))
            except ValueError:
                log.exception("ASR disabled: invalid settings")
            executors.append(TranslateExecutor(runtime))
        return executors

    return build


MIKO_OFF = "未配置 Miko（mdcx_python / mdcx_root / mdcx_profile）"


def default_lookup(
    settings: WorkflowSettings,
) -> Callable[[LocalRuntime], MetadataLookup] | None:
    """The U1 metadata preview runs on the same Miko environment as scrape."""
    if not (settings.mdcx_python and settings.mdcx_root and settings.mdcx_profile):
        return None
    python, root, profile = (
        settings.mdcx_python,
        settings.mdcx_root,
        settings.mdcx_profile,
    )

    def build(runtime: LocalRuntime) -> MetadataLookup:
        return MetadataLookup(runtime, python=python, root=root, profile=profile)

    return build


def default_scrape_step(settings: WorkflowSettings) -> LocalScrapeStep | None:
    """Miko post-processing (M01) only when its environment is fully configured."""
    if not (settings.mdcx_python and settings.mdcx_root and settings.mdcx_profile):
        return None
    return LocalScrapeStep(
        python=settings.mdcx_python,
        root=settings.mdcx_root,
        profile=settings.mdcx_profile,
        ffprobe=settings.ffprobe_path,
    )
