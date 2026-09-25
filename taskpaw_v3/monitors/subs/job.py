"""`SubsJob`: one planned subtitle job — ASR attempts, outcome, publishing (#177).

Paths in, outcomes out. No retry policy and no counters live here: retry,
degrade and abort are the plugin's policy. Every publish goes through
`<target>.<generation>.tmp`, so `start()` can sweep a crashed run's leftovers
by generation, and the tmp is moved into place WITHOUT replacing anything
(#191): a target that already exists is refused (`PublishResult` `exists`),
never overwritten. `discard_ja()` drops the `.ja.srt` checkpoint once the
plugin settled the job `completed` (#187); `discard_own_ja()` only when this
job published it in this run (#191). `progress(now)` is the live ASR progress
of the current attempt, parsed from the child's captured tail (#189,
read-only observation).
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.child import ChildProcess
from taskpaw_v3.monitors.subs.progress import AsrProgress
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import RunId
from taskpaw_v3.monitors.subs.whisperjav import attempt_dir, build_argv, read_outcome

log = logging.getLogger("taskpaw.subs.job")

Terminal = Literal["completed", "failed", "skipped"]
SkipReason = Literal[
    "restore_failed",
    "no_llm_key",
    "unstable",
    "cancelled",
    "no_exe",
    "subtitle exists",
    "transcript exists",
    "subtitle state unreadable",
    "translation_paused",  # #192 AC8: no translation service for 2 h
]
# #191: the skips of a film whose subtitles exist (已有字幕), whose transcript
# appeared meanwhile, or whose folder could not be read — so nobody can tell
# (fail closed; the next Start tries again).
SUBTITLE_EXISTS: SkipReason = "subtitle exists"
TRANSCRIPT_EXISTS: SkipReason = "transcript exists"
SUBTITLE_UNREADABLE: SkipReason = "subtitle state unreadable"

# #189 (C2): what one progress poll reads of the ASR child's captured output.
ASR_TAIL_LINES = 40
ASR_TAIL_CHARS = 16000

# #191 (AC4): Windows `os.rename` refuses an existing target (FileExistsError —
# verified on NTFS for existing, case-variant, read-only, open and directory
# targets, A1); elsewhere `rename` replaces, so a hard link is the refusing
# move there (`link(2)`: EEXIST, A3). Both are as atomic as `os.replace`.
_RENAME_REFUSES = os.name == "nt"
# `os.link` failures that mean "this filesystem has no hard links".
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS})


def _present(path: Path) -> bool:
    """Whether a directory entry named `path` exists. Raises `OSError` when
    that cannot be told — the caller fails closed."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _move_into_place(tmp: Path, target: Path) -> None:
    """Move the finished `tmp` to `target` without replacing anything; raises
    `FileExistsError` when `target` exists. A filesystem without hard links
    falls back to a checked `os.replace` (a race window remains there)."""
    if _RENAME_REFUSES:
        os.rename(tmp, target)
        return
    try:
        os.link(tmp, target)
    except FileExistsError:
        raise
    except OSError as e:
        if e.errno not in _NO_HARD_LINKS:
            raise
        log.debug(
            "subs: no hard links for %s (%s); checked replace instead",
            target.name,
            errno.errorcode.get(e.errno or 0, e.errno),
        )
        if _present(target):
            raise FileExistsError(errno.EEXIST, "already exists", str(target)) from e
        os.replace(tmp, target)
        return
    try:
        tmp.unlink()
    except OSError as e:  # published; the age-gated sweep takes the tmp later
        log.warning(
            "subs: published %s but could not remove %s (%s)",
            target.name,
            tmp.name,
            type(e).__name__,
        )


@dataclass(frozen=True)
class PublishResult:
    """What one publish did (#191 AC4) — a distinct type (F8), so a caller
    still reading it as an error text fails the type check. `ok`: written;
    `exists`: refused — a file of that name was already there (nothing
    written, nothing replaced); `error`: the write or the move failed.
    `detail` is empty when `ok`, else one log/alert line."""

    kind: Literal["ok", "exists", "error"]
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def source_identity(path: Path) -> tuple[int, int]:
    """(size, mtime_ns); raises OSError (e.g. the file was removed)."""
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


