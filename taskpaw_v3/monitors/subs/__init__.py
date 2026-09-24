"""Shared subtitle engine (#177, reused by #179's `avsubs` task).

Knows nothing about Jasna: paths in, outcomes out.

- `child`      — `ChildProcess` (readers, bounded tail, descendant tracking,
                 `terminate_tree` -> bool) + `Eof` + `asr_env`
- `whisperjav` — engine presets, argv, owned-flag rule, `validate_fields`,
                 manifest outcome
- `srt`        — strict parse/serialize
- `translate`  — `Translator`, the `llm-worker` client thread
- `job`        — `SubsJob`: ASR attempts, identity check, atomic publishing

Nothing here is imported from `lada.py` (C1).
"""

from taskpaw_v3.monitors.subs.child import LLM_ENV_PREFIX, ChildProcess, Eof, asr_env
from taskpaw_v3.monitors.subs.job import (
    JobOutcome,
    SkipReason,
    SubsJob,
    Terminal,
    source_identity,
)
from taskpaw_v3.monitors.subs.srt import Cue, SrtError
from taskpaw_v3.monitors.subs.translate import (
    CANCELLED,
    RunId,
    TranslateRequest,
    TranslateResult,
    Translator,
    needs_llm_key,
)
from taskpaw_v3.monitors.subs.whisperjav import (
    DEFAULT_ENGINE,
    ENGINES,
    AsrOutcome,
    Engine,
    owned_flags_in,
    validate_fields,
)

__all__ = [
    "CANCELLED",
    "DEFAULT_ENGINE",
    "ENGINES",
    "AsrOutcome",
    "ChildProcess",
    "Cue",
    "Engine",
    "Eof",
    "JobOutcome",
    "LLM_ENV_PREFIX",
    "RunId",
    "SkipReason",
    "SrtError",
    "SubsJob",
    "Terminal",
    "TranslateRequest",
    "TranslateResult",
    "Translator",
    "asr_env",
    "needs_llm_key",
    "owned_flags_in",
    "source_identity",
    "validate_fields",
]
