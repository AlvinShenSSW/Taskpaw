"""Source generation is checked at actual public consumers, not just intake UI."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_approvals import (
    archive,
    archive_receipt,
    cleanup_command,
    context_command,
)
from taskpaw_v3.tests.test_workflow_films import (
    claim,
    complete_return,
    evidence,
    film,
    final_manifest,
    result_command,
    skip_compute,
)
from taskpaw_v3.tests.test_workflow_intake_publication import snapshot_fixture
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m

intake = fixtures.intake


def dirty(h, adapter, observed):
    view = h.store.get_intake(adapter, observed.package_id)
    h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": view.version,
                "coverage": "partial",
                "observed_at": h.clock.now(),
            }
        ),
    )


def prepared_source(intake):
    h, adapter, root, observed, command = snapshot_fixture(
        intake, eligible=True, allow_test_reuse=True
    )
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    source = command.snapshot.manifests[0]
    complete_return(h, source.film_id)
    claimed = claim(h, source.film_id, "scrape")
    final = final_manifest(h, source.film_id, source, claimed)
    h.store.put_final_manifest(
        claimed[1],
        h.meta(),
        m.FinalManifestPut(
            film_id=source.film_id,
            expected_version=h.store.get_film(h.operator, source.film_id).film.version,
            attempt_id=claimed[2].attempt_id,
            manifest=final,
        ),
    )
    h.store.finish_attempt(
        claimed[1],
        h.meta(),
        result_command(h, claimed, output_hash=final.manifest_hash),
    )
    context = context_command(h, source.film_id, source, final)
    saved = h.store.put_approval_context(h.operator, h.meta(), context)
    approval = m.ApprovalCreate(
        film_id=source.film_id,
        expected_version=saved.version,
        approval_id=identity(),
        context_revision=saved.context_revision,
        context_hash=saved.context_hash,
        binding=context.binding,
        expires_at=h.clock.now() + timedelta(hours=1),
    )
    h.store.approve(h.operator, h.meta(), approval)
    return h, adapter, observed, source, final, context, approval


def test_source_change_blocks_claim_before_a_new_input_revision_exists(intake):
    h, adapter, _, observed, command = snapshot_fixture(
        intake, eligible=True, allow_test_reuse=True
    )
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    source = command.snapshot.manifests[0]
    skip_compute(h, source.film_id)
    dirty(h, adapter, observed)
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        claim(h, source.film_id)
    assert h.store.get_film(h.operator, source.film_id).film.current_input_revision == 1


def test_existing_attempt_result_survives_source_change_but_next_claim_is_blocked(
    intake,
):
    from taskpaw_v3.tests.test_workflow_films import receipt

    h, adapter, _, observed, command = snapshot_fixture(
        intake, eligible=True, allow_test_reuse=True
    )
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    film_id = command.snapshot.manifests[0].film_id
    skip_compute(h, film_id)
    claimed = claim(h, film_id)
    assert (
        h.store.get_input_manifest(claimed[1], film_id, 1)
        == command.snapshot.manifests[0]
    )
    outsider = h.component("receiver_adapter")[2]
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.get_input_manifest(outsider, film_id, 1)
    dirty(h, adapter, observed)
    proof = receipt(h, claimed)
    h.store.accept_artifact_receipt(claimed[1], h.meta(), proof)
    h.store.finish_attempt(
        claimed[1],
        h.meta(),
        result_command(
            h,
            claimed,
            output_hash=proof.output_manifest_hash,
            receipt_id=proof.receipt_id,
        ),
    )
    assert (
        next(
            s
            for s in h.store.get_film(h.operator, film_id).stages
            if s.stage == "return"
        ).status
        == "succeeded"
    )
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        claim(h, film_id, "scrape")


def test_legacy_manifest_command_cannot_replace_managed_input(intake):
    h, adapter, _, _, command = snapshot_fixture(intake, eligible=True)
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    source = command.snapshot.manifests[0]
    payload = source.model_dump(mode="json")
    payload.update(revision=2, source_binding=None)
    payload["manifest_hash"] = m.manifest_digest(payload)
    manifest = m.InputManifest.model_validate(payload)
    with pytest.raises(m.WorkflowError, match="managed_snapshot_required"):
        h.store.put_input_manifest(
            adapter,
            h.meta(),
            m.InputManifestPut(
                film_id=source.film_id,
                expected_version=h.store.get_film(adapter, source.film_id).film.version,
                manifest=manifest,
            ),
        )


def test_dirty_source_cannot_reapprove_or_replace_context(intake):
    h, adapter, observed, _, _, context, approval = prepared_source(intake)
    dirty(h, adapter, observed)
    film = h.store.get_film(h.operator, approval.film_id).film
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        h.store.put_approval_context(
            h.operator,
            h.meta(),
            context.model_copy(
                update={
                    "expected_version": film.version,
                    "expected_context_revision": film.current_context_revision,
                }
            ),
        )
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        h.store.approve(
            h.operator,
            h.meta(),
            approval.model_copy(
                update={"expected_version": film.version, "approval_id": identity()}
            ),
        )
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        archive(h, approval)


def test_archive_fact_survives_dirty_source_and_independent_workcopy_can_clean(intake):
    # F02-P1 N04: after an accepted receipt for this approval, a changed INBOX
    # must not strand input cleanup; exactness moves to each member's handle.
    h, adapter, observed, _, final, _, approval = prepared_source(intake)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    dirty(h, adapter, observed)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    started = h.store.begin_cleanup(h.operator, h.meta(), command)
    assert started.status == "admitted"
    first, *rest = command.manifest.members
    result = h.store.record_cleanup_result(
        cleaner,
        h.meta(),
        m.CleanupResult(
            operation_id=command.operation_id,
            expected_version=started.version,
            items=(
                m.CleanupItemResult(
                    item_id=first.item_id,
                    status="deleted",
                    evidence=evidence(h, cleaner.principal_id),
                    error_code=None,
                    recorded_at=h.clock.now(),
                ),
                *(
                    m.CleanupItemResult(
                        item_id=item.item_id,
                        status="blocked_changed",
                        evidence=evidence(h, cleaner.principal_id, status="failed"),
                        error_code="content_changed",
                        recorded_at=h.clock.now(),
                    )
                    for item in rest
                ),
            ),
        ),
    )
    assert result.status == "running"
    view = h.store.film_workflow(h.operator, approval.film_id)
    (cleanup,) = view.cleanup_operations
    assert {i.item_id: i.status for i in cleanup.items} == {
        first.item_id: "deleted",
        **{item.item_id: "blocked_changed" for item in rest},
    }
    assert view.cleanup_input_status != "succeeded"
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.record_cleanup_result(
            cleaner,
            h.meta(),
            m.CleanupResult(
                operation_id=command.operation_id,
                expected_version=result.version,
                items=(
                    m.CleanupItemResult(
                        item_id=first.item_id,
                        status="failed",
                        evidence=evidence(h, cleaner.principal_id, status="failed"),
                        error_code="retry",
                        recorded_at=h.clock.now(),
                    ),
                ),
            ),
        )
    accepted = h.store.begin_cleanup(
        h.operator,
        h.meta(),
        cleanup_command(h, approval, receipt, "workcopy", cleaner.principal_id),
    )
    assert accepted.status == "admitted"


def test_dirty_source_input_cleanup_still_refuses_a_shared_original(intake):
    h, adapter, observed, _, final, _, approval = prepared_source(intake)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    dirty(h, adapter, observed)
    film(h)  # Same fixture source object, retained by a film with no archive.
    _, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.begin_cleanup(h.operator, h.meta(), command)
    with pytest.raises(m.WorkflowError, match="not_found"):
        h.store.get_operation(h.operator, command.operation_id)


def test_receipt_of_another_approval_does_not_relax_input_source_check(intake):
    h, adapter, observed, _, final, _, approval = prepared_source(intake)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    current = h.store.get_film(h.operator, approval.film_id).film
    other = approval.model_copy(
        update={"approval_id": identity(), "expected_version": current.version}
    )
    h.store.approve(h.operator, h.meta(), other)
    dirty(h, adapter, observed)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    with pytest.raises(m.WorkflowError, match="source_not_admissible"):
        h.store.begin_cleanup(
            h.operator,
            h.meta(),
            cleanup_command(h, other, receipt, "input", cleaner.principal_id),
        )


@pytest.mark.parametrize("change", ["revoke", "expire"])
def test_workcopy_exception_cannot_bypass_revocation_or_expiry(intake, change):
    h, adapter, observed, _, final, _, approval = prepared_source(intake)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    dirty(h, adapter, observed)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    if change == "revoke":
        h.store.revoke_approval(
            h.operator,
            h.meta(),
            m.ApprovalRevocation(
                approval_id=approval.approval_id,
                expected_version=1,
                reason="synthetic revoked",
            ),
        )
    else:
        h.clock.value += timedelta(hours=2)
    with pytest.raises(m.WorkflowError, match="approval_revoked|approval_expired"):
        h.store.begin_cleanup(
            h.operator,
            h.meta(),
            cleanup_command(h, approval, receipt, "workcopy", cleaner.principal_id),
        )
