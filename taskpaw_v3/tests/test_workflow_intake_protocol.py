"""Typed bounded process messages; no files, executable or native proof inferred."""

import io
import struct
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import intake_protocol as p
from taskpaw_v3.workflow import models as m


def uid():
    return str(uuid4())


def work_request(videos=1):
    stamp = datetime.now(timezone.utc)
    root_id, work_id, film_id = uid(), uid(), uid()

    def native(root, object_id):
        return m.NativeIdentity(
            scheme="windows_file_id",
            root_id=root,
            volume_or_export_id="fixture-volume",
            object_id=object_id,
            observed_at=stamp,
            evidence_id=uid(),
        )

    policy = im.IntakePolicy(policy_revision=1)
    film_policy = m.FilmPolicy(
        policy_revision=1,
        required_stages=(
            "intake",
            "restore",
            "asr",
            "translate",
            "return",
            "scrape",
            "archive",
            "cleanup_input",
            "cleanup_workcopy",
        ),
        subtitle_requirement="none",
        required_image_roles=("poster",),
        folder_cover_required=False,
    )
    root = im.IntakeRootPut(
        expected_version=0,
        root_id=root_id,
        native_identity=native(root_id, "input"),
        private_path="C:/fixture/input",
        work_root_id=work_id,
        work_native_identity=native(work_id, "work"),
        private_work_path="C:/fixture/work",
        component_id=uid(),
        policy=policy,
        film_policy=film_policy,
        enabled=True,
    )
    members = tuple(
        im.ObservedMember(
            member_id=uid(),
            relative_path=f"video-{n}.mp4",
            entry_kind="file",
            native_identity=native(root_id, str(n)),
            size_bytes=1,
            mtime_ns=1,
        )
        for n in range(videos)
    )
    grouping = im.PackageGrouping(
        grouping_revision=1,
        film_groups=(
            im.FilmGroup(
                group_key="fixture",
                film_id=film_id,
                video_member_ids=tuple(x.member_id for x in members),
                members=tuple(
                    im.GroupedMember(
                        member_id=x.member_id, role="main_video", volume_ordinal=n + 1
                    )
                    for n, x in enumerate(members)
                ),
            ),
        ),
        parent_context="Actor",
        status="ready",
        reasons=(),
    )
    observation = im.IntakeObservation(
        root_id=root_id,
        root_config_revision=1,
        package_id=uid(),
        expected_package_version=1,
        package_native_identity=native(root_id, "package"),
        relative_path="Actor/Film",
        parent_context="Actor",
        observed_at=stamp,
        coverage="complete",
        trigger="rescan",
        members=members,
        observation_digest="a" * 64,
        grouping=grouping,
        writer_state="clear",
    )
    admission = im.IntakeSnapshotAdmission(
        expected_version=1,
        package_id=observation.package_id,
        source_generation=1,
        root_config_revision=1,
        grouping_revision=1,
        observation_digest=observation.observation_digest,
        operation_id=uid(),
        snapshot_id=uid(),
        owner_boot_id=uid(),
        policy_revision=1,
    )
    return p.IntakeWorkRequest(
        owner_id=uid(),
        producer_id=uid(),
        admission=admission,
        root=root,
        observation=observation,
        stable_started_at=stamp - timedelta(seconds=60),
        input_revisions=(p.FilmInputRevision(film_id=film_id, input_revision=1),),
        ffprobe=p.ProbeArtifact(
            executable="C:/fixture/ffprobe.exe",
            sha256="b" * 64,
            size_bytes=1,
            version="fixture-1",
        ),
    )


def test_roundtrip_and_request_binding():
    request = work_request()
    frame = p.WorkRequestEvent(operation_id=request.operation_id, request=request)
    assert p.read_frame(io.BytesIO(p.encode_frame(frame))) == frame
    assert request.video_ids == tuple(x.member_id for x in request.observation.members)
    with pytest.raises(ValidationError):
        p.WorkRequestEvent(operation_id=uid(), request=request)
    with pytest.raises(ValidationError):
        p.IntakeWorkRequest.model_validate(
            request.model_dump() | {"input_revisions": ()}
        )


def test_budget_4096_4097_and_strict_fields():
    assert len(work_request(4096).video_ids) == 4096
    with pytest.raises(ValidationError):
        work_request(4097)
    request = work_request()
    for change in (
        {"schema_version": True},
        {"arbitrary_argv": ["cmd"]},
        {"owner_id": "bad"},
    ):
        with pytest.raises(ValidationError):
            p.IntakeWorkRequest.model_validate(request.model_dump() | change)


@pytest.mark.parametrize(
    "body", [b'{"kind":"stop","kind":"stop"}', b'{"value":NaN}', b"{}trailing"]
)
def test_invalid_json_is_fixed_error(body):
    with pytest.raises(m.WorkflowError, match="intake_protocol"):
        p.read_frame(io.BytesIO(struct.pack(">BI", 6, len(body)) + body))


def test_control_frame_and_payload_budget_are_independent():
    with pytest.raises(m.WorkflowError, match="intake_limit"):
        p.read_frame(io.BytesIO(struct.pack(">BI", 6, p.CONTROL_BYTES + 1)))
    budget = p.TransportBudget()
    budget.consume(1, p.PAYLOAD_BYTES - 100)
    budget.consume(4, 100)
    with pytest.raises(m.WorkflowError, match="intake_limit"):
        budget.consume(4, 1)
    with pytest.raises(m.WorkflowError, match="intake_limit"):
        p.TransportBudget().consume(6, p.CONTROL_BYTES + 1)


def test_truncated_eof_is_not_a_result():
    assert p.read_frame(io.BytesIO()) is None
    with pytest.raises(m.WorkflowError, match="intake_protocol"):
        p.read_frame(io.BytesIO(b"\x01\x00"))
    with pytest.raises(m.WorkflowError, match="intake_protocol"):
        p.read_frame(io.BytesIO(struct.pack(">BI", 6, 3) + b"{}"))


def test_cumulative_control_bytes_and_count_cannot_reset():
    budget = p.TransportBudget()
    for _ in range(p.CONTROL_TOTAL_BYTES // p.CONTROL_BYTES):
        budget.consume(2, p.CONTROL_BYTES)
    with pytest.raises(m.WorkflowError, match="intake_limit"):
        budget.consume(7, 1)
    budget = p.TransportBudget()
    for _ in range(p.CONTROL_COUNT):
        budget.consume(2, 0)
    with pytest.raises(m.WorkflowError, match="intake_limit"):
        budget.consume(2, 0)


@pytest.mark.parametrize("code,size", [(0, 1), (True, 1), (1, True), (1, -1)])
def test_budget_rejects_invalid_header_types(code, size):
    with pytest.raises(m.WorkflowError, match="intake_protocol"):
        p.TransportBudget().consume(code, size)


def test_bounded_partial_writes_preserve_complete_frame():
    class Partial(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])

    stream = Partial()
    frame = p.StopCommand(operation_id=uid())
    p.write_frame(stream, frame)
    assert p.read_frame(io.BytesIO(stream.getvalue())) == frame


def test_two_payload_budget_checks_declared_size_before_allocating():
    budget = p.TransportBudget()
    budget.consume(1, p.PAYLOAD_BYTES - 10)
    with pytest.raises(m.WorkflowError, match="intake_limit"):
        p.read_frame(io.BytesIO(struct.pack(">BI", 4, 11)), budget=budget)
