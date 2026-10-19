"""Private suspended fixed ffprobe primitive, inheriting its helper's Job.

There is no free-command entry point. The helper owns this object before setup;
parent Job settlement remains mandatory even when local closure succeeds.
"""

from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from taskpaw_v3.hub.server.upstream_worker import _WindowsAPI

from . import intake_models as im
from .intake_protocol import IntakeWorkRequest
from .media_facts import build_ffprobe_argv
from .models import WorkflowError


class SuspendedProbe:
    def __init__(self) -> None:
        self.api = _WindowsAPI()
        self.process: int | None = None
        self.thread: int | None = None
        self._handles: set[int] = set()
        self._fds: set[int] = set()
        self._streams: list[io.FileIO] = []
        self._threads: list[threading.Thread] = []
        self._payload = bytearray()
        self._error = threading.Event()
        self._fault = False
        self._limit = 0
        self._waited = False

    @contextmanager
    def _native_scope(self) -> Iterator[None]:
        """Record a failed native observation before generic worker mapping."""
        try:
            yield
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            self._fault = True
            raise WorkflowError("unknown_owner") from None

    def _reader(self, stream: io.FileIO, *, capture: bool) -> None:
        used = 0
        try:
            while True:
                part = stream.read(65536)
                if not part:
                    return
                used += len(part)
                if used > (self._limit if capture else 65536):
                    self._error.set()
                    return
                if capture:
                    self._payload.extend(part)
        except OSError:
            self._error.set()
            self._fault = True
        finally:
            try:
                stream.close()
            except OSError:
                self._fault = True
                self._error.set()

    def _pipe(self) -> tuple[io.FileIO, int]:
        read, write = self.api.pipe()
        self._handles.update((read, write))
        fd = self.api.fd(read, os.O_RDONLY)
        self._handles.remove(read)
        self._fds.add(fd)
        stream = io.FileIO(fd, "rb", closefd=True)
        self._fds.remove(fd)
        self._streams.append(stream)
        return stream, write

    def start(
        self,
        request: IntakeWorkRequest,
        member_id: str,
        private_path: str,
        sequence: int,
    ) -> im.IntakeProcessBirth:
        argv = build_ffprobe_argv(
            request.ffprobe.executable, private_path, policy=request.root.policy.probe
        )
        with self._native_scope():
            return self._start_suspended(request, member_id, sequence, argv)

    def _start_suspended(
        self,
        request: IntakeWorkRequest,
        member_id: str,
        sequence: int,
        argv: tuple[str, ...],
    ) -> im.IntakeProcessBirth:
        self._limit = request.root.policy.probe.parse_limits.max_bytes
        stdout, out_write = self._pipe()
        stderr, err_write = self._pipe()
        null = os.open(os.devnull, os.O_RDONLY)
        self._fds.add(null)
        child = [self.api.oshandle(null), out_write, err_write]
        for handle in child:
            self.api.inherit(handle, True)
        # CREATE_SUSPENDED only, with explicit stdio inheritance, no breakaway.
        self.process, self.thread, pid, _ = self.api.create_process(
            list(argv), os.environ.copy(), child
        )
        actual_pid, created = self.api.identity(self.process, 0)  # Must inherit a Job.
        if actual_pid != pid:
            self._fault = True
            raise WorkflowError("unknown_owner")
        birth = im.IntakeProcessBirth(
            pid=pid,
            creation_filetime_100ns=created,
            role="probe",
            item_id=member_id,
            sequence=sequence,
        )
        self._close_setup()
        for stream, capture in ((stdout, True), (stderr, False)):
            thread = threading.Thread(
                target=self._reader,
                args=(stream,),
                kwargs={"capture": capture},
                name="intake-probe-output",
                daemon=False,
            )
            self._threads.append(thread)
            thread.start()
        return birth

    def _close_setup(self) -> None:
        for handle in tuple(self._handles):
            self.api.close(handle)
            self._handles.remove(handle)
        for fd in tuple(self._fds):
            os.close(fd)
            self._fds.remove(fd)

    def resume(self) -> None:
        if self.thread is None:
            raise WorkflowError("intake_protocol")
        with self._native_scope():
            self.api.resume(self.thread)
            self.api.close(self.thread)
            self.thread = None

    def read_result(
        self, deadline: float, check_cancelled: Callable[[], None]
    ) -> bytes:
        assert self.process is not None
        while True:
            check_cancelled()
            if self._error.is_set():
                raise WorkflowError("intake_limit")
            if time.monotonic() >= deadline:
                raise WorkflowError("intake_timeout")
            with self._native_scope():
                result = self.api.poll(self.process)
            if result is not None:
                with self._native_scope():
                    self.api.wait(self.process, max(0, deadline - time.monotonic()))
                self._waited = True
                for thread in self._threads:
                    thread.join(max(0, deadline - time.monotonic()))
                if any(thread.is_alive() for thread in self._threads):
                    self._fault = True
                    raise WorkflowError("unknown_owner")
                if self._error.is_set():
                    raise WorkflowError("intake_limit")
                if result != 0:
                    raise WorkflowError("probe_failed")
                return bytes(self._payload)
            self._error.wait(min(0.02, max(0, deadline - time.monotonic())))

    def settle(self, deadline: float) -> bool:
        try:
            if self.process is not None and not self._waited:
                if self.api.poll(self.process) is None:
                    self.api.kill(self.process)
                self.api.wait(self.process, max(0, deadline - time.monotonic()))
                self._waited = True
        except (OSError, subprocess.TimeoutExpired):
            self._fault = True
        try:
            self._close_setup()
        except OSError:
            self._fault = True
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            return False
        if self.process is not None and not self._waited:
            return False
        for stream in self._streams:
            try:
                stream.close()
            except OSError:
                self._fault = True
        for name in ("thread", "process"):
            handle = getattr(self, name)
            if handle is not None:
                try:
                    self.api.close(handle)
                    setattr(self, name, None)
                except OSError:
                    self._fault = True
        return (
            not self._fault
            and self.process is None
            and self.thread is None
            and not self._handles
            and not self._fds
            and all(v.closed for v in self._streams)
        )
