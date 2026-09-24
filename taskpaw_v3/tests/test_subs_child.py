"""`ChildProcess` with real `sys.executable -c` children (#177, subs/child.py)."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time

import pytest

from taskpaw_v3.monitors.subs import child as child_mod
from taskpaw_v3.monitors.subs.child import ChildProcess, Eof

PY = sys.executable


def _drain(q: "queue.Queue[object]", timeout: float = 15.0) -> list[object]:
    """Everything on `q` up to and including the Eof sentinel."""
    out: list[object] = []
    deadline = time.monotonic() + timeout
    while True:
        item = q.get(timeout=max(0.01, deadline - time.monotonic()))
        out.append(item)
        if isinstance(item, Eof):
            return out


def _wait_exit(c: ChildProcess, timeout: float = 15.0) -> int:
    return c.proc.wait(timeout=timeout)


def _cleanup(c: ChildProcess) -> None:
    if c.poll() is None:
        c.terminate_tree(5.0)
    c.close_stdin()
    c.join_readers(5.0)


def test_tail_captures_merged_stdout_stderr_split_on_cr():
    code = (
        "import sys\n"
        "sys.stdout.write('p 1%\\rp 50%\\rp 100%\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('err line\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('last no newline'); sys.stdout.flush()\n"
    )
    c = ChildProcess([PY, "-c", code])
    try:
        assert _wait_exit(c) == 0
        c.join_readers(5.0)
        lines = c.tail(lines=10).splitlines()
        assert lines[:3] == ["p 1%", "p 50%", "p 100%"]
        assert "err line" in lines
        assert lines[-1] == "last no newline"
        assert c.tail(lines=1) == "last no newline"
        assert len(c.tail(lines=10, max_chars=5)) == 5
    finally:
        _cleanup(c)


def test_tail_is_bounded_by_tail_lines():
    code = "for i in range(100): print('line', i)"
    c = ChildProcess([PY, "-c", code], tail_lines=5)
    try:
        _wait_exit(c)
        c.join_readers(5.0)
        assert c.tail(lines=50).splitlines() == [f"line {i}" for i in range(95, 100)]
    finally:
        _cleanup(c)


def test_line_sink_gets_stdout_lines_then_eof_and_stderr_goes_to_tail():
    code = (
        "import sys\n"
        "print('one'); print(''); print('二'); sys.stdout.flush()\n"
        "sys.stderr.write('warn\\n'); sys.stderr.flush()\n"
    )
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([PY, "-X", "utf8", "-c", code], line_sink=sink)
    try:
        items = _drain(sink)
        assert items == ["one", "二", Eof(c.pid)]
        _wait_exit(c)
        c.join_readers(5.0)
        assert "warn" in c.tail()
    finally:
        _cleanup(c)


def test_eof_carries_pid():
    assert Eof(5) == Eof(5) and Eof(5) != Eof(6)
    assert Eof(7).pid == 7


def test_write_line_echo_and_close_stdin_idempotent():
    code = (
        "import sys\n"
        "for raw in sys.stdin.buffer:\n"
        "    sys.stdout.buffer.write(b'echo:' + raw); sys.stdout.flush()\n"
    )
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([PY, "-c", code], stdin_pipe=True, line_sink=sink)
    try:
        c.write_line('{"id": "a", "text": "日本語"}')
        assert sink.get(timeout=15) == 'echo:{"id": "a", "text": "日本語"}'
        c.write_line("second")
        assert sink.get(timeout=15) == "echo:second"
        c.close_stdin()
        c.close_stdin()  # idempotent
        assert _wait_exit(c) == 0
        assert sink.get(timeout=15) == Eof(c.pid)
        with pytest.raises(OSError):
            c.write_line("after close")
    finally:
        _cleanup(c)


def test_write_line_after_child_exited_raises_oserror():
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([PY, "-c", "pass"], stdin_pipe=True, line_sink=sink)
    try:
        _wait_exit(c)
        _drain(sink)
        with pytest.raises(OSError):
            # A pipe buffer may swallow the first small write on some platforms;
            # a dead reader is reported by one of a few writes at the latest.
            for _ in range(64):
                c.write_line("x" * 4096)
    finally:
        _cleanup(c)


def test_write_line_without_stdin_pipe_raises_oserror():
    c = ChildProcess([PY, "-c", "pass"])
    try:
        with pytest.raises(OSError):
            c.write_line("x")
        c.close_stdin()  # no pipe: a quiet no-op
    finally:
        _cleanup(c)


def test_asr_style_child_stdin_is_devnull():
    # A child that reads stdin must see EOF at once (D15): never a hang, never
    # an interactive prompt.
    code = "import sys; data = sys.stdin.read(); print('EOF', len(data))"
    c = ChildProcess([PY, "-c", code])
    try:
        assert _wait_exit(c, 15) == 0
        c.join_readers(5.0)
        assert "EOF 0" in c.tail()
        assert c.proc.stdin is None
    finally:
        _cleanup(c)


def test_popen_error_propagates(tmp_path):
    with pytest.raises(OSError):
        ChildProcess([str(tmp_path / "does-not-exist.exe")])


def test_reader_threads_are_named_daemons():
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([PY, "-c", "import time; time.sleep(30)"], line_sink=sink)
    try:
        names = {t.name: t for t in threading.enumerate()}
        out = names.get(f"subs-child-{c.pid}-out")
        err = names.get(f"subs-child-{c.pid}-err")
        assert out is not None and out.daemon
        assert err is not None and err.daemon
    finally:
        _cleanup(c)
    assert not any(
        t.name.startswith(f"subs-child-{c.pid}-") for t in threading.enumerate()
    )


def test_join_readers_returns_within_timeout_on_live_child():
    c = ChildProcess([PY, "-c", "import time; time.sleep(60)"])
    try:
        t0 = time.monotonic()
        c.join_readers(0.2)
        assert time.monotonic() - t0 < 1.0
        assert c.poll() is None
    finally:
        _cleanup(c)


def test_terminate_tree_on_exited_child_returns_quietly():
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        t0 = time.monotonic()
        c.terminate_tree(1.0)  # Windows: taskkill rc 128 (already gone) is fine
        assert time.monotonic() - t0 < 5.0
    finally:
        _cleanup(c)


_TREE = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _grandchild_pid(sink: "queue.Queue[object]") -> int:
    line = sink.get(timeout=30)
    assert isinstance(line, str), line
    return int(line)


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill tree kill (Windows)")
def test_terminate_tree_windows_kills_live_two_level_tree():
    psutil = pytest.importorskip("psutil")
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([PY, "-c", _TREE], line_sink=sink)
    try:
        gpid = _grandchild_pid(sink)
        assert psutil.pid_exists(gpid)
        t0 = time.monotonic()
        c.terminate_tree(5.0)
        assert time.monotonic() - t0 < 8.0
        assert c.poll() is not None
        deadline = time.monotonic() + 5
        while psutil.pid_exists(gpid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not psutil.pid_exists(gpid), "grandchild survived the tree kill"
    finally:
        _cleanup(c)


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill argv (Windows)")
def test_terminate_tree_windows_taskkill_argv_first_then_wait(monkeypatch):
    c = ChildProcess([PY, "-c", "import time; time.sleep(60)"])
    calls: list[tuple] = []
    real_run = subprocess.run

    def spy_run(argv, **kw):
        calls.append((argv, kw))
        assert c.poll() is None  # taskkill runs FIRST, while the tree is intact
        return real_run(argv, **kw)

    try:
        monkeypatch.setattr(child_mod.subprocess, "run", spy_run)
        c.terminate_tree(5.0)
        assert c.poll() is not None
        (argv, kw) = calls[0]
        assert argv == ["taskkill", "/PID", str(c.pid), "/T", "/F"]
        assert isinstance(argv, list)
        assert kw.get("shell", False) is False
        assert kw["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert kw["capture_output"] is True and kw["check"] is False
    finally:
        monkeypatch.undo()
        _cleanup(c)


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill rc (Windows)")
def test_terminate_tree_windows_rc128_is_not_an_error(monkeypatch, caplog):
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        monkeypatch.setattr(
            child_mod.subprocess,
            "run",
            lambda argv, **kw: subprocess.CompletedProcess(argv, 128, b"", b""),
        )
        caplog.set_level("WARNING")
        c.terminate_tree(1.0)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
    finally:
        monkeypatch.undo()
        _cleanup(c)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminate→kill")
def test_terminate_tree_posix_terminate_then_kill():
    # The child ignores SIGTERM, so terminate() alone cannot end it: kill() must.
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([PY, "-c", code], line_sink=sink)
    try:
        assert sink.get(timeout=30) == "ready"
        t0 = time.monotonic()
        c.terminate_tree(0.5)
        assert c.poll() is not None
        assert time.monotonic() - t0 < 5.0
    finally:
        _cleanup(c)


def test_terminate_tree_live_single_child():
    c = ChildProcess([PY, "-c", "import time; time.sleep(60)"])
    try:
        c.terminate_tree(5.0)
        assert c.poll() is not None
        c.join_readers(2.0)
    finally:
        _cleanup(c)
