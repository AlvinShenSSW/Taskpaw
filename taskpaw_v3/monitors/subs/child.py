"""`ChildProcess`: one external child with its own reader threads (#177).

Two shapes share this helper:

- the ASR child (WhisperJAV): `stdin=DEVNULL` (D15), stdout+stderr merged
  into a bounded tail, split on `\\r`/`\\n` (tqdm rewrites lines with `\\r`);
- the LLM worker: `stdin_pipe=True`, each stdout line delivered to
  `line_sink` followed by an `Eof(pid)` sentinel at EOF, stderr into the tail.

Nothing here blocks except the bounded `terminate_tree`. On Windows the tree
kill is `taskkill /PID <pid> /T /F` issued FIRST, while the tree is intact
(C7: once the launcher has exited `/T` can no longer find the grandchildren).

#179 (C11): every `poll()` while the direct child lives merges its descendants
(psutil `children(recursive=True)`) into `_tracked {pid: create_time}`, so a
grandchild orphaned by an exited launcher (Windows does not re-parent) is
still killed by `terminate_tree` / `kill_tracked` — only while its create time
still matches (pid-reuse safe). `asr_env()` is the ASR child's scrubbed
environment (C6). Nothing is imported from `lada.py` (C1); the reader mirrors
its style.
"""

from __future__ import annotations

import collections
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Any, Callable, Mapping, Optional

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency
    psutil = None

log = logging.getLogger("taskpaw.subs.child")

_CHUNK = 4096
_TASKKILL_NOT_FOUND = 128  # the process is already gone — not an error
_TASKKILL_MIN_S = 0.5  # taskkill always gets at least this long to run
_FINAL_KILL_WAIT_S = 1.0  # the one wait after the last-resort proc.kill() (N4)

LLM_ENV_PREFIX = "TASKPAW_LLM_"


def asr_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """A copy of `base` (default: the agent's environment) minus every
    `TASKPAW_LLM_*` variable (case-insensitive): the LLM key must never reach
    the ASR child (#177 AC10; moved here from Jasna by #179 C6)."""
    src: Mapping[str, str] = os.environ if base is None else base
    return {k: v for k, v in src.items() if not k.upper().startswith(LLM_ENV_PREFIX)}


@dataclass(frozen=True)
class Eof:
    """Delivered on `line_sink` once the child's stdout reaches EOF. Carries
    the pid of the child it belongs to (D4)."""

    pid: int


