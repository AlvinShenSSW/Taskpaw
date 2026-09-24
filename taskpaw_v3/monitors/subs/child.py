"""`ChildProcess`: one external child with its own reader threads (#177).

Two shapes share this helper:

- the ASR child (WhisperJAV): `stdin=DEVNULL` (D15), stdout+stderr merged
  into a bounded tail, split on `\\r`/`\\n` (tqdm rewrites lines with `\\r`);
- the LLM worker: `stdin_pipe=True`, each stdout line delivered to
  `line_sink` followed by an `Eof(pid)` sentinel at EOF, stderr into the tail.

Nothing here blocks except the bounded `terminate_tree`. On Windows the tree
kill is `taskkill /PID <pid> /T /F` issued FIRST, while the tree is intact
(C7: once the launcher has exited `/T` can no longer find the grandchildren).
Nothing is imported from `lada.py` (C1); the reader mirrors its style.
"""

from __future__ import annotations

import collections
import logging
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import IO, Callable, Mapping, Optional

log = logging.getLogger("taskpaw.subs.child")

_CHUNK = 4096
_TASKKILL_NOT_FOUND = 128  # the process is already gone — not an error


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
        return self.proc.poll()

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

    def terminate_tree(self, timeout: float = 5.0) -> None:
        """Windows: `taskkill /PID <pid> /T /F` first (the whole tree, C7),
        then wait → kill. POSIX: terminate → wait → kill of the DIRECT child
        only (D18) — descendants are not signalled. Bounded by ~`timeout` +
        2 s (+ the taskkill run itself on Windows). Never raises."""
        if sys.platform == "win32":
            self._taskkill(timeout)
        else:
            if self.proc.poll() is not None:
                return
            try:
                self.proc.terminate()
            except OSError as e:  # ProcessLookupError: exited meanwhile
                log.debug("child %s: terminate failed (%s)", self.pid, type(e).__name__)
        self._wait_then_kill(timeout)

    def _taskkill(self, timeout: float) -> None:
        # FIRST, while the tree is intact (C7). The Popen handle is still open,
        # so the pid cannot have been reused even if the child already exited.
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
                creationflags=_no_window_flags(),
                capture_output=True,
                check=False,
                timeout=max(1.0, timeout),
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("child %s: taskkill failed (%s)", self.pid, type(e).__name__)
            return
        if r.returncode not in (0, _TASKKILL_NOT_FOUND):
            log.warning("child %s: taskkill exited %s", self.pid, r.returncode)

    def _wait_then_kill(self, timeout: float) -> None:
        try:
            self.proc.wait(timeout=max(0.0, timeout))
            return
        except subprocess.TimeoutExpired:
            log.info("child %s: still alive after %.1fs; killing", self.pid, timeout)
        try:
            self.proc.kill()
        except OSError as e:
            log.debug("child %s: kill failed (%s)", self.pid, type(e).__name__)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("child %s: did not exit after kill", self.pid)

    def join_readers(self, timeout: float = 2.0) -> None:
        """Join the reader threads within one overall `timeout`."""
        deadline = time.monotonic() + max(0.0, timeout)
        for t in self._readers:
            t.join(max(0.0, deadline - time.monotonic()))
