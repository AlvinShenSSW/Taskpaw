"""A watcher hint invalidates admission before any slow filesystem recheck."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_operations import prepare
from taskpaw_v3.workflow import intake_discovery_models as dm
from taskpaw_v3.workflow.models import WorkflowError

intake = fixtures.intake


def notice(h, observed, view, **changes):
    values = dict(
        expected_version=view.version,
        package_id=view.package_id,
        root_config_revision=view.root_config_revision,
        source_generation=view.source_generation,
        observed_at=h.clock.now(),
        kind="dirty",
        trigger="notification",
    )
    values.update(changes)
    return dm.IntakeSourceNotice(**values)


def test_notice_immediately_cancels_publication_and_requires_fresh_stability(intake):
    h, _, adapter, _ = intake
    observed, admission = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), admission)
    view = h.store.get_intake(adapter, observed.package_id)
    h.store.record_intake_source_notice(adapter, h.meta(), notice(h, observed, view))
    dirty = h.store.get_intake(adapter, observed.package_id)
    # A notification is a dirty hint, not proof of a different generation.
    assert dirty.source_generation == view.source_generation
    assert dirty.state == "receiving" and dirty.operation_id == admission.operation_id
    operation = h.store.get_intake_operation(adapter, admission.operation_id)
    assert not operation.publication_allowed and operation.state == "admitted"


def test_unchanged_notification_recheck_reuses_published_input_revision(intake):
    from taskpaw_v3.tests.test_workflow_intake_publication import snapshot_fixture

    h, adapter, _, observed, complete = snapshot_fixture(intake, eligible=True)
    h.store.finish_intake_snapshot(adapter, h.meta(), complete)
    before = h.store.get_intake(adapter, observed.package_id)
    h.store.record_intake_source_notice(adapter, h.meta(), notice(h, observed, before))
    dirty = h.store.get_intake(adapter, observed.package_id)
    assert dirty.state == "receiving"
    h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={"expected_package_version": dirty.version, "trigger": "rescan"}
        ),
    )
    after = h.store.get_intake(adapter, observed.package_id)
    assert after.source_generation == before.source_generation
    assert after.state == "operational_ready"
    assert after.eligibility == before.eligibility
    for manifest in complete.snapshot.manifests:
        assert (
            h.store.get_film(adapter, manifest.film_id).film.current_input_revision
            == manifest.revision
        )


def test_missing_requires_a_complete_rescan_event_and_keeps_canonical_history(intake):
    h, _, adapter, _ = intake
    observed, _ = prepare(intake)
    view = h.store.get_intake(adapter, observed.package_id)
    with pytest.raises(ValueError):
        notice(h, observed, view, kind="missing")
    command = notice(h, observed, view, kind="missing", trigger="complete_rescan")
    meta = h.meta()
    result = h.store.record_intake_source_notice(adapter, meta, command)
    assert h.store.record_intake_source_notice(adapter, meta, command) == result
    missing = h.store.get_intake(adapter, observed.package_id)
    assert missing.state == "missing" and missing.grouping == view.grouping
    h.clock.value += timedelta(seconds=1)
    h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": missing.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    returned = h.store.get_intake(adapter, observed.package_id)
    assert (
        returned.package_id == view.package_id and returned.state == "waiting_stability"
    )


@pytest.mark.parametrize("change", ["principal", "cas", "generation", "root", "future"])
def test_unbound_notice_cannot_invalidate_another_generation(intake, change):
    h, _, adapter, _ = intake
    observed, _ = prepare(intake)
    view = h.store.get_intake(adapter, observed.package_id)
    updates = {
        "cas": {"expected_version": view.version + 1},
        "generation": {"source_generation": view.source_generation + 1},
        "root": {"root_config_revision": view.root_config_revision + 1},
        "future": {"observed_at": h.clock.now() + timedelta(seconds=1)},
    }
    actor = h.component("intake_adapter")[2] if change == "principal" else adapter
    with pytest.raises(WorkflowError):
        h.store.record_intake_source_notice(
            actor, h.meta(), notice(h, observed, view, **updates.get(change, {}))
        )
    assert h.store.get_intake(adapter, observed.package_id) == view
