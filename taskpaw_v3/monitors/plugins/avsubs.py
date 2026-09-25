"""`avsubs` monitor — the standalone「AV 翻译 (subtitles)」task (#179).

Points at a library folder, walks it (recursively by default) and, for every
video without a same-named `.srt`, writes `<stem>.ja.srt` (WhisperJAV) and
`<stem>.srt` (Simplified Chinese through the agent's LLM setting) next to the
video. The `.ja.srt` is only the resume checkpoint (#187): it is deleted once
the job settles `completed` with its `.srt` published — a pre-existing library
`.ja.srt` included — and kept on every other path, so the next Start only
translates. Everything engine-related is the shared `taskpaw_v3.monitors.subs`
package (#177); this module owns tree planning (`plan_tree`), the task's queue,
settlement, abort, `done` and status.

The #177 rules carry over verbatim: every planned job reaches exactly one
terminal state, settled only under `_launch_lock` with `_settled` checked
before any publish; blocking side effects of a settlement are DEFERRED and run
by the lock holder right after it released the lock (`_run_deferred`); only
`_dispatch()` advances the queue.

GPU: the WhisperJAV child never runs at the same time as another GPU task's
child — every ASR launch first takes the process-wide lease
(`core/gpu_lease.py`); a refusal is a visible wait (`waiting for GPU (held by
…)`), retried on the next check. The lease is released on every ASR end path,
only after the tracked process tree was killed (C11); translate-only work never
touches it.

No `shell=True`; the LLM key never reaches the ASR child (`asr_env`), argv,
logs, events or the detail line.

Progress view (#189): a per-run `FilmTracker` is marked at the existing counter
points (ASR start, the `.ja.srt` publish, `translator.submit`, `_settle`) and
the status adds the per-film stepper (`film`, `steps`, `films`, `films_more`,
`model` while translating) derived from what is live at status time, plus
`queue_pre_done`. Read-only observation; the queue semantics are unchanged.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import queue
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from pydantic import Field, field_validator, model_validator

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core.generation import next_generation
from taskpaw_v3.core.llm import get_llm_settings
from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
    State,
)
from taskpaw_v3.monitors.plugins.host_metrics import read_gpu
from taskpaw_v3.monitors.plugins.lada import _cpu_mem
from taskpaw_v3.monitors.subs import asr_env, bounded, exists_quietly

# `Translator` and `ChildProcess` are bound as module-level names and looked up
# at call time, so tests can monkeypatch `AV.Translator` / `AV.ChildProcess`
# for supervisor-created instances (D10).
from taskpaw_v3.monitors.subs.child import ChildProcess
from taskpaw_v3.monitors.subs.job import SubsJob, source_identity
from taskpaw_v3.monitors.subs.progress import (
    ASR,
    AVSUBS_STEPS,
    NAME_CHARS,
    TRANSLATE,
    FilmTracker,
    LiveFacts,
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
from taskpaw_v3.monitors.subs.util import step_numbers
from taskpaw_v3.monitors.subs.whisperjav import (
    DEFAULT_ENGINE,
    Engine,
    attempt_dir,
    validate_fields,
)

log = logging.getLogger("taskpaw.monitors.avsubs")

Kind = Literal["full", "translate_only"]

_OWNER = "AV 翻译 (subtitles)"
_STAGING = ".avsubs"
_ABORT_AFTER = 3
_ASR_MAX_ATTEMPTS = 2
_SRT_TMP_RE = re.compile(r"\.srt\.\d+\.tmp$", re.IGNORECASE)
_TMP_SWEEP_AGE_S = 600.0  # C3/M12: never sweep a temp another task may be writing
_EXT_RE = re.compile(r"[a-z0-9]+")
_MAX_LISTED = 5
_DETAIL_CHARS = 800  # alert detail cap (same value as lada's crash detail)

# Windows reparse tags that make a directory a LINK (C1): a junction / mount
# point and a symbolic link. Any other reparse point (OneDrive placeholders,
# dedup, …) is a real folder and is descended (M1).
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_LINK_TAGS = frozenset({_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK})
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


# ── pure planning (unit-tested without a GPU) ─────────────────────────────
@dataclass(frozen=True)
class TreeItem:
    source: Path
    relpath: str  # POSIX, relative to the root — the job id
    ja_target: Path
    zh_target: Path
    kind: Kind
    identity: tuple[int, int]


@dataclass(frozen=True)
class TreePlan:
    items: list[TreeItem]
    done: int
    collisions: list[tuple[Path, Path]]  # (loser, the source that owns the targets)
    errors: list[str]
    # Every directory that held a qualifying video (the C3 temp sweep's scope).
    folders: list[Path] = field(default_factory=list)


def _printable(text: str) -> str:
    """`text` with anything not encodable as UTF-8 escaped (for reports)."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _is_link_dir(entry: Any) -> bool:
    """A symlink, or (Windows) a junction / symlink reparse point — never
    descended (C1). Python-3.10-safe: `os.path.isjunction` is 3.12+, so the
    reparse tag of the un-followed stat is checked instead."""
    try:
        if entry.is_symlink():
            return True
        st = entry.stat(follow_symlinks=False)
    except OSError as e:
        # Not a link we can prove; descending it will report it if unreadable.
        log.debug("avsubs: cannot stat %s (%s)", entry.name, type(e).__name__)
        return False
    return getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def _should_descend(entry: Any) -> bool:
    """Descend a subdirectory unless it is hidden by name (`.x`), a link
    (C1), or carries the Windows HIDDEN / SYSTEM attribute (M2:
    `$RECYCLE.BIN`, `System Volume Information`)."""
    if entry.name.startswith("."):
        return False
    if _is_link_dir(entry):
        return False
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError as e:
        log.debug("avsubs: cannot stat %s (%s)", entry.name, type(e).__name__)
        return True
    return not attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM)


