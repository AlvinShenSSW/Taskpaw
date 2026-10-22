"""F02-P1 store additions: adapter reboot, archive failure and workflow reads."""

from datetime import timedelta

import pytest
from pydantic import SecretStr

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_approvals import (
    archive,
    archive_receipt,
    cleanup_command,
    context_command,
    prepared,
)
from taskpaw_v3.tests.test_workflow_films import evidence, film, input_manifest
from taskpaw_v3.tests.test_workflow_intake_fences import prepared_source
from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.auth import authenticate, digest_secret
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake
EXTERNAL = ("archive", "cleanup_input", "cleanup_workcopy")


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path / "f02.db")
    try:
        yield value
    finally:
        value.store.close()


def regrant(h, grant, **changes):
    secret = SecretStr("synthetic-rebooted-component-credential")
    update = {
        "credential_id": identity(),
        "boot_id": identity(),
        "generation": h.store.component_generation(h.operator, grant.principal_id) + 1,
        "secret_digest": digest_secret(secret),
        "expires_at": h.clock.now() + timedelta(days=1),
        **changes,
    }
    command = grant.model_copy(update=update)
    h.store.grant_component(h.operator, h.meta(), command)
    return command, authenticate(h.store, command.credential_id, secret)


def item_result(h, producer, item_id, status="deleted"):
    return m.CleanupItemResult(
        item_id=item_id,
        status=status,
        evidence=evidence(
            h,
            producer,
            kind="same_handle_deleted",
            status="passed" if status in ("deleted", "already_absent") else "failed",
        ),
        error_code=None if status in ("deleted", "already_absent") else status,
        recorded_at=h.clock.now(),
    )


def failure(h, admitted, *, version=1, producer=None):
    command, auth, _ = admitted
    return m.ArchiveFailure(
        operation_id=command.operation_id,
        expected_version=version,
        error_code="filmpaw_failed: target exists",
        evidence=evidence(
            h, producer or auth.principal_id, kind="filmpaw_task", status="failed"
        ),
    )


def stage_status(h, film_id, name):
    return next(
        job.status
        for job in h.store.get_film(h.operator, film_id).stages
        if job.stage == name
    )


def new_approval(h, film_id, source, final):
    context = context_command(h, film_id, source, final)
    saved = h.store.put_approval_context(h.operator, h.meta(), context)
    approval = m.ApprovalCreate(
        film_id=film_id,
        expected_version=saved.version,
        approval_id=identity(),
        context_revision=saved.context_revision,
        context_hash=saved.context_hash,
        binding=context.binding,
        expires_at=h.clock.now() + timedelta(hours=1),
    )
    h.store.approve(h.operator, h.meta(), approval)
    return approval


def archived(h, final, approval):
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    return admitted, receipt


def clean(h, approval, receipt, scope, cleaner):
    command = cleanup_command(h, approval, receipt, scope, cleaner.principal_id)
    started = h.store.begin_cleanup(h.operator, h.meta(), command)
    return h.store.record_cleanup_result(
        cleaner,
        h.meta(),
        m.CleanupResult(
            operation_id=command.operation_id,
            expected_version=started.version,
            items=tuple(
                item_result(h, cleaner.principal_id, item.item_id)
                for item in command.manifest.members
            ),
        ),
    )


def test_rebooted_cleanup_adapter_keeps_one_owner_and_continues(h):
    _, final, _, approval = prepared(h)
    _, receipt = archived(h, final, approval)
    grant, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", grant.principal_id)
    started = h.store.begin_cleanup(h.operator, h.meta(), command)
    assert h.store.component_generation(h.operator, grant.principal_id) == 1
    assert h.store.component_generation(h.operator, identity()) == 0
    rebooted, proof = regrant(h, grant)
    assert h.store.component_generation(h.operator, grant.principal_id) == 2
    with pytest.raises(m.WorkflowError, match="unauthorized"):
        h.store.get_operation(cleaner, command.operation_id)
    first = command.manifest.members[0]
    recorded = h.store.record_cleanup_result(
        proof,
        h.meta(),
        m.CleanupResult(
            operation_id=command.operation_id,
            expected_version=started.version,
            items=(item_result(h, grant.principal_id, first.item_id),),
        ),
    )
    assert recorded.status == "running"
    workcopy = cleanup_command(h, approval, receipt, "workcopy", grant.principal_id)
    assert h.store.begin_cleanup(h.operator, h.meta(), workcopy).status == "admitted"
    job = next(
        stage
        for stage in h.store.get_film(h.operator, approval.film_id).stages
        if stage.stage == "cleanup_workcopy"
    )
    (attempt,) = h.store.list_attempts(h.operator, job.job_id)
    assert attempt.boot_id == rebooted.boot_id
    assert attempt.owner_principal_id == grant.principal_id
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.component_generation(proof, grant.principal_id)


