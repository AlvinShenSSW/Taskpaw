"""Real ffprobe, synthetic media, exact source/frozen intake role; no services."""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_build import work_request
from taskpaw_v3.tests.test_workflow_intake_files import held
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import intake_owner as owner
from taskpaw_v3.workflow import intake_protocol as wire

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows actual intake")
package = fixtures.package


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_real_worker_seventeen_actual_probes_and_verified_complete_copy(
    package, tmp_path, monkeypatch, frozen
):
    executable = os.environ.get("TASKPAW_INTAKE_FROZEN")
    if frozen and not executable:
        pytest.skip("Exact frozen intake executable not supplied")
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Actual ffmpeg/ffprobe tools not supplied")
    _, directory, _, _ = package
    sample = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x144:rate=24",
            "-t",
            "0.25",
            "-c:v",
            "libx264",
            str(sample),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    (directory / "movie.mp4").unlink()
    (directory / "movie.zh.srt").unlink()
    for index in range(1, 18):
        shutil.copyfile(sample, directory / f"AAA-{index:03d}.mp4")
    with held(package) as source:
        request = work_request(source, tmp_path)
    artifact = Path(ffprobe)
    with artifact.open("rb") as stream:
        artifact_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    request = request.model_copy(
        update={
            "ffprobe": wire.ProbeArtifact(
                executable=str(artifact),
                sha256=artifact_hash,
                size_bytes=artifact.stat().st_size,
                version="test-host-artifact",
            )
        }
    )
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    if frozen:
        assert Path(executable).is_file()
        monkeypatch.setattr(
            owner, "_helper_argv", lambda: [executable, "workflow-intake"]
        )
    native = owner.start_snapshot_suspended(request)
    probes, result, observed = [], None, None
    try:
        initial = native.owner_observation()
        assert not initial.identity_uncertain
        native.resume_helper(persisted_owner_version=1)
        while result is None:
            event = native.next_event(deadline=time.monotonic() + 30)
            if isinstance(event, wire.ProbeEvent):
                observed = native.observe_probe(event.notice)
                assert not observed.identity_uncertain
                probes.append(event.notice)
                # The service/store durability seam is separately exercised;
                # this test owns only actual native birth/ACK/closure behavior.
                native.ack_probe(
                    im.ProbeResumeAck(
                        **event.notice.model_dump(), owner_version=len(probes) + 1
                    )
                )
            elif isinstance(event, wire.ResultEvent):
                result = event.snapshot
            else:
                assert isinstance(event, wire.ProgressEvent), event
    finally:
        proof = native.settle(deadline=time.monotonic() + 10)
    assert result is not None and len(probes) == 17
    assert proof.state == "stopped", proof
    assert proof.job_active_zero and proof.io_joined and proof.owned_handles_closed
    assert sum(value.birth.role == "probe" for value in proof.births) == 17
    assert all(v.signaled and v.waited and v.handle_closed for v in proof.births)
    assert native.owner_observation().observed_total >= 18
    assert len(result.manifests) == len(result.media_probes) == 17
    assert all(
        p.width == 256 and p.height == 144 and p.duration_ms == 250
        for p in result.media_probes
    )
    assert all(
        p.raw_facts.ffprobe_version
        and p.raw_facts.ffprobe_version != "test-host-artifact"
        for p in result.media_probes
    )
    for manifest in result.manifests:
        for item in manifest.readiness.workcopy.items:
            copied = Path(request.root.private_work_path) / item.relative_path
            assert copied.read_bytes() == sample.read_bytes()
    completion = (
        Path(request.root.private_work_path)
        / "intake"
        / request.operation_id
        / "completion.json"
    )
    assert im.PackageSnapshot.model_validate_json(completion.read_bytes()) == result
    assert len(list(directory.glob("*.mp4"))) == 17
