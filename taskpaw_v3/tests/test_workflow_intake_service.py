"""Explicit controller service; owned native roots, no existing Hub or Agent."""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as f
from taskpaw_v3.tests.test_workflow_intake_discovery import config_for
from taskpaw_v3.tests.test_workflow_store import Harness
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow.intake import IntakeService
from taskpaw_v3.workflow.models import WorkflowError

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native intake service")
package = f.package


@pytest.fixture
def service(package, tmp_path):
    h = Harness(tmp_path / "service.db")
    h.clock.value = datetime.now(timezone.utc)
    grant, _, adapter, _ = h.component("intake_adapter")
    config = config_for(package, tmp_path).model_copy(
        update={"component_id": grant.component_id}
    )
    h.store.configure_intake_root(h.operator, h.meta(), config)
    monotonic = [100.0]
    obj = IntakeService(
        h.store,
        adapter,
        root_id=config.root_id,
        meta=h.meta,
        owner_boot_id=grant.boot_id,
        ffprobe=None,
        controller=True,
        enabled=True,
        clock=h.clock.now,
        monotonic=lambda: monotonic[0],
    )
    try:
        yield h, adapter, config, obj, monotonic
    finally:
        obj.stop(timeout=5)
        h.store.close()


def test_ordinary_role_cannot_construct_or_start_discovery(service):
    h, adapter, config, obj, _ = service
    with pytest.raises(WorkflowError, match="controller_required"):
        IntakeService(
            h.store,
            adapter,
            root_id=config.root_id,
            meta=h.meta,
            owner_boot_id=obj.owner_boot_id,
            ffprobe=None,
            controller=False,
            enabled=True,
        )
    assert not obj.status().running
    assert not h.store.list_intakes(adapter, im.IntakeListQuery()).items


