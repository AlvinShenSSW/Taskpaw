"""Persisted intake history does not grant fresh work after restart or archive."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_approvals import archive, archive_receipt
from taskpaw_v3.tests.test_workflow_intake_fences import prepared_source
from taskpaw_v3.tests.test_workflow_intake_operations import (
    exit_proof,
    owner,
    prepare,
    record,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake


def test_restart_starts_a_new_observed_stability_window(intake):
    h, _, adapter, root = intake
    observed, command = prepare(intake)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    h.clock.value += timedelta(hours=1)
    view = h.store.get_intake(adapter, observed.package_id)
    first = h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": view.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    with pytest.raises(m.WorkflowError, match="intake_not_stable"):
        h.store.begin_intake_snapshot(
            adapter,
            h.meta(),
            command.model_copy(update={"expected_version": first.version}),
        )
    h.clock.value += timedelta(seconds=root.policy.stable_seconds)
    second = h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": first.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    assert (
        h.store.begin_intake_snapshot(
            adapter,
            h.meta(),
            command.model_copy(update={"expected_version": second.version}),
        ).status
        == "admitted"
    )


def test_known_birth_exit_does_not_require_capturing_ended_bootstrap_layers(intake):
    h, _, adapter, _ = intake
    _, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    # Native provider may observe a bootloader's lifetime accounting after it
    # already exited. Every actually opened/observed birth must still be waited.
    value = owner(h, command, observed_total=3)
    recorded = record(h, adapter, command, value)
    result = im.IntakeSnapshotResult(
        expected_version=recorded.version,
        operation_id=command.operation_id,
        source_generation=command.source_generation,
        outcome="cancelled",
        reason="cancelled",
        exit_proof=exit_proof(h, command, value),
        snapshot=None,
    )
    assert (
        h.store.finish_intake_snapshot(adapter, h.meta(), result).status == "cancelled"
    )
    assert h.store.get_intake(adapter, command.package_id).operation_id is None


def test_changed_archived_package_waits_for_reconciliation_without_new_snapshot(intake):
    h, adapter, observed, source, final, _, approval = prepared_source(intake)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    before = h.store.get_film(h.operator, source.film_id)
    for delta in (1, 60):
        h.clock.value += timedelta(seconds=delta)
        view = h.store.get_intake(adapter, observed.package_id)
        # Exact content cannot restore the old approval after uncertainty.
        h.store.observe_intake(
            adapter,
            h.meta(),
            observed.model_copy(
                update={
                    "expected_package_version": view.version,
                    "observed_at": h.clock.now(),
                    "writer_state": "busy" if delta == 1 else "clear",
                }
            ),
        )
    h.clock.value += timedelta(seconds=60)
    view = h.store.get_intake(adapter, observed.package_id)
    h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": view.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    view = h.store.get_intake(adapter, observed.package_id)
    assert (view.state, view.reason) == ("needs_attention", "recovery_required")
    command = im.IntakeSnapshotAdmission(
        expected_version=view.version,
        package_id=view.package_id,
        source_generation=view.source_generation,
        root_config_revision=view.root_config_revision,
        grouping_revision=view.grouping.grouping_revision,
        observation_digest=observed.observation_digest,
        operation_id=identity(),
        snapshot_id=identity(),
        owner_boot_id=intake[1].boot_id,
        policy_revision=1,
    )
    with pytest.raises(m.WorkflowError, match="archived_source_changed"):
        h.store.begin_intake_snapshot(adapter, h.meta(), command)
    after = h.store.get_film(h.operator, source.film_id)
    assert after.film.current_input_revision == before.film.current_input_revision
    assert after.stages == before.stages
    assert after.film.context_state == "invalidated"
