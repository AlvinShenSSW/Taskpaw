"""Whole-root dirty scans survive crashes and cannot be cleared by stale scans."""

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_operations import prepare
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_discovery_models as dm
from taskpaw_v3.workflow.models import WorkflowError
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake


def begin(h, root, version=0, trigger="overflow"):
    return dm.IntakeRootScanBegin(
        root_id=root.root_id,
        root_config_revision=1,
        expected_version=version,
        scan_id=identity(),
        observed_at=h.clock.now(),
        trigger=trigger,
    )


def finish(h, command, version, complete=True):
    return dm.IntakeRootScanFinish(
        root_id=command.root_id,
        root_config_revision=1,
        expected_version=version,
        scan_id=command.scan_id,
        observed_at=h.clock.now(),
        complete=complete,
        reason="observed" if complete else "permission_denied",
    )


def test_overflow_root_fence_is_durable_and_only_complete_current_scan_releases(intake):
    h, _, adapter, root = intake
    _, admission = prepare(intake)
    command = begin(h, root)
    started = h.store.begin_intake_root_scan(adapter, h.meta(), command)
    with pytest.raises(WorkflowError, match="source_rescan_required"):
        h.store.begin_intake_snapshot(adapter, h.meta(), admission)
    incomplete = h.store.finish_intake_root_scan(
        adapter, h.meta(), finish(h, command, started.version, False)
    )
    assert h.store.get_intake_root_scan(adapter, root.root_id).required
    retry = begin(h, root, incomplete.version)
    restarted = h.store.begin_intake_root_scan(adapter, h.meta(), retry)
    h.store.finish_intake_root_scan(
        adapter, h.meta(), finish(h, retry, restarted.version)
    )
    assert not h.store.get_intake_root_scan(adapter, root.root_id).required
    h.store.begin_intake_snapshot(adapter, h.meta(), admission)


def test_new_notification_and_restart_cannot_accept_old_complete_scan(intake):
    h, _, adapter, root = intake
    prepare(intake)
    first = begin(h, root)
    started = h.store.begin_intake_root_scan(adapter, h.meta(), first)
    next_scan = begin(h, root, started.version, "notification")
    h.store.begin_intake_root_scan(adapter, h.meta(), next_scan)
    with pytest.raises(WorkflowError, match="cas_conflict"):
        h.store.finish_intake_root_scan(
            adapter, h.meta(), finish(h, first, started.version)
        )
    before = h.store.get_intake_root_scan(adapter, root.root_id)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    after = h.store.get_intake_root_scan(adapter, root.root_id)
    assert after.required and after.version > before.version
    with pytest.raises(WorkflowError):
        h.store.finish_intake_root_scan(
            adapter, h.meta(), finish(h, next_scan, before.version)
        )


def test_root_scan_status_does_not_expose_paths_and_unrelated_component_cannot_write(
    intake,
):
    h, _, adapter, root = intake
    prepare(intake)
    command = begin(h, root)
    other = h.component("intake_adapter")[2]
    with pytest.raises(WorkflowError, match="forbidden"):
        h.store.begin_intake_root_scan(other, h.meta(), command)
    h.store.begin_intake_root_scan(adapter, h.meta(), command)
    observer = h.component("observer")[2]
    status = h.store.get_intake_root_scan(observer, root.root_id)
    assert root.private_path not in status.model_dump_json()
    assert root.private_work_path not in status.model_dump_json()


def test_required_rescan_blocks_an_existing_input_at_the_actual_public_claim(intake):
    from taskpaw_v3.tests.test_workflow_films import claim, skip_compute
    from taskpaw_v3.tests.test_workflow_intake_publication import snapshot_fixture

    h, adapter, root, _, command = snapshot_fixture(
        intake, eligible=True, allow_test_reuse=True
    )
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    film_id = command.snapshot.manifests[0].film_id
    skip_compute(h, film_id)
    scan = begin(h, root)
    started = h.store.begin_intake_root_scan(adapter, h.meta(), scan)
    with pytest.raises(WorkflowError, match="source_not_admissible"):
        claim(h, film_id)
    h.store.finish_intake_root_scan(adapter, h.meta(), finish(h, scan, started.version))
    claim(h, film_id)
