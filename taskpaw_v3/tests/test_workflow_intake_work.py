"""The local helper request comes only from the admitted durable operation."""

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_operations import prepare
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.intake_protocol import ProbeArtifact

intake = fixtures.intake


def artifact():
    return ProbeArtifact(
        executable="C:/synthetic/ffprobe.exe",
        sha256="0" * 64,
        size_bytes=1,
        version="synthetic-test",
    )


def test_local_request_contains_only_current_admitted_configuration_and_revisions(
    intake,
):
    h, grant, adapter, root = intake
    observed, admitted = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), admitted)
    owner_id = identity()
    work = h.store.get_intake_work(
        adapter, admitted.operation_id, owner_id=owner_id, ffprobe=artifact()
    )
    assert (
        work.admission == admitted
        and work.root == root
        and work.observation == observed
    )
    assert work.owner_id == owner_id and work.producer_id == grant.principal_id
    assert work.stable_started_at < observed.observed_at
    assert [(v.film_id, v.input_revision) for v in work.input_revisions] == [
        (observed.grouping.film_groups[0].film_id, 1)
    ]
    assert h.store.get_intake_configuration(adapter, root.root_id) == root
    assert h.store.get_intake_observation(adapter, observed.package_id) == observed


@pytest.mark.parametrize("actor", ["operator", "other_intake", "observer", "receiver"])
def test_execution_request_is_only_for_the_original_intake_principal(intake, actor):
    h, _, adapter, _ = intake
    _, admitted = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), admitted)
    proof = (
        h.operator
        if actor == "operator"
        else h.component(
            {
                "other_intake": "intake_adapter",
                "observer": "observer",
                "receiver": "receiver_adapter",
            }[actor]
        )[2]
    )
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.get_intake_work(
            proof, admitted.operation_id, owner_id=identity(), ffprobe=artifact()
        )


def test_read_summary_authority_does_not_disclose_private_configuration(intake):
    h, _, adapter, root = intake
    observed, _ = prepare(intake)
    observer = h.component("observer")[2]
    assert (
        h.store.get_intake(observer, observed.package_id).package_id
        == observed.package_id
    )
    for proof in (observer, h.component("intake_adapter")[2]):
        with pytest.raises(m.WorkflowError, match="forbidden"):
            h.store.get_intake_configuration(proof, root.root_id)
        with pytest.raises(m.WorkflowError, match="forbidden"):
            h.store.get_intake_observation(proof, observed.package_id)
    assert h.store.get_intake_configuration(h.operator, root.root_id) == root


@pytest.mark.parametrize("change", ["cancel", "source", "owner", "config"])
def test_stale_or_already_started_admission_cannot_issue_another_launch_request(
    intake, change
):
    from taskpaw_v3.tests.test_workflow_intake_operations import owner, record

    h, _, adapter, root = intake
    observed, admitted = prepare(intake)
    begun = h.store.begin_intake_snapshot(adapter, h.meta(), admitted)
    if change == "cancel":
        h.store.cancel_intake_snapshot(
            h.operator,
            h.meta(),
            im.IntakeCancel(
                expected_version=begun.version,
                operation_id=admitted.operation_id,
                reason="cancelled",
            ),
        )
    elif change == "source":
        view = h.store.get_intake(adapter, observed.package_id)
        h.store.observe_intake(
            adapter,
            h.meta(),
            observed.model_copy(
                update={"expected_package_version": view.version, "coverage": "partial"}
            ),
        )
    elif change == "config":
        h.store.configure_intake_root(
            h.operator, h.meta(), root.model_copy(update={"expected_version": 1})
        )
    else:
        record(h, adapter, admitted, owner(h, admitted))
    with pytest.raises(m.WorkflowError, match="intake_not_launchable"):
        h.store.get_intake_work(
            adapter, admitted.operation_id, owner_id=identity(), ffprobe=artifact()
        )
