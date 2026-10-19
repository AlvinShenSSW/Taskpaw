"""Fixed local intake messages. Values alone never establish native ownership."""

from __future__ import annotations

import json
import struct
import threading
from pathlib import PureWindowsPath
from typing import Annotated, BinaryIO, Literal, TypeAlias

from pydantic import Field, ValidationError, field_validator, model_validator
from typing_extensions import Self

from . import intake_models as im
from . import models as m

CONTROL_BYTES = 64 * 1024
PAYLOAD_BYTES = 64 * 1024 * 1024  # Initial request plus terminal result, together.
CONTROL_TOTAL_BYTES = 16 * 1024 * 1024
CONTROL_COUNT = 65536
QUEUE_ITEMS = 8


class ProbeArtifact(m.Model):
    executable: Annotated[str, Field(min_length=1, max_length=32767, repr=False)]
    sha256: m.Digest
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
    version: m.Text

    @field_validator("executable")
    @classmethod
    def local_path(cls, value: str) -> str:
        path = PureWindowsPath(value)
        if (
            not path.is_absolute()
            or len(path.drive) != 2
            or path.drive[1] != ":"
            or ":" in value[2:]
            or ".." in path.parts
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("probe_local_path")
        return value


class FilmInputRevision(m.Model):
    film_id: m.Id
    input_revision: m.Revision


class IntakeWorkRequest(m.Model):
    owner_id: m.Id
    producer_id: m.Id
    admission: im.IntakeSnapshotAdmission
    root: im.IntakeRootPut = Field(repr=False)
    observation: im.IntakeObservation
    stable_started_at: m.UtcTime
    input_revisions: Annotated[
        tuple[FilmInputRevision, ...], Field(min_length=1, max_length=4096)
    ]
    ffprobe: ProbeArtifact

    @property
    def operation_id(self) -> str:
        return self.admission.operation_id

    @property
    def video_ids(self) -> tuple[str, ...]:
        grouping = self.observation.grouping
        assert grouping is not None
        return tuple(
            item for group in grouping.film_groups for item in group.video_member_ids
        )

    @model_validator(mode="after")
    def bindings(self) -> Self:
        a, r, o = self.admission, self.root, self.observation
        grouping = o.grouping
        if (
            not r.enabled
            or o.coverage != "complete"
            or o.writer_state != "clear"
            or grouping is None
            or grouping.status != "ready"
            or o.package_native_identity is None
            or a.package_id != o.package_id
            or o.root_id != r.root_id
            or a.root_config_revision != o.root_config_revision
            or a.root_config_revision != r.expected_version + 1
            or a.grouping_revision != grouping.grouping_revision
            or a.policy_revision != r.policy.policy_revision
            or a.observation_digest != o.observation_digest
            or self.stable_started_at > o.observed_at
        ):
            raise ValueError("intake_request_binding")
        members = {item.member_id: item for item in o.members}
        expected = {
            item.member_id for group in grouping.film_groups for item in group.members
        }
        expected.update(item.member_id for item in grouping.shared_members)
        expected.update(grouping.directory_member_ids)
        if (
            set(members) != expected
            or not 1 <= len(members) <= r.policy.max_members
            or any(
                item.native_identity is None
                or item.size_bytes is None
                or item.mtime_ns is None
                or item.entry_kind not in {"file", "directory"}
                or item.temporary
                for item in o.members
            )
            or not 1 <= len(self.video_ids) <= 4096
            or any(members[item].entry_kind != "file" for item in self.video_ids)
        ):
            raise ValueError("intake_request_members")
        revisions = {item.film_id for item in self.input_revisions}
        if len(revisions) != len(self.input_revisions) or revisions != {
            group.film_id for group in grouping.film_groups
        }:
            raise ValueError("intake_input_revisions")
        return self


class WorkRequestEvent(m.Model):
    kind: Literal["request"] = "request"
    operation_id: m.Id
    request: IntakeWorkRequest

    @model_validator(mode="after")
    def binding(self) -> Self:
        if self.operation_id != self.request.operation_id:
            raise ValueError("intake_message_binding")
        return self


class ProgressEvent(m.Model):
    kind: Literal["progress"] = "progress"
    operation_id: m.Id
    member_id: m.Id
    phase: Literal["copy", "verify"]
    bytes_completed: im.BoundCount


class ProbeEvent(m.Model):
    kind: Literal["probe"] = "probe"
    operation_id: m.Id
    notice: im.ProbeBirthNotice

    @model_validator(mode="after")
    def binding(self) -> Self:
        if self.operation_id != self.notice.operation_id:
            raise ValueError("intake_message_binding")
        return self


class ResultEvent(m.Model):
    kind: Literal["result"] = "result"
    operation_id: m.Id
    snapshot: im.PackageSnapshot

    @model_validator(mode="after")
    def binding(self) -> Self:
        if self.operation_id != self.snapshot.admission.operation_id:
            raise ValueError("intake_message_binding")
        return self


class FailureEvent(m.Model):
    kind: Literal["failure"] = "failure"
    operation_id: m.Id
    reason: im.Reason


class StopCommand(m.Model):
    kind: Literal["stop"] = "stop"
    operation_id: m.Id


class AckCommand(m.Model):
    kind: Literal["ack"] = "ack"
    operation_id: m.Id
    ack: im.ProbeResumeAck

    @model_validator(mode="after")
    def binding(self) -> Self:
        if self.operation_id != self.ack.operation_id:
            raise ValueError("intake_message_binding")
        return self


WorkerEvent: TypeAlias = ProgressEvent | ProbeEvent | ResultEvent | FailureEvent
ParentCommand: TypeAlias = WorkRequestEvent | StopCommand | AckCommand
Frame: TypeAlias = WorkerEvent | ParentCommand
_TYPES: dict[int, type[m.Model]] = {
    1: WorkRequestEvent,
    2: ProgressEvent,
    3: ProbeEvent,
    4: ResultEvent,
    5: FailureEvent,
    6: StopCommand,
    7: AckCommand,
}


class TransportBudget:
    """Shared between both local pipe directions; no truncation or reset."""

    def __init__(self) -> None:
        self.payload_bytes = 0
        self.control_bytes = 0
        self.control_count = 0
        self._lock = threading.Lock()

    def consume(self, code: int, size: int) -> None:
        if (
            type(code) is not int
            or code not in _TYPES
            or type(size) is not int
            or size < 0
        ):
            raise m.WorkflowError("intake_protocol")
        with self._lock:
            if code in {1, 4}:
                if size > PAYLOAD_BYTES - self.payload_bytes:
                    raise m.WorkflowError("intake_limit")
                self.payload_bytes += size
            else:
                if (
                    size > CONTROL_BYTES
                    or size > CONTROL_TOTAL_BYTES - self.control_bytes
                    or self.control_count >= CONTROL_COUNT
                ):
                    raise m.WorkflowError("intake_limit")
                self.control_bytes += size
                self.control_count += 1


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise ValueError("nonfinite")


def _exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.read(min(size - len(data), 65536))
        if not part:
            raise m.WorkflowError("intake_protocol")
        data.extend(part)
    return bytes(data)


def read_frame(
    stream: BinaryIO, *, budget: TransportBudget | None = None
) -> Frame | None:
    first = stream.read(1)
    if not first:
        return None
    header = first + _exact(stream, 4)
    code, size = struct.unpack(">BI", header)
    (budget or TransportBudget()).consume(code, size)
    raw = _exact(stream, size)
    try:
        value = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
        nodes, pending = 0, [(value, 0)]
        while pending:
            current, depth = pending.pop()
            nodes += 1
            if depth > 64 or nodes > 2_000_000:
                raise ValueError("structure_limit")
            if isinstance(current, dict):
                pending.extend((v, depth + 1) for v in current.values())
            elif isinstance(current, list):
                pending.extend((v, depth + 1) for v in current)
        result = _TYPES[code].model_validate(value)
        if not isinstance(
            result,
            (
                WorkRequestEvent,
                ProgressEvent,
                ProbeEvent,
                ResultEvent,
                FailureEvent,
                StopCommand,
                AckCommand,
            ),
        ):
            raise ValueError("frame_type")
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError, ValidationError):
        raise m.WorkflowError("intake_protocol") from None


def encode_frame(frame: Frame, *, budget: TransportBudget | None = None) -> bytes:
    code = next((key for key, cls in _TYPES.items() if type(frame) is cls), None)
    if code is None:
        raise m.WorkflowError("intake_protocol")
    raw = frame.model_dump_json().encode("utf-8")
    (budget or TransportBudget()).consume(code, len(raw))
    return struct.pack(">BI", code, len(raw)) + raw


def write_frame(
    stream: BinaryIO, frame: Frame, *, budget: TransportBudget | None = None
) -> None:
    remaining = memoryview(encode_frame(frame, budget=budget))
    while remaining:
        count = stream.write(remaining)
        if not count:
            raise m.WorkflowError("intake_protocol")
        remaining = remaining[count:]
    stream.flush()
