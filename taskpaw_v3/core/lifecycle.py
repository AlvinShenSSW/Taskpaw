"""Graceful-shutdown primitive shared by interactive (#5 X-exit) and service modes.

A single registry that, on stop, runs every registered cleanup LIFO,
terminates registered managed child processes, and is idempotent + bounded.
This is the V3 answer to the V2 "click X → tray → zombie process holding the
port" problem (design §1.3#7, §7.1): one stop path that always fully releases.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from typing import Callable

log = logging.getLogger("taskpaw.lifecycle")


class GracefulShutdown:
    def __init__(self, child_timeout: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        self._children: list[tuple[str, subprocess.Popen]] = []
        self._done = False
        self._child_timeout = child_timeout
        self.stopped = threading.Event()

    def register(self, name: str, callback: Callable[[], None]) -> None:
        """Register a cleanup callback (e.g. stop a monitor thread)."""
        with self._lock:
            self._callbacks.append((name, callback))

    def register_child(self, name: str, proc: subprocess.Popen) -> None:
        """Register a managed child process (e.g. lada-cli) to terminate on stop."""
        with self._lock:
            self._children.append((name, proc))

    def install_signal_handlers(self) -> None:
        """Trigger shutdown on SIGTERM/SIGINT (service mode / Ctrl-C)."""

        def _handler(signum, _frame):
            log.info("Received signal %s → graceful shutdown", signum)
            self.shutdown()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError) as e:
                # Not on the main thread, or unsupported — non-fatal.
                log.debug("Could not install handler for %s: %s", sig, e)

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

        self.stopped.set()
        log.info("Graceful shutdown complete")

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
