"""Fixed suspended probe protocol. Synthetic callbacks do not attest native exit."""

import sys

import pytest

from taskpaw_v3.tests.test_workflow_intake_protocol import work_request
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import intake_probe as probe
from taskpaw_v3.workflow.models import WorkflowError

# The intake owner is the Windows controller's native helper (the product refuses other
# platforms with intake_platform); Windows CI runs every test here.
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only intake owner"
)


class NativeProbe:
    def __init__(self):
        self.resumed = False
        self.closed = False
        self.launched = False

    def start(self, request, member_id, private_path, sequence):
        self.launched = True
        return im.IntakeProcessBirth(
            pid=13,
            creation_filetime_100ns=123,
            role="probe",
            item_id=member_id,
            sequence=sequence,
        )

    def resume(self):
        self.resumed = True

    def read_result(self, deadline, check_cancelled):
        check_cancelled()
        return b'{"streams":[{"index":0,"codec_type":"video","codec_name":"h264","width":1920,"height":1080,"pix_fmt":"yuv420p","avg_frame_rate":"24/1","r_frame_rate":"24/1","duration":"1.0"}],"format":{"format_name":"mov,mp4,m4a,3gp,3g2,mj2","duration":"1.0"}}'

    def settle(self, deadline):
        self.closed = True
        return True


def test_no_ack_and_wrong_ack_never_resume(monkeypatch):
    for wrong in (False, True):
        native = NativeProbe()
        monkeypatch.setattr(probe, "_new_probe", lambda: native)
        request = work_request()
        notices = []

        def receive(deadline):
            if not wrong:
                raise WorkflowError("intake_timeout")
            return im.ProbeResumeAck(
                **notices[0].model_dump(), owner_version=1
            ).model_copy(update={"sequence": 2})

        with pytest.raises(WorkflowError):
            probe.probe_video(
                request,
                member_id=request.video_ids[0],
                private_path="C:/fixture/copy.mp4",
                sequence=1,
                on_birth=notices.append,
                receive_ack=receive,
                check_cancelled=lambda: None,
            )
        assert (
            len(notices) == 1
            and native.launched
            and native.closed
            and not native.resumed
        )


def test_matching_ack_then_fixed_parser(monkeypatch):
    native = NativeProbe()
    monkeypatch.setattr(probe, "_new_probe", lambda: native)
    request = work_request()
    notices = []
    facts = probe.probe_video(
        request,
        member_id=request.video_ids[0],
        private_path="C:/fixture/copy.mp4",
        sequence=1,
        on_birth=notices.append,
        receive_ack=lambda deadline: im.ProbeResumeAck(
            **notices[0].model_dump(), owner_version=2
        ),
        check_cancelled=lambda: None,
    )
    assert native.resumed and native.closed
    assert facts.streams[0].width == 1920


def test_unsettled_probe_cannot_produce_facts(monkeypatch):
    native = NativeProbe()
    native.settle = lambda deadline: False
    monkeypatch.setattr(probe, "_new_probe", lambda: native)
    request = work_request()
    notices = []
    with pytest.raises(WorkflowError, match="unknown_owner"):
        probe.probe_video(
            request,
            member_id=request.video_ids[0],
            private_path="C:/fixture/copy.mp4",
            sequence=1,
            on_birth=notices.append,
            receive_ack=lambda deadline: im.ProbeResumeAck(
                **notices[0].model_dump(), owner_version=2
            ),
            check_cancelled=lambda: None,
        )


def test_wrong_item_rejected_before_creation(monkeypatch):
    native = NativeProbe()
    monkeypatch.setattr(probe, "_new_probe", lambda: native)
    request = work_request()
    with pytest.raises(WorkflowError):
        probe.probe_video(
            request,
            member_id=request.producer_id,
            private_path="C:/fixture/copy.mp4",
            sequence=1,
            on_birth=lambda n: None,
            receive_ack=lambda d: None,
            check_cancelled=lambda: None,
        )
    assert not native.launched


def test_deadline_error_is_not_relabelled_as_a_parse_failure(monkeypatch):
    native = NativeProbe()
    monkeypatch.setattr(probe, "_new_probe", lambda: native)
    request = work_request()

    def expired(deadline):
        raise WorkflowError("intake_timeout")

    with pytest.raises(WorkflowError, match="intake_timeout"):
        probe.probe_video(
            request,
            member_id=request.video_ids[0],
            private_path="C:/fixture/copy.mp4",
            sequence=1,
            on_birth=lambda _: None,
            receive_ack=expired,
            check_cancelled=lambda: None,
        )
    assert native.closed and not native.resumed
