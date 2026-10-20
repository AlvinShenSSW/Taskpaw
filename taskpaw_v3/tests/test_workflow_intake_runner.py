"""Actual fixed helper plus public store; media and every path are synthetic."""

import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_build import work_request
from taskpaw_v3.tests.test_workflow_intake_files import held
from taskpaw_v3.tests.test_workflow_store import Harness
from taskpaw_v3.workflow import intake_owner as owner
from taskpaw_v3.workflow import intake_protocol as wire
from taskpaw_v3.workflow import intake_runner as runner
from taskpaw_v3.workflow.models import WorkflowError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native intake/store")
package = fixtures.package


@pytest.fixture
def ready(package, tmp_path, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("Actual media tools not supplied")
    target = package[1] / "movie.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x144:rate=24",
            "-t",
            "0.25",
            "-c:v",
            "libx264",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with held(package) as source:
        request = work_request(source, tmp_path)
    artifact = Path(ffprobe)
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    tool = wire.ProbeArtifact(
        executable=str(artifact),
        sha256=digest,
        size_bytes=artifact.stat().st_size,
        version="fixture-artifact",
    )
    h = Harness(tmp_path / "runner.db")
    h.clock.value = datetime.now(timezone.utc) - timedelta(seconds=60)
    grant, _, adapter, _ = h.component("intake_adapter")
    root = request.root.model_copy(update={"component_id": grant.component_id})
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observation = request.observation.model_copy(
        update={"expected_package_version": 0, "observed_at": h.clock.now()}
    )
    first = h.store.observe_intake(adapter, h.meta(), observation)
    h.clock.now = lambda: datetime.now(timezone.utc)
    observation = observation.model_copy(
        update={"expected_package_version": first.version, "observed_at": h.clock.now()}
    )
    h.store.observe_intake(adapter, h.meta(), observation)
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    controller = runner.SnapshotRunner(
        h.store, adapter, meta=h.meta, owner_boot_id=grant.boot_id, ffprobe=tool
    )
    try:
        yield h, adapter, observation, controller
    finally:
        controller.stop()
        h.store.close()


def test_actual_probe_ack_occurs_only_after_exact_owner_is_durable(ready, monkeypatch):
    h, adapter, observation, controller = ready
    original, persisted = owner.OwnedIntakeSnapshot.ack_probe, []

    def checked(native, ack):
        view = h.store.get_intake_operation(adapter, ack.operation_id)
        assert view.version == ack.owner_version
        assert ack.birth in view.owner.births
        persisted.append(ack.birth)
        original(native, ack)

    monkeypatch.setattr(owner.OwnedIntakeSnapshot, "ack_probe", checked)
    result = controller.run(observation.package_id)
    assert result.state == "completed" and len(persisted) == 1
    assert result.exit_proof.state == "stopped"
    assert result.result.snapshot.media_probes[0].raw_facts.ffprobe_version
    assert controller.active_operation_id is None
    assert h.store.get_intake(adapter, observation.package_id).state == "blocked"


def test_copy_progress_is_visible_only_while_the_snapshot_runs(ready, monkeypatch):
    """H2 G1: the 入库扫描 step shows the helper's copy/verify bytes."""
    h, adapter, observation, controller = ready
    total = sum(v.size_bytes or 0 for v in observation.members)
    seen = []
    original = owner.OwnedIntakeSnapshot.next_event

    def watched(native, *, deadline):
        seen.append(controller.progress)  # what the previous event left visible
        return original(native, deadline=deadline)

    monkeypatch.setattr(owner.OwnedIntakeSnapshot, "next_event", watched)
    assert controller.progress is None
    assert controller.run(observation.package_id).state == "completed"
    assert controller.progress is None
    assert seen[0] == {
        "package_id": observation.package_id,
        "phase": "copy",
        "bytes_completed": 0,
        "total": total,
    }
    copied = [p for p in seen[1:] if p and p["bytes_completed"]]
    assert copied and all(p["package_id"] == observation.package_id for p in copied)
    assert all(0 < p["bytes_completed"] <= p["total"] for p in copied)
    assert {p["phase"] for p in copied} <= {"copy", "verify"}


@pytest.mark.parametrize(
    "failure", ["transient", "persistent", "rejected", "lost_response"]
)
def test_terminal_database_failure_retains_proof_and_retries_without_native_launch(
    ready, monkeypatch, failure
):
    h, adapter, observation, controller = ready
    original = h.store.finish_intake_snapshot
    calls, launches = [], []
    start = owner.start_snapshot_suspended

    def counted_start(request):
        launches.append(request.operation_id)
        return start(request)

    def failed(auth, meta, command):
        calls.append(command)
        if failure == "persistent" or len(calls) == 1:
            if failure == "lost_response":
                original(auth, meta, command)
            raise WorkflowError(
                "precondition_failed"
                if failure == "rejected"
                else "authority_unavailable"
            )
        return original(auth, meta, command)

    monkeypatch.setattr(owner, "start_snapshot_suspended", counted_start)
    monkeypatch.setattr(h.store, "finish_intake_snapshot", failed)
    if failure == "persistent":
        with pytest.raises(WorkflowError, match="authority_unavailable"):
            controller.run(observation.package_id)
        assert controller.has_pending_settlement
        assert controller.active_operation_id == launches[0]
        assert len(calls) == 2
        monkeypatch.setattr(h.store, "finish_intake_snapshot", original)
        # Actual public service retry consumer, even with no ready packages.
        from taskpaw_v3.workflow.intake import IntakeService

        service = IntakeService(
            h.store,
            adapter,
            root_id=observation.root_id,
            meta=h.meta,
            owner_boot_id=controller.owner_boot_id,
            ffprobe=None,
            controller=True,
            enabled=True,
            clock=h.clock.now,
        )
        service._runner = controller
        result = service.run_ready_once()
    else:
        result = controller.run(observation.package_id)
    assert len(launches) == 1
    assert result.exit_proof.state == "stopped"
    if failure == "rejected":
        assert result.state == "failed" and result.result.snapshot is None
        assert (
            h.store.get_intake(adapter, observation.package_id).reason
            == "publication_blocked"
        )
    else:
        assert result.state == "completed" and result.result.snapshot is not None
    assert controller.active_operation_id is None
    assert not controller.has_pending_settlement


def test_persistence_failure_before_probe_ack_stops_then_records_actual_closure(
    ready, monkeypatch
):
    h, adapter, observation, controller = ready
    original, calls, acknowledgements = h.store.record_intake_owner, [], []

    def fail_second(*args):
        calls.append(True)
        if len(calls) == 2:
            raise WorkflowError("authority_unavailable")
        return original(*args)

    monkeypatch.setattr(h.store, "record_intake_owner", fail_second)
    monkeypatch.setattr(
        owner.OwnedIntakeSnapshot,
        "ack_probe",
        lambda *args: acknowledgements.append(True),
    )
    result = controller.run(observation.package_id)
    assert acknowledgements == []
    assert result.state == "failed" and result.result.snapshot is None
    assert result.exit_proof.state == "stopped"
    assert result.exit_proof.job_active_zero and result.exit_proof.io_joined
    assert result.exit_proof.owned_handles_closed
    assert controller.active_operation_id is None


def test_durable_cancel_precedes_native_stop_and_never_publishes(ready, monkeypatch):
    h, adapter, observation, controller = ready
    original = owner.OwnedIntakeSnapshot.observe_probe

    def cancel(native, notice):
        value = original(native, notice)
        controller.stop()
        view = h.store.get_intake_operation(adapter, notice.operation_id)
        assert view.state == "stopping" and not view.publication_allowed
        return value

    monkeypatch.setattr(owner.OwnedIntakeSnapshot, "observe_probe", cancel)
    result = controller.run(observation.package_id)
    assert result.result.snapshot is None and result.exit_proof.state == "stopped"
    assert result.state == "cancelled"


@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "revised"])
def test_service_scan_fence_failure_still_stops_actual_owned_helper(
    ready, tmp_path, monkeypatch, enabled
):
    import threading

    from taskpaw_v3.tests.test_workflow_intake_owner import synthetic_helper
    from taskpaw_v3.workflow.intake import IntakeService

    h, adapter, observation, controller = ready
    synthetic_helper(tmp_path, monkeypatch, block=True)
    obj = IntakeService(
        h.store,
        adapter,
        root_id=observation.root_id,
        meta=h.meta,
        owner_boot_id=controller.owner_boot_id,
        ffprobe=controller.ffprobe,
        controller=True,
        enabled=True,
    )
    obj._runner = controller  # Explicit fixture assembly, no fake native owner.
    reading, errors = threading.Event(), []
    original = owner.OwnedIntakeSnapshot.next_event

    def next_event(native, **kwargs):
        reading.set()
        return original(native, **kwargs)

    def run():
        try:
            controller.run(observation.package_id)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(owner.OwnedIntakeSnapshot, "next_event", next_event)
    worker = threading.Thread(target=run, daemon=False)
    obj._worker = worker
    worker.start()
    try:
        assert reading.wait(5)
        native = controller._native
        assert native is not None and native.owner_observation().observed_active
        config = h.store.get_intake_configuration(adapter, observation.root_id)
        h.store.configure_intake_root(
            h.operator,
            h.meta(),
            config.model_copy(
                update={
                    "expected_version": config.expected_version + 1,
                    "enabled": enabled,
                }
            ),
        )
        # Failed root bookkeeping remains visible, despite actual native stop.
        assert not obj.stop(timeout=2)
        assert controller._cancelled.is_set() and native._stopping
        assert obj.status().retained_unknown
    finally:
        controller.stop()
        worker.join(5)
        assert not worker.is_alive()
        obj.stop(timeout=1)
    assert not errors and native._closed
    assert controller.active_operation_id is None
