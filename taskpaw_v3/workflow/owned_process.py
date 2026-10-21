"""Workflow child processes owned by one kill-on-close Job per attempt (T03-P1 §3.3).

Each child is created suspended, assigned to the attempt's KILL_ON_JOB_CLOSE
Job and only then resumed, so it and every descendant run inside the Job.
Closing the Job (including the agent process exiting for any reason) ends the
whole tree. Settlement means the Job has no active process left.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

LineSink = Callable[[str, str], None]  # (stream name, decoded line)

_SUSPENDED = 0x4
_NO_WINDOW = 0x08000000


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if sys.platform == "win32" else "latin-1", "replace")


MAX_LINE = 4096  # characters kept per output line (T04 E8)
_MAX_PENDING = 1 << 16  # bytes buffered without a line break before flushing


def _emit(raw: bytes, name: str, sink: LineSink | None) -> None:
    text = _decode(raw)[:MAX_LINE]
    if sink is not None and text:
        try:
            sink(name, text)
        except Exception:  # a progress callback never stops draining
            pass


def _pump(stream: Any, name: str, sink: LineSink | None) -> None:
    """Drain one pipe; lines end at LF or CR (tqdm redraws with CR)."""
    pending = b""
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            pending += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            *lines, pending = pending.split(b"\n")
            for raw in lines:
                _emit(raw, name, sink)
            if len(pending) > _MAX_PENDING:
                _emit(pending, name, sink)
                pending = b""
        _emit(pending, name, sink)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


# Defined only on Windows at runtime; the type checker sees it on every platform.
if sys.platform == "win32" or TYPE_CHECKING:
    from taskpaw_v3.hub.server.upstream_worker import _WindowsAPI

    class _OwnedAPI(_WindowsAPI):
        """`_WindowsAPI.create_process` plus a working directory and no window."""

        def create_process_in(
            self, argv: Sequence[str], env: dict | None, handles: list[int], cwd: str
        ) -> tuple[int, int, int, int]:
            startup = cast(Any, subprocess).STARTUPINFO()
            startup.dwFlags |= self.winapi.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
            startup.lpAttributeList = {"handle_list": handles}
            return cast(
                tuple[int, int, int, int],
                self.winapi.CreateProcess(
                    None,
                    subprocess.list2cmdline(list(argv)),
                    None,
                    None,
                    True,
                    _SUSPENDED | _NO_WINDOW,
                    env,
                    cwd,
                    startup,
                ),
            )

        def created_at(self, handle: int) -> int:
            times = [self.filetime() for _ in range(4)]
            self.checked(
                self.kernel.GetProcessTimes(
                    handle, *(self.ctypes.byref(t) for t in times)
                )
            )
            return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


class Child:
    """One launched process; its output is drained by reader threads."""

    def __init__(self, job: OwnedJob, identity: str, handle: Any) -> None:
        self._job, self.identity, self._handle = job, identity, handle
        self.threads: list[threading.Thread] = []
        self._exit: int | None = None

    def poll(self) -> int | None:
        if self._exit is not None or self._job.closed:
            return self._exit
        if sys.platform == "win32":
            code = self._job._api.poll(self._handle)
        else:
            code = self._handle.poll()
        if code is not None:
            self._exit = code
        return code

    def kill(self) -> None:
        """End this process only (a probe past its bound); the rest of the
        attempt's Job runs on. A process that already exited is left alone."""
        if self._job.closed or self.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                self._job._api.kill(self._handle)
            except OSError:  # exited meanwhile
                pass
            return
        import signal

        try:
            os.killpg(self._handle.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def wait(
        self, cancel: threading.Event | None = None, poll: float = 0.2
    ) -> int | None:
        """Wait for exit; None when `cancel` is set first."""
        while True:
            code = self.poll()
            if code is not None:
                for worker in self.threads:
                    worker.join(timeout=5)
                return code
            if cancel is not None:
                if cancel.wait(poll):
                    return None
            else:
                time.sleep(poll)


class OwnedJob:
    """All processes of one attempt. Owned by one dispatcher thread."""

    # Set only on Windows (see __init__).
    _api: _OwnedAPI
    _job: int

    def __init__(self, on_launch: Callable[[str], None] | None = None) -> None:
        self._on_launch = on_launch
        self.children: list[Child] = []
        self.closed = False
        if sys.platform == "win32":
            self._api = _OwnedAPI()
            self._job = self._api.create_job()
            try:
                self._api.configure_job(self._job)
            except BaseException:
                self._api.close(self._job)
                raise

    def launch(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        on_line: LineSink | None = None,
    ) -> Child:
        if self.closed:
            raise RuntimeError("job closed")
        child = (
            self._launch_windows(argv, cwd, env, on_line)
            if sys.platform == "win32"
            else self._launch_posix(argv, cwd, env, on_line)
        )
        self.children.append(child)
        if self._on_launch is not None:
            self._on_launch(child.identity)
        return child

    def _launch_windows(
        self,
        argv: Sequence[str],
        cwd: str,
        env: dict[str, str] | None,
        on_line: LineSink | None,
    ) -> Child:
        api = self._api
        streams: list[tuple[str, io.FileIO]] = []
        child_handles: list[int] = []
        null = os.open(os.devnull, os.O_RDONLY)
        try:
            for name in ("stdout", "stderr"):
                read, write = api.pipe()
                child_handles.append(write)
                streams.append((name, io.FileIO(api.fd(read, os.O_RDONLY), "rb")))
            handles = [api.oshandle(null), *child_handles]
            for handle in handles:
                api.inherit(handle, True)
            process, thread, _, _ = api.create_process_in(argv, env, handles, cwd)
        except BaseException:
            for _, stream in streams:
                stream.close()
            raise
        finally:
            os.close(null)
            for handle in child_handles:
                try:
                    api.close(handle)
                except OSError:
                    pass
        try:
            api.assign(self._job, process)
            pid, created = api.identity(process, self._job)
        except BaseException:
            api.kill(process)
            api.close(thread)
            api.close(process)
            for _, stream in streams:
                stream.close()
            raise
        child = Child(self, f"{pid}:{created}", process)
        for name, stream in streams:
            worker = threading.Thread(
                target=_pump, args=(stream, name, on_line), daemon=True
            )
            worker.start()
            child.threads.append(worker)
        try:
            api.resume(thread)
        finally:
            api.close(thread)
        return child

    def _launch_posix(
        self,
        argv: Sequence[str],
        cwd: str,
        env: dict[str, str] | None,
        on_line: LineSink | None,
    ) -> Child:
        popen = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        child = Child(self, f"{popen.pid}:0", popen)
        for name, stream in (("stdout", popen.stdout), ("stderr", popen.stderr)):
            worker = threading.Thread(
                target=_pump, args=(stream, name, on_line), daemon=True
            )
            worker.start()
            child.threads.append(worker)
        return child

    def terminate(self) -> None:
        """End every process of the attempt (idempotent)."""
        if self.closed:
            return
        if sys.platform == "win32":
            try:
                self._api.terminate_job(self._job)
            except OSError:
                pass
            return
        import signal

        for child in self.children:
            try:
                os.killpg(child._handle.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def active(self) -> int:
        if self.closed:
            return 0
        if sys.platform == "win32":
            return self._api.active(self._job)
        return sum(1 for child in self.children if child.poll() is None)

    def settle(self, timeout: float) -> bool:
        """Wait until no process is left, then close; False = still running."""
        deadline = time.monotonic() + max(0.0, timeout)
        while self.active() > 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        for child in self.children:
            child.poll()
            for worker in child.threads:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        self._close()
        return True

    def _close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if sys.platform == "win32":
            for child in self.children:
                try:
                    self._api.close(child._handle)
                except OSError:
                    pass
            try:
                self._api.close(self._job)
            except OSError:
                pass


def process_alive(identity: str) -> bool:
    """True when the process recorded as `pid:creation` still runs (D13).

    A missing pid, or a pid reused by a process with another creation time,
    means the recorded process has ended.
    """
    try:
        pid_text, created_text = identity.split(":", 1)
        pid, created = int(pid_text), int(created_text)
    except ValueError:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True
    api = _OwnedAPI()
    try:
        handle = api.open_process(pid)
    except OSError:
        return False
    try:
        if api.poll(handle) is not None:
            return False
        return api.created_at(handle) == created
    except OSError:
        return False
    finally:
        try:
            api.close(handle)
        except OSError:
            pass


def own_identity() -> str:
    """`pid:creation` of the current process."""
    if sys.platform != "win32":
        return f"{os.getpid()}:0"
    api = _OwnedAPI()
    handle = api.open_process(os.getpid())
    try:
        return f"{os.getpid()}:{api.created_at(handle)}"
    finally:
        api.close(handle)