@dataclass(frozen=True)
class JobOutcome:
    kind: Literal["succeeded", "no_speech", "failed", "unstable"]
    cues: tuple[Cue, ...]
    detail: str


@dataclass
class SubsJob:
    run: RunId
    job_id: str
    media: Path
    relpath: str
    ja_target: Path
    zh_target: Path
    staging_root: Path
    exe: str
    engine: str
    extra: str
    identity: Optional[tuple[int, int]] = None
    attempt: int = 0
    child: Optional[ChildProcess] = None
    started_at: float = 0.0
    # #189: the current attempt's progress parser (a new one per attempt).
    _asr_progress: Optional[AsrProgress] = field(
        default=None, init=False, repr=False, compare=False
    )
    # #191 (AC6): THIS job published its `.ja.srt` in this run — only then may
    # a refused `.srt` take the transcript with it (`discard_own_ja`).
    ja_published: bool = field(default=False, init=False, compare=False)

    # ── ASR ──────────────────────────────────────────────────────────────
    def start_asr(
        self, spawn: Callable[..., ChildProcess] = ChildProcess
    ) -> Optional[str]:
        """Launch one ASR attempt into a fresh attempt dir. Returns None on
        success, "unstable" when the media changed or vanished, else the launch
        error text ("launch: <TypeName>: <msg>"). Never raises."""
        try:
            ident = source_identity(self.media)
        except OSError as e:
            log.info(
                "subs job %s: media unavailable (%s)", self.job_id, type(e).__name__
            )
            return "unstable"
        if self.identity is None:
            self.identity = ident
        elif ident != self.identity:
            return "unstable"
        self.attempt += 1
        out_dir = attempt_dir(self.staging_root, self.relpath, self.attempt)
        try:
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True)
            tmp_dir = self.staging_root / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            argv = build_argv(
                self.exe, self.media, out_dir, tmp_dir, self.engine, self.extra
            )
            self.child = spawn(argv)
        except Exception as e:  # recorded by the caller as a failed attempt
            self.child = None
            return f"launch: {type(e).__name__}: {e}"
        self.started_at = time.monotonic()
        self._asr_progress = AsrProgress(self.started_at)
        return None

    def poll_asr(self) -> Optional[JobOutcome]:
        """None while the child runs (or when there is none); once it exited:
        reap it and map the attempt's outcome (media changed → unstable)."""
        child = self.child
        if child is None:
            return None
        rc = child.poll()
        if rc is None:
            return None
        self._kill_lingering(child)
        child.join_readers(1.0)
        self.child = None
        try:
            now: Optional[tuple[int, int]] = source_identity(self.media)
        except OSError:
            now = None
        if now is None or now != self.identity:
            return JobOutcome("unstable", (), "source changed during ASR")
        out_dir = attempt_dir(self.staging_root, self.relpath, self.attempt)
        o = read_outcome(out_dir, rc, child.tail())
        return JobOutcome(o.kind, o.cues, o.detail)

    def progress(self, now: float) -> Optional[dict[str, Any]]:
        """#189: the live ASR progress of the current attempt (a fresh
        `AsrProgress.snapshot`), fed the child's captured tail on every call.
        None without a live child, or when the child has no `tail` (or it
        failed). Read-only: never touches the child's lifecycle; never
        raises. Call it from the thread that polls the job. `now` must be a
        `time.monotonic()` reading (the clock of `started_at`)."""
        child, parser = self.child, self._asr_progress
        if child is None or parser is None:
            return None
        tail = getattr(child, "tail", None)
        if not callable(tail):
            return None
        try:
            text = tail(lines=ASR_TAIL_LINES, max_chars=ASR_TAIL_CHARS)
        except Exception as e:  # observation only: never break a status poll
            log.debug(
                "subs job %s: reading the ASR tail failed (%s)",
                self.job_id,
                type(e).__name__,
            )
            return None
        parser.feed_text(text, now)
        return parser.snapshot(now)

    def _kill_lingering(self, child: ChildProcess) -> None:
        """m1: the direct child has exited — kill any tracked descendant that
        still runs (create time checked, psutil only) and name it. Test doubles
        without `kill_tracked` are fine."""
        kill_tracked = getattr(child, "kill_tracked", None)
        if kill_tracked is None:
            return
        survivors = kill_tracked()
        if survivors:
            log.warning(
                "subs job %s: ASR child %s exited but tracked processes were "
                "still running; killed: %s",
                self.job_id,
                child.pid,
                survivors,
            )

    def terminate(self, timeout: float = 5.0) -> bool:
        """Tree-kill and join the ASR child (harmless on an exited one).

        Returns `terminate_tree(...) is not False` (N2: a double returning None
        counts as gone); True without a child. `child` is reset — UNLESS the
        direct child is still running after the kill, in which case it stays
        set so the plugins' live-child guards keep blocking new launches and
        the normal poll path reaps it later."""
        child = self.child
        if child is None:
            return True
        gone = child.terminate_tree(timeout) is not False
        if child.poll() is None:
            log.error(
                "subs job %s: ASR child %s still running after the kill",
                self.job_id,
                child.pid,
            )
            return gone
        child.join_readers(2.0)
        self.child = None
        return gone

    # ── publishing ───────────────────────────────────────────────────────
    def _publish(self, target: Path, text: str) -> PublishResult:
        """Write `<target>.<gen>.tmp`, then move it into place WITHOUT
        replacing (#191 AC4): a target that is already there — checked first
        (C1: defence in depth), refused by the move itself — is `exists`. The
        tmp is removed on every path that does not publish it."""
        tmp = target.with_name(f"{target.name}.{self.run[1]}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            if _present(target):
                raise FileExistsError(errno.EEXIST, "already exists", str(target))
            _move_into_place(tmp, target)
        except FileExistsError:
            self._drop_tmp(tmp)
            return PublishResult(
                "exists", f"{target.name} already exists; not replaced"
            )
        except OSError as e:
            self._drop_tmp(tmp)
            return PublishResult(
                "error", f"publish {target.name}: {type(e).__name__}: {e}"
            )
        return PublishResult("ok")

    def _drop_tmp(self, tmp: Path) -> None:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup:
            log.warning(
                "subs job %s: could not remove %s (%s)",
                self.job_id,
                tmp.name,
                type(cleanup).__name__,
            )

    def publish_ja(self, cues: Iterable[Cue]) -> PublishResult:
        res = self._publish(self.ja_target, srt.serialize(cues))
        if res.ok:
            self.ja_published = True
        return res

    def publish_zh(self, cues: Iterable[Cue]) -> PublishResult:
        return self._publish(self.zh_target, srt.serialize(cues))

    def publish_empty(self) -> PublishResult:
        """0-byte `.ja.srt` and `.srt` (no speech). A refused `.srt` takes the
        empty `.ja.srt` this call just wrote with it (#191 AC4); a refused
        `.ja.srt` writes nothing."""
        res = self.publish_ja([])
        if not res.ok:
            return res
        res = self.publish_zh([])
        if res.kind == "exists":
            err = self.discard_ja()
            if err is not None:
                log.warning("subs job %s: %s", self.job_id, err)
        return res

    def discard_ja(self) -> Optional[str]:
        """Delete the `.ja.srt` once the Chinese `.srt` is published (#187).

        The transcript is only the resume checkpoint for a translation that did
        not finish; after a successful publish it has no further use. A missing
        file is fine. Returns an error text when the delete failed (locked,
        permission) — the caller logs it; never raises."""
        try:
            self.ja_target.unlink(missing_ok=True)
        except (OSError, ValueError) as e:
            return f"could not remove {self.ja_target.name}: {type(e).__name__}: {e}"
        return None

    def discard_own_ja(self) -> Optional[str]:
        """#191 (AC6): `discard_ja()` only when THIS job published the
        `.ja.srt` in this run — a transcript that was already in the library
        (translate-only) or appeared meanwhile stays. Never raises."""
        return self.discard_ja() if self.ja_published else None

    def load_ja(self) -> list[Cue]:
        return srt.load(self.ja_target)
