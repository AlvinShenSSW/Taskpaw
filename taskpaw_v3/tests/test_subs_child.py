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


# ── #179: asr_env, descendant tracking, terminate_tree -> bool (C6, C11) ──

# The uv venv's `sys.executable` is a trampoline that adds a process level
# (N9); the base interpreter gives the exact tree shape the tests describe.
BASE_PY = getattr(sys, "_base_executable", None) or sys.executable

# launcher → grandchild (sleep 60, stdio detached so no pipe outlives the
# launcher); prints the grandchild pid, then exits when its stdin closes.
_LAUNCHER = (
    "import subprocess, sys\n"
    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
    "    stderr=subprocess.DEVNULL)\n"
    "print(p.pid, flush=True)\n"
    "sys.stdin.readline()\n"
)


def _kill_pid(pid: int) -> None:
    psutil = pytest.importorskip("psutil")
    try:
        psutil.Process(pid).kill()
    except psutil.Error:
        pass


def _gone(pid: int, create_time: float, timeout: float = 5.0) -> bool:
    psutil = pytest.importorskip("psutil")
    deadline = time.monotonic() + timeout
    while True:
        try:
            p = psutil.Process(pid)
            if p.create_time() != create_time or p.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _launch_tree() -> tuple[ChildProcess, int, float]:
    psutil = pytest.importorskip("psutil")
    sink: "queue.Queue[object]" = queue.Queue()
    c = ChildProcess([BASE_PY, "-c", _LAUNCHER], stdin_pipe=True, line_sink=sink)
    gpid = _grandchild_pid(sink)
    return c, gpid, psutil.Process(gpid).create_time()


def _orphan_tree() -> tuple[ChildProcess, int, float]:
    """A tree whose launcher has exited AFTER one tracking poll: the grandchild
    is orphaned and only reachable through the tracked pid."""
    c, gpid, ctime = _launch_tree()
    assert c.poll() is None
    assert c._tracked.get(gpid) == ctime
    c.close_stdin()
    assert _wait_exit(c) == 0
    return c, gpid, ctime


def test_llm_env_prefix_constant():
    assert child_mod.LLM_ENV_PREFIX == "TASKPAW_LLM_"


def test_subs_package_reexports_the_179_additions():
    from taskpaw_v3.monitors import subs
    from taskpaw_v3.monitors.subs import whisperjav

    assert subs.asr_env is child_mod.asr_env
    assert subs.LLM_ENV_PREFIX == child_mod.LLM_ENV_PREFIX
    assert subs.validate_fields is whisperjav.validate_fields
    assert {"asr_env", "LLM_ENV_PREFIX", "validate_fields"} <= set(subs.__all__)


def test_asr_env_strips_taskpaw_llm_vars_only():
    base = {
        "PATH": "C:/bin",
        "TASKPAW_LLM_API_KEY": "sk-secret",
        "taskpaw_llm_model": "grok",  # case-insensitive, like Jasna's _asr_env
        "TASKPAW_LLM_API_BASE": "https://x",
        "TASKPAW_AGENT_TOKEN": "keep",
        "MY_TASKPAW_LLM_X": "keep",
    }
    env = child_mod.asr_env(base)
    assert env == {
        "PATH": "C:/bin",
        "TASKPAW_AGENT_TOKEN": "keep",
        "MY_TASKPAW_LLM_X": "keep",
    }
    assert base["TASKPAW_LLM_API_KEY"] == "sk-secret"  # input untouched (a copy)


