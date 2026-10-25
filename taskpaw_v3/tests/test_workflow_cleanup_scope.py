"""Workcopy cleanup never authorizes a retained original through a new label."""

import sqlite3
from datetime import timedelta

import pytest

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
    input_manifest,
    result_command,
)
from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.store import WorkflowStore


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "cleanup-scope.db")
    try:
        yield harness
    finally:
        harness.store.close()


def archived(h, *, relabel=None):
    film_id, source, _, _ = film(h)
    original = next(item for item in source.items if item.role == "main_video")
    complete_return(h, film_id)
    claimed = claim(h, film_id, "scrape")

    def change(data):
        if relabel is None:
            return
        main = next(item for item in data["items"] if item["role"] == "main_video")
        # The final item ID stays different: it cannot distinguish the object.
        assert main["item_id"] != original.item_id
        main.update(
            source_root_id=original.source_root_id,
            relative_path=original.relative_path,
            native_identity=original.native_identity.model_dump(mode="json"),
            size_bytes=original.size_bytes,
            sha256=original.sha256,
        )
        if relabel == "declared_output_root_is_input":
            data["output_root_id"] = original.source_root_id
            for item in data["items"]:
                item["source_root_id"] = original.source_root_id
                item["native_identity"]["root_id"] = original.source_root_id

    final = final_manifest(h, film_id, source, claimed, change=change)
    h.store.put_final_manifest(
        claimed[1],
        h.meta(),
        m.FinalManifestPut(
            film_id=film_id,
            expected_version=h.store.get_film(h.operator, film_id).film.version,
            attempt_id=claimed[2].attempt_id,
            manifest=final,
        ),
    )
    h.store.finish_attempt(
        claimed[1],
        h.meta(),
        result_command(h, claimed, output_hash=final.manifest_hash),
    )
    context = context_command(h, film_id, source, final).model_dump(mode="json")
    workcopy = m.CleanupManifest(
        scope="workcopy",
        source_manifest_hash=final.manifest_hash,
        members=tuple(
            m.CleanupMember(
                item_id=item.item_id,
                root_id=item.source_root_id,
                relative_path=item.relative_path,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                native_identity=item.native_identity,
            )
            for item in final.items
        ),
    )
    context["binding"]["workcopy_cleanup"] = workcopy.model_dump(mode="json")
    context["binding"]["workcopy_cleanup_hash"] = m.digest(workcopy)
    context = m.ApprovalContextPut.model_validate(context)
    accepted = h.store.put_approval_context(h.operator, h.meta(), context)
    approval = m.ApprovalCreate(
        film_id=film_id,
        expected_version=accepted.version,
        approval_id=identity(),
        context_revision=accepted.context_revision,
        context_hash=accepted.context_hash,
        binding=context.binding,
        expires_at=h.clock.now() + timedelta(hours=1),
    )
    h.store.approve(h.operator, h.meta(), approval)
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    return source, final, approval, admitted, receipt, cleaner


def assert_refused_without_intent(h, prepared, command):
    _, _, approval, admitted, _, cleaner = prepared
    before = h.store.get_film(h.operator, approval.film_id)
    archive_before = h.store.get_operation(h.operator, admitted[0].operation_id)
    with sqlite3.connect(h.path) as conn:
        counts = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("operations", "cleanup_items", "receipts", "attempts")
        )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.begin_cleanup(h.operator, h.meta(), command)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    with pytest.raises(m.WorkflowError, match="not_found"):
        h.store.get_operation(cleaner, command.operation_id)
    assert h.store.get_film(h.operator, approval.film_id) == before
    assert h.store.get_operation(h.operator, admitted[0].operation_id) == archive_before
    assert archive_before.archive.status == "verified"
    with sqlite3.connect(h.path) as conn:
        assert counts == tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("operations", "cleanup_items", "receipts", "attempts")
        )


@pytest.mark.parametrize(
    "relabel", ["mixed_root_final", "declared_output_root_is_input"]
)
def test_original_cannot_be_relabelled_workcopy_after_shared_input_refusal(h, relabel):
    prepared = archived(h, relabel=relabel)
    _, final, approval, _, receipt, cleaner = prepared
    other_id, _, _, _ = film(h)
    other_before = h.store.get_film(h.operator, other_id)
    assert other_before.context is None
    assert (
        next(stage for stage in other_before.stages if stage.stage == "archive").status
        == "pending"
    )
    assert all(item.source_root_id == final.output_root_id for item in final.items) is (
        relabel == "declared_output_root_is_input"
    )
    for scope in ("input", "workcopy"):
        command = cleanup_command(h, approval, receipt, scope, cleaner.principal_id)
        assert_refused_without_intent(h, prepared, command)
    assert h.store.get_film(h.operator, other_id) == other_before


