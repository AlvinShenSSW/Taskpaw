"""`SubsJob`: one planned subtitle job — ASR attempts, outcome, publishing (#177).

Paths in, outcomes out. No retry policy and no counters live here: retry,
degrade and abort are the plugin's policy. Every publish goes through
`<target>.<generation>.tmp` + `os.replace`, so `start()` can sweep a crashed
run's leftovers by generation.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.child import ChildProcess
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import RunId
from taskpaw_v3.monitors.subs.whisperjav import attempt_dir, build_argv, read_outcome

log = logging.getLogger("taskpaw.subs.job")

Terminal = Literal["completed", "failed", "skipped"]
SkipReason = Literal["restore_failed", "no_llm_key", "unstable", "cancelled", "no_exe"]


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
    def _publish(self, target: Path, text: str) -> Optional[str]:
        tmp = target.with_name(f"{target.name}.{self.run[1]}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, target)
        except OSError as e:
            try:
                tmp.unlink(missing_ok=True)
            except OSError as cleanup:
                log.warning(
                    "subs job %s: could not remove %s (%s)",
                    self.job_id,
                    tmp.name,
                    type(cleanup).__name__,
                )
            return f"publish {target.name}: {type(e).__name__}: {e}"
        return None

    def publish_ja(self, cues: Iterable[Cue]) -> Optional[str]:
        return self._publish(self.ja_target, srt.serialize(cues))

    def publish_zh(self, cues: Iterable[Cue]) -> Optional[str]:
        return self._publish(self.zh_target, srt.serialize(cues))

    def publish_empty(self) -> Optional[str]:
        """0-byte `.ja.srt` and `.srt` (no speech)."""
        return self._publish(self.ja_target, "") or self._publish(self.zh_target, "")

    def load_ja(self) -> list[Cue]:
        return srt.load(self.ja_target)
