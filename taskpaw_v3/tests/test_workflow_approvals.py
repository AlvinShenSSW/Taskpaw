"""One approval binds exact output and both cleanup scopes; facts outlive it."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests.test_workflow_films import evidence, fixture, ready_film
from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.store import WorkflowStore


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path / "approval.db")
    try:
        yield value
    finally:
        value.store.close()


def context_command(h, film_id, source, final):
    data = fixture("approval-context-v1.json")
    binding = data["binding"]
    binding.update(
        film_id=film_id,
        input_hash=source.manifest_hash,
        final_hash=final.manifest_hash,
        postprocess_digest=m.digest(final.postprocess),
    )
    binding["input_cleanup"]["source_manifest_hash"] = source.manifest_hash
    binding["workcopy_cleanup"]["source_manifest_hash"] = final.manifest_hash
    binding["input_cleanup_hash"] = m.digest(binding["input_cleanup"])
    binding["workcopy_cleanup_hash"] = m.digest(binding["workcopy_cleanup"])
    for proof in data["observations"].values():
        if isinstance(proof, dict):
            proof["producer_id"] = h.operator.principal_id
    current = h.store.get_film(h.operator, film_id).film
    return m.ApprovalContextPut(
        film_id=film_id,
        expected_version=current.version,
        expected_context_revision=current.current_context_revision or 0,
        binding=m.ApprovalBinding.model_validate(binding),
        observations=m.ApprovalObservations.model_validate(data["observations"]),
    )


def prepared(h):
    film_id, source, final = ready_film(h)
    cmd = context_command(h, film_id, source, final)
    result = h.store.put_approval_context(h.operator, h.meta(), cmd)
    approval = m.ApprovalCreate(
        film_id=film_id,
        expected_version=result.version,
        approval_id=identity(),
        context_revision=result.context_revision,
        context_hash=result.context_hash,
        binding=cmd.binding,
        expires_at=h.clock.now() + timedelta(hours=1),
    )
    h.store.approve(h.operator, h.meta(), approval)
    return source, final, cmd, approval


def archive(h, approval):
    grant, _, auth, _ = h.component("archive_adapter")
    command = m.ArchiveAdmission(
        film_id=approval.film_id,
        operation_id=identity(),
        expected_version=h.store.get_film(h.operator, approval.film_id).film.version,
        approval_id=approval.approval_id,
        binding_hash=m.digest(approval.binding),
        adapter_id=grant.principal_id,
    )
    result = h.store.begin_archive(h.operator, h.meta(), command)
    return command, auth, result


def archive_receipt(h, admitted, final, approval):
    command, auth, _ = admitted
    directory = approval.binding.target.native_identity.model_copy(
        update={"object_id": "new-committed-film-object"}
    )
    return m.ArchiveReceipt(
        receipt_id=identity(),
        operation_id=command.operation_id,
        approval_id=approval.approval_id,
        producer_id=auth.principal_id,
        producer_version="synthetic-1",
        target=approval.binding.target,
        target_directory_identity=directory,
        final_manifest_hash=final.manifest_hash,
        items=tuple(
            m.VerifiedItem(
                item_id=item.item_id,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                evidence=evidence(h, auth.principal_id, kind="nas_hash"),
            )
            for item in final.items
        ),
        commit_marker="f" * 64,
        durable_reference=identity(),
        accepted_at=h.clock.now(),
    )


def cleanup_command(h, approval, receipt, scope, producer):
    manifest = (
        approval.binding.input_cleanup
        if scope == "input"
        else approval.binding.workcopy_cleanup
    )
    proof = evidence(h, producer, kind="source_recheck")
    writer = proof.model_copy(
        update={"summary": m.EvidenceSummary(active_writers=False)}
    )
    observation = m.SourceObservation(
        manifest_hash=m.digest(manifest),
        membership_digest=m.digest(
            tuple(sorted(manifest.members, key=lambda member: member.item_id))
        ),
        items=manifest.members,
        writer_check=writer,
        content_check=proof,
        identity_check=proof,
        membership_check=proof,
        shared_references_released=proof,
    )
    return m.CleanupAdmission(
        film_id=approval.film_id,
        operation_id=identity(),
        approval_id=approval.approval_id,
        archive_receipt_id=receipt.receipt_id,
        scope=scope,
        manifest=manifest,
        manifest_hash=m.digest(manifest),
        source_observation=observation,
        expected_version=h.store.get_film(h.operator, approval.film_id).film.version,
    )


def test_complete_approval_archive_two_cleanups_and_reopen(h):
    source, final, context, approval = prepared(h)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    for scope in ("input", "workcopy"):
        command = cleanup_command(h, approval, receipt, scope, cleaner.principal_id)
        result = h.store.begin_cleanup(h.operator, h.meta(), command)
        h.store.record_cleanup_result(
            cleaner,
            h.meta(),
            m.CleanupResult(
                operation_id=command.operation_id,
                expected_version=result.version,
                items=tuple(
                    m.CleanupItemResult(
                        item_id=item.item_id,
                        status="deleted",
                        evidence=evidence(
                            h, cleaner.principal_id, kind="same_handle_deleted"
                        ),
                        error_code=None,
                        recorded_at=h.clock.now(),
                    )
                    for item in command.manifest.members
                ),
            ),
        )
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    view = h.store.get_film(h.operator, approval.film_id)
    assert {job.stage: job.status for job in view.stages}[
        "cleanup_input"
    ] == "succeeded"
    assert {job.stage: job.status for job in view.stages}[
        "cleanup_workcopy"
    ] == "succeeded"
    assert (
        h.store.get_operation(h.operator, admitted[0].operation_id).archive.status
        == "verified"
    )


@pytest.mark.parametrize(
    "change",
    [
        "metadata",
        "candidate",
        "actor",
        "host",
        "export",
        "root",
        "native",
        "path",
        "absent",
        "input_cleanup",
        "workcopy_cleanup",
    ],
)
def test_context_change_blocks_both_old_admissions_without_erasing_receipt(h, change):
    _, final, context, approval = prepared(h)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    data = context.binding.model_dump(mode="json")
    if change in ("metadata", "candidate"):
        data[change + "_revision"] += 1
    elif change == "actor":
        data["actor_ids"] = [identity()]
    elif change in ("host", "export"):
        data["target"][change + "_identity"] = "changed"
    elif change == "native":
        data["target"]["native_identity"]["object_id"] = "replacement-root"
    elif change == "root":
        data["target"]["root_id"] = data["target"]["native_identity"]["root_id"] = (
            identity()
        )
    elif change == "path":
        data["target"]["relative_path"] = "Different/Film"
    elif change == "absent":
        data["target"]["expected_absent"] = False
    else:
        data[change]["members"] = data[change]["members"][:-1]
        data[change + "_hash"] = m.digest(data[change])
    current = h.store.get_film(h.operator, approval.film_id).film
    update = context.model_copy(
        update={
            "binding": m.ApprovalBinding.model_validate(data),
            "expected_version": current.version,
            "expected_context_revision": current.current_context_revision,
        }
    )
    h.store.put_approval_context(h.operator, h.meta(), update)
    new_version = h.store.get_film(h.operator, approval.film_id).film.version
    with pytest.raises(m.WorkflowError, match="binding_changed"):
        h.store.begin_archive(
            h.operator,
            h.meta(),
            admitted[0].model_copy(
                update={"operation_id": identity(), "expected_version": new_version}
            ),
        )
    with pytest.raises(m.WorkflowError, match="binding_changed"):
        h.store.begin_cleanup(
            h.operator,
            h.meta(),
            cleanup_command(h, approval, receipt, "input", cleaner.principal_id),
        )
    assert (
        h.store.get_operation(h.operator, admitted[0].operation_id).archive.status
        == "verified"
    )


def test_expiry_blocks_new_cleanup_but_accepts_admitted_archive_fact(h):
    _, final, _, approval = prepared(h)
    admitted = archive(h, approval)
    h.clock.value += timedelta(hours=2)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    with pytest.raises(m.WorkflowError, match="approval_expired"):
        h.store.begin_cleanup(
            h.operator,
            h.meta(),
            cleanup_command(h, approval, receipt, "input", cleaner.principal_id),
        )


def test_incomplete_archive_receipt_never_authorizes_cleanup(h):
    _, final, _, approval = prepared(h)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.accept_archive_receipt(
            admitted[1],
            h.meta(),
            receipt.model_copy(update={"items": receipt.items[:-1]}),
        )
    assert (
        h.store.get_operation(h.operator, admitted[0].operation_id).archive.status
        == "admitted"
    )


def test_unknown_context_cas_replay_and_restore_never_revives_old_approval(h):
    _, _, context, approval = prepared(h)
    old = h.store.get_film(h.operator, approval.film_id)
    failed = context.observations.model_copy(
        update={
            "target": context.observations.target.model_copy(
                update={"status": "unknown"}
            )
        }
    )
    command = context.model_copy(
        update={
            "expected_version": old.film.version,
            "expected_context_revision": old.film.current_context_revision,
            "observations": failed,
        }
    )
    meta = h.meta()
    result = h.store.put_approval_context(h.operator, meta, command)
    assert result.status == "blocked"
    assert h.store.put_approval_context(h.operator, meta, command) == result
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        h.store.put_approval_context(h.operator, h.meta(), command)
    with pytest.raises(m.WorkflowError, match="binding_changed"):
        h.store.approve(
            h.operator,
            h.meta(),
            approval.model_copy(
                update={"approval_id": identity(), "expected_version": result.version}
            ),
        )
    restore = context.model_copy(
        update={
            "expected_version": result.version,
            "expected_context_revision": result.context_revision,
        }
    )
    restored = h.store.put_approval_context(h.operator, h.meta(), restore)
    _, _, observer, _ = h.component()
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.put_approval_context(
            observer,
            h.meta(),
            restore.model_copy(update={"expected_version": restored.version}),
        )
    grant, _, _, _ = h.component("archive_adapter")
    with pytest.raises(m.WorkflowError, match="binding_changed"):
        h.store.begin_archive(
            h.operator,
            h.meta(),
            m.ArchiveAdmission(
                film_id=approval.film_id,
                operation_id=identity(),
                expected_version=restored.version,
                approval_id=approval.approval_id,
                binding_hash=m.digest(approval.binding),
                adapter_id=grant.principal_id,
            ),
        )


@pytest.mark.parametrize(
    "change", ["writing", "content", "identity", "membership", "unknown"]
)
def test_changed_source_keeps_archive_fact_and_has_no_cleanup_intent(h, change):
    _, final, _, approval = prepared(h)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    observation = command.source_observation
    if change == "writing":
        observation = observation.model_copy(
            update={
                "writer_check": observation.writer_check.model_copy(
                    update={"summary": m.EvidenceSummary(active_writers=True)}
                )
            }
        )
    elif change == "membership":
        observation = observation.model_copy(update={"items": observation.items[:-1]})
    elif change == "unknown":
        observation = observation.model_copy(
            update={
                "identity_check": observation.identity_check.model_copy(
                    update={"status": "unknown"}
                )
            }
        )
    else:
        member = observation.items[0]
        altered = (
            member.model_copy(update={"sha256": "0" * 64})
            if change == "content"
            else member.model_copy(
                update={
                    "native_identity": member.native_identity.model_copy(
                        update={"object_id": "new-file-same-path"}
                    )
                }
            )
        )
        observation = observation.model_copy(
            update={"items": (altered, *observation.items[1:])}
        )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.begin_cleanup(
            h.operator,
            h.meta(),
            command.model_copy(update={"source_observation": observation}),
        )
    with pytest.raises(m.WorkflowError, match="not_found"):
        h.store.get_operation(h.operator, command.operation_id)
    assert (
        h.store.get_operation(h.operator, admitted[0].operation_id).archive.status
        == "verified"
    )


def test_cleanup_cannot_expand_scope_and_records_partial_results_after_expiry(h):
    _, final, _, approval = prepared(h)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    intent = h.store.begin_cleanup(h.operator, h.meta(), command)
    h.clock.value += timedelta(hours=2)
    item = m.CleanupItemResult(
        item_id=identity(),
        status="deleted",
        evidence=evidence(h, cleaner.principal_id),
        error_code=None,
        recorded_at=h.clock.now(),
    )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.record_cleanup_result(
            cleaner,
            h.meta(),
            m.CleanupResult(
                operation_id=command.operation_id, expected_version=1, items=(item,)
            ),
        )
    result = h.store.record_cleanup_result(
        cleaner,
        h.meta(),
        m.CleanupResult(
            operation_id=command.operation_id,
            expected_version=intent.version,
            items=(
                item.model_copy(
                    update={"item_id": command.manifest.members[0].item_id}
                ),
            ),
        ),
    )
    assert result.status == "running"
    assert (
        h.store.get_film(h.operator, approval.film_id).stages[-2].status != "succeeded"
    )


def test_shared_original_waits_for_other_film_archive_and_approval(h):
    from taskpaw_v3.tests.test_workflow_films import film

    _, final, _, approval = prepared(h)
    film(h)  # Fixture reuses the exact source object identity, not just its name.
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.begin_cleanup(
            h.operator,
            h.meta(),
            cleanup_command(h, approval, receipt, "input", cleaner.principal_id),
        )


@pytest.mark.parametrize(
    ("relation", "blocked"),
    [
        ("null", True),
        ("other_scheme", True),
        ("other_volume", True),
        ("same_native_other_path", True),
        ("different_object", False),
        ("null_other_path", False),
        ("null_other_root", False),
    ],
)
def test_cleanup_checks_unknown_same_location_without_merging_proved_distinct(
    h, relation, blocked
):
    from taskpaw_v3.tests.test_workflow_films import film, input_manifest

    _, final, _, approval = prepared(h)
    other_id, _, _, intake = film(h, intake=False)

    def change(data):
        for item in data["items"]:
            if relation in {"null", "null_other_path", "null_other_root"}:
                item["native_identity"] = None
            elif relation == "other_scheme":
                item["native_identity"]["scheme"] = "export_object_id"
            elif relation == "other_volume":
                item["native_identity"]["volume_or_export_id"] = "other-domain"
            elif relation == "different_object":
                item["native_identity"]["object_id"] += "-different"
            if relation in {"same_native_other_path", "null_other_path"}:
                item["relative_path"] = "Renamed/" + item["relative_path"]
            elif relation == "null_other_root":
                item["source_root_id"] = identity()
            else:
                # Controlled Windows locations compare case-insensitively.
                item["relative_path"] = item["relative_path"].upper()

    other = input_manifest(other_id, change=change)
    h.store.put_input_manifest(
        intake,
        h.meta(),
        m.InputManifestPut(film_id=other_id, expected_version=1, manifest=other),
    )
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    if blocked:
        with pytest.raises(m.WorkflowError, match="precondition_failed"):
            h.store.begin_cleanup(h.operator, h.meta(), command)
    else:
        assert h.store.begin_cleanup(h.operator, h.meta(), command).status == "admitted"