def test_actual_scan_discovers_package_reuses_identity_and_marks_positive_missing(
    service, package
):
    h, adapter, config, obj, mono = service
    assert obj.scan_once()
    (first,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    assert first.relative_path == "Actor/Film" and first.state == "waiting_stability"
    package[1].rename(package[1].with_name("Renamed"))
    h.clock.value += timedelta(seconds=30)
    mono[0] += 30
    assert obj.scan_once()
    (second,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    assert (
        second.package_id == first.package_id
        and second.relative_path == "Actor/Renamed"
    )
    assert (
        second.grouping.film_groups[0].film_id == first.grouping.film_groups[0].film_id
    )
    (package[1].with_name("Renamed")).rename(package[0].parent / "outside-input")
    h.clock.value += timedelta(seconds=30)
    mono[0] += 30
    assert obj.scan_once()
    assert h.store.get_intake(adapter, first.package_id).state == "missing"


def test_wall_clock_jump_does_not_count_toward_live_stability(service):
    h, adapter, _, obj, mono = service
    assert obj.scan_once()
    h.clock.value += timedelta(hours=1)
    mono[0] += 1
    assert obj.scan_once()
    assert obj.ready_packages() == ()
    h.clock.value += timedelta(seconds=60)
    mono[0] += 60
    assert obj.scan_once()
    assert len(obj.ready_packages()) == 1
    assert obj.run_ready_once() is None
    assert obj.status().reason == "tool_missing"
    assert (
        h.store.list_intakes(adapter, im.IntakeListQuery()).items[0].operation_id
        is None
    )


def test_partial_root_scan_never_marks_unobserved_package_missing(service, monkeypatch):
    from taskpaw_v3.workflow import intake_walk

    h, adapter, config, obj, _ = service
    assert obj.scan_once()
    (first,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    original = intake_walk.walk_root

    def partial(*args, **kwargs):
        from dataclasses import replace

        return replace(
            original(*args, **kwargs),
            candidates=(),
            coverage="partial",
            reasons=("permission_denied",),
        )

    monkeypatch.setattr(intake_walk, "walk_root", partial)
    assert not obj.scan_once()
    assert h.store.get_intake(adapter, first.package_id).state != "missing"
    assert h.store.get_intake_root_scan(adapter, config.root_id).required


def test_actual_notification_thread_and_service_are_joined_before_stop_returns(
    service, package
):
    h, adapter, config, obj, _ = service
    obj.start()
    end = time.monotonic() + 5
    while (
        not h.store.list_intakes(adapter, im.IntakeListQuery()).items
        and time.monotonic() < end
    ):
        time.sleep(0.02)
    assert h.store.list_intakes(adapter, im.IntakeListQuery()).items
    assert obj.stop(timeout=5)
    assert not obj.status().running and not obj.status().retained_unknown
    assert h.store.get_intake_root_scan(adapter, config.root_id).required


def test_missing_record_failure_keeps_root_fenced(service, package, monkeypatch):
    h, adapter, config, obj, _ = service
    assert obj.scan_once()
    package[1].rename(package[0].parent / "outside-input")

    def unavailable(*args, **kwargs):
        raise WorkflowError("authority_unavailable")

    monkeypatch.setattr(obj, "_notice", unavailable)
    assert not obj.scan_once()
    assert h.store.get_intake_root_scan(adapter, config.root_id).required


def test_stop_deadline_retains_blocked_durable_stop_owner(service, monkeypatch):
    _, _, _, obj, _ = service
    release = threading.Event()
    original = obj._begin_scan

    def blocked(*args, **kwargs):
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, "_begin_scan", blocked)
    timer = threading.Timer(0.5, release.set)
    timer.start()
    try:
        start = time.monotonic()
        stopped = obj.stop(timeout=0.05)
        elapsed = time.monotonic() - start
        assert elapsed < 0.3
        assert not stopped and obj.status().retained_unknown
    finally:
        release.set()
        timer.join()
        assert obj.stop(timeout=2)


def test_stop_retains_unjoined_discovery_owner_until_native_call_returns(
    service, monkeypatch
):
    from taskpaw_v3.workflow import intake_walk

    _, _, _, obj, _ = service
    entered, release = threading.Event(), threading.Event()
    original = intake_walk.walk_root

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(intake_walk, "walk_root", blocked)
    obj.start()
    try:
        assert entered.wait(2)
        assert not obj.stop(timeout=0.05)
        assert obj.status().retained_unknown
    finally:
        release.set()
        assert obj.stop(timeout=2)


def test_external_source_observation_cannot_reuse_old_monotonic_stability(
    service, package
):
    from taskpaw_v3.workflow.intake_discovery import observe_package

    h, adapter, config, obj, mono = service
    assert obj.scan_once()
    h.clock.value += timedelta(seconds=60)
    mono[0] += 60
    assert obj.scan_once() and obj.ready_packages()
    (item,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    (package[1] / "movie.mp4").write_bytes(b"changed source")
    changed = observe_package(
        config,
        item.relative_path,
        previous=h.store.get_intake_observation(adapter, item.package_id),
        package_version=item.version,
        observed_at=h.clock.now(),
        trigger="notification",
    )
    h.store.observe_intake(adapter, h.meta(), changed)
    assert obj.ready_packages() == ()


def test_replaced_directory_history_does_not_interrupt_later_live_notification_scan(
    service, package
):
    from taskpaw_v3.workflow.intake_watch import DirtyBatch

    h, adapter, config, obj, _ = service
    assert obj.scan_once()
    (original,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    saved = package[0].parent / "previous-object"
    package[1].rename(saved)
    package[1].mkdir()
    (package[1] / "NEW-002.mp4").write_bytes(b"new object")
    assert obj.scan_once()
    records = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    assert len(records) == 2
    current = next(v for v in records if v.package_id != original.package_id)
    assert obj.scan_once(batch=DirtyBatch(("Actor/Film/NEW-002.mp4",), False))
    assert h.store.get_intake(adapter, original.package_id).state == "missing"
    assert h.store.get_intake(adapter, current.package_id).state == "waiting_stability"
    assert not h.store.get_intake_root_scan(adapter, config.root_id).required


def test_stop_retains_direct_public_scan_until_actual_source_close(
    service, package, monkeypatch
):
    from taskpaw_v3.workflow import intake

    _, _, _, obj, _ = service
    entered, release = threading.Event(), threading.Event()
    original = intake.observe_package

    def blocked(*args, **kwargs):
        with f.held(package):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

    monkeypatch.setattr(intake, "observe_package", blocked)
    scanner = threading.Thread(target=obj.scan_once, daemon=False)
    scanner.start()
    try:
        assert entered.wait(3)
        assert not obj.stop(timeout=0.05)
        status = obj.status()
        assert status.running and status.scanning and status.retained_unknown
        with pytest.raises(PermissionError):
            package[1].rename(package[1].with_name("still-pinned"))
    finally:
        release.set()
        scanner.join(5)
        assert not scanner.is_alive()
        assert obj.stop(timeout=2)
    package[1].rename(package[1].with_name("closed-after-scan"))


def test_stop_and_public_scan_admission_are_one_atomic_boundary(service, monkeypatch):
    from taskpaw_v3.workflow import intake_walk

    _, _, _, obj, _ = service
    entered, release, stop_done = (threading.Event() for _ in range(3))
    original_lock, original_walk = obj._scan_lock, intake_walk.walk_root
    seen = []
    outcomes = []

    class AdmissionBarrier:
        """Delay only the scan gate; use the actual underlying mutex."""

        def acquire(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original_lock.acquire(*args, **kwargs)

        def release(self):
            return original_lock.release()

    def walk(*args, **kwargs):
        # An admitted scan is still owned: stop cannot certify it, and its walk
        # must observe the stop instead of racing past it.
        assert stop_done.wait(5)
        seen.append(kwargs["cancelled"]())
        return original_walk(*args, **kwargs)

    def stop():
        outcomes.append(obj.stop(timeout=0.1))
        stop_done.set()

    monkeypatch.setattr(obj, "_scan_lock", AdmissionBarrier())
    monkeypatch.setattr(intake_walk, "walk_root", walk)
    scanner = threading.Thread(target=obj.scan_once, daemon=False)
    stopper = threading.Thread(target=stop, daemon=False)
    scanner.start()
    try:
        assert entered.wait(3)
        stopper.start()
    finally:
        release.set()
        scanner.join(10)
        stopper.join(10)
        assert not scanner.is_alive() and not stopper.is_alive()
    assert outcomes == [False]
    assert seen == [True]
    assert obj.stop(timeout=2)
    assert not obj.scan_once()
    assert seen == [True]