def test_rebooted_archive_adapter_records_failure_of_its_open_operation(h):
    _, _, _, approval = prepared(h)
    admitted = archive(h, approval)
    grant = m.ComponentGrant.model_validate(
        {
            "component_id": identity(),
            "principal_id": admitted[1].principal_id,
            "credential_id": identity(),
            "boot_id": identity(),
            "generation": 1,
            "role": "archive_adapter",
            "secret_digest": "0" * 64,
            "expires_at": h.clock.now() + timedelta(days=1),
        }
    )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        regrant(h, grant)  # A different component cannot take over the principal.
    with h.store._lock:
        component = h.store._conn.execute(
            "SELECT component_id FROM principals WHERE principal_id=?",
            (grant.principal_id,),
        ).fetchone()[0]
    _, proof = regrant(h, grant.model_copy(update={"component_id": component}))
    result = h.store.fail_archive(proof, h.meta(), failure(h, admitted))
    assert result.status == "failed"


@pytest.mark.parametrize("role", ["intake_adapter", "scrape_adapter", "observer"])
def test_other_components_keep_their_boot_on_regrant(h, role):
    grant, _, _, _ = h.component(role)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        regrant(h, grant)
    same_boot, _ = regrant(h, grant, boot_id=grant.boot_id)
    assert same_boot.generation == 2


@pytest.mark.parametrize("change", ["role", "component", "generation", "first"])
def test_rebooted_adapter_still_needs_role_component_and_next_generation(h, change):
    grant, _, _, _ = h.component("archive_adapter")
    update = {
        "role": {"role": "cleanup_adapter"},
        "component": {"component_id": identity()},
        "generation": {"generation": 3},
        "first": {"principal_id": identity(), "generation": 2},
    }[change]
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        regrant(h, grant, **update)
    assert h.store.component_generation(h.operator, grant.principal_id) == 1


def test_failed_archive_is_terminal_until_a_new_approval_archives_again(h):
    source, final, _, approval = prepared(h)
    admitted = archive(h, approval)
    _, _, other, _ = h.component("archive_adapter")
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.fail_archive(
            other, h.meta(), failure(h, admitted, producer=other.principal_id)
        )
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.fail_archive(h.operator, h.meta(), failure(h, admitted))
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.fail_archive(
            admitted[1], h.meta(), failure(h, admitted, producer=other.principal_id)
        )
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        h.store.fail_archive(admitted[1], h.meta(), failure(h, admitted, version=2))
    meta = h.meta()
    command = failure(h, admitted)
    result = h.store.fail_archive(admitted[1], meta, command)
    assert (result.status, result.version) == ("failed", 2)
    assert h.store.fail_archive(admitted[1], meta, command) == result
    operation = h.store.get_operation(h.operator, admitted[0].operation_id).archive
    assert operation.status == "failed"
    assert operation.error_code == "filmpaw_failed: target exists"
    assert operation.evidence == (command.evidence,)
    assert stage_status(h, approval.film_id, "archive") == "failed"
    job = next(
        stage
        for stage in h.store.get_film(h.operator, approval.film_id).stages
        if stage.stage == "archive"
    )
    (attempt,) = h.store.list_attempts(h.operator, job.job_id)
    assert attempt.status == "failed"
    assert attempt.error_code == "filmpaw_failed: target exists"
    assert attempt.ended_at is not None
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.fail_archive(admitted[1], h.meta(), failure(h, admitted, version=2))
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.accept_archive_receipt(
            admitted[1], h.meta(), archive_receipt(h, admitted, final, approval)
        )
    view = h.store.film_workflow(h.operator, approval.film_id)
    assert [(op.status, op.resolved) for op in view.archive_operations] == [
        ("failed", True)
    ]
    renewed = new_approval(h, approval.film_id, source, final)
    again = archive(h, renewed)
    assert again[2].status == "admitted"
    assert stage_status(h, approval.film_id, "archive") == "running"


