"""Shared subtitle engine (#177, reused by #179's `avsubs` task).

Knows nothing about Jasna: paths in, outcomes out.

- `child`      — `ChildProcess` (readers, bounded tail, descendant tracking,
                 `terminate_tree` -> bool) + `Eof` + `asr_env`
- `whisperjav` — engine presets, argv, owned-flag rule, `validate_fields`,
                 manifest outcome
- `srt`        — strict parse/serialize
- `translate`  — `Translator`, the `llm-worker` client thread
- `job`        — `SubsJob`: ASR attempts, identity check, atomic publishing,
                 dropping the `.ja.srt` checkpoint once completed (#187),
                 live ASR `progress` (#189)
- `progress`   — pure progress observation (#189): `AsrProgress` (WhisperJAV
                 phases/scenes), `FilmTracker` + `LiveFacts` (per-film steps
                 and rows), `parse_eta`
- `util`       — `bounded` (alert-sized detail) and `exists_quietly`

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
from taskpaw_v3.monitors.subs.progress import (
    AVSUBS_STEPS,
    JASNA_STEPS,
    AsrProgress,
    FilmTracker,
    LiveFacts,
    parse_eta,
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
from taskpaw_v3.monitors.subs.util import bounded, exists_quietly
from taskpaw_v3.monitors.subs.whisperjav import (
    DEFAULT_ENGINE,
    ENGINES,
    AsrOutcome,
    Engine,
    owned_flags_in,
    validate_fields,
)

__all__ = [
    "AVSUBS_STEPS",
    "CANCELLED",
    "DEFAULT_ENGINE",
    "ENGINES",
    "JASNA_STEPS",
    "AsrOutcome",
    "AsrProgress",
    "ChildProcess",
    "Cue",
    "Engine",
    "Eof",
    "FilmTracker",
    "JobOutcome",
    "LLM_ENV_PREFIX",
    "LiveFacts",
    "RunId",
    "SkipReason",
    "SrtError",
    "SubsJob",
    "Terminal",
    "TranslateRequest",
    "TranslateResult",
    "Translator",
    "asr_env",
    "bounded",
    "exists_quietly",
    "needs_llm_key",
    "owned_flags_in",
    "parse_eta",
    "source_identity",
    "validate_fields",
]
