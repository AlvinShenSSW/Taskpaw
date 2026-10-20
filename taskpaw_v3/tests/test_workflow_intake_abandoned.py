"""An intake operation left by an exited controller boot can be settled (T03-P1 D01)."""

from datetime import timedelta

import pytest
from pydantic import SecretStr

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_operations import prepare
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.auth import authenticate, digest_secret
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake


def next_boot_adapter(h, grant):
    """The controller's next boot: same component, new principal and boot."""
    secret = SecretStr("synthetic-next-boot-intake-credential")
    value = m.ComponentGrant(
        component_id=grant.component_id,
        principal_id=identity(),
        credential_id=identity(),
        boot_id=identity(),
        generation=1,
        role="intake_adapter",
        secret_digest=digest_secret(secret),
        expires_at=h.clock.now() + timedelta(days=1),
    )
    h.store.grant_component(h.operator, h.meta(), value)
    return value, authenticate(h.store, value.credential_id, secret)


def abandoned(intake):
    """Admit a snapshot, then 'crash' the controller by reopening the store."""
    h, grant, adapter, root = intake
    observed, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    return observed, command


def settle(h, op, boot_id):
    return h.store.settle_abandoned_intake(
        h.operator,
        h.meta(),
        im.IntakeAbandonedSettlement(
            expected_version=op.version,
            operation_id=op.operation_id,
            current_owner_boot_id=boot_id,
        ),
    )


def test_previous_boot_operation_is_cancelled_and_intake_resumes(intake):
    h, grant, _, root = intake
    observed, command = abandoned(intake)
    nxt, adapter = next_boot_adapter(h, grant)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    assert (op.operation_id, op.owner_boot_id) == (command.operation_id, grant.boot_id)

    assert settle(h, op, nxt.boot_id).status == "cancelled"
    assert h.store.list_unresolved_intake_operations(h.operator) == ()
    view = h.store.get_intake(adapter, observed.package_id)
    assert view.state == "waiting_stability" and view.operation_id is None

    # A fresh stability window and snapshot under the new boot are admitted.
    seen = h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": view.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    h.clock.value += timedelta(seconds=root.policy.stable_seconds)
    stable = h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": seen.version,
                "observed_at": h.clock.now(),
                "trigger": "rescan",
            }
        ),
    )
    again = command.model_copy(
        update={
            "expected_version": stable.version,
            "operation_id": identity(),
            "snapshot_id": identity(),
            "owner_boot_id": nxt.boot_id,
        }
    )
    assert h.store.begin_intake_snapshot(adapter, h.meta(), again).status == "admitted"


def test_settlement_is_idempotent_and_never_reopens(intake):
    h, grant, _, _ = intake
    abandoned(intake)
    nxt, _ = next_boot_adapter(h, grant)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    meta = h.meta()
    command = im.IntakeAbandonedSettlement(
        expected_version=op.version,
        operation_id=op.operation_id,
        current_owner_boot_id=nxt.boot_id,
    )
    first = h.store.settle_abandoned_intake(h.operator, meta, command)
    assert h.store.settle_abandoned_intake(h.operator, meta, command) == first
    later = h.store.get_intake_operation(h.operator, op.operation_id)
    assert later.state == "cancelled" and not later.publication_allowed


def test_same_boot_unknown_keeps_the_t02_blocking_rule(intake):
    h, grant, adapter, _ = intake
    abandoned(intake)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        settle(h, op, grant.boot_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        settle(h, op, identity())  # no intake principal holds that boot
    assert len(h.store.list_unresolved_intake_operations(h.operator)) == 1


def test_only_the_operator_settles_or_lists(intake):
    h, grant, _, _ = intake
    abandoned(intake)
    nxt, adapter = next_boot_adapter(h, grant)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.list_unresolved_intake_operations(adapter)
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.settle_abandoned_intake(
            adapter,
            h.meta(),
            im.IntakeAbandonedSettlement(
                expected_version=op.version,
                operation_id=op.operation_id,
                current_owner_boot_id=nxt.boot_id,
            ),
        )
