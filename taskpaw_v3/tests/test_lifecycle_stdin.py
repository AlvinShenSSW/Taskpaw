"""Windows stdin lifeline (#377): the desktop shell closing the backend's stdin
pipe runs the backend's graceful shutdown.

The shell cannot signal a Windows backend, so it spawns it with a piped stdin and
TASKPAW_STDIN_LIFELINE=1, and closes the pipe on quit. The backend only PEEKS the
pipe (a pending read hangs Python grandchildren) and hands its children NUL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from taskpaw_v3.core import lifecycle
from taskpaw_v3.core.lifecycle import GracefulShutdown

ENV = "TASKPAW_STDIN_LIFELINE"
POLLER = "taskpaw-stdin-lifeline"
STOPPER = "taskpaw-stdin-eof"

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="the stdin lifeline is Windows-only"
)


def _thread(name: str) -> threading.Thread | None:
    return next((t for t in threading.enumerate() if t.name == name), None)


@pytest.fixture
def std_input_restored():
    """The watcher repoints this process's STD_INPUT_HANDLE at NUL; give the pytest
    worker its own handle back afterwards."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel.SetStdHandle.restype = wintypes.BOOL
    std_input = 0xFFFFFFF6  # (DWORD)-10, STD_INPUT_HANDLE
    saved = kernel.GetStdHandle(std_input)
    yield
    kernel.SetStdHandle(std_input, saved)


# ── gate (all OSes) ──────────────────────────────────────────────────────────


def test_lifeline_off_when_variable_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert GracefulShutdown().watch_parent_stdin() is False
    assert _thread(POLLER) is None


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_lifeline_off_for_values_other_than_1(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    assert GracefulShutdown().watch_parent_stdin() is False
    assert ENV not in os.environ
    assert _thread(POLLER) is None


def test_lifeline_inert_off_windows(monkeypatch):
    # macOS/Linux never start a watcher, even if the variable leaks in.
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setattr(lifecycle.sys, "platform", "darwin")
    assert GracefulShutdown().watch_parent_stdin() is False
    assert ENV not in os.environ
    assert _thread(POLLER) is None


# ── Windows watcher ──────────────────────────────────────────────────────────


@windows_only
def test_closed_pipe_runs_graceful_shutdown(monkeypatch, std_input_restored):
    r, w = os.pipe()
    gs = GracefulShutdown()
    ran: list[bool] = []
    # Codex gate: the stopper must be non-daemon even though the poller is daemon.
    gs.register("probe", lambda: ran.append(threading.current_thread().daemon))
    monkeypatch.setenv(ENV, "1")
    watcher = None
    try:
        assert gs.watch_parent_stdin(r) is True
        watcher = _thread(POLLER)
        assert watcher is not None and watcher.daemon
        assert ENV not in os.environ  # popped: children never see it
        os.close(w)
        w = -1
        assert gs.stopped.wait(5)
        assert ran == [False]
    finally:
        if w != -1:
            os.close(w)
        if watcher is not None:
            watcher.join(timeout=5)
        os.close(r)


@windows_only
def test_shutdown_in_progress_ends_the_poll_quietly(
    monkeypatch, std_input_restored, caplog
):
    r, w = os.pipe()
    gs = GracefulShutdown()
    monkeypatch.setenv(ENV, "1")
    caplog.set_level("INFO", logger="taskpaw.lifecycle")
    watcher = None
    try:
        assert gs.watch_parent_stdin(r) is True
        watcher = _thread(POLLER)
        assert watcher is not None
        time.sleep(0.6)  # at least one real peek before the stop
        gs.shutdown()
        os.close(w)
        w = -1
        time.sleep(1.0)  # two poll intervals
        assert "closed stdin" not in caplog.text
        assert _thread(STOPPER) is None
        watcher.join(timeout=5)
        assert not watcher.is_alive()
    finally:
        if w != -1:
            os.close(w)
        if watcher is not None:
            watcher.join(timeout=5)  # never close r while it may be peeked
        os.close(r)


@windows_only
def test_bad_fd_does_not_start_the_watcher(monkeypatch):
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    monkeypatch.setenv(ENV, "1")
    assert GracefulShutdown().watch_parent_stdin(r) is False
    assert _thread(POLLER) is None


@windows_only
def test_non_pipe_stdin_does_not_start_the_watcher():
    # stdin is NUL: the first peek would fail and stop the backend at startup.
    script = """
import logging, threading, time
from taskpaw_v3.core.lifecycle import GracefulShutdown
logging.basicConfig()
gs = GracefulShutdown()
gs.install_signal_handlers()
time.sleep(1.5)
names = [t.name for t in threading.enumerate()]
print(gs.is_stopping, "taskpaw-stdin-lifeline" in names)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, ENV: "1"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False"]
    assert "not a pipe" in result.stderr


@windows_only
def test_shell_closing_stdin_stops_the_backend_and_children_get_nul():
    # A stdin-reading child sees EOF at once (it inherits NUL, not the lifeline),
    # a concurrent Python grandchild starts normally (no pending read), and closing
    # the pipe runs the registered stop on a non-daemon thread, then exits cleanly.
    script = """
import subprocess, sys
from taskpaw_v3.core.lifecycle import GracefulShutdown
gs = GracefulShutdown()
gs.register("probe", lambda: print("STOPPED-MARKER", flush=True))
gs.install_signal_handlers()
reader = subprocess.Popen(
    [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"],
    stdout=subprocess.PIPE, text=True,
)
quick = subprocess.run(
    [sys.executable, "-c", "print(1)"], capture_output=True, text=True, timeout=8
)
out, _ = reader.communicate(timeout=8)
print("READER", out.strip(), flush=True)
print("QUICK", quick.stdout.strip(), flush=True)
gs.stopped.wait()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, ENV: "1"},
    )
    assert proc.stdin is not None and proc.stdout is not None
    watchdog = threading.Timer(60, proc.kill)
    watchdog.start()
    try:
        lines = [proc.stdout.readline().strip() for _ in range(2)]
        if lines != ["READER ''", "QUICK 1"]:
            # Close the pipe first: a grandchild blocked reading it holds stderr.
            proc.stdin.close()
            _, err = proc.communicate(timeout=30)
            pytest.fail(f"child output {lines!r}; stderr:\n{err}")
        started = time.monotonic()
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0
        assert time.monotonic() - started < 10
        assert "STOPPED-MARKER" in proc.stdout.read()
    finally:
        watchdog.cancel()
        proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
