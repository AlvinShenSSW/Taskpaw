"""Owned real copy with a labeled synthetic probe; native pipeline tested separately."""

import io
import sys
from pathlib import Path

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_build import work_request
from taskpaw_v3.tests.test_workflow_intake_files import held
from taskpaw_v3.tests.test_workflow_media_facts import parse, source
from taskpaw_v3.workflow import intake_protocol as wire
from taskpaw_v3.workflow import intake_worker as worker
from taskpaw_v3.workflow.intake_files import IntakeFileError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows intake worker")
package = fixtures.package


def request_for(package, tmp_path):
    with held(package) as opened:
        request = work_request(opened, tmp_path)
    artifact = tmp_path / "synthetic-tool.exe"
    artifact.write_bytes(b"synthetic artifact, never executed")
    import hashlib

    return request.model_copy(
        update={
            "ffprobe": wire.ProbeArtifact(
                executable=str(artifact),
                sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                size_bytes=artifact.stat().st_size,
                version="fixture",
            )
        }
    )


class Channel:
    def __init__(self, request):
        self.request = request
        self.events = []

    def check_cancelled(self):
        pass

    def send(self, event):
        self.events.append(event)

    def receive_ack(self, deadline):
        raise AssertionError("Synthetic probe must not request native authority")


def test_worker_result_is_sent_after_verified_copy_and_all_file_owners_close(
    package, tmp_path, monkeypatch
):
    request = request_for(package, tmp_path)
    channel = Channel(request)
    probes = []

    def synthetic_probe(req, *, member_id, private_path, sequence, **kwargs):
        probes.append((member_id, sequence))
        assert Path(private_path).is_relative_to(Path(req.root.private_work_path))
        assert Path(private_path).read_bytes() == b"synthetic bytes\x00" * 100
        with pytest.raises(OSError):
            Path(package[1] / "movie.mp4").write_bytes(b"writer")
        return parse(source())

    monkeypatch.setattr(worker, "probe_video", synthetic_probe)
    result = worker.process(request, channel)
    assert probes == [(request.video_ids[0], 1)]
    assert result.admission == request.admission
    completion = (
        Path(request.root.private_work_path)
        / "intake"
        / request.operation_id
        / "completion.json"
    )
    assert completion.is_file()
    # Return, not merely a sent result, requires all file contexts to have closed.
    (package[1] / "movie.mp4").write_bytes(b"after closure")
    assert all(isinstance(v, wire.ProgressEvent) for v in channel.events)


def test_mismatched_tool_never_creates_a_private_operation(package, tmp_path):
    request = request_for(package, tmp_path)
    Path(request.ffprobe.executable).write_bytes(b"changed artifact")
    with pytest.raises(IntakeFileError):
        worker.process(request, Channel(request))
    assert not (Path(request.root.private_work_path) / "intake").exists()


def test_cancellation_never_publishes_completion(package, tmp_path):
    request = request_for(package, tmp_path)
    channel = Channel(request)

    def cancel():
        raise worker.WorkflowError("cancelled")

    channel.check_cancelled = cancel
    with pytest.raises(worker.WorkflowError, match="cancelled"):
        worker.process(request, channel)
    assert not list(Path(request.root.private_work_path).rglob("completion.json"))


def test_progress_is_cumulative_real_bytes_and_bounded_for_a_day(
    package, tmp_path, monkeypatch
):
    request = request_for(package, tmp_path)
    request = request.model_copy(
        update={
            "root": request.root.model_copy(
                update={
                    "policy": request.root.policy.model_copy(
                        update={"operation_deadline_seconds": 86400}
                    )
                }
            )
        }
    )
    channel = Channel(request)
    progress = worker.Progress(request, channel)
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    member = request.observation.members[0]
    for number in range(100_000):
        clock[0] += 0.864
        progress("copy", member.relative_path, number * 65536)
    assert 1 < len(channel.events) <= 40001
    counts = [event.bytes_completed for event in channel.events]
    assert counts == sorted(set(counts))
    budget = wire.TransportBudget()
    for event in channel.events:
        wire.encode_frame(event, budget=budget)
    assert budget.control_count + 2 * 4096 < wire.CONTROL_COUNT
    assert budget.control_bytes + 4096 * 2 * 512 < wire.CONTROL_TOTAL_BYTES


def test_fixed_worker_rejects_args_and_non_request_without_default_config(monkeypatch):
    called = []
    monkeypatch.setattr(worker, "_native_input", lambda: called.append(True))
    assert worker.main(["--anything"]) == 2
    assert called == []
    assert worker.run(io.BytesIO(), io.BytesIO()) == 2


def test_packaged_fixed_dispatch(monkeypatch):
    from taskpaw_v3.packaging import backend_main

    called = []
    monkeypatch.setattr(worker, "main", lambda argv: called.append(argv) or 17)
    assert backend_main.main(["workflow-intake"]) == 17
    assert called == [[]]