def _norm_exts(extensions: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for ext in extensions:
        e = str(ext).strip().casefold()
        if e.startswith("."):
            e = e[1:]
        if e:
            out.add(e)
    return out


def plan_tree(root: str, recursive: bool, extensions: Iterable[str]) -> TreePlan:
    """Pure: walk `root` (iteratively, `os.scandir`) and plan every video.

    Raises `OSError` when `root` itself cannot be listed. An unreadable
    subfolder and a name that is not encodable as UTF-8 (D10) are reported in
    `errors` and skipped. Qualifying files (extension in the set, case-
    insensitive; no `.tmp.` in the name; not a macOS `._` AppleDouble file)
    are ordered by `(relpath.casefold(), relpath)`; in that order each reserves its `<stem>.ja.srt` and `<stem>.srt`
    (key: `normcase(realpath(dir)/name)`) — a file whose target is already
    reserved is a collision (M8). Then: zh exists (0 bytes counts) → done;
    ja exists → `translate_only`; else `full`."""
    exts = _norm_exts(extensions)
    errors: list[str] = []
    folders: list[Path] = []
    candidates: list[tuple[str, Path, str]] = []  # (relpath, source, real dir)
    stack: list[tuple[str, str]] = [(os.fspath(root), "")]
    is_root = True
    while stack:
        path, rel = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as e:
            if is_root:
                raise
            errors.append(f"{_printable(rel)}/: unreadable ({type(e).__name__})")
            continue
        finally:
            is_root = False
        real_dir: Optional[str] = None
        for entry in entries:
            name = entry.name
            relpath = f"{rel}/{name}" if rel else name
            try:
                if entry.is_dir():
                    if recursive and _should_descend(entry):
                        stack.append((entry.path, relpath))
                    continue
                if not entry.is_file():
                    continue
            except OSError as e:
                errors.append(f"{_printable(relpath)}: {type(e).__name__}")
                continue
            suffix = os.path.splitext(name)[1][1:].casefold()
            if not suffix or suffix not in exts:
                continue
            if ".tmp." in name.casefold():
                continue  # a staging/temp file (e.g. Jasna's `x-破解.tmp.mp4`)
            if name.startswith("._"):
                continue  # macOS AppleDouble metadata, not a video
            try:
                relpath.encode("utf-8")
            except UnicodeEncodeError:
                errors.append(f"{_printable(relpath)}: name is not valid Unicode")
                continue
            if real_dir is None:
                real_dir = os.path.realpath(path)  # once per directory (M15)
                folders.append(Path(path))
            candidates.append((relpath, Path(entry.path), real_dir))

    candidates.sort(key=lambda c: (c[0].casefold(), c[0]))
    reserved: dict[str, Path] = {}
    items: list[TreeItem] = []
    collisions: list[tuple[Path, Path]] = []
    done = 0
    for relpath, source, real_dir in candidates:
        ja = source.with_name(f"{source.stem}.ja.srt")
        zh = source.with_name(f"{source.stem}.srt")
        keys = [os.path.normcase(os.path.join(real_dir, t.name)) for t in (ja, zh)]
        owner = next((reserved[k] for k in keys if k in reserved), None)
        if owner is not None:
            collisions.append((source, owner))
            continue
        for k in keys:
            reserved[k] = source
        if exists_quietly(zh):
            done += 1
            continue
        kind: Kind = "translate_only" if exists_quietly(ja) else "full"
        try:
            identity = source_identity(source)  # D11
        except OSError as e:
            errors.append(f"{_printable(relpath)}: {type(e).__name__}")
            continue
        items.append(TreeItem(source, relpath, ja, zh, kind, identity))
    return TreePlan(items, done, collisions, errors, folders)


def sweep_srt_temps(
    folders: Iterable[Path], max_age: float = _TMP_SWEEP_AGE_S
) -> list[Path]:
    """Best effort: delete `*.srt.<digits>.tmp` publish leftovers in `folders`
    that have not been touched for `max_age` seconds (C3 — a fresh one may be
    another task's publish in progress). Returns what was removed."""
    now = time.time()
    removed: list[Path] = []
    for folder in folders:
        try:
            with os.scandir(folder) as it:
                entries = [e for e in it if _SRT_TMP_RE.search(e.name)]
        except OSError as e:
            log.warning("avsubs: temp sweep of %s skipped (%s)", folder, e)
            continue
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if now - entry.stat(follow_symlinks=False).st_mtime < max_age:
                    continue
                os.unlink(entry.path)
            except OSError as e:
                log.warning("avsubs: could not sweep %s: %s", entry.path, e)
                continue
            removed.append(Path(entry.path))
    return removed


def _idle_note(plan: TreePlan, root: str) -> str:
    """The idle detail of a Start with nothing to do (K2): never a
    `(0 already have .srt)`; collisions are counted when there are any."""
    why: list[str] = []
    if plan.done:
        why.append(f"{plan.done} already have .srt")
    if plan.collisions:
        why.append(f"{len(plan.collisions)} skipped as name collisions")
    if why:
        return f"nothing to subtitle ({', '.join(why)})"
    return f"no video files under {root}"


def _default_spawn(argv: list[str]) -> ChildProcess:
    # Resolved at call time so `AV.ChildProcess` can be monkeypatched (D10);
    # stdin=DEVNULL and a merged tail are ChildProcess's defaults.
    return ChildProcess(argv, env=asr_env())


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as e:
        log.warning("avsubs: could not remove %s: %s", path, e)


# ── config ────────────────────────────────────────────────────────────────
class AvsubsConfig(BaseMonitorConfig):
    avsubs_root_folder: str = Field(
        "",
        description="The video library folder to scan (required). Every video "
        "without a same-named .srt gets <name>.srt (Simplified Chinese) next to "
        "it; videos that already have a .srt are skipped. The Japanese transcript "
        "<name>.ja.srt is an intermediate: it is deleted once the .srt is written "
        "and kept when translation does not finish — an existing .ja.srt is "
        "reused (translation only), then deleted the same way. Hidden, "
        "linked or mounted folders, and macOS ._ metadata files, are skipped. A .avsubs working folder is created here. "
        "The GPU is shared with Jasna, one file at a time. Do not let two active "
        "tasks cover overlapping folders, and do not point it at the output "
        "folder of a Jasna task that has AV 翻译 on.",
    )
    avsubs_recursive: bool = Field(
        True,
        description="Also scan every subfolder (default on). Off: only the videos "
        "directly in the library folder.",
    )
    avsubs_extensions: list[str] = Field(
        ["mp4"],
        description="Video extensions to process, without the dot, case-"
        'insensitive (default ["mp4"]; e.g. add mkv).',
    )
    whisperjav_exe_path: str = Field(
        "",
        description="Full path to whisperjav.exe (e.g. "
        r"C:\WhisperJAV\Scripts\whisperjav.exe) — NOT the folder. Required.",
    )
    whisperjav_engine: Engine = Field(
        DEFAULT_ENGINE,
        description="WhisperJAV preset: anime-whisper (default; best on the "
        "prototype), large-v3, large-v2, qwen3, or custom (no preset flags — "
        "set --mode/--model/--qwen-generator yourself in the extra args).",
    )
    whisperjav_extra_args: str = Field(
        "",
        description="Extra WhisperJAV flags appended to every transcription, e.g. "
        "--sensitivity aggressive (quote Windows paths that contain spaces). "
        "TaskPaw owns --output-dir, --output-format, --language, --temp-dir and "
        "--no-signature (and, unless the engine is custom, --mode/--model/"
        "--qwen-generator); those, and any argparse abbreviation of them 4 or "
        "more characters long (e.g. --out), are rejected. --translate* options "
        "are always rejected (they would put an API key on the command line).",
    )
    avsubs_gpu_monitor: bool = Field(
        True,
        description="Report GPU% / VRAM via nvidia-smi (turn off on a machine "
        "without an NVIDIA GPU).",
    )

    @field_validator("avsubs_extensions")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for raw in value:
            ext = raw.strip().lower()
            if ext.startswith("."):
                ext = ext[1:]
            if not _EXT_RE.fullmatch(ext):
                raise ValueError(
                    f"avsubs_extensions: {raw!r} is not an extension (letters and "
                    "digits only, e.g. mp4)"
                )
            if ext not in out:
                out.append(ext)
        if not out:
            raise ValueError("avsubs_extensions needs at least one extension")
        return out

    @model_validator(mode="after")
    def _validate_avsubs(self) -> "AvsubsConfig":
        if not self.avsubs_root_folder.strip():
            raise ValueError(
                f"{_OWNER} needs avsubs_root_folder — the library folder to scan"
            )
        validate_fields(
            self.whisperjav_exe_path,
            self.whisperjav_engine,
            self.whisperjav_extra_args,
            required=True,
            owner=_OWNER,
        )
        return self


# ── the task ──────────────────────────────────────────────────────────────
class AvsubsInstance(MonitorInstance):
    """One library run.

    Locks: `_launch_lock` (RLock) guards every settlement, the ASR spawn and
    stop; blocking side effects (tree kill, translator cancel, rmtree) are
    deferred until it is released. `_gpu_lock` (leaf) makes the lease release
    exactly-once. Lock order: `_launch_lock` → `_gpu_lock` → the lease's own
    leaf lock."""

    def __init__(self, instance_id: str, config: AvsubsConfig) -> None:
        super().__init__(instance_id, config)
        self._launch_lock = threading.RLock()
        self._gpu_lock = threading.Lock()
        self._stopping = threading.Event()
        self._spawn: Callable[[list[str]], ChildProcess] = _default_spawn
        self._started = False
        self._gpu_held_run: Optional[RunId] = None
        self._reset((instance_id, 0))

    @property
    def _cfg(self) -> AvsubsConfig:
        return self.config  # type: ignore[return-value]

    def _reset(self, run: RunId) -> None:
        """Every per-run field back to its initial value under `run`."""
        self._run: RunId = run
        self._staging_root: Optional[Path] = None
        self._queue: list[TreeItem] = []
        self._jobs: dict[str, SubsJob] = {}
        self._kinds: dict[str, Kind] = {}
        self._settled: dict[str, tuple[str, str]] = {}
        # #189: per-film step outcomes, marked at the counter points
        self._tracker = FilmTracker(AVSUBS_STEPS)
        self._pre_done = 0
        self._completed = 0
        self._failed = 0
        self._skipped = 0
        self._total = 0
        self._streak = 0
        self._aborted = False
        self._launch_error: Optional[str] = None
        self._asr_job: Optional[SubsJob] = None
        # IR8: ids of jobs whose DIRECT child survived a kill (rule c). Only
        # these may be reaped by `_poll_asr` while stopping; any other exited
        # child is left to `_stop_asr`, which publishes its transcript.
        self._survivor_jobs: set[str] = set()
        self._translator: Optional[Translator] = None
        self._waiting_gpu = False
        self._advance_requested = False
        self._deferred: list[Callable[[], None]] = []
        self._had_work = False
        self._done_emitted = False
        self._key_alerted = False
        self._survivor_alerted = False
        self._idle_note = ""

    # ── GPU lease hooks ──────────────────────────────────────────────────
    def _gpu_try(self) -> bool:
        """Never blocks. True: this run holds the lease. False: registered (or
        refreshed) as a waiter — or, while stopping, withdrawn at once (D6)."""
        ok = gpu_lease.try_acquire(
            self._run, self._cfg.poll_interval, label=self._cfg.name
        )
        if ok:
            with self._gpu_lock:
                self._gpu_held_run = self._run
        elif self._stopping.is_set():
            gpu_lease.withdraw(self._run)
        return ok

    def _gpu_rel(self) -> None:
        """Release the hold exactly once (whichever path gets here first)."""
        with self._gpu_lock:
            run = self._gpu_held_run
            self._gpu_held_run = None
        if run is not None:
            gpu_lease.release(run)

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, emit: EventEmitter) -> None:
        cfg = self._cfg
        # Idempotent restart: a previous run's ASR child, translator, hold or
        # registered wait is stopped (and its OLD run released + withdrawn)
        # before the new generation is taken (N7).
        if (
            self._asr_job is not None
            or self._translator is not None
            or self._gpu_held_run is not None
            or self._waiting_gpu
        ):
            self.stop()
        self._stopping.clear()
        self._reset((self.instance_id, next_generation()))
        self._started = True
        root = cfg.avsubs_root_folder.strip()
        digest = hashlib.sha1(self.instance_id.encode("utf-8")).hexdigest()[:8]
        staging = Path(root) / _STAGING / f"avsubs-{digest}"
        self._staging_root = staging
        # C2: only this task's own WhisperJAV temp dir — never a sibling's.
        _remove_tree(staging / "tmp")
        try:
            is_dir, exists = Path(root).is_dir(), Path(root).exists()
        except OSError as e:
            self._emit_launch_error(emit, f"cannot read {root} ({e})")
            return
        if not exists:
            self._emit_launch_error(emit, f"avsubs_root_folder not found: {root}")
            return
        if not is_dir:
            self._emit_launch_error(emit, f"avsubs_root_folder is not a folder: {root}")
            return
        try:
            plan = plan_tree(root, cfg.avsubs_recursive, cfg.avsubs_extensions)
        except OSError as e:
            self._emit_launch_error(
                emit, f"cannot scan {root} ({e}); fix the folder and Start again"
            )
            return
        sweep_srt_temps(plan.folders)  # C3: age-gated
        self._pre_done = plan.done
        self._failed = len(plan.collisions)
        self._total = plan.done + len(plan.items) + len(plan.collisions)
        if plan.collisions:
            listed = ", ".join(
                f"{_printable(a.name)} vs {_printable(b.name)}"
                for a, b in plan.collisions[:_MAX_LISTED]
            )
            emit(
                "alert",
                f"{cfg.name}: subtitle name collisions",
                f"{len(plan.collisions)} video(s) would write another video's "
                f"subtitles and were skipped: {listed}",
                dedupe_key=f"{self.instance_id}:collisions",
            )
        if plan.errors:
            listed = "; ".join(plan.errors[:_MAX_LISTED])
            more = len(plan.errors) - _MAX_LISTED
            emit(
                "alert",
                f"{cfg.name}: some files or folders were skipped",
                f"{len(plan.errors)} item(s) could not be planned: {listed}"
                + (f" (+{more} more)" if more > 0 else ""),
                dedupe_key=f"{self.instance_id}:scan-errors",
            )
        exe = cfg.whisperjav_exe_path.strip()
        for item in plan.items:
            # #189 (M1): every film joins the tracker in plan order before
            # anything can settle; an existing .ja.srt → asr `done`.
            self._tracker.add(
                item.relpath, {ASR: "done"} if item.kind == "translate_only" else {}
            )
            self._kinds[item.relpath] = item.kind
            self._jobs[item.relpath] = SubsJob(
                run=self._run,
                job_id=item.relpath,
                media=item.source,
                relpath=item.relpath,
                ja_target=item.ja_target,
                zh_target=item.zh_target,
                staging_root=staging,
                exe=exe,
                engine=cfg.whisperjav_engine,
                extra=cfg.whisperjav_extra_args,
                identity=item.identity,
            )
        try:
            exe_ok = Path(exe).is_file()
        except OSError:
            exe_ok = False
        if not exe_ok:
            # C9: an error, every job skipped(no_exe); no translator, no lease,
            # never `done`.
            self._launch_error = f"whisperjav.exe not found at {exe}"
            emit(
                "alert",
                f"{cfg.name}: whisperjav.exe not found",
                f"whisperjav_exe_path ({exe}) is not a file; {len(self._jobs)} "
                "file(s) skipped. Fix the path and Start again.",
                dedupe_key=f"{self.instance_id}:avsubs-noexe",
            )
            with self._launch_lock:
                for job_id in list(self._jobs):
                    self._settle(job_id, "skipped", "no_exe", emit)
            self._run_deferred()
            return
        if not plan.items:
            # Nothing to do is not an event (M7), and no translator thread.
            self._idle_note = _idle_note(plan, root)
            return
        self._queue = [i for i in plan.items if i.kind == "full"]
        try:
            translator = Translator(self._run, name=self.instance_id)
            # Assigned BEFORE the re-check below (CX2): a concurrent stop()
            # either snapshots it (and cancels it) or set the flag we see.
            self._translator = translator
            translator.start()
            if self._stopping.is_set():
                translator.cancel()
                translator.join(1.0)
        except Exception as e:  # start() never raises
            log.warning("avsubs %s: translator did not start: %s", self.instance_id, e)
            self._translator = None
            self._emit_launch_error(
                emit, f"the translator did not start ({type(e).__name__})"
            )
            return
        self._had_work = True
        self._start_translate_only(emit)
        self._advance_requested = True
        self._dispatch(emit)

    def _start_translate_only(self, emit: EventEmitter) -> None:
        """Every existing `.ja.srt` is loaded OUTSIDE the lock (N8), then
        settled/submitted under it (no GPU, M14)."""
        loaded: list[tuple[SubsJob, Optional[list[Cue]], str]] = []
        for relpath, kind in self._kinds.items():
            if kind != "translate_only":
                continue
            if self._stopping.is_set():
                return
            job = self._jobs[relpath]
            try:
                loaded.append((job, job.load_ja(), ""))
            except (SrtError, OSError) as e:
                loaded.append(
                    (job, None, bounded(f"unreadable .ja.srt: {e}", _DETAIL_CHARS))
                )
        with self._launch_lock:
            for job, cues, err in loaded:
                # m5: a Stop (or an abort settled by the previous job) that
                # landed meanwhile — never submit after it.
                if self._stopping.is_set() or self._aborted:
                    break
                if cues is None:
                    self._fail_unreadable_ja(job, err, emit)
                else:
                    self._submit_translation(job, cues, emit)
        self._run_deferred()

    def _emit_launch_error(self, emit: EventEmitter, msg: str) -> None:
        self._launch_error = msg
        emit(
            "alert",
            f"{self._cfg.name} error",
            msg,
            dedupe_key=f"{self.instance_id}:launch",
        )

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.1, timeout)
        self._stopping.set()
        # Cancel the translator FIRST (bounded) so its llm-worker is already
        # going away while we wait for the lock. It takes none of our locks.
        translator = self._translator
        if translator is not None:
            try:
                translator.cancel()
            except Exception as e:  # never let cleanup abort the tree kill
                log.warning(
                    "avsubs %s: translator cancel failed: %s", self.instance_id, e
                )
        acquired = self._launch_lock.acquire(
            timeout=max(0.1, deadline - time.monotonic())
        )
        try:
            if acquired:
                self._stop_asr(deadline)
            else:
                # D13: the no-orphan guarantee wins over tidiness.
                log.warning(
                    "avsubs %s: stop() could not take the launch lock within "
                    "%.1fs; killing the ASR tree without it",
                    self.instance_id,
                    timeout,
                )
                job = self._asr_job  # read once
                if job is not None and job.child is not None:
                    left = max(0.1, deadline - time.monotonic())
                    if not job.terminate(timeout=left):
                        log.error(
                            "avsubs %s: a WhisperJAV process survived the stop (%s)",
                            self.instance_id,
                            job.job_id,
                        )
                    self._note_survivor(job)
            self._gpu_rel()
            gpu_lease.withdraw(self._run)
            self._waiting_gpu = False
        finally:
            if acquired:
                self._launch_lock.release()
        if translator is not None:
            try:
                translator.join(max(0.1, deadline - time.monotonic()))
            except Exception as e:  # never raise out of stop()
                log.warning(
                    "avsubs %s: translator join failed: %s", self.instance_id, e
                )

    def _stop_asr(self, deadline: float) -> None:
        """Under `_launch_lock` (stop): a live ASR child is tree-killed; one
        that already exited 0 unpolled keeps its `.ja.srt` — including the
        empty one of a no-speech result (CX5) — never the zh."""
        job = self._asr_job
        child = job.child if job is not None else None  # read once
        if job is None or child is None:
            self._asr_job = None
            return
        if child.poll() is None:
            left = max(0.1, min(5.0, deadline - time.monotonic()))
            gone = job.terminate(timeout=left)
            self._note_survivor(job)
        else:
            outcome = job.poll_asr()
            err: Optional[str] = None
            if job.job_id in self._settled or job.job_id in self._survivor_jobs:
                pass  # a killed child (e.g. an aborted run's): never publish
            elif outcome is not None and outcome.kind == "succeeded":
                err = job.publish_ja(outcome.cues)
            elif outcome is not None and outcome.kind == "no_speech":
                err = job.publish_ja([])  # CX5: the empty ja only
            if err is not None:
                log.warning("avsubs %s: %s", self.instance_id, err)
            gone = job.terminate(timeout=0.5)  # joins readers; no-op once reaped
        if not gone:
            log.error(
                "avsubs %s: a WhisperJAV process survived the stop (%s)",
                self.instance_id,
                job.job_id,
            )
        if job.child is None:
            self._asr_job = None
            self._survivor_jobs.discard(job.job_id)

    # ── queue ────────────────────────────────────────────────────────────
    def _dispatch(self, emit: EventEmitter) -> None:
        """The ONLY caller of `_advance()`. A loop, not recursion: a launch
        that settles synchronously just requests again."""
        while self._advance_requested:
            self._advance_requested = False
            self._advance(emit)

    def _advance(self, emit: EventEmitter) -> None:
        if self._stopping.is_set() or self._aborted or self._launch_error is not None:
            return
        if self._asr_live():
            return  # one GPU child at a time
        if not self._queue:
            self._maybe_done(emit)
            return
        if not self._gpu_try():
            self._waiting_gpu = True  # the item stays at the head
            return
        self._waiting_gpu = False
        self._start_asr(self._queue[0], emit)

    def _run_deferred(self) -> None:
        """Run the blocking side effects queued by settlements — called by
        every lock holder right after it released `_launch_lock`."""
        while self._deferred:
            fn = self._deferred.pop(0)
            try:
                fn()
            except Exception as e:  # never let cleanup break check()
                log.warning(
                    "avsubs %s: deferred cleanup failed: %s", self.instance_id, e
                )

    def _start_asr(self, item: TreeItem, emit: EventEmitter) -> None:
        """Launch the head item's ASR with the lease held. Never dispatches;
        the release (when nothing is left running) happens after the lock.
        Everything — the pop and the job lookup included (K3) — is inside the
        exception fence, so any raise releases the lease."""
        job: Optional[SubsJob] = None
        release = False
        try:
            if self._queue and self._queue[0] is item:
                self._queue.pop(0)
            job = self._jobs[item.relpath]
            with self._launch_lock:
                if self._stopping.is_set() or self._aborted:
                    release = True
                else:
                    err = job.start_asr(self._spawn)
                    if (
                        err is not None
                        and err != "unstable"
                        and job.attempt < _ASR_MAX_ATTEMPTS
                    ):
                        err = job.start_asr(self._spawn)  # one retry
                    if self._stopping.is_set():
                        # stop() raced the spawn: never orphan the child.
                        self._kill_for_stop(job)
                        release = True
                    elif err is None:
                        self._asr_job = job
                        self._tracker.start(job.job_id, ASR, job.started_at)  # #189
                    elif err == "unstable":
                        self._settle_unstable(job, emit)
                        release = True
                        self._advance_requested = True
                    else:
                        self._settle(job.job_id, "failed", err, emit)
                        self._alert_job(job.job_id, err, emit)
                        release = True
                        self._advance_requested = True
        except Exception as e:  # D10: a bug must not strand the lease
            log.exception("avsubs %s: launching %s failed", self.instance_id, item)
            with self._launch_lock:
                if self._queue and self._queue[0] is item:
                    self._queue.pop(0)  # never retry the same item forever
                if job is None:
                    # F2: the lookup itself failed — still give a planned job
                    # its terminal state, so `done` can fire.
                    detail = f"internal: {type(e).__name__}"
                    try:
                        if (
                            item.relpath in self._jobs
                            and item.relpath not in self._settled
                        ):
                            self._settle(item.relpath, "failed", detail, emit)
                            self._alert_job(item.relpath, detail, emit)
                    except Exception as e2:  # never raise out of check()
                        log.warning(
                            "avsubs %s: settle failed: %s", self.instance_id, e2
                        )
                    release = True
                    self._advance_requested = True
                    return
                if (
                    job.child is not None
                    and self._asr_job is not job
                    and not self._kill_for_stop(job)
                ):
                    # S2: a spawned child we could not kill is surfaced once,
                    # like the abort path. If the DIRECT child still runs, it
                    # stays the live ASR job (rule c) and keeps the lease: the
                    # `finally` below releases only once `job.child` is None,
                    # and the poll path (or stop()) reaps it and releases then.
                    self._alert_survivor(emit)
                detail = f"internal: {type(e).__name__}"
                self._settle(job.job_id, "failed", detail, emit)
                self._alert_job(job.job_id, detail, emit)
                release = True
                self._advance_requested = True
        finally:
            if release and (job is None or job.child is None):
                self._gpu_rel()
            self._run_deferred()

    def _kill_for_stop(self, job: SubsJob) -> bool:
        """Under the lock: kill a just-spawned child we must not keep. A direct
        child that survives stays the live ASR job (rule c) so stop()/the poll
        path still reach it; the lease is then released by them. Returns the
        kill's result (False: a tracked process survived)."""
        gone = job.terminate(timeout=2.0)
        if not gone:
            log.error(
                "avsubs %s: a WhisperJAV process survived the kill (%s)",
                self.instance_id,
                job.job_id,
            )
        if job.child is not None:
            self._asr_job = job
            self._note_survivor(job)
        return gone

    def _note_survivor(self, job: SubsJob) -> None:
        """Record a job whose direct child is still running after its kill
        (rule c) — the only kind the stopping branch of `_poll_asr` reaps."""
        if job.child is not None:
            self._survivor_jobs.add(job.job_id)

    def _poll_asr(self, emit: EventEmitter) -> None:
        job = self._asr_job
        if job is None:
            return
        release: Optional[bool]
        try:
            with self._launch_lock:
                release = self._poll_asr_locked(job, emit)
        except Exception as e:  # F1: a bug must never strand the lease / raise
            release = self._fence_poll(job, e, emit)
        if release is None:
            return  # nothing happened (or stop() owns the job)
        if release:
            self._gpu_rel()
        self._run_deferred()
        self._dispatch(emit)

    def _poll_asr_locked(self, job: SubsJob, emit: EventEmitter) -> Optional[bool]:
        """The locked body of `_poll_asr` (caller holds `_launch_lock`).
        Returns None when there is nothing to follow up (no release, no
        deferred work, no dispatch), else whether to release the lease."""
        release = False
        if self._asr_job is not job:
            return None
        if self._stopping.is_set():
            # stop() owns the run. IR8: only a child recorded as a kill
            # survivor (rule c) is reaped here once it has exited — never
            # published, settled or followed by a launch. Any other child
            # (e.g. one that finished 0 while stop() cancels the
            # translator) is left to `_stop_asr`, which publishes its ja.
            child = job.child
            if (
                job.job_id in self._survivor_jobs
                and child is not None
                and child.poll() is not None
            ):
                job.poll_asr()
                if job.child is None:
                    self._asr_job = None
                    self._survivor_jobs.discard(job.job_id)
            return None
        if job.job_id in self._settled:
            release = self._reap_settled(job)
        else:
            outcome = job.poll_asr()
            if outcome is None:
                return None
            terminal = True
            name = job.job_id
            if outcome.kind == "succeeded":
                err = job.publish_ja(outcome.cues)
                if err is not None:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
                else:
                    self._tracker.finish(name, ASR, "done", time.monotonic())
                    self._submit_translation(job, list(outcome.cues), emit)
            elif outcome.kind == "no_speech":
                err = job.publish_empty()
                if err is None:
                    self._tracker.finish(name, ASR, "done", time.monotonic())
                    self._settle_completed(job, "no speech", emit)
                else:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
            elif outcome.kind == "unstable":
                self._settle_unstable(job, emit)
            elif job.attempt < _ASR_MAX_ATTEMPTS:
                err = job.start_asr(self._spawn)  # retry, same hold
                if self._stopping.is_set():
                    # D35: exactly _start_asr's post-spawn re-check.
                    self._kill_for_stop(job)
                    if job.child is None:
                        self._asr_job = None
                        release = True
                    terminal = False
                elif err is None:
                    terminal = False  # the job stays current
                elif err == "unstable":  # D33: like a final failure
                    self._settle_unstable(job, emit)
                else:
                    self._settle(name, "failed", err, emit)
                    self._alert_job(name, err, emit)
            else:
                self._settle(name, "failed", outcome.detail, emit)
                self._alert_job(name, outcome.detail, emit)
            if terminal:
                self._asr_job = None
                release = True
                self._advance_requested = True
        return release

    def _fence_poll(self, job: SubsJob, exc: Exception, emit: EventEmitter) -> bool:
        """F1: `_poll_asr` raised. Settle the job `failed(internal)` once (one
        raise-safe alert), request an advance, and release the lease — unless
        its DIRECT child still runs (rule c): then `_asr_job` and the lease
        stay with it and `_reap_settled` releases once it has exited."""
        log.exception(
            "avsubs %s: polling %s failed", self.instance_id, job.job_id, exc_info=exc
        )
        with self._launch_lock:
            child = job.child
            try:
                live = child is not None and child.poll() is None
            except Exception as e:  # treat an unpollable child as gone
                log.warning("avsubs %s: poll failed: %s", self.instance_id, e)
                live = False
            detail = f"internal: {type(exc).__name__}"
            if job.job_id not in self._settled:
                try:
                    self._settle(job.job_id, "failed", detail, emit)
                    self._alert_job(job.job_id, detail, emit)
                except Exception as e:  # never raise out of check()
                    log.warning("avsubs %s: settle failed: %s", self.instance_id, e)
            self._advance_requested = True
            if live:
                return False
            if child is not None:
                try:
                    # IR12: reap like `_reap_settled` (kill_tracked + reader
                    # join) — never terminate(): no taskkill under the lock.
                    job.poll_asr()
                except Exception as e:  # never raise out of check()
                    log.warning("avsubs %s: reap failed: %s", self.instance_id, e)
            if self._asr_job is job:
                self._asr_job = None
            self._survivor_jobs.discard(job.job_id)
            if job.attempt > 0 and job.child is None:
                self._defer_remove(job)  # M11 (a second removal is harmless)
            return True

    def _reap_settled(self, job: SubsJob) -> bool:
        """Under the lock: a settled job whose direct child survived its kill
        (rule c) is reaped once it has exited. True when it is gone."""
        child = job.child
        if child is not None:
            if child.poll() is None:
                return False
            job.poll_asr()  # reaps: kills lingering tracked pids, joins readers
            if job.child is not None:
                return False
        self._asr_job = None
        self._survivor_jobs.discard(job.job_id)
        self._advance_requested = True
        self._defer_remove(job)
        return True

    # ── settlement ───────────────────────────────────────────────────────
    def _job_dir(self, job: SubsJob) -> Path:
        return attempt_dir(job.staging_root, job.relpath, 1).parent

    def _defer_remove(self, job: SubsJob) -> None:
        """Queue the removal of the job's staging dir (M11) — an rmtree is
        blocking work, so it runs after the lock is released."""
        self._deferred.append(functools.partial(_remove_tree, self._job_dir(job)))

    def _alert_job(self, job_id: str, detail: str, emit: EventEmitter) -> None:
        emit(
            "alert",
            f"{self._cfg.name}: subtitles for {_printable(job_id)} failed",
            bounded(detail, _DETAIL_CHARS),  # bounded tail, never the argv
            dedupe_key=f"{self.instance_id}:avsubs:{job_id}",
        )

    def _settle_unstable(self, job: SubsJob, emit: EventEmitter) -> None:
        self._settle(job.job_id, "skipped", "unstable", emit)
        emit(
            "alert",
            f"{self._cfg.name}: {_printable(job.job_id)} changed during transcription",
            f"{_printable(job.media.name)} changed or disappeared while it was "
            "being transcribed; its subtitles are skipped for this run.",
            dedupe_key=f"{self.instance_id}:avsubs-unstable:{job.job_id}",
        )

    def _alert_no_key(self, emit: EventEmitter) -> None:
        if self._key_alerted:
            return
        self._key_alerted = True
        emit(
            "alert",
            f"{self._cfg.name}: no LLM API key",
            "AV 翻译 needs the agent's LLM API key to translate; files are skipped "
            "(their .ja.srt is kept) until a key is set.",
            dedupe_key=f"{self.instance_id}:avsubs-nokey",
        )

    def _fail_unreadable_ja(
        self, job: SubsJob, detail: str, emit: EventEmitter
    ) -> None:
        log.warning("avsubs %s: %s: %s", self.instance_id, job.job_id, detail)
        # D9: a pre-existing transcript we cannot read is not a run failure.
        self._settle(job.job_id, "failed", detail, emit, streak=False)
        self._alert_job(job.job_id, detail, emit)

    def _settle(
        self,
        job_id: str,
        terminal: str,
        reason: str,
        emit: EventEmitter,
        *,
        streak: bool = True,
    ) -> None:
        """The ONE place a job becomes terminal (callers hold `_launch_lock`).
        No-op when it already is. Three consecutive failures → `_abort`."""
        if job_id in self._settled or job_id not in self._jobs:
            return
        self._settled[job_id] = (terminal, reason)
        if terminal == "completed":
            self._completed += 1
            self._streak = 0
        elif terminal == "failed":
            self._failed += 1
            if streak:
                self._streak += 1
        else:
            self._skipped += 1
            if reason == "no_llm_key":
                self._streak = 0
        # #189: before `_abort` can emit (M1)
        self._tracker.settle_subs(job_id, terminal, time.monotonic())
        job = self._jobs[job_id]
        # M11: the job's attempt dirs go once it settled — except while its
        # ASR child is still live (the kill cleanup removes them after the
        # kill, N6).
        if job.attempt > 0 and job.child is None:
            self._defer_remove(job)
        if self._streak >= _ABORT_AFTER and not self._aborted:
            self._abort(emit)

    def _settle_completed(self, job: SubsJob, detail: str, emit: EventEmitter) -> None:
        """Under `_launch_lock`, right after `job`'s zh was published: settle it
        `completed` and delete its `.ja.srt` (#187) — a pre-existing library one
        too (owner's rule). The transcript is only the checkpoint a later Start
        resumes an unfinished translation from, so every other terminal path
        keeps it. A single unlink, like the publish's `os.replace`; a failure
        is logged and never fails the job or raises."""
        self._settle(job.job_id, "completed", detail, emit)
        if self._settled.get(job.job_id, ("", ""))[0] != "completed":
            return
        err = job.discard_ja()
        if err is not None:
            log.warning(
                "avsubs %s: %s: %s", self.instance_id, _printable(job.job_id), err
            )

    def _abort(self, emit: EventEmitter) -> None:
        """Under the lock (C7): flags, settlement and one alert; the kill →
        release + withdraw → translator cancel part is deferred until the lock
        is released (m3: the lease is ALWAYS released after the kill)."""
        self._aborted = True
        self._waiting_gpu = False
        job = self._asr_job
        for job_id in list(self._jobs):
            self._settle(job_id, "skipped", "cancelled", emit)
        self._queue = []
        emit(
            "alert",
            f"{self._cfg.name}: AV 翻译 aborted",
            f"AV 翻译 aborted after {_ABORT_AFTER} consecutive failures | "
            f"Queue: {self._pre_done + self._completed}/{self._total} done, "
            f"{self._failed} failed, {self._skipped} skipped",
            dedupe_key=f"{self.instance_id}:avsubs-aborted",
        )
        translator = self._translator
        run = self._run

        def cleanup() -> None:
            gone = True
            if job is not None and job.child is not None:
                gone = job.terminate()
                self._note_survivor(job)
            if not gone:
                log.error(
                    "avsubs %s: a WhisperJAV process survived the abort (%s)",
                    self.instance_id,
                    job.job_id if job is not None else "?",
                )
                self._alert_survivor(emit)
            self._gpu_rel()
            gpu_lease.withdraw(run)
            if job is not None and job.child is None:
                if self._asr_job is job:
                    self._asr_job = None
                _remove_tree(self._job_dir(job))
            if translator is not None:
                translator.cancel()
                translator.join(2.0)

        self._deferred.append(cleanup)

    def _alert_survivor(self, emit: EventEmitter) -> None:
        if self._survivor_alerted:
            return
        self._survivor_alerted = True
        emit(
            "alert",
            f"{self._cfg.name}: WhisperJAV may still be running",
            "a WhisperJAV process may still be running; check Task Manager",
            dedupe_key=f"{self.instance_id}:avsubs-survivor",
        )

    def _submit_translation(
        self, job: SubsJob, cues: Optional[list[Cue]], emit: EventEmitter
    ) -> None:
        """Under `_launch_lock`. An empty transcript needs no request and no key
        (CX3); otherwise the key is checked per job (live-apply)."""
        if cues is None:
            try:
                cues = job.load_ja()
            except (SrtError, OSError) as e:
                self._fail_unreadable_ja(
                    job, bounded(f"unreadable .ja.srt: {e}", _DETAIL_CHARS), emit
                )
                return
        if not cues:
            err = job.publish_zh([])
            if err is None:
                self._settle_completed(job, "no speech", emit)
            else:
                self._settle(job.job_id, "failed", err, emit)
                self._alert_job(job.job_id, err, emit)
            return
        settings = get_llm_settings()
        if not settings.api_key and needs_llm_key(settings.api_base):
            self._settle(job.job_id, "skipped", "no_llm_key", emit)
            self._alert_no_key(emit)
            return
        translator = self._translator
        if translator is None:
            self._settle(job.job_id, "failed", "translator not running", emit)
            self._alert_job(job.job_id, "translator not running", emit)
            return
        translator.submit(TranslateRequest(self._run, job.job_id, tuple(cues)))
        # #189 (N2): submitted = queued until the translator reports it live
        self._tracker.start(job.job_id, TRANSLATE, time.monotonic())

    def _settle_results(self, emit: EventEmitter) -> None:
        """Drain the translator's results; each is settled under the lock with
        `_settled` checked BEFORE any publish; deferred work and dispatch run
        after each release."""
        translator = self._translator
        if translator is None:
            return
        while True:
            try:
                result = translator.results.get_nowait()
            except queue.Empty:
                return
            if result is CANCELLED or not isinstance(result, TranslateResult):
                continue
            if result.run != self._run:
                continue  # an earlier generation's result
            with self._launch_lock:
                if self._stopping.is_set():
                    return
                job = self._jobs.get(result.job_id)
                if job is not None and result.job_id not in self._settled:
                    if result.outcome == "translated":
                        err = job.publish_zh(result.zh_cues)
                        if err is None:
                            self._settle_completed(job, "", emit)
                        else:
                            self._settle(job.job_id, "failed", err, emit)
                            self._alert_job(job.job_id, err, emit)
                    elif result.detail == "no LLM key":  # C8
                        self._settle(job.job_id, "skipped", "no_llm_key", emit)
                        self._alert_no_key(emit)
                    else:
                        self._settle(job.job_id, "failed", result.detail, emit)
                        self._alert_job(job.job_id, result.detail, emit)
            self._run_deferred()
            self._dispatch(emit)

    def _maybe_done(self, emit: EventEmitter) -> None:
        """`done` exactly once, when every planned job is terminal, no child is
        live and the translator is idle. Stop/abort/errors never emit."""
        if not self._had_work or self._done_emitted:
            return
        if self._stopping.is_set() or self._aborted or self._launch_error is not None:
            return
        if self._queue or self._asr_job is not None:
            return
        if len(self._settled) < len(self._jobs):
            return
        tr = self._translator
        if tr is not None and (tr.queued() or tr.in_flight() or not tr.results.empty()):
            return
        self._done_emitted = True
        emit(
            "done",
            f"{self._cfg.name} complete",
            f"AV 翻译 complete | Queue: {self._pre_done + self._completed}/"
            f"{self._total} done, {self._failed} failed, {self._skipped} skipped "
            f"| {datetime.now():%Y-%m-%d %H:%M:%S}",
        )
        gpu_lease.withdraw(self._run)
        # F2: end the idle translator (and its llm-worker) now, not at Stop.
        if tr is not None:
            self._translator = None
            try:
                tr.cancel()
                tr.join(2.0)
            except Exception as e:  # cleanup must never break check()
                log.warning(
                    "avsubs %s: translator shutdown failed: %s", self.instance_id, e
                )

    # ── check ────────────────────────────────────────────────────────────
    def check(self, emit: EventEmitter) -> MonitorStatus:
        if not self._started:
            return MonitorStatus(state="error", detail="not started")
        self._settle_results(emit)  # (1)
        self._run_deferred()  # (2) D9: anything a holder left behind
        if self._launch_error is not None:  # (3)
            return self._build_status("error", self._launch_error)
        if self._aborted:  # (4)
            self._poll_asr(emit)  # reaps a survivor (rule c); never launches
            return self._build_status("degraded", self._aborted_detail())
        self._poll_asr(emit)  # (5)
        if (
            self._waiting_gpu and not self._asr_live() and not self._stopping.is_set()
        ):  # (6) retry the refused acquire
            self._waiting_gpu = False
            self._advance_requested = True
            self._dispatch(emit)
            if not self._waiting_gpu and not self._asr_live():
                gpu_lease.withdraw(self._run)  # D12: the GPU is not needed now
        # CX1: the poll or the retry above may have settled a third
        # consecutive failure (e.g. a file's final ASR attempt) — report it in
        # THIS check, exactly as the early guards do.
        if self._launch_error is not None:
            return self._build_status("error", self._launch_error)
        if self._aborted:
            return self._build_status("degraded", self._aborted_detail())
        self._maybe_done(emit)  # (7)
        busy = self._asr_live() or self._translating() > 0
        return self._build_status("running" if busy else "idle")  # (8)

    def _asr_live(self) -> bool:
        job = self._asr_job
        return job is not None and job.child is not None

    def _translating(self) -> int:
        tr = self._translator
        if tr is None:
            return 0
        return tr.queued() + (1 if tr.in_flight() else 0)

    def _aborted_detail(self) -> str:
        return (
            f"AV 翻译 aborted after {_ABORT_AFTER} consecutive failures · "
            f"{self._pre_done + self._completed}/{self._total} done, "
            f"{self._failed} failed, {self._skipped} skipped"
        )

    def _phase(self) -> Optional[str]:
        if self._asr_live():
            return "asr"
        if self._translating():
            return "translate"
        if self._waiting_gpu:
            return "waiting_gpu"
        return None

    def _gpu_blocker(self) -> str:
        """Who blocks this run's GPU wait: the lease's blocking label — but ""
        while the lease is free and reserved for THIS run (IR9: never name
        this run itself; the grant comes on our next try)."""
        if gpu_lease.reserved_for() == self._run:
            return ""
        return gpu_lease.blocking_label()

    def _waiting_text(self) -> str:
        label = self._gpu_blocker()
        return f"waiting for GPU (held by {label})" if label else "waiting for GPU"

    def _progress_view(self) -> dict:
        """#189: the per-film stepper (`film`, `steps`, `films`, `films_more`
        — all or none) and, while a translation runs, its `model`. Derived
        from what is live NOW (D3): the ASR child with its WhisperJAV
        progress, the translator's in-flight request, and the GPU wait of the
        queue head `_advance` launches next. Read-only."""
        now = time.monotonic()
        active: dict[str, str] = {}
        numbers: dict[str, dict] = {}
        job = self._asr_job
        if job is not None and job.child is not None:
            active[ASR] = job.job_id
            numbers[ASR] = step_numbers(job.progress(now))
        translator = self._translator
        request = translator.progress(now) if translator is not None else None
        job_id = request.get("job_id") if request is not None else None
        if isinstance(job_id, str):
            active[TRANSLATE] = job_id
            numbers[TRANSLATE] = step_numbers(request)
        waiting: Optional[tuple[str, str]] = None
        if self._waiting_gpu and not self._asr_live() and self._queue:
            waiting = (self._queue[0].relpath, ASR)
        holder = bounded(self._gpu_blocker(), NAME_CHARS) if waiting else ""
        view = self._tracker.view(LiveFacts(active, waiting, holder, numbers), now)
        model = numbers.get(TRANSLATE, {}).get("model")
        if view and model:
            view["model"] = model
        return view

    def _build_status(
        self, state: State, detail: Optional[str] = None
    ) -> MonitorStatus:
        cfg = self._cfg
        metrics: dict = {}
        translating = self._translating()
        done = self._pre_done + self._completed
        if self._total:
            metrics["queue_completed"] = done
            metrics["queue_total"] = self._total
            metrics["queue_failed"] = self._failed
            metrics["queue_skipped"] = self._skipped
            metrics["queue_remaining"] = max(
                0, self._total - done - self._failed - self._skipped
            )
            # #189 (D8): the videos that had their .srt at scan (already in
            # queue_completed) — the 已有字幕 chip
            metrics["queue_pre_done"] = self._pre_done
        job = self._asr_job
        if job is not None and job.child is not None:
            metrics["current_file"] = job.relpath
        phase = self._phase()
        if phase is not None:
            metrics["phase"] = phase
        metrics["subs_translating"] = translating
        metrics.update(self._progress_view())
        metrics.update(_cpu_mem())
        if cfg.avsubs_gpu_monitor:
            gpu = read_gpu()
            if gpu:
                metrics["gpu_pct"] = gpu["util_pct"]
                metrics["gpu_mem_used_mb"] = gpu["mem_used_mb"]
                metrics["gpu_mem_total_mb"] = gpu["mem_total_mb"]
        if detail is None:
            detail = self._detail(state, phase, translating, done)
        return MonitorStatus(state=state, detail=detail, metrics=metrics)

    def _detail(
        self, state: str, phase: Optional[str], translating: int, done: int
    ) -> str:
        count = f"{done}/{self._total} done"
        job = self._asr_job
        if phase == "asr" and job is not None:
            secs = max(0, int(time.monotonic() - job.started_at))
            elapsed = f"{secs // 60:02d}:{secs % 60:02d} elapsed"
            head = f"transcribing: {_printable(job.relpath)} [{job.engine}]"
            return " · ".join((head, elapsed, f"translating {translating}", count))
        if phase == "translate":
            text = f"translating {translating} · {count}"
            if self._waiting_gpu:
                text += f" · {self._waiting_text()}"
            return text
        if phase == "waiting_gpu":
            return f"{self._waiting_text()} · {count}"
        if self._idle_note:
            return self._idle_note
        return f"{state} · {count}" if self._total else state


class AvsubsPlugin(MonitorPlugin):
    type_id = "avsubs"
    display_name = "AV 翻译 (subtitles)"
    category = "task"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return AvsubsConfig

    @classmethod
    def ui_schema(cls) -> dict:
        # Path fields are flagged for the file/folder picker widget (#71); help
        # text comes from the field descriptions.
        return {
            "ui:order": [
                "name",
                "avsubs_root_folder",
                "avsubs_recursive",
                "avsubs_extensions",
                "whisperjav_exe_path",
                "whisperjav_engine",
                "whisperjav_extra_args",
                "avsubs_gpu_monitor",
                "poll_interval",
                "timeout",
                "*",
            ],
            "avsubs_root_folder": {"ui:options": {"taskpawPath": "directory"}},
            "whisperjav_exe_path": {"ui:options": {"taskpawPath": "file"}},
        }

    def manual_start(self, config: BaseMonitorConfig) -> bool:
        # Managed only: Start launches WhisperJAV on the GPU, so the task is
        # added STOPPED and never auto-starts at boot (owner rule).
        return True

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return AvsubsInstance(instance_id, config)  # type: ignore[arg-type]
