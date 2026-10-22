"""Fixed, database-free Windows intake helper inside its parent's owned Job."""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes as w
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast

from . import intake_models as im
from . import intake_protocol as wire
from .intake_build import build_snapshot, check_observation
from .intake_copy import PrivateCopy
from .intake_files import HeldPackage, PinnedFile, _last_error
from .intake_probe import probe_video
from .models import WorkflowError
from .raw_media_models import RawMediaFacts


class NativeInput:
    """Read only bytes already in the pipe; no unowned reader thread or read-ahead."""

    def __init__(self, fd: int):
        import msvcrt

        self.fd = fd
        self.handle = getattr(msvcrt, "get_osfhandle")(fd)
        self.deadline = time.monotonic() + 30
        self._kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        self._peek = self._kernel.PeekNamedPipe
        self._peek.argtypes = [
            w.HANDLE,
            w.LPVOID,
            w.DWORD,
            w.LPVOID,
            ctypes.POINTER(w.DWORD),
            w.LPVOID,
        ]
        self._peek.restype = w.BOOL

    def available(self) -> int:
        count = w.DWORD()
        if not self._peek(self.handle, None, 0, None, ctypes.byref(count), None):
            if _last_error() in {109, 232}:
                return -1
            raise WorkflowError("intake_protocol")
        return count.value

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b""
        while True:
            if time.monotonic() >= self.deadline:
                raise WorkflowError("intake_timeout")
            available = self.available()
            if available < 0:
                return b""
            if available:
                return os.read(self.fd, min(available, size, 65536))
            time.sleep(0.01)


class Channel(Protocol):
    def check_cancelled(self) -> None: ...
    def send(self, event: wire.WorkerEvent) -> None: ...
    def receive_ack(self, deadline: float) -> im.ProbeResumeAck: ...


class WorkerChannel:
    def __init__(
        self,
        request: wire.IntakeWorkRequest,
        reader: BinaryIO,
        writer: BinaryIO,
        budget: wire.TransportBudget,
    ):
        self.request, self.reader, self.writer, self.budget = (
            request,
            reader,
            writer,
            budget,
        )
        self.deadline = (
            time.monotonic() + request.root.policy.operation_deadline_seconds
        )

    def _read(self, deadline: float) -> wire.Frame:
        if isinstance(self.reader, NativeInput):
            self.reader.deadline = min(deadline, self.deadline)
        result = wire.read_frame(self.reader, budget=self.budget)
        if result is None or result.operation_id != self.request.operation_id:
            raise WorkflowError("intake_protocol")
        if isinstance(result, wire.StopCommand):
            raise WorkflowError("cancelled")
        return result

    def check_cancelled(self) -> None:
        if time.monotonic() >= self.deadline:
            raise WorkflowError("intake_timeout")
        if isinstance(self.reader, NativeInput) and self.reader.available() != 0:
            self._read(self.deadline)
            # No unsolicited command is legal outside the probe ACK handshake.
            raise WorkflowError("intake_protocol")

    def send(self, event: wire.WorkerEvent) -> None:
        if time.monotonic() >= self.deadline:
            raise WorkflowError("intake_timeout")
        wire.write_frame(self.writer, event, budget=self.budget)

    def receive_ack(self, deadline: float) -> im.ProbeResumeAck:
        value = self._read(deadline)
        if not isinstance(value, wire.AckCommand):
            raise WorkflowError("intake_protocol")
        return value.ack


class Progress:
    """At most 40,001 timed frames, leaving control capacity for 4,096 probes."""

    def __init__(self, request: wire.IntakeWorkRequest, channel: Channel):
        self.request, self.channel = request, channel
        self.members = {
            v.relative_path: v.member_id for v in request.observation.members
        }
        self.interval = max(1.0, request.root.policy.operation_deadline_seconds / 40000)
        self.next_at = 0.0

    def __call__(self, phase: str, relative: str, count: int) -> None:
        now = time.monotonic()
        if now < self.next_at:
            return
        self.channel.send(
            wire.ProgressEvent(
                operation_id=self.request.operation_id,
                member_id=self.members[relative],
                phase=cast(Literal["copy", "verify"], phase),
                bytes_completed=count,
            )
        )
        self.next_at = now + self.interval


def process(request: wire.IntakeWorkRequest, channel: Channel) -> im.PackageSnapshot:
    """A result may leave this function only after every file context closes."""
    config, observation = request.root, request.observation
    progress = Progress(request, channel)
    with PinnedFile(
        Path(request.ffprobe.executable),
        sha256=request.ffprobe.sha256,
        size_bytes=request.ffprobe.size_bytes,
    ):
        channel.check_cancelled()
        with HeldPackage(
            Path(config.private_path),
            observation.relative_path,
            root_id=config.root_id,
            expected_root=config.native_identity,
            expected_package=observation.package_native_identity,
            policy=config.policy,
        ) as source:
            check_observation(request, source)
            with PrivateCopy(
                source,
                Path(config.private_work_path),
                root_id=config.work_root_id,
                expected_root=config.work_native_identity,
                operation_id=request.operation_id,
            ) as target:
                target.copy_members(
                    on_progress=progress, check_cancelled=channel.check_cancelled
                )
                paths = {v.member_id: v.relative_path for v in observation.members}
                facts: dict[str, RawMediaFacts] = {}
                for sequence, member in enumerate(request.video_ids, 1):
                    facts[member] = probe_video(
                        request,
                        member_id=member,
                        private_path=str(target.private_path(paths[member])),
                        sequence=sequence,
                        on_birth=lambda notice: channel.send(
                            wire.ProbeEvent(
                                operation_id=request.operation_id, notice=notice
                            )
                        ),
                        receive_ack=channel.receive_ack,
                        check_cancelled=channel.check_cancelled,
                    )
                channel.check_cancelled()
                snapshot = build_snapshot(
                    request, source=source, copy=target, facts=facts
                )
                target.publish(
                    snapshot.model_dump_json().encode("utf-8"),
                    on_progress=progress,
                    check_cancelled=channel.check_cancelled,
                )
    channel.check_cancelled()
    return snapshot


def _reason(exc: Exception) -> im.Reason:
    code = str(exc)
    if code in {"close_failed", "unknown_owner"}:
        return "owner_unknown"
    if code in {"cancelled", "source_changed", "waiting_capacity", "probe_failed"}:
        return cast(im.Reason, code)
    if code in {"limit_exceeded", "intake_limit", "intake_timeout"}:
        return "limit_exceeded"
    if isinstance(exc, FileNotFoundError):
        return "tool_missing"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    return "copy_failed"


def run(reader: BinaryIO, writer: BinaryIO) -> int:
    budget = wire.TransportBudget()
    request = None
    try:
        first = wire.read_frame(reader, budget=budget)
        if not isinstance(first, wire.WorkRequestEvent):
            return 2
        request = first.request
        channel = WorkerChannel(request, reader, writer, budget)
        snapshot = process(request, channel)
        channel.send(
            wire.ResultEvent(operation_id=request.operation_id, snapshot=snapshot)
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        if request is not None:
            try:
                wire.write_frame(
                    writer,
                    wire.FailureEvent(
                        operation_id=request.operation_id, reason=_reason(exc)
                    ),
                    budget=budget,
                )
            except (OSError, ValueError, RuntimeError):
                pass
        return 1


def _native_input() -> BinaryIO:
    return cast(BinaryIO, NativeInput(sys.stdin.fileno()))


def main(argv: list[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv) or sys.platform != "win32":
        return 2
    return run(_native_input(), sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