def _no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class ChildProcess:
    def __init__(
        self,
        argv: list[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        stdin_pipe: bool = False,
        line_sink: Optional["queue.Queue[object]"] = None,
        tail_lines: int = 40,
    ) -> None:
        self._tail: collections.deque[str] = collections.deque(
            maxlen=max(1, tail_lines)
        )
        self._tail_lock = threading.Lock()
        self._line_sink = line_sink
        # Popen errors propagate: the caller records them.
        self.proc: subprocess.Popen = subprocess.Popen(
            list(argv),
            shell=False,
            stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if line_sink is None else subprocess.PIPE,
            bufsize=0,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            creationflags=_no_window_flags(),
        )
        self.pid: int = self.proc.pid
        # Descendants seen while the child lived (C11): pid → create_time. A
        # tiny lock guards it; readers iterate over a copy (m4).
        self._tracked: dict[int, float] = {}
        self._tracked_lock = threading.Lock()
        self._readers: list[threading.Thread] = []
        if line_sink is None:
            self._start_reader("out", self._tail_reader, self.proc.stdout)
        else:
            self._start_reader("out", self._line_reader, self.proc.stdout)
            self._start_reader("err", self._tail_reader, self.proc.stderr)

    # ── readers ──────────────────────────────────────────────────────────
    def _start_reader(
        self,
        tag: str,
        target: Callable[[IO[bytes]], None],
        stream: Optional[IO[bytes]],
    ) -> None:
        if stream is None:
            return
        t = threading.Thread(
            target=target,
            args=(stream,),
            name=f"subs-child-{self.pid}-{tag}",
            daemon=True,
        )
        self._readers.append(t)
        t.start()

    def _add_tail(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if line:
            with self._tail_lock:
                self._tail.append(line)

    def _tail_reader(self, stream: IO[bytes]) -> None:
        buf = b""
        try:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                buf += chunk
                parts = buf.replace(b"\r", b"\n").split(b"\n")
                buf = parts.pop()
                for part in parts:
                    self._add_tail(part)
        except (OSError, ValueError) as e:
            # The pipe closes under us as the child is killed — expected.
            log.debug("child %s: tail reader ended (%s)", self.pid, type(e).__name__)
        finally:
            # A crash line printed right before exit may lack a trailing newline.
            self._add_tail(buf)
            self._close_quietly(stream)

    def _line_reader(self, stream: IO[bytes]) -> None:
        sink = self._line_sink
        assert sink is not None
        buf = b""
        try:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()
                for part in parts:
                    self._put_line(sink, part)
        except (OSError, ValueError) as e:
            log.debug("child %s: line reader ended (%s)", self.pid, type(e).__name__)
        finally:
            self._put_line(sink, buf)
            self._close_quietly(stream)
            sink.put(Eof(self.pid))

    @staticmethod
    def _put_line(sink: "queue.Queue[object]", raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line:
            sink.put(line)

    def _close_quietly(self, stream: IO[bytes]) -> None:
        try:
            stream.close()
        except OSError as e:
            log.debug(
                "child %s: closing a pipe failed (%s)", self.pid, type(e).__name__
            )

    # ── API ──────────────────────────────────────────────────────────────
    def poll(self) -> Optional[int]:
        """The exit code, or None while the child runs — in which case its
        current descendants are merged into the tracked set (one psutil
        snapshot; never raises)."""
        rc = self.proc.poll()
        if rc is None:
            self._track_descendants()
        return rc

    # ── descendant tracking (C11) ────────────────────────────────────────
    def _track_descendants(self) -> None:
        if psutil is None:
            return
        found: dict[int, float] = {}
        try:
            # `children()` builds each Process with its create time already
            # cached, so the per-child `create_time()` costs nothing extra.
            for kid in psutil.Process(self.pid).children(recursive=True):
                try:
                    found[kid.pid] = kid.create_time()
                except Exception as e:  # best effort, per child
                    log.debug("child %s: skip %s (%s)", self.pid, kid.pid, e)
        except Exception as e:  # tracking must never break poll()
            log.debug("child %s: descendant scan failed (%s)", self.pid, e)
            return
        if found:
            with self._tracked_lock:
                self._tracked.update(found)  # a reused pid gets its new time

    def _tracked_copy(self) -> list[tuple[int, float]]:
        with self._tracked_lock:
            return list(self._tracked.items())

    def _ours(self, pid: int, create_time: float) -> Optional[Any]:
        """The live psutil process of a tracked entry, or None when it is gone
        (or a zombie) or the pid now belongs to another process."""
        try:
            p = psutil.Process(pid)
            if p.create_time() != create_time:
                return None  # pid reused: never ours, never killed
            if p.status() == psutil.STATUS_ZOMBIE:
                return None
            return p
        except Exception as e:  # NoSuchProcess is the usual, quiet case
            if psutil is None or not isinstance(e, psutil.NoSuchProcess):
                log.debug("child %s: cannot inspect %s (%s)", self.pid, pid, e)
            return None

    def _kill_ours(self) -> list[Any]:
        """psutil-kill every tracked process that is still ours. Returns only
        those whose `kill()` succeeded (K4); a failed kill (AccessDenied,
        NoSuchProcess, …) is logged and left to the caller's survivor check
        (`tracked_running()`)."""
        if psutil is None:
            return []
        killed: list[Any] = []
        for pid, ctime in self._tracked_copy():
            p = self._ours(pid, ctime)
            if p is None:
                continue
            try:
                p.kill()
            except Exception as e:  # reported through the caller's result
                log.warning(
                    "child %s: killing tracked pid %s failed (%s)",
                    self.pid,
                    pid,
                    type(e).__name__,
                )
                continue
            killed.append(p)
        return killed

    def tracked_running(self) -> list[int]:
        """The pids of tracked descendants still running with their tracked
        create time (pid-reuse safe). No kill, no wait. Never raises."""
        if psutil is None:
            return []
        try:
            return [
                pid for pid, ctime in self._tracked_copy() if self._ours(pid, ctime)
            ]
        except Exception as e:  # never raises
            log.warning("child %s: tracked scan failed (%s)", self.pid, e)
            return []

    def kill_tracked(self) -> list[int]:
        """psutil-kill every tracked descendant still running with its tracked
        create time (no taskkill, no wait). Returns the pids actually killed
        (a failed kill is logged, not listed). Never raises."""
        try:
            return [p.pid for p in self._kill_ours()]
        except Exception as e:  # never raises
            log.warning("child %s: kill_tracked failed (%s)", self.pid, e)
            return []

    def write_line(self, line: str) -> None:
        """Write `line` + `\\n` (UTF-8) to stdin and flush. Raises OSError when
        the child is gone (errno 22 on Windows, BrokenPipeError elsewhere) or
        stdin is not a pipe / already closed."""
        stdin = self.proc.stdin
        if stdin is None:
            raise OSError("child stdin is not a pipe")
        data = memoryview((line + "\n").encode("utf-8"))
        try:
            while data:
                n = stdin.write(data)
                if n is None:  # a non-blocking pipe would block: not our mode
                    raise OSError("child stdin would block")
                data = data[n:]
            stdin.flush()
        except ValueError:  # write to a closed file
            raise OSError("child stdin is closed") from None

    def close_stdin(self) -> None:
        """Idempotent; errors are logged and swallowed (the child may be gone)."""
        stdin = self.proc.stdin
        if stdin is None:
            return
        # No lock: close must never wait behind a writer blocked on a full pipe.
        try:
            stdin.close()
        except OSError as e:
            log.debug("child %s: close stdin failed (%s)", self.pid, type(e).__name__)

    def tail(self, lines: int = 10, max_chars: int = 800) -> str:
        with self._tail_lock:
            recent = list(self._tail)[-lines:] if lines > 0 else []
        text = "\n".join(recent)
        return text[-max_chars:] if max_chars > 0 else ""

    def terminate_tree(self, timeout: float = 5.0) -> bool:
        """Kill the child's whole tree within ONE deadline (N4); report
        whether it is gone.

        1. refresh the tracked descendants once more if the child still lives;
        2. Windows: `taskkill /PID <pid> /T /F` while the tree is intact (C7),
           bounded by the remaining time; POSIX: terminate the direct child;
        3. psutil-kill every tracked pid still running with its tracked create
           time (pid-reuse safe) — this reaches orphans of an exited launcher;
        4. wait for those processes and the child until the deadline; a child
           still running then gets one `proc.kill()` + ≤ 1 s wait.

        Returns True iff the direct child has exited and no tracked process is
        running. Bounded by ~`timeout` + 1.5 s. Never raises."""
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            self._kill_tree(deadline)
        except Exception as e:  # never raises; the check below reports
            log.warning("child %s: tree kill failed (%s)", self.pid, e)
        try:
            child_gone = self.proc.poll() is not None
            survivors = self.tracked_running()
        except Exception as e:  # never raises
            log.warning("child %s: tree check failed (%s)", self.pid, e)
            return False
        if survivors:
            log.error(
                "child %s: tracked processes survived the kill: %s",
                self.pid,
                survivors,
            )
        return child_gone and not survivors

    def _kill_tree(self, deadline: float) -> None:
        if self.proc.poll() is None:
            self._track_descendants()
        if sys.platform == "win32":
            self._taskkill(max(_TASKKILL_MIN_S, deadline - time.monotonic()))
        elif self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError as e:  # ProcessLookupError: exited meanwhile
                log.debug("child %s: terminate failed (%s)", self.pid, type(e).__name__)
        procs = self._kill_ours()
        if procs:
            try:
                psutil.wait_procs(procs, timeout=max(0.0, deadline - time.monotonic()))
            except Exception as e:  # the final check reports survivors
                log.debug("child %s: wait_procs failed (%s)", self.pid, e)
        try:
            self.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            return
        except subprocess.TimeoutExpired:
            log.info("child %s: still alive at the deadline; killing", self.pid)
        try:
            self.proc.kill()
        except OSError as e:
            log.debug("child %s: kill failed (%s)", self.pid, type(e).__name__)
        try:
            self.proc.wait(timeout=_FINAL_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            log.error("child %s: did not exit after kill", self.pid)

    def _taskkill(self, timeout: float) -> None:
        # FIRST, while the tree is intact (C7). The Popen handle is still open,
        # so the pid cannot have been reused even if the child already exited.
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                creationflags=_no_window_flags(),
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("child %s: taskkill failed (%s)", self.pid, type(e).__name__)
            return
        if r.returncode not in (0, _TASKKILL_NOT_FOUND):
            log.warning("child %s: taskkill exited %s", self.pid, r.returncode)

    def join_readers(self, timeout: float = 2.0) -> None:
        """Join the reader threads within one overall `timeout`."""
        deadline = time.monotonic() + max(0.0, timeout)
        for t in self._readers:
            t.join(max(0.0, deadline - time.monotonic()))
