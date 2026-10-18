"""Windows one-shot writer verification, with private Job and directory ownership."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from taskpaw_v3.hub.server.upstream_worker import _WindowsProcess


class VerificationError(OSError):
    """Fixed diagnostics only; never carries child output or command values."""


_gate = threading.Lock()
_pending: WindowsVerification | None = None


def _new_process() -> _WindowsProcess:
    # Reuse suspended creation/Job assignment, not the Hub's separate
    # lifetime-member receipt/census policy.
    from taskpaw_v3.hub.server.upstream_worker import _WindowsProcess

    return _WindowsProcess()


class WindowsVerification:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.directory: tempfile.TemporaryDirectory[str] | None = None
        self.proc: _WindowsProcess | None = None
        self.threads: list[threading.Thread] = []
        self.io_failed = threading.Event()
        self.tree_exited = False
        self.sampling_failed = False

    def __enter__(self) -> WindowsVerification:
        global _pending
        if not _gate.acquire(blocking=False):
            raise VerificationError("writer verification busy")
        try:
            if _pending is not None:
                if not _pending._cleanup():
                    raise VerificationError("writer cleanup incomplete")
                _pending._remove_directory()
                _pending = None
            self.directory = tempfile.TemporaryDirectory(
                prefix=".activity-check-", dir=self.state_dir
            )
            return self
        except BaseException:
            _gate.release()
            raise

    def __exit__(self, *exc) -> None:
        global _pending
        try:
            # run() already attempted bounded cleanup. An unproven owner and
            # its directory remain reachable for the next entry's retry.
            if self.proc is not None:
                _pending = self
                raise VerificationError("writer cleanup incomplete")
            try:
                self._remove_directory()
            except OSError:
                _pending = self
                raise
        finally:
            _gate.release()

    @property
    def path(self) -> str:
        assert self.directory is not None
        return self.directory.name

    def _remove_directory(self) -> None:
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def _write(self, raw: bytes) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        pipe = self.proc.stdin
        try:
            remaining = memoryview(raw)
            while remaining:
                count = pipe.write(remaining)
                if not count:
                    raise OSError()
                remaining = remaining[count:]
        except (OSError, ValueError):
            self.io_failed.set()
        finally:
            try:
                pipe.close()
            except OSError:
                self.io_failed.set()

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        pipe = self.proc.stdout
        try:
            while True:
                part = pipe.read(4096)
                if not part:
                    break
        except (OSError, ValueError):
            self.io_failed.set()
        finally:
            try:
                pipe.close()
            except OSError:
                self.io_failed.set()

    def run(self, argv: list[str], raw: str, timeout: float) -> int:
        if self.proc is not None:
            raise VerificationError("writer cleanup incomplete")
        data = raw.encode("utf-8")
        self.threads = []
        self.io_failed.clear()
        self.tree_exited = False
        self.sampling_failed = False
        deadline = time.monotonic() + timeout
        self.proc = _new_process()  # Publish ownership before native setup.
        try:
            self.proc.start(argv, os.environ.copy())
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("writer verification", timeout)
            self.proc.resume()
            for target, args in ((self._write, (data,)), (self._read, ())):
                thread = threading.Thread(target=target, args=args, daemon=False)
                self.threads.append(thread)
                thread.start()
            while True:
                try:
                    self.proc.capture_members()
                except OSError:
                    self.sampling_failed = True
                    raise VerificationError("writer process sampling failed") from None
                if self.io_failed.is_set():
                    raise VerificationError("writer communication failed")
                code = self.proc.poll()
                if code is not None and not any(t.is_alive() for t in self.threads):
                    if self.io_failed.is_set():
                        raise VerificationError("writer communication failed")
                    return code
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("writer verification", timeout)
                self.io_failed.wait(min(0.01, remaining))
        finally:
            if not self._cleanup():
                raise VerificationError("writer cleanup incomplete")

    def _cleanup(self) -> bool:
        proc = self.proc
        if proc is None:
            return True
        end = time.monotonic() + 1
        try:
            if not self.tree_exited:
                if proc.assigned:
                    try:
                        proc.capture_members()
                    except OSError:
                        self.sampling_failed = True
                # A failed sample must not prevent termination of the owned Job.
                proc.terminate_owned()
                proc.wait(timeout=max(0, end - time.monotonic()))
                for member in proc.members:
                    if not member.valid or member.handle is None:
                        return False
                    proc.api.wait(member.handle, max(0, end - time.monotonic()))
                    member.signaled = True
                # Containment plus each captured identity's signal, not the
                # Hub's stronger historical-total equality. Members which exit
                # before any snapshot have no independent retained-handle proof.
                # Active zero alone can precede an inner Bash handle's signal.
                while proc.job is not None:
                    total, active = proc.api.accounting(proc.job)
                    if (
                        type(total) is not int
                        or type(active) is not int
                        or not 0 <= active <= total
                    ):
                        return False
                    if active == 0:
                        break
                    if time.monotonic() >= end:
                        return False
                    time.sleep(min(0.01, max(0, end - time.monotonic())))
                # A failed open may lose its reservation in the shared sampler.
                # Later snapshots cannot establish that missing birth identity,
                # so this owner's sample failure must remain fail-closed.
                if self.sampling_failed:
                    return False
                self.tree_exited = True
            for thread in self.threads:
                if thread.ident is not None:
                    thread.join(max(0, end - time.monotonic()))
            if any(thread.is_alive() for thread in self.threads):
                return False
            for pipe in (proc.stdin, proc.stdout):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            # close() records each successful partial closure. Keep the same
            # object and exit proof if a later close fails, so retry is safe.
            proc.close()
            self.proc = None
            return True
        except (OSError, subprocess.TimeoutExpired):
            return False
