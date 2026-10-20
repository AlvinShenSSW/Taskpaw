"""The shipped local pipeline: executors built from agent.yaml (T04-P1 E9).

A missing tool path leaves its stage without an executor, which T03 reports as
"没有可用的执行器"; there is never a second, manually added Jasna monitor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from taskpaw_v3.core.config import WorkflowSettings

from .exec_asr import AsrExecutor
from .exec_restore import RestoreExecutor
from .exec_translate import TranslateExecutor
from .executors import StageExecutor
from .local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.pipeline")


def default_executors(
    settings: WorkflowSettings,
) -> Callable[[LocalRuntime], list[StageExecutor]]:
    def build(runtime: LocalRuntime) -> list[StageExecutor]:
        executors: list[StageExecutor] = []
        if settings.jasna_exe_path:
            try:
                executors.append(
                    RestoreExecutor(
                        runtime,
                        settings.jasna_exe_path,
                        settings.ltx_gpu,
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