def test_asr_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("TASKPAW_LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("TASKPAW_TEST_KEEP", "1")
    env = child_mod.asr_env()
    assert "TASKPAW_LLM_API_KEY" not in env
    assert env["TASKPAW_TEST_KEEP"] == "1"
    assert isinstance(env, dict)


def test_poll_tracks_descendants_while_the_child_lives():
    c, gpid, ctime = _launch_tree()
    try:
        assert c.poll() is None
        assert c._tracked.get(gpid) == ctime
        assert c.pid not in c._tracked  # only descendants
    finally:
        c.terminate_tree(5.0)
        _kill_pid(gpid)
        _cleanup(c)


def test_poll_tracking_never_raises(monkeypatch):
    c = ChildProcess([PY, "-c", "import time; time.sleep(60)"])
    try:

        def boom(pid):
            raise RuntimeError("psutil exploded")

        monkeypatch.setattr(child_mod.psutil, "Process", boom)
        assert c.poll() is None
        assert c._tracked == {}
    finally:
        monkeypatch.undo()
        _cleanup(c)


def test_poll_does_not_track_after_the_child_exited(monkeypatch):
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        calls: list[int] = []

        def spy(pid):
            calls.append(pid)
            raise RuntimeError("must not be called")

        monkeypatch.setattr(child_mod.psutil, "Process", spy)
        assert c.poll() == 0
        assert calls == []
    finally:
        monkeypatch.undo()
        _cleanup(c)


@pytest.mark.skipif(sys.platform != "win32", reason="taskkill tree kill (Windows)")
def test_terminate_tree_returns_true_for_a_killed_two_level_tree():
    c, gpid, ctime = _launch_tree()
    try:
        t0 = time.monotonic()
        assert c.terminate_tree(5.0) is True
        assert time.monotonic() - t0 < 6.5
        assert c.poll() is not None
        assert _gone(gpid, ctime, timeout=0.5)
    finally:
        _kill_pid(gpid)
        _cleanup(c)


def test_terminate_tree_kills_the_tracked_grandchild_after_the_launcher_exited():
    # N1: once the launcher has exited, taskkill /T cannot find the grandchild
    # (Windows does not re-parent) — only the tracked pid still reaches it.
    psutil = pytest.importorskip("psutil")
    c, gpid, ctime = _orphan_tree()
    try:
        assert psutil.Process(gpid).is_running()  # orphaned, still alive
        assert c.terminate_tree(5.0) is True
        assert _gone(gpid, ctime, timeout=0.5)
    finally:
        _kill_pid(gpid)
        _cleanup(c)


def test_terminate_tree_true_for_an_exited_child_with_nothing_tracked():
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        assert c._tracked == {}
        assert c.terminate_tree(1.0) is True
        assert c.terminate_tree(1.0) is True  # idempotent
    finally:
        _cleanup(c)


def test_terminate_tree_false_while_a_tracked_process_survives(monkeypatch):
    psutil = pytest.importorskip("psutil")
    c, gpid, ctime = _orphan_tree()
    try:
        monkeypatch.setattr(psutil.Process, "kill", lambda self: None)
        assert c.terminate_tree(0.5) is False
        assert c.terminate_tree(0.5) is False  # still there: still False
        assert psutil.Process(gpid).create_time() == ctime  # really alive
    finally:
        monkeypatch.undo()
        _kill_pid(gpid)
        _cleanup(c)


def test_terminate_tree_is_bounded_by_one_deadline(monkeypatch):
    # A survivor makes the wait run to the deadline: the whole call is still
    # bounded by timeout + ~1.5 s (N4), never a fresh budget per step.
    psutil = pytest.importorskip("psutil")
    c, gpid, _ = _orphan_tree()
    try:
        monkeypatch.setattr(psutil.Process, "kill", lambda self: None)
        t0 = time.monotonic()
        assert c.terminate_tree(1.0) is False
        elapsed = time.monotonic() - t0
        assert 0.9 <= elapsed <= 1.0 + 1.5
    finally:
        monkeypatch.undo()
        _kill_pid(gpid)
        _cleanup(c)


def test_terminate_tree_live_child_bounded_when_its_descendant_survives(monkeypatch):
    # POSIX: the direct child is terminated, the tracked grandchild's psutil
    # kill is a no-op → False within the bound. Windows: taskkill /T takes the
    # intact tree down anyway → True. Either way: bounded, child gone.
    psutil = pytest.importorskip("psutil")
    c, gpid, _ = _launch_tree()
    try:
        monkeypatch.setattr(psutil.Process, "kill", lambda self: None)
        t0 = time.monotonic()
        result = c.terminate_tree(1.0)
        assert time.monotonic() - t0 <= 1.0 + 1.5
        assert c.poll() is not None
        assert result is (sys.platform == "win32")
    finally:
        monkeypatch.undo()
        _kill_pid(gpid)
        _cleanup(c)


def test_terminate_tree_never_kills_a_reused_pid():
    psutil = pytest.importorskip("psutil")
    # An unrelated process whose pid "reuses" a tracked entry (different
    # create time): it must survive and must not count as ours.
    stranger = subprocess.Popen(
        [BASE_PY, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    c = ChildProcess([PY, "-c", "pass"])
    try:
        real_ctime = psutil.Process(stranger.pid).create_time()
        _wait_exit(c)
        with c._tracked_lock:
            c._tracked[stranger.pid] = real_ctime - 1000.0
        assert c.terminate_tree(1.0) is True
        assert c.kill_tracked() == []
        assert stranger.poll() is None
        assert psutil.Process(stranger.pid).create_time() == real_ctime
    finally:
        stranger.kill()
        stranger.wait(10)
        _cleanup(c)


def test_terminate_tree_never_raises(monkeypatch):
    c = ChildProcess([PY, "-c", "import time; time.sleep(60)"])
    try:

        def boom(*a, **kw):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(child_mod.psutil, "wait_procs", boom)
        monkeypatch.setattr(child_mod.psutil, "Process", boom)
        t0 = time.monotonic()
        assert c.terminate_tree(1.0) in (True, False)
        assert time.monotonic() - t0 <= 1.0 + 1.5
    finally:
        monkeypatch.undo()
        _cleanup(c)


def test_kill_tracked_kills_lingering_processes_by_create_time():
    c, gpid, ctime = _orphan_tree()
    try:
        # Windows also tracks the launcher's conhost.exe (a real descendant).
        assert gpid in c.kill_tracked()
        assert _gone(gpid, ctime)
        assert c.kill_tracked() == []  # nothing left
    finally:
        _kill_pid(gpid)
        _cleanup(c)


def test_kill_tracked_never_raises(monkeypatch):
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        with c._tracked_lock:
            c._tracked[12345] = 1.0

        def boom(*a, **kw):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(child_mod.psutil, "Process", boom)
        assert c.kill_tracked() == []
    finally:
        monkeypatch.undo()
        _cleanup(c)


def test_tracking_is_safe_while_terminate_runs_on_another_thread():
    # m4: poll() merges into _tracked while terminate_tree iterates it.
    c, gpid, _ = _launch_tree()
    stop = threading.Event()
    errors: list[BaseException] = []

    def poller() -> None:
        try:
            bogus = range(40_000_000, 40_000_050)
            while not stop.is_set():
                c.poll()
                with c._tracked_lock:  # churn the size: add, then drop
                    for pid in bogus:
                        c._tracked[pid] = 1.0
                with c._tracked_lock:
                    for pid in bogus:
                        c._tracked.pop(pid, None)
        except BaseException as e:  # surfaced below
            errors.append(e)

    t = threading.Thread(target=poller)
    t.start()
    try:
        for _ in range(3):
            assert c.terminate_tree(1.0) in (True, False)
    finally:
        stop.set()
        t.join(10)
        _kill_pid(gpid)
        _cleanup(c)
    assert errors == []


# ── #179 repair cycle 3 (S4/IR2): tracked_running(), bounded, exists_quietly ──
def test_tracked_running_lists_live_tracked_pids_without_killing():
    psutil = pytest.importorskip("psutil")
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        me = psutil.Process()
        with c._tracked_lock:
            c._tracked[me.pid] = me.create_time()  # live, same create time
            c._tracked[4_000_000] = 1.0  # no such process
        assert c.tracked_running() == [me.pid]
        assert c._tracked_running() == [me.pid]  # the old name still works
        with c._tracked_lock:
            c._tracked[me.pid] = me.create_time() - 1000.0  # a "reused" pid
        assert c.tracked_running() == []  # never ours
    finally:
        _cleanup(c)


def test_tracked_running_sees_an_orphaned_grandchild_until_it_is_killed():
    c, gpid, ctime = _orphan_tree()
    try:
        assert gpid in c.tracked_running()
        assert not _gone(gpid, ctime, timeout=0.2)  # listing never kills
        assert gpid in c.kill_tracked()
        assert _gone(gpid, ctime)
        assert gpid not in c.tracked_running()
    finally:
        _kill_pid(gpid)
        _cleanup(c)


def test_tracked_running_never_raises(monkeypatch):
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        with c._tracked_lock:
            c._tracked[12345] = 1.0

        def boom(*a, **kw):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(child_mod.psutil, "Process", boom)
        assert c.tracked_running() == []
        monkeypatch.setattr(c, "_tracked_copy", boom)
        assert c.tracked_running() == []
    finally:
        monkeypatch.undo()
        _cleanup(c)


def test_bounded_keeps_the_head_and_the_end_within_the_limit():
    from taskpaw_v3.monitors.subs.util import bounded

    assert bounded("  short  ", 800) == "short"
    assert bounded("", 10) == "" and bounded(None, 10) == ""  # type: ignore[arg-type]
    text = "exit code 1: " + "x" * 5000 + " LAST"
    out = bounded(text, 800)
    assert len(out) <= 800
    assert out.startswith("exit code 1: ") and out.endswith(" LAST")
    assert " … " in out
    # the #177 shape at the 800 cap: 80 head chars + " … " + 717 tail chars
    assert out == text[:80] + " … " + text[-717:]
    exact = "y" * 800
    assert bounded(exact, 800) == exact  # at the limit: untouched
    for limit in (1, 2, 3, 5, 9, 83, 200):
        got = bounded(text, limit)
        assert len(got) <= limit and got.endswith(text[-1])
    assert bounded(text, 0) == "" and bounded(text, -5) == ""


def test_exists_quietly(tmp_path, monkeypatch):
    from pathlib import Path

    from taskpaw_v3.monitors.subs.util import exists_quietly

    f = tmp_path / "a.srt"
    f.write_text("x", encoding="utf-8")
    assert exists_quietly(f) is True
    assert exists_quietly(str(f)) is True
    assert exists_quietly(tmp_path / "missing.srt") is False

    def boom(self, *a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "exists", boom)
    try:
        got: object = exists_quietly(f)
    except OSError:
        got = "raised"
    finally:
        monkeypatch.undo()  # pytest itself calls Path.exists when reporting
    assert got is False  # unreadable → "not there yet"


def test_subs_package_reexports_the_shared_helpers():
    from taskpaw_v3.monitors import subs
    from taskpaw_v3.monitors.subs import util

    assert subs.bounded is util.bounded
    assert subs.exists_quietly is util.exists_quietly
    assert {"bounded", "exists_quietly"} <= set(subs.__all__)
    # C1: the subs package never imports lada
    for name, mod in list(sys.modules.items()):
        if name.startswith("taskpaw_v3.monitors.subs"):
            src = getattr(mod, "__file__", "") or ""
            if src.endswith(".py"):
                text = open(src, encoding="utf-8").read()
                assert "plugins.lada" not in text, name


# ── #179 review cycle 5: K4 / K5 ─────────────────────────────────────────
def test_kill_tracked_lists_only_pids_whose_kill_succeeded(monkeypatch, caplog):
    psutil = pytest.importorskip("psutil")
    c = ChildProcess([PY, "-c", "pass"])
    try:
        _wait_exit(c)
        me = psutil.Process()
        with c._tracked_lock:
            c._tracked[me.pid] = me.create_time()  # "ours", still running

        def denied(self):
            raise psutil.AccessDenied(self.pid)

        monkeypatch.setattr(psutil.Process, "kill", denied)
        with caplog.at_level("WARNING", logger="taskpaw.subs.child"):
            assert c.kill_tracked() == []  # not killed → not listed
        assert any("AccessDenied" in rec.getMessage() for rec in caplog.records)
        assert c.tracked_running() == [me.pid]  # the survivor check still sees it

        def gone(self):
            raise psutil.NoSuchProcess(self.pid)

        monkeypatch.setattr(psutil.Process, "kill", gone)
        assert c.kill_tracked() == []

        killed: list[int] = []
        monkeypatch.setattr(psutil.Process, "kill", lambda self: killed.append(1))
        assert c.kill_tracked() == [me.pid]  # a successful kill is listed
        assert killed == [1]
    finally:
        monkeypatch.undo()
        _cleanup(c)


def test_exists_quietly_treats_an_invalid_path_as_missing(tmp_path, monkeypatch):
    from pathlib import Path

    from taskpaw_v3.monitors.subs.util import exists_quietly

    assert exists_quietly(str(tmp_path) + chr(0) + "x.srt") is False
    assert exists_quietly(tmp_path) is True

    def invalid(self, *a, **kw):
        raise ValueError("embedded null byte")

    # pathlib swallows some of these itself (3.8+); the helper must not
    # depend on that for any ValueError.
    monkeypatch.setattr(Path, "exists", invalid)
    try:
        got: object = exists_quietly(tmp_path)
    except ValueError:
        got = "raised"
    finally:
        monkeypatch.undo()  # pytest itself calls Path.exists when reporting
    assert got is False
