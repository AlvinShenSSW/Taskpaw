"""The fixed worker's sequential probe handshake; never opens a workflow DB."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from . import intake_models as im
from .intake_probe_native import SuspendedProbe
from .intake_protocol import IntakeWorkRequest
from .media_facts import parse_ffprobe
from .models import WorkflowError
from .raw_media_models import RawMediaFacts


def _new_probe() -> SuspendedProbe:
    return SuspendedProbe()


def probe_video(
    request: IntakeWorkRequest,
    *,
    member_id: str,
    private_path: str,
    sequence: int,
    on_birth: Callable[[im.ProbeBirthNotice], None],
    receive_ack: Callable[[float], im.ProbeResumeAck],
    check_cancelled: Callable[[], None],
) -> RawMediaFacts:
    """Caller holds the validated ffprobe artifact and source package throughout.

    ``receive_ack`` is the fixed worker's bounded pipe receive, not an authority
    factory. Only the parent sends it after exact native birth/store admission.
    """
    if sys.platform != "win32":
        raise WorkflowError("intake_platform")
    if (
        type(sequence) is not int
        or not 1 <= sequence <= len(request.video_ids)
        or member_id != request.video_ids[sequence - 1]
    ):
        raise WorkflowError("intake_protocol")
    check_cancelled()
    deadline = time.monotonic() + request.root.policy.probe.deadline_seconds
    native = _new_probe()
    try:
        birth = native.start(request, member_id, private_path, sequence)
        notice = im.ProbeBirthNotice(
            operation_id=request.operation_id,
            item_id=member_id,
            sequence=sequence,
            birth=birth,
        )
        on_birth(notice)
        ack = im.ProbeResumeAck.model_validate(receive_ack(deadline))
        if (
            im.ProbeBirthNotice.model_validate(
                ack.model_dump(exclude={"owner_version"})
            )
            != notice
            or time.monotonic() >= deadline
        ):
            raise WorkflowError("intake_protocol")
        check_cancelled()
        native.resume()
        payload = native.read_result(deadline, check_cancelled)
        return parse_ffprobe(payload, limits=request.root.policy.probe.parse_limits)
    except WorkflowError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise WorkflowError("probe_failed") from None
    finally:
        if not native.settle(deadline):
            raise WorkflowError("unknown_owner")