def test_context_cannot_change_while_archive_is_unresolved(h):
    source, final, _, approval = prepared(h)
    admitted = archive(h, approval)
    before = h.store.get_film(h.operator, approval.film_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.put_approval_context(
            h.operator, h.meta(), context_command(h, approval.film_id, source, final)
        )
    assert h.store.get_film(h.operator, approval.film_id) == before
    h.store.fail_archive(admitted[1], h.meta(), failure(h, admitted))
    saved = h.store.put_approval_context(
        h.operator, h.meta(), context_command(h, approval.film_id, source, final)
    )
    assert saved.status == "ready"


def test_film_workflow_reports_approval_archive_receipt_and_cleanup_items(h):
    _, final, _, approval = prepared(h)
    view = h.store.film_workflow(h.operator, approval.film_id)
    film = h.store.get_film(h.operator, approval.film_id).film
    assert (view.film_id, view.version, view.input_revision) == (
        approval.film_id,
        film.version,
        film.current_input_revision,
    )
    assert view.approval.approval_id == approval.approval_id
    assert view.approval.binding == approval.binding
    assert view.approval.binding_hash == m.digest(approval.binding)
    assert view.approval.expires_at == approval.expires_at
    assert view.approval.revoked is False
    assert view.archive_operations == () and view.cleanup_operations == ()
    assert view.archive_receipt_id is None and view.archive_receipt_approval_id is None
    assert view.package_id is None
    assert (
        view.scrape_status,
        view.archive_status,
        view.cleanup_input_status,
        view.cleanup_workcopy_status,
    ) == ("succeeded", "pending", "pending", "pending")
    admitted = archive(h, approval)
    (operation,) = h.store.film_workflow(
        h.operator, approval.film_id
    ).archive_operations
    assert (
        operation.operation_id,
        operation.approval_id,
        operation.status,
        operation.version,
        operation.resolved,
        operation.error_code,
    ) == (admitted[0].operation_id, approval.approval_id, "admitted", 1, False, None)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    command = cleanup_command(h, approval, receipt, "input", cleaner.principal_id)
    started = h.store.begin_cleanup(h.operator, h.meta(), command)
    first, second = command.manifest.members
    h.store.record_cleanup_result(
        cleaner,
        h.meta(),
        m.CleanupResult(
            operation_id=command.operation_id,
            expected_version=started.version,
            items=(
                item_result(h, cleaner.principal_id, first.item_id, "blocked_writing"),
            ),
        ),
    )
    view = h.store.film_workflow(h.operator, approval.film_id)
    assert view.archive_receipt_id == receipt.receipt_id
    assert view.archive_receipt_approval_id == approval.approval_id
    assert [(op.status, op.resolved) for op in view.archive_operations] == [
        ("verified", True)
    ]
    assert view.archive_status == "succeeded"
    (cleanup,) = view.cleanup_operations
    assert (
        cleanup.operation_id,
        cleanup.approval_id,
        cleanup.scope,
        cleanup.version,
        cleanup.status,
        cleanup.resolved,
    ) == (command.operation_id, approval.approval_id, "input", 2, "running", False)
    assert {i.item_id: i.status for i in cleanup.items} == {
        first.item_id: "blocked_writing",
        second.item_id: "pending",
    }
    assert view.cleanup_input_status == "unknown"
    h.store.revoke_approval(
        h.operator,
        h.meta(),
        m.ApprovalRevocation(
            approval_id=approval.approval_id, expected_version=1, reason="synthetic"
        ),
    )
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert h.store.film_workflow(h.operator, approval.film_id).approval.revoked is True
    _, _, observer, _ = h.component()
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.film_workflow(observer, approval.film_id)
    with pytest.raises(m.WorkflowError, match="not_found"):
        h.store.film_workflow(h.operator, identity())


def test_film_without_approval_has_no_workflow_approval(h):
    film_id, _, _, _ = film(h)
    view = h.store.film_workflow(h.operator, film_id)
    assert view.approval is None
    assert view.scrape_status == "pending"


def test_list_jobs_reports_external_stages_once_dependencies_finish(h):
    _, final, _, approval = prepared(h)
    pending = h.store.list_jobs(h.operator, "pending", EXTERNAL)
    assert [(job.film_id, job.stage) for job in pending] == [
        (approval.film_id, "archive")
    ]
    admitted = archive(h, approval)
    assert h.store.list_jobs(h.operator, "pending", EXTERNAL) == ()
    assert [
        job.stage for job in h.store.list_jobs(h.operator, "running", EXTERNAL)
    ] == ["archive"]
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    assert sorted(
        job.stage for job in h.store.list_jobs(h.operator, "pending", EXTERNAL)
    ) == ["cleanup_input", "cleanup_workcopy"]
    with pytest.raises(m.WorkflowError, match="invalid_request"):
        h.store.list_jobs(h.operator, "pending", ("intake",))


def link_sibling(h, package_id):
    """Link another current film to the package (multi-film snapshot shape)."""
    sibling_id, _, _, adapter = film(h, intake=False)

    def distinct(data):
        for item in data["items"]:
            item["native_identity"]["object_id"] += "-sibling"
            item["relative_path"] = "sibling/" + item["relative_path"]

    h.store.put_input_manifest(
        adapter,
        h.meta(),
        m.InputManifestPut(
            film_id=sibling_id,
            expected_version=1,
            manifest=input_manifest(sibling_id, change=distinct),
        ),
    )
    with h.store._lock:
        conn = h.store._conn
        row = conn.execute(
            "SELECT snapshot_id,source_generation,grouping_revision,created_at "
            "FROM intake_films WHERE package_id=? LIMIT 1",
            (package_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO intake_films (film_id,input_revision,package_id,snapshot_id,"
            "source_generation,grouping_revision,created_at) VALUES (?,?,?,?,?,?,?)",
            (sibling_id, 1, package_id, *row),
        )
        conn.commit()
    return sibling_id


def retirement(h, package_id):
    return m.ArchivedPackageRetirement(
        package_id=package_id,
        expected_version=h.store.get_intake(h.operator, package_id).version,
    )


def test_package_retires_after_every_linked_film_cleaned_input(intake):
    h, _, observed, source, final, _, approval = prepared_source(intake)
    package_id = observed.package_id
    view = h.store.film_workflow(h.operator, source.film_id)
    assert view.package_id == package_id
    assert view.intake_operation_id is not None
    assert view.snapshot_id is not None and view.snapshot_work_path is not None
    (only,) = h.store.package_films(h.operator, package_id)
    assert (only.film_id, only.cleanup_input_status, only.cleanup_workcopy_status) == (
        source.film_id,
        "pending",
        "pending",
    )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.retire_archived_package(h.operator, h.meta(), retirement(h, package_id))
    _, receipt = archived(h, final, approval)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    clean(h, approval, receipt, "input", cleaner)
    stale = retirement(h, package_id)
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        h.store.retire_archived_package(
            h.operator,
            h.meta(),
            stale.model_copy(update={"expected_version": stale.expected_version + 1}),
        )
    _, _, outsider, _ = h.component("archive_adapter")
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.retire_archived_package(outsider, h.meta(), stale)
    retired = h.store.retire_archived_package(h.operator, h.meta(), stale)
    assert retired.status == "retired"
    assert h.store.get_intake(h.operator, package_id).state == "missing"
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.retire_archived_package(h.operator, h.meta(), retirement(h, package_id))
    # Workcopy cleanup still completes after the package record is retired.
    clean(h, approval, receipt, "workcopy", cleaner)
    assert stage_status(h, source.film_id, "cleanup_workcopy") == "succeeded"


def test_sibling_still_in_pipeline_blocks_package_retirement(intake):
    h, _, observed, source, final, _, approval = prepared_source(intake)
    package_id = observed.package_id
    sibling = link_sibling(h, package_id)
    _, receipt = archived(h, final, approval)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    clean(h, approval, receipt, "input", cleaner)
    films = {
        item.film_id: (item.cleanup_input_status, item.cleanup_workcopy_status)
        for item in h.store.package_films(h.operator, package_id)
    }
    assert films == {
        source.film_id: ("succeeded", "pending"),
        sibling: ("pending", "pending"),
    }
    before = h.store.get_intake(h.operator, package_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.retire_archived_package(h.operator, h.meta(), retirement(h, package_id))
    assert h.store.get_intake(h.operator, package_id) == before