def test_own_retained_input_is_not_workcopy_even_after_its_archive(h):
    prepared = archived(h, relabel="mixed_root_final")
    _, _, approval, _, receipt, cleaner = prepared
    original_cleanup = cleanup_command(
        h, approval, receipt, "input", cleaner.principal_id
    )
    # No other film needs this input, so the separate input-scope gate can admit it.
    assert (
        h.store.begin_cleanup(h.operator, h.meta(), original_cleanup).status
        == "admitted"
    )
    workcopy = cleanup_command(h, approval, receipt, "workcopy", cleaner.principal_id)
    assert_refused_without_intent(h, prepared, workcopy)


def test_independent_workcopy_remains_available_while_shared_original_waits(h):
    prepared = archived(h)
    _, _, approval, _, receipt, cleaner = prepared
    film(h)
    assert_refused_without_intent(
        h,
        prepared,
        cleanup_command(h, approval, receipt, "input", cleaner.principal_id),
    )
    command = cleanup_command(h, approval, receipt, "workcopy", cleaner.principal_id)
    meta = h.meta()
    admitted = h.store.begin_cleanup(h.operator, meta, command)
    assert admitted.status == "admitted"
    assert h.store.begin_cleanup(h.operator, meta, command) == admitted
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert (
        h.store.get_operation(cleaner, command.operation_id).cleanup.manifest
        == command.manifest
    )
    with sqlite3.connect(h.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM cleanup_items WHERE operation_id=?",
            (command.operation_id,),
        ).fetchone()[0] == len(command.manifest.members)


@pytest.mark.parametrize(
    ("relation", "blocked"),
    [
        ("same_native_same_path", True),
        ("same_native_other_path", True),
        ("same_native_other_root", True),
        ("null_same_path", True),
        ("other_scheme_same_path", True),
        ("other_domain_same_path", True),
        ("different_object_same_path", False),
        ("different_object_other_root", False),
        ("null_other_path", False),
        ("null_other_root", False),
        ("other_domain_other_root", False),
    ],
)
def test_workcopy_checks_retained_input_arriving_after_approval(h, relation, blocked):
    prepared = archived(h)
    _, final, approval, _, receipt, cleaner = prepared
    main = next(item for item in final.items if item.role == "main_video")
    other_id, _, _, intake = film(h, intake=False)

    def change(data):
        item = next(item for item in data["items"] if item["role"] == "main_video")
        assert item["item_id"] != main.item_id
        item.update(
            source_root_id=main.source_root_id,
            relative_path=main.relative_path.upper(),
            native_identity=main.native_identity.model_dump(mode="json"),
        )
        if "other_root" in relation:
            item["source_root_id"] = item["native_identity"]["root_id"] = identity()
        if "other_path" in relation or relation == "same_native_other_root":
            item["relative_path"] = "renamed/" + item["relative_path"]
        if relation.startswith("null"):
            item["native_identity"] = None
        elif relation.startswith("other_scheme"):
            item["native_identity"]["scheme"] = "export_object_id"
        elif relation.startswith("other_domain"):
            item["native_identity"]["volume_or_export_id"] = "another-domain"
        elif relation.startswith("different_object"):
            item["native_identity"]["object_id"] = "proved-distinct-object"

    source = input_manifest(other_id, change=change)
    h.store.put_input_manifest(
        intake,
        h.meta(),
        m.InputManifestPut(film_id=other_id, expected_version=1, manifest=source),
    )
    other_before = h.store.get_film(h.operator, other_id)
    command = cleanup_command(h, approval, receipt, "workcopy", cleaner.principal_id)
    if blocked:
        assert_refused_without_intent(h, prepared, command)
    else:
        assert h.store.begin_cleanup(h.operator, h.meta(), command).status == "admitted"
    assert h.store.get_film(h.operator, other_id) == other_before


def test_input_result_releases_only_the_deleted_name_of_a_shared_file(h):
    prepared = archived(h)
    source, _, approval, _, receipt, cleaner = prepared
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    admitted = h.store.begin_cleanup(h.operator, h.meta(), command)
    original = source.items[0]
    other_id, _, _, intake = film(h, intake=False)

    def renamed(data):
        data["items"][0].update(
            native_identity=original.native_identity.model_dump(mode="json"),
            source_root_id=original.source_root_id,
            relative_path="another-name/" + original.relative_path,
        )

    other = input_manifest(other_id, change=renamed)
    h.store.put_input_manifest(
        intake,
        h.meta(),
        m.InputManifestPut(film_id=other_id, expected_version=1, manifest=other),
    )
    h.store.record_cleanup_result(
        cleaner,
        h.meta(),
        m.CleanupResult(
            operation_id=command.operation_id,
            expected_version=admitted.version,
            items=(
                m.CleanupItemResult(
                    item_id=original.item_id,
                    status="deleted",
                    evidence=evidence(h, cleaner.principal_id),
                    error_code=None,
                    recorded_at=h.clock.now(),
                ),
            ),
        ),
    )
    with sqlite3.connect(h.path) as conn:
        assert (
            conn.execute(
                "SELECT retained FROM source_refs WHERE film_id=? AND item_id=?",
                (approval.film_id, original.item_id),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT retained FROM source_refs WHERE film_id=? AND item_id=?",
                (other_id, other.items[0].item_id),
            ).fetchone()[0]
            == 1
        )
