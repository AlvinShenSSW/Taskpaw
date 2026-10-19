"""Public indexed discovery lookup and bounded durable scan coverage."""

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as f
from taskpaw_v3.tests.test_workflow_intake_root_scan import begin
from taskpaw_v3.workflow import intake_discovery_models as dm
from taskpaw_v3.workflow.models import WorkflowError

intake = f.intake


def test_lookup_prefers_exact_native_identity_after_directory_rename(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = f.observation(h, root)
    h.store.observe_intake(adapter, h.meta(), observed)
    found = h.store.find_intake(
        adapter,
        dm.IntakeLookup(
            root_id=root.root_id,
            relative_path="Performer/Renamed",
            native_identity=observed.package_native_identity,
        ),
    )
    assert found.package_id == observed.package_id
    assert (
        h.store.find_intake(
            adapter,
            dm.IntakeLookup(root_id=root.root_id, relative_path=observed.relative_path),
        ).package_id
        == observed.package_id
    )
    assert (
        h.store.find_intake(
            adapter,
            dm.IntakeLookup(root_id=root.root_id, relative_path="Performer/Unknown"),
        )
        is None
    )


def test_scan_membership_is_durable_bounded_and_bound_to_current_scan(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = f.observation(h, root)
    item = h.store.observe_intake(adapter, h.meta(), observed)
    scan = begin(h, root)
    started = h.store.begin_intake_root_scan(adapter, h.meta(), scan)
    assert (
        len(h.store.list_intake_unseen(adapter, root.root_id, scan.scan_id).items) == 1
    )
    seen = dm.IntakeScanSeen(
        package_id=observed.package_id,
        expected_version=item.version,
        root_id=root.root_id,
        root_config_revision=1,
        scan_id=scan.scan_id,
    )
    h.store.mark_intake_scan_seen(adapter, h.meta(), seen)
    assert not h.store.list_intake_unseen(adapter, root.root_id, scan.scan_id).items
    later = begin(h, root, started.version)
    h.store.begin_intake_root_scan(adapter, h.meta(), later)
    with pytest.raises(WorkflowError, match="binding_changed"):
        h.store.list_intake_unseen(adapter, root.root_id, scan.scan_id)
    assert (
        len(h.store.list_intake_unseen(adapter, root.root_id, later.scan_id).items) == 1
    )


def test_scan_index_is_scoped_and_cannot_mark_a_replaced_observation_seen(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = f.observation(h, root)
    item = h.store.observe_intake(adapter, h.meta(), observed)
    scan = begin(h, root)
    h.store.begin_intake_root_scan(adapter, h.meta(), scan)
    seen = dm.IntakeScanSeen(
        package_id=observed.package_id,
        expected_version=item.version,
        root_id=root.root_id,
        root_config_revision=1,
        scan_id=scan.scan_id,
    )
    other = h.component("intake_adapter")[2]
    with pytest.raises(WorkflowError, match="forbidden"):
        h.store.mark_intake_scan_seen(other, h.meta(), seen)
    h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(update={"expected_package_version": item.version}),
    )
    with pytest.raises(WorkflowError, match="cas_conflict"):
        h.store.mark_intake_scan_seen(adapter, h.meta(), seen)
