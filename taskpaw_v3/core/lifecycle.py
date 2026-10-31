"""Graceful-shutdown primitive shared by interactive (#5 X-exit) and service modes.

A single registry that, on stop, runs every registered cleanup LIFO,
terminates registered managed child processes, and is idempotent + bounded.
This is the V3 answer to the V2 "click X → tray → zombie process holding the
port" problem (design §1.3#7, §7.1): one stop path that always fully releases.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

log = logging.getLogger("taskpaw.lifecycle")

# Set by the Windows desktop shell when our stdin is its lifeline pipe (#377).
STDIN_LIFELINE_ENV = "TASKPAW_STDIN_LIFELINE"
_STDIN_POLL_INTERVAL = 0.5
# NUL behind STD_INPUT_HANDLE once the lifeline is hidden from children. Opened
# once and never closed: a registry can be collected while a straggler thread
# still spawns, and a closed std handle breaks every later spawn (WinError 6).
_STDIN_NUL_FD: int | None = None


class GracefulShutdown:
    def __init__(self, child_timeout: float = 5.0) -> None:
        self._lock = threading.RLock()
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        self._children: list[tuple[str, subprocess.Popen]] = []
        self._done = False
        self._callbacks_done = False
        self._completion_holds = 0
        self._completion_claimed = False
        self._child_timeout = child_timeout
        self.stopped = threading.Event()

    def register(self, name: str, callback: Callable[[], None]) -> None:
        """Register a cleanup callback (e.g. stop a monitor thread)."""
        with self._lock:
            self._callbacks.append((name, callback))

    @property
    def is_stopping(self) -> bool:
        """Sticky stop request, including while callbacks are still running."""
        # This flag only transitions False -> True. Do not hold the registry's
        # mutex across a checkpoint that a signal can interrupt.
        return self._done

    def hold_completion(self) -> Callable[[], None]:
        """Defer the completed-stop event while a launcher can acquire resources.

        The returned release is idempotent. A completed caller-supplied registry
        remains completed; the launcher must check its sticky stop request.
        """
        with self._lock:
            if self._completion_claimed:
                return lambda: None
            self._completion_holds += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                self._completion_holds -= 1
            self._complete_if_ready()

        return release

    def _complete_if_ready(self) -> None:
        with self._lock:
            complete = (
                self._done
                and self._callbacks_done
                and self._completion_holds == 0
                and not self._completion_claimed
            )
            if complete:
                self._completion_claimed = True
        if complete:
            # logged before the event: a main thread woken by it may exit at
            # once, and a write still in flight then aborts the interpreter
            log.info("Graceful shutdown complete")
            self.stopped.set()

    def register_child(self, name: str, proc: subprocess.Popen) -> None:
        """Register a managed child process (e.g. lada-cli) to terminate on stop."""
        with self._lock:
            self._children.append((name, proc))

    def install_signal_handlers(self) -> None:
        """Trigger shutdown on SIGTERM/SIGINT (service mode / Ctrl-C), and on
        Windows when the desktop shell closes our stdin (watch_parent_stdin)."""

        def _handler(signum, _frame):
            log.info("Received signal %s → graceful shutdown", signum)
            self.shutdown()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError) as e:
                # Not on the main thread, or unsupported — non-fatal.
                log.debug("Could not install handler for %s: %s", sig, e)
        self.watch_parent_stdin()

    def watch_parent_stdin(self, fd: int = 0) -> bool:
        """Windows desktop: stop gracefully once the shell closes our stdin pipe.

        The shell cannot signal a Windows backend, so it pipes our stdin, sets
        TASKPAW_STDIN_LIFELINE=1, and closes the pipe on quit. A daemon thread
        only PEEKS the pipe: a pending read makes Python grandchildren hang at
        startup. A broken pipe runs shutdown() on a non-daemon thread, as
        restart.request does. Children get NUL, never the pipe, as their stdin.
        Returns whether the watcher started.
        """
        global _STDIN_NUL_FD
        if os.environ.pop(STDIN_LIFELINE_ENV, None) != "1":
            return False
        if sys.platform != "win32":
            return False
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        peek.restype = wintypes.BOOL
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        kernel.SetStdHandle.restype = wintypes.BOOL
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError as e:
            log.warning("Stdin lifeline disabled: no stdin handle (%s)", e)
            return False
        kind = kernel.GetFileType(handle)
        if kind != 3:  # FILE_TYPE_PIPE
            log.warning("Stdin lifeline disabled: stdin is not a pipe (type %d)", kind)
            return False
        # Hide the pipe: subprocess hands stdin=None children GetStdHandle's NUL.
        # Never dup2 over fd 0 or close the pipe (that breaks frozen spawns).
        if _STDIN_NUL_FD is None:
            _STDIN_NUL_FD = os.open(os.devnull, os.O_RDONLY)
        std_input = 0xFFFFFFF6  # (DWORD)-10, STD_INPUT_HANDLE
        if not kernel.SetStdHandle(std_input, msvcrt.get_osfhandle(_STDIN_NUL_FD)):
            log.warning(
                "Stdin lifeline: could not hide the pipe from children (error %d)",
                ctypes.get_last_error(),
            )
        avail = wintypes.DWORD()

        def poll() -> None:
            while not self.is_stopping and peek(
                handle, None, 0, None, ctypes.byref(avail), None
            ):
                time.sleep(_STDIN_POLL_INTERVAL)
            if self.is_stopping:
                return  # already stopping; never start a thread late
            log.info(
                "Desktop shell closed stdin (error %d) → graceful shutdown",
                ctypes.get_last_error(),
            )
            threading.Thread(target=self.shutdown, name="taskpaw-stdin-eof").start()

        threading.Thread(
            target=poll, name="taskpaw-stdin-lifeline", daemon=True
        ).start()
        return True

    def shutdown(self) -> None:
        """Run all cleanups + terminate children. Idempotent; safe to call twice."""
        with self._lock:
            if self._done:
                return
            self._done = True
            callbacks = list(reversed(self._callbacks))
            children = list(self._children)

        for name, cb in callbacks:  # LIFO: tear down in reverse of setup
            try:
                cb()
            except Exception as e:
                log.error("Shutdown callback %r failed: %s", name, e)

        for name, proc in children:
            self._terminate_child(name, proc)

        with self._lock:
            self._callbacks_done = True
        self._complete_if_ready()

    def _terminate_child(self, name: str, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return  # already exited
        try:
            proc.terminate()
            try:
                proc.wait(timeout=self._child_timeout)
            except subprocess.TimeoutExpired:
                log.warning("Child %r did not exit; killing", name)
                proc.kill()
                proc.wait(timeout=self._child_timeout)
        except Exception as e:
            log.error("Failed to terminate child %r: %s", name, e)


class StartupShutdown:
    """Coordinate one launcher's startup with its registered stop callback.

    A signal on the startup thread may return into a partially finished start().
    Revoke immediately, but defer teardown until that operation unwinds, so its
    newly acquired resources are included. The startup stack owns this deferred
    cleanup; a server thread requesting stop must return so cleanup can join it.
    Once cancelled, checkpoints prevent further starts.
    """

    def __init__(
        self,
        shutdown: GracefulShutdown,
        deactivate: Callable[[], None],
        cleanup: Callable[[], None],
    ) -> None:
        self._shutdown = shutdown
        self._deactivate = deactivate
        self._cleanup = cleanup
        self._lock = threading.RLock()
        self._revoke_lock = threading.RLock()
        self._cancelled = threading.Event()
        self._starting = True
        self._deactivated = False
        self._cleaned = False
        self._release_completion = shutdown.hold_completion()

    def checkpoint(self) -> None:
        if self._cancelled.is_set() or self._shutdown.is_stopping:
            raise RuntimeError("API startup failed: cancelled")

    def stop(self) -> None:
        self._cancelled.set()
        with self._revoke_lock:
            if not self._deactivated:
                self._deactivated = True
                self._deactivate()
        with self._lock:
            clean = not self._starting and not self._cleaned
            if clean:
                self._cleaned = True
        if clean:
            self._cleanup()

    def finish(self) -> None:
        try:
            with self._lock:
                self._starting = False
                clean = (
                    self._cancelled.is_set() or self._shutdown.is_stopping
                ) and not self._cleaned
                if clean:
                    self._cleaned = True
            if clean:
                # Ensure deactivation has completed before teardown. This also
                # covers an already-stopped registry whose callback never ran.
                self.stop()
                self._cleanup()
        finally:
            self._release_completion()
