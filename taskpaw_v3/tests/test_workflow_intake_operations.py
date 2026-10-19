"""Synthetic public lifecycle tests; DTO proofs are not native process evidence."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_store import observation
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake


def prepare(intake, *, stable=True):
    h, grant, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    result = h.store.observe_intake(adapter, h.meta(), observed)
    if stable:
        h.clock.value += timedelta(seconds=root.policy.stable_seconds)
        observed = observed.model_copy(
            update={
                "expected_package_version": result.version,
                "observed_at": h.clock.now(),
                "trigger": "rescan",
            }
        )
        result = h.store.observe_intake(adapter, h.meta(), observed)
    command = im.IntakeSnapshotAdmission(
        expected_version=result.version,
        package_id=observed.package_id,
        source_generation=1,
        root_config_revision=1,
        grouping_revision=1,
        observation_digest=observed.observation_digest,
        operation_id=identity(),
        snapshot_id=identity(),
        owner_boot_id=grant.boot_id,
        policy_revision=1,
    )
    return observed, command


def owner(h, command, **changes):
    data = dict(
        owner_boot_id=command.owner_boot_id,
        owner_id=identity(),
        births=(
            im.IntakeProcessBirth(pid=123, creation_filetime_100ns=456, role="helper"),
        ),
        observed_total=1,
        observed_active=1,
        identity_uncertain=False,
        failure_codes=(),
        observed_at=h.clock.now(),
    )
    data.update(changes)
    return im.IntakeOwner(**data)


def record(h, adapter, command, value, version=1):
    return h.store.record_intake_owner(
        adapter,
        h.meta(),
        im.IntakeOwnerObservation(
            expected_version=version,
            operation_id=command.operation_id,
            source_generation=command.source_generation,
            owner_boot_id=command.owner_boot_id,
            owner=value,
        ),
    )


def exit_proof(h, command, value=None, **changes):
    data = dict(
        operation_id=command.operation_id,
        owner_boot_id=command.owner_boot_id,
        owner_id=value.owner_id if value else identity(),
        source_generation=1,
        state="stopped" if value else "no_process_created",
        births=tuple(
            im.IntakeBirthExit(birth=b, signaled=True, waited=True, handle_closed=True)
            for b in value.births
        )
        if value
        else (),
        job_active_zero=True,
        io_joined=True,
        owned_handles_closed=True,
        failure_codes=(),
        observed_at=h.clock.now(),
    )
    data.update(changes)
    return im.IntakeExitProof(**data)


def test_admission_is_durable_idempotent_and_holds_global_slot(intake):
    h, _, adapter, _ = intake
    observed, command = prepare(intake)
    meta = h.meta()
    result = h.store.begin_intake_snapshot(adapter, meta, command)
    assert result.operation_id == command.operation_id and result.version == 1
    assert h.store.begin_intake_snapshot(adapter, meta, command) == result
    view = h.store.get_intake_operation(adapter, command.operation_id)
    assert view.admission == command and view.state == "admitted" and view.owner is None
    package = h.store.get_intake(adapter, observed.package_id)
    assert (
        package.state == "snapshotting" and package.operation_id == command.operation_id
    )
    with pytest.raises(m.WorkflowError, match="owner_unresolved"):
        h.store.begin_intake_snapshot(
            adapter,
            h.meta(),
            command.model_copy(
                update={
                    "expected_version": package.version,
                    "operation_id": identity(),
                    "snapshot_id": identity(),
                }
            ),
        )
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    recovered = h.store.get_intake_operation(adapter, command.operation_id)
    assert recovered.state == "unknown" and recovered.admission == command
    assert h.store.get_intake(adapter, observed.package_id).state == "unknown_owner"


def test_elapsed_clock_without_complete_second_observation_is_not_stable(intake):
    h, _, adapter, _ = intake
    _, command = prepare(intake, stable=False)
    h.clock.value += timedelta(seconds=100)
    with pytest.raises(m.WorkflowError, match="intake_not_stable"):
        h.store.begin_intake_snapshot(adapter, h.meta(), command)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_generation", 2),
        ("grouping_revision", 2),
        ("root_config_revision", 2),
        ("policy_revision", 2),
        ("owner_boot_id", "33333333-3333-4333-8333-333333333333"),
    ],
)
def test_admission_checks_all_persisted_bindings(intake, field, value):
    h, _, adapter, _ = intake
    _, command = prepare(intake)
    with pytest.raises(m.WorkflowError, match="binding_changed"):
        h.store.begin_intake_snapshot(
            adapter, h.meta(), command.model_copy(update={field: value})
        )


def test_owner_cannot_forget_known_birth_or_switch_owner(intake):
    h, _, adapter, _ = intake
    _, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    known = owner(h, command)
    result = record(h, adapter, command, known)
    for replacement in (
        known.model_copy(update={"births": ()}),
        known.model_copy(update={"owner_id": identity()}),
    ):
        with pytest.raises(m.WorkflowError, match="owner_evidence_changed"):
            record(h, adapter, command, replacement, result.version)
    assert h.store.get_intake_operation(adapter, command.operation_id).owner == known


def test_uncertainty_is_sticky_and_clean_exit_cannot_release_slot(intake):
    h, _, adapter, _ = intake
    _, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    uncertain = owner(
        h, command, identity_uncertain=True, failure_codes=("open_failed",)
    )
    recorded = record(h, adapter, command, uncertain)
    with pytest.raises(m.WorkflowError, match="owner_evidence_changed"):
        record(
            h,
            adapter,
            command,
            uncertain.model_copy(
                update={"identity_uncertain": False, "failure_codes": ()}
            ),
            recorded.version,
        )
    with pytest.raises(m.WorkflowError, match="owner_unresolved"):
        h.store.finish_intake_snapshot(
            adapter,
            h.meta(),
            im.IntakeSnapshotResult(
                expected_version=recorded.version,
                operation_id=command.operation_id,
                source_generation=1,
                outcome="failed",
                reason="copy_failed",
                snapshot=None,
                exit_proof=exit_proof(h, command, uncertain),
            ),
        )


def test_cancel_keeps_owner_then_closed_failed_result_releases_slot(intake):
    h, _, adapter, _ = intake
    observed, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    known = owner(h, command)
    recorded = record(h, adapter, command, known)
    cancelled = h.store.cancel_intake_snapshot(
        h.operator,
        h.meta(),
        im.IntakeCancel(
            expected_version=recorded.version,
            operation_id=command.operation_id,
            reason="cancelled",
        ),
    )
    view = h.store.get_intake_operation(adapter, command.operation_id)
    assert (
        not view.publication_allowed
        and view.owner == known
        and view.state == "stopping"
    )
    done = h.store.finish_intake_snapshot(
        adapter,
        h.meta(),
        im.IntakeSnapshotResult(
            expected_version=cancelled.version,
            operation_id=command.operation_id,
            source_generation=1,
            outcome="cancelled",
            reason="cancelled",
            snapshot=None,
            exit_proof=exit_proof(h, command, known),
        ),
    )
    assert done.status == "cancelled"
    assert h.store.get_intake(adapter, observed.package_id).operation_id is None


def test_only_original_owner_can_report_but_operator_can_read(intake):
    h, _, adapter, _ = intake
    _, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    other = h.component("intake_adapter")[2]
    for actor in (other, h.operator):
        with pytest.raises(m.WorkflowError, match="forbidden"):
            record(h, actor, command, owner(h, command))
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.get_intake_operation(other, command.operation_id)
    assert (
        h.store.get_intake_operation(h.operator, command.operation_id).admission
        == command
    )
