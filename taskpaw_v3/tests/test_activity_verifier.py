"""Windows writer verification owns the actual canonical shell process tree."""

import io
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from taskpaw_v3.integrations import activity_setup as setup
from taskpaw_v3.integrations import activity_verifier as verifier


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Git Bash ownership")
def test_windows_verify_timeout_reaps_writer_before_directory_cleanup(
    tmp_path, monkeypatch
):
    created = []
    native_create = verifier._new_process

    def create():
        proc = native_create()
        created.append(proc)
        return proc

    monkeypatch.setattr(verifier, "_new_process", create)
    pid_file = tmp_path / "owned.pid"
    writer = tmp_path / "held_writer.py"
    writer.write_text(
        "import os, pathlib, sys, time\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--path') + 1])\n"
        "stream = output.open('wb')\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    command = setup.render_command(
        sys.executable,
        str(writer),
        "claude",
        str(tmp_path / "state.json"),
        windows=True,
    )
    errors = []

    def verify():
        try:
            setup.verify_writer(command, "claude", tmp_path)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=verify)
    owners = []
    handles = []
    thread.start()
    try:
        end = time.monotonic() + 4
        while not pid_file.exists() and time.monotonic() < end:
            time.sleep(0.01)
        assert pid_file.exists(), "owned writer never started"
        child = psutil.Process(int(pid_file.read_text()))
        for proc in [child, *child.parents()]:
            if proc.pid == os.getpid():
                break
            owners.append(proc)
        api = created[0].api
        for proc in owners:
            handle = api.open_process(proc.pid)
            handles.append(handle)
            assert api.identity(handle, created[0].job)[0] == proc.pid
        thread.join(8)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], setup.SetupError)
        assert str(errors[0]) == "writer check timed out"
        # Bind identity while each owned process is alive, then require native
        # signal state. PID disappearance is a separate OS-object observation.
        assert all(api.poll(handle) is not None for handle in handles)
        assert not list(tmp_path.glob(".activity-check-*"))
    finally:
        for handle in handles:
            api.close(handle)
        # Only identities captured from this fixture's own process ancestry.
        for proc in owners:
            try:
                if proc.is_running():
                    proc.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(owners, timeout=3)
        thread.join(8)


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows Git Bash command")
@pytest.mark.parametrize("outcome", ["success", "nonzero", "verbose"])
def test_windows_verify_actual_command_success_and_nonzero(tmp_path, outcome):
    writer = setup.WRITER
    if outcome == "nonzero":
        writer = tmp_path / "failed_writer.py"
        writer.write_text("raise SystemExit(3)\n")
    elif outcome == "verbose":
        writer = tmp_path / "verbose_writer.py"
        writer.write_text(
            "import runpy, sys\n"
            "sys.stdout.write('x' * 131072)\n"
            "sys.stdout.flush()\n"
            f"runpy.run_path({str(setup.WRITER)!r}, run_name='__main__')\n"
        )
    command = setup.render_command(
        sys.executable,
        str(writer),
        "claude",
        str(tmp_path / "state.json"),
        windows=True,
    )
    if outcome != "nonzero":
        setup.verify_writer(command, "claude", tmp_path)
    else:
        with pytest.raises(setup.SetupError, match="^writer command failed$"):
            setup.verify_writer(command, "claude", tmp_path)
    assert not list(tmp_path.glob(".activity-check-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Native suspended process cleanup")
def test_native_assignment_failure_reaps_suspended_process(tmp_path, monkeypatch):
    from taskpaw_v3.hub.server.upstream_worker import _WindowsAPI, _WindowsProcess

    api = _WindowsAPI()
    proc = _WindowsProcess(api)
    handles = []

    def fail_assignment(job, process):
        handles.append(api.open_process(proc.pid))
        raise OSError("PRIVATE_ASSIGNMENT_FAILURE")

    monkeypatch.setattr(api, "assign", fail_assignment)
    monkeypatch.setattr(verifier, "_new_process", lambda: proc)
    try:
        with pytest.raises(OSError, match="^PRIVATE_ASSIGNMENT_FAILURE$"):
            with verifier.WindowsVerification(tmp_path) as owner:
                owner.run([setup.shell_for("claude"), "-c", "exit 0"], "{}", 5)
        assert handles and all(api.poll(handle) is not None for handle in handles)
        assert proc.process is None and proc.thread is None and proc.job is None
        assert proc.stdin.closed and proc.stdout.closed
        assert verifier._pending is None and not list(tmp_path.iterdir())
    finally:
        for handle in handles:
            api.close(handle)
        if proc.process is not None:
            proc.terminate_owned()
            proc.wait(timeout=3)
            proc.close()


class _NativeFixture:
    def __init__(self, phase=None, output=b""):
        self.phase = phase
        self.refuse = phase is not None
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.api = self
        self.job = object()
        self.assigned = True
        self.closed = False
        self.started = False
        self.resumed = False
        self.terminated = False
        self.partial_closed = False
        self.members = [SimpleNamespace(valid=True, handle=object(), signaled=False)]

    def start(self, argv, env):
        self.argv = argv
        self.started = True
        if self.phase == "start":
            raise OSError("PRIVATE_START_FAILURE")

    def resume(self):
        self.resumed = True

    def poll(self):
        return 0

    def terminate_owned(self):
        if self.refuse and self.phase == "terminate":
            raise OSError("PRIVATE_TERMINATE_FAILURE")
        self.terminated = True

    def wait(self, handle=None, timeout=None):
        phase = "wait" if handle is None else "member_wait"
        if self.refuse and self.phase == phase:
            raise verifier.subprocess.TimeoutExpired("owned fixture", timeout)
        return 0

    def capture_members(self):
        if self.refuse and self.phase == "capture":
            raise OSError("PRIVATE_CAPTURE_FAILURE")
        if self.phase == "invalid_member":
            self.members[0].valid = not self.refuse

    def accounting(self, job):
        if self.refuse and self.phase == "accounting":
            raise OSError("PRIVATE_ACCOUNTING_FAILURE")
        return 3, 0  # Short-lived shell children need no lifetime census here.

    def close(self):
        self.partial_closed = True
        if self.refuse and self.phase == "close":
            raise OSError("PRIVATE_CLOSE_FAILURE")
        self.job = None
        self.closed = True


@pytest.mark.parametrize(
    "phase",
    [
        "terminate",
        "wait",
        "accounting",
        "close",
        "member_wait",
        "invalid_member",
    ],
)
def test_cleanup_failure_retains_process_and_directory_until_retry(
    tmp_path, monkeypatch, phase
):
    old = _NativeFixture(phase)
    created = []

    def create():
        proc = old if not created else _NativeFixture()
        created.append(proc)
        return proc

    monkeypatch.setattr(verifier, "_new_process", create)
    try:
        with pytest.raises(
            verifier.VerificationError, match="^writer cleanup incomplete$"
        ):
            with verifier.WindowsVerification(tmp_path) as owner:
                directory = Path(owner.path)
                (directory / "held.sqlite").write_bytes(b"owned")
                with pytest.raises(verifier.VerificationError):
                    owner.run(["exact", "shell", "command"], "{}", timeout=5)
                with pytest.raises(verifier.VerificationError):
                    owner.run(["must", "not", "replace"], "{}", timeout=5)
                assert owner.proc is old and created == [old]
        assert verifier._pending is owner
        assert owner.proc is old and directory.is_dir() and not old.closed
        assert all(not thread.is_alive() for thread in owner.threads)
        before = list(tmp_path.iterdir())
        with pytest.raises(
            verifier.VerificationError, match="^writer cleanup incomplete$"
        ):
            with verifier.WindowsVerification(tmp_path):
                pytest.fail("retained cleanup must refuse a new verification")
        assert created == [old] and list(tmp_path.iterdir()) == before
        old.refuse = False
        with verifier.WindowsVerification(tmp_path) as fresh:
            assert not directory.exists() and old.closed
            assert verifier._pending is None
            assert fresh.run(["next", "command"], "{}", timeout=5) == 0
        assert len(created) == 2 and created[1].closed
        assert not list(tmp_path.iterdir())
    finally:
        old.refuse = False
        with verifier.WindowsVerification(tmp_path):
            pass  # Retry any retained test owner before releasing its fixture.


def test_native_start_failure_closes_partial_owner_and_directory(tmp_path, monkeypatch):
    proc = _NativeFixture("start")
    monkeypatch.setattr(verifier, "_new_process", lambda: proc)
    with pytest.raises(OSError, match="PRIVATE_START_FAILURE"):
        with verifier.WindowsVerification(tmp_path) as owner:
            owner.run(["exact", "command"], "{}", timeout=5)
    assert proc.started and not proc.resumed and proc.terminated and proc.closed
    assert verifier._pending is None and not list(tmp_path.iterdir())


@pytest.mark.parametrize("failed_call", [1, 2])
def test_failed_sample_cannot_be_forgotten_by_a_later_empty_snapshot(
    tmp_path, monkeypatch, failed_call
):
    class LostSample(_NativeFixture):
        def __init__(self):
            super().__init__()
            self.samples = 0

        def capture_members(self):
            self.samples += 1
            if self.samples == failed_call:
                # Mirrors OpenProcess failure: capture_members removes the
                # unopened reservation. Later snapshots omit this known PID.
                raise OSError("PRIVATE_OPEN_FAILURE")

    proc = LostSample()
    created = []

    def create():
        created.append(proc)
        return proc

    monkeypatch.setattr(verifier, "_new_process", create)
    owner = verifier.WindowsVerification(tmp_path)
    try:
        with pytest.raises(verifier.VerificationError):
            with owner:
                directory = Path(owner.path)
                owner.run(["exact", "command"], "{}", 5)
        assert verifier._pending is owner and proc.terminated
        assert directory.is_dir() and not proc.closed
        with pytest.raises(
            verifier.VerificationError, match="^writer cleanup incomplete$"
        ):
            with verifier.WindowsVerification(tmp_path):
                pytest.fail("a failed sample was forgotten on retry")
        assert created == [proc] and owner.proc is proc and directory.is_dir()
    finally:
        # Release only this injected proof failure for fixture teardown. The
        # production owner has no evidence allowing it to clear this condition.
        owner.sampling_failed = False
        with verifier.WindowsVerification(tmp_path):
            pass


def test_overlapping_verification_refuses_before_directory_or_process(tmp_path):
    with verifier.WindowsVerification(tmp_path) as owner:
        with pytest.raises(
            verifier.VerificationError, match="^writer verification busy$"
        ):
            with verifier.WindowsVerification(tmp_path):
                pytest.fail("overlapping owner was admitted")
        assert list(tmp_path.iterdir()) == [Path(owner.path)]
    assert not list(tmp_path.iterdir())


def test_live_io_owner_retains_directory_until_thread_is_joined(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class HeldOutput(io.BytesIO):
        def read(self, size):
            entered.set()
            release.wait()
            return b""

    proc = _NativeFixture()
    proc.stdout = HeldOutput()
    created = []

    def create():
        created.append(proc)
        return proc

    monkeypatch.setattr(verifier, "_new_process", create)
    owner = verifier.WindowsVerification(tmp_path)
    try:
        with pytest.raises(
            verifier.VerificationError, match="^writer cleanup incomplete$"
        ):
            with owner:
                directory = Path(owner.path)
                owner.run(["exact", "command"], "{}", timeout=0.1)
        assert entered.is_set() and any(t.is_alive() for t in owner.threads)
        assert verifier._pending is owner and owner.proc is proc
        assert directory.is_dir() and not proc.closed
        with pytest.raises(
            verifier.VerificationError, match="^writer cleanup incomplete$"
        ):
            with verifier.WindowsVerification(tmp_path):
                pytest.fail("an unjoined I/O owner admitted another verification")
        assert created == [proc] and list(tmp_path.iterdir()) == [directory]
        release.set()
        with verifier.WindowsVerification(tmp_path):
            assert all(not t.is_alive() for t in owner.threads)
            assert not directory.exists() and proc.closed
        assert not list(tmp_path.iterdir()) and verifier._pending is None
    finally:
        release.set()
        for thread in owner.threads:
            thread.join(3)
        with verifier.WindowsVerification(tmp_path):
            pass


def test_verification_discards_verbose_output_in_bounded_reads(tmp_path, monkeypatch):
    reads = []

    class Output(io.BytesIO):
        def read(self, size):
            reads.append(size)
            return super().read(size)

    proc = _NativeFixture()
    proc.stdout = Output(b"x" * (1024 * 1024))
    monkeypatch.setattr(verifier, "_new_process", lambda: proc)
    with verifier.WindowsVerification(tmp_path) as owner:
        assert owner.run(["exact", "command"], "{}", 5) == 0
    assert len(reads) > 1 and set(reads) == {4096} and proc.closed
    assert verifier._pending is None and not list(tmp_path.iterdir())
