"""Persistent film commands, using public credentials and real SQLite."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.tests.test_workflow_workers import enroll
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.store import WorkflowStore

FIXTURES = Path(__file__).parent / "fixtures" / "workflow"
STAGES = (
    "intake",
    "restore",
    "asr",
    "translate",
    "return",
    "scrape",
    "archive",
    "cleanup_input",
    "cleanup_workcopy",
)


@pytest.fixture
def h(tmp_path):
    value = Harness(tmp_path / "films.db")
    try:
        yield value
    finally:
        value.store.close()


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def input_manifest(film_id, *, revision=1, change=None):
    data = fixture("input-manifest-v1.json")
    data.update(film_id=film_id, revision=revision)
    if change:
        change(data)
    data["manifest_hash"] = m.manifest_digest(data)
    return m.InputManifest.model_validate(data)


def evidence(h, producer, *, kind="completed", status="passed"):
    return m.EvidenceRef(
        evidence_id=identity(),
        kind=kind,
        producer_id=producer,
        producer_version="synthetic-1",
        observed_at=h.clock.now(),
        received_at=h.clock.now(),
        payload_digest="b" * 64,
        status=status,
    )


def rows(h, table):
    assert table in {
        "stages",
        "attempts",
        "assignments",
        "operations",
        "receipts",
        "manifests",
        "resource_owners",
        "source_refs",
    }
    with sqlite3.connect(h.path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


def film(h, *, intake=True, required_images=("poster", "folder_cover")):
    grant, _, auth, _ = h.component("intake_adapter")
    policy = m.FilmPolicy(
        policy_revision=1,
        required_stages=STAGES,
        subtitle_requirement="chinese",
        skippable_stages=("restore", "asr", "translate"),
        skip_evidence_kinds=("verified_reuse",),
        required_image_roles=required_images,
        folder_cover_required=True,
    )
    command = m.FilmCreate(
        film_id=identity(),
        intake_session_id=identity(),
        display_name="Synthetic film",
        policy=policy,
    )
    h.store.create_film(auth, h.meta(), command)
    manifest = input_manifest(command.film_id)
    if intake:
        h.store.put_input_manifest(
            auth,
            h.meta(),
            m.InputManifestPut(
                film_id=command.film_id, expected_version=1, manifest=manifest
            ),
        )
    return command.film_id, manifest, grant, auth


def stage(h, film_id, name):
    return next(
        x for x in h.store.get_film(h.operator, film_id).stages if x.stage == name
    )


def skip_compute(h, film_id):
    for name in ("restore", "asr", "translate"):
        job = stage(h, film_id, name)
        h.store.skip_stage(
            h.operator,
            h.meta(),
            m.StageSkip(
                job_id=job.job_id,
                expected_version=job.version,
                reason="Existing validated output",
                policy_revision=1,
                input_hash=job.input_hash,
                evidence=(evidence(h, h.operator.principal_id, kind="verified_reuse"),),
            ),
        )


def tools():
    return m.ToolVersions(
        tool="synthetic",
        tool_version="1",
        model="synthetic",
        model_version="1",
        profile="synthetic",
        profile_revision=1,
        config_digest="c" * 64,
    )


def claim(h, film_id, name="return"):
    grant, _, auth, _ = h.component(
        "receiver_adapter" if name == "return" else "scrape_adapter"
    )
    job = stage(h, film_id, name)
    command = m.ClaimRequest(
        job_id=job.job_id,
        expected_version=job.version,
        attempt_id=identity(),
        operation_id=identity(),
        worker_id=None,
        boot_id=grant.boot_id,
        profile_revision=1,
        config_digest=tools().config_digest,
        resource_id=None,
        tool_versions=tools(),
    )
    result = h.store.claim(auth, h.meta(), command)
    return grant, auth, command, result


def assignment(h, assignment_id):
    return m.AssignmentRecord.model_validate_json(
        next(x for x in rows(h, "assignments") if x["assignment_id"] == assignment_id)[
            "body_json"
        ]
    )


def result_command(
    h,
    claimed,
    *,
    outcome="succeeded",
    output_hash=None,
    receipt_id=None,
    local_owner=None,
):
    grant, auth, cmd, result = claimed
    current = assignment(h, result.assignment_id)
    job = next(x for x in rows(h, "stages") if x["job_id"] == cmd.job_id)
    return m.AttemptResult(
        assignment_id=current.assignment_id,
        expected_version=current.version,
        worker_id=cmd.worker_id,
        boot_id=cmd.boot_id,
        attempt_id=cmd.attempt_id,
        authority_epoch=current.authority_epoch,
        fencing_token=current.fencing_token,
        local_owner=local_owner,
        job_id=cmd.job_id,
        input_hash=job["input_hash"],
        outcome=outcome,
        result_manifest_hash=output_hash,
        artifact_receipt_id=receipt_id,
        evidence=(evidence(h, auth.principal_id),),
        error_code="synthetic_failure" if outcome == "failed" else None,
    )


def receipt(h, claimed):
    _, auth, cmd, _ = claimed
    item_id = identity()
    item_evidence = evidence(h, auth.principal_id, kind="receiver_hash").model_copy(
        update={"summary": m.EvidenceSummary(item_ids=(item_id,))}
    )
    return m.ArtifactReceipt(
        receipt_id=identity(),
        receiver_id=auth.principal_id,
        operation_id=cmd.operation_id,
        attempt_id=cmd.attempt_id,
        input_hash=next(x for x in rows(h, "stages") if x["job_id"] == cmd.job_id)[
            "input_hash"
        ],
        output_manifest_hash="d" * 64,
        status="passed",
        items=(
            m.VerifiedItem(
                item_id=item_id,
                size_bytes=1200,
                sha256="e" * 64,
                evidence=item_evidence,
            ),
        ),
        durable_reference=identity(),
        accepted_at=h.clock.now(),
    )


def complete_return(h, film_id):
    skip_compute(h, film_id)
    claimed = claim(h, film_id)
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
    return claimed


def final_manifest(h, film_id, manifest, claimed, *, change=None):
    data = fixture("final-manifest-v1.json")
    data.update(
        film_id=film_id,
        input_revision=manifest.revision,
        input_hash=manifest.manifest_hash,
    )
    data["postprocess"]["attempt_id"] = claimed[2].attempt_id
    data["postprocess"]["tool_version"] = claimed[2].tool_versions.tool_version
    data["postprocess"]["config_digest"] = claimed[2].tool_versions.config_digest
    if change:
        change(data)
    data["manifest_hash"] = m.manifest_digest(data)
    return m.FinalManifest.model_validate(data)


def ready_film(h):
    """Public-command path to a verified final package; no direct table edits."""
    film_id, manifest, _, _ = film(h)
    complete_return(h, film_id)
    claimed = claim(h, film_id, "scrape")
    final = final_manifest(h, film_id, manifest, claimed)
    view = h.store.get_film(h.operator, film_id)
    h.store.put_final_manifest(
        claimed[1],
        h.meta(),
        m.FinalManifestPut(
            film_id=film_id,
            expected_version=view.film.version,
            attempt_id=claimed[2].attempt_id,
            manifest=final,
        ),
    )
    h.store.finish_attempt(
        claimed[1],
        h.meta(),
        result_command(h, claimed, output_hash=final.manifest_hash),
    )
    return film_id, manifest, final


def test_intake_is_atomic_idempotent_and_survives_restart(h):
    film_id, manifest, _, auth = film(h, intake=False)
    cmd = m.InputManifestPut(film_id=film_id, expected_version=1, manifest=manifest)
    meta = h.meta()
    first = h.store.put_input_manifest(auth, meta, cmd)
    before = h.store.get_film(h.operator, film_id)
    assert len(before.stages) == 9
    assert stage(h, film_id, "intake").status == "succeeded"
    assert len(rows(h, "source_refs")) == len(manifest.items)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert h.store.put_input_manifest(auth, meta, cmd) == first
    assert h.store.get_film(h.operator, film_id) == before
    assert len(rows(h, "stages")) == 9


@pytest.mark.parametrize(
    "part", ["writer_check", "full_read", "source_recheck", "workcopy", "media_probe"]
)
def test_incomplete_readiness_never_creates_jobs(h, part):
    film_id, _, _, auth = film(h, intake=False)

    def change(data):
        ready = data["readiness"]
        proof = (
            ready["workcopy"]["verification"]
            if part == "workcopy"
            else ready["media_probes"][0]["evidence"]
            if part == "media_probe"
            else ready[part]
        )
        proof["status"] = "unknown"

    manifest = input_manifest(film_id, change=change)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.put_input_manifest(
            auth,
            h.meta(),
            m.InputManifestPut(film_id=film_id, expected_version=1, manifest=manifest),
        )
    assert rows(h, "stages") == []
    assert rows(h, "manifests") == []
    assert h.store.get_film(h.operator, film_id).film.version == 1


def test_concurrent_intake_cas_has_one_complete_winner(h):
    film_id, manifest, _, auth = film(h, intake=False)
    cmd = m.InputManifestPut(film_id=film_id, expected_version=1, manifest=manifest)

    def submit(_):
        try:
            return h.store.put_input_manifest(auth, h.meta(), cmd).status
        except m.WorkflowError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results.count("cas_conflict") == 1
    assert len(rows(h, "stages")) == 9
    assert len(rows(h, "manifests")) == 1


def test_dependencies_roles_and_skip_policy_cannot_be_bypassed(h):
    film_id, _, _, _ = film(h)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        claim(h, film_id)
    job = stage(h, film_id, "return")
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.skip_stage(
            h.operator,
            h.meta(),
            m.StageSkip(
                job_id=job.job_id,
                expected_version=1,
                reason="unsafe",
                policy_revision=1,
                input_hash=job.input_hash,
                evidence=(evidence(h, h.operator.principal_id, kind="verified_reuse"),),
            ),
        )
    job = stage(h, film_id, "restore")
    _, _, observer, _ = h.component()
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.skip_stage(
            observer,
            h.meta(),
            m.StageSkip(
                job_id=job.job_id,
                expected_version=1,
                reason="observer",
                policy_revision=1,
                input_hash=job.input_hash,
                evidence=(evidence(h, observer.principal_id, kind="verified_reuse"),),
            ),
        )


def test_return_requires_receiver_receipt_with_exact_attempt_and_hash(h):
    film_id, _, _, _ = film(h)
    skip_compute(h, film_id)
    claimed = claim(h, film_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.finish_attempt(claimed[1], h.meta(), result_command(h, claimed))
    proof = receipt(h, claimed)
    bad = proof.model_copy(update={"attempt_id": identity()})
    with pytest.raises(m.WorkflowError):
        h.store.accept_artifact_receipt(claimed[1], h.meta(), bad)
    h.store.accept_artifact_receipt(claimed[1], h.meta(), proof)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.finish_attempt(
            claimed[1],
            h.meta(),
            result_command(
                h, claimed, output_hash="f" * 64, receipt_id=proof.receipt_id
            ),
        )
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
    assert stage(h, film_id, "return").status == "succeeded"


def test_expired_assignment_rejects_late_result_until_explicit_reconcile(h):
    film_id, _, _, _ = film(h)
    skip_compute(h, film_id)
    claimed = claim(h, film_id)
    h.clock.value += timedelta(seconds=61)
    proof = receipt(h, claimed)
    h.store.accept_artifact_receipt(claimed[1], h.meta(), proof)
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
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
    current = assignment(h, claimed[3].assignment_id)
    h.store.mark_unknown(
        h.operator,
        h.meta(),
        m.UnknownObservation(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="Lease expired",
            operation_id=claimed[2].operation_id,
            local_owner=None,
            evidence=(),
        ),
    )
    assert stage(h, film_id, "return").status == "unknown"
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        claim(h, film_id)
    current = assignment(h, current.assignment_id)
    h.store.resolve_assignment(
        h.operator,
        h.meta(),
        m.AssignmentResolution(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="Receiver receipt reconciled",
            operation_id=claimed[2].operation_id,
            local_owner=None,
            evidence=(evidence(h, h.operator.principal_id, kind="reconciled"),),
            outcome="succeeded",
        ),
    )
    assert stage(h, film_id, "return").status == "succeeded"
    assert assignment(h, current.assignment_id).status == "resolved"


def test_final_candidate_requires_current_scrape_success_before_publication(h):
    film_id, manifest, _, _ = film(h)
    complete_return(h, film_id)
    claimed = claim(h, film_id, "scrape")
    final = final_manifest(h, film_id, manifest, claimed)
    view = h.store.get_film(h.operator, film_id)
    h.store.put_final_manifest(
        claimed[1],
        h.meta(),
        m.FinalManifestPut(
            film_id=film_id,
            expected_version=view.film.version,
            attempt_id=claimed[2].attempt_id,
            manifest=final,
        ),
    )
    assert h.store.get_film(h.operator, film_id).film.current_final_revision is None
    h.store.finish_attempt(
        claimed[1],
        h.meta(),
        result_command(h, claimed, output_hash=final.manifest_hash),
    )
    assert h.store.get_film(h.operator, film_id).film.current_final_revision == 1


def test_new_input_does_not_forget_running_old_owner_or_accept_old_result(h):
    film_id, _, _, intake_auth = film(h)
    skip_compute(h, film_id)
    claimed = claim(h, film_id)
    view = h.store.get_film(h.operator, film_id)
    h.store.put_input_manifest(
        intake_auth,
        h.meta(),
        m.InputManifestPut(
            film_id=film_id,
            expected_version=view.film.version,
            manifest=input_manifest(film_id, revision=2),
        ),
    )
    assert len(rows(h, "stages")) == 18
    assert len(rows(h, "assignments")) == 1
    assert rows(h, "assignments")[0]["resolved_at"] is None
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        h.store.finish_attempt(claimed[1], h.meta(), result_command(h, claimed))
    view = h.store.get_film(h.operator, film_id)
    assert view.film.current_input_revision == 2
    assert view.film.context_state == "invalidated"


def test_unknown_resolution_is_required_before_retry_and_keeps_attempt_history(h):
    film_id, _, _, _ = film(h)
    skip_compute(h, film_id)
    claimed = claim(h, film_id)
    h.store.finish_attempt(
        claimed[1], h.meta(), result_command(h, claimed, outcome="unknown")
    )
    job = stage(h, film_id, "return")
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.retry_stage(
            h.operator,
            h.meta(),
            m.StageRetry(
                job_id=job.job_id,
                expected_version=job.version,
                reason="Do not forget owner",
            ),
        )
    current = assignment(h, claimed[3].assignment_id)
    h.store.resolve_assignment(
        h.operator,
        h.meta(),
        m.AssignmentResolution(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="No result remains",
            operation_id=claimed[2].operation_id,
            local_owner=None,
            evidence=(evidence(h, h.operator.principal_id, kind="reconciled"),),
            outcome="failed",
        ),
    )
    job = stage(h, film_id, "return")
    h.store.retry_stage(
        h.operator,
        h.meta(),
        m.StageRetry(
            job_id=job.job_id, expected_version=job.version, reason="Explicit retry"
        ),
    )
    newer = claim(h, film_id)
    assert newer[2].attempt_id != claimed[2].attempt_id
    attempts = rows(h, "attempts")
    assert [x["attempt_number"] for x in attempts] == [1, 2]
    assert attempts[0]["status"] == "failed"


def compute_worker(h, *, binding=None, max_width=1920, max_height=1080):
    registered, _, _, _, _, auth, _, _ = enroll(h)
    current = h.store.get_worker(h.operator, registered.worker_id).worker
    h.store.record_boot(
        auth,
        h.meta(),
        m.BootObservation(
            worker_id=current.worker_id,
            expected_version=current.version,
            boot_id=identity(),
            known_epoch=1,
            assignment_ids=(),
            local_owners=(),
        ),
    )
    current = h.store.get_worker(h.operator, registered.worker_id).worker
    physical = m.PhysicalGpuBinding(
        resource_id=binding.resource_id if binding else identity(),
        device_id=current.device_id,
        device_identity=binding.device_identity if binding else identity(),
        gpu_identity=binding.gpu_identity if binding else identity(),
        capability_revision=1,
        evidence=evidence(h, h.operator.principal_id, kind="physical_identity"),
    )
    probe = m.HardwareProbe(
        revision=1,
        source="synthetic_probe",
        observed_at=h.clock.now(),
        cpu="synthetic",
        ram_mib=32768,
        gpu_model="synthetic GPU",
        vram_mib=8192,
        evidence=evidence(h, h.operator.principal_id, kind="hardware_probe"),
        device_identity=physical.device_identity,
        gpu_identity=physical.gpu_identity,
        free_disk_bytes=10**9,
        model_ready=True,
    )
    approved = m.ApprovedCapability(
        revision=1,
        probe_revision=1,
        approved_by=h.operator.principal_id,
        approved_at=h.clock.now(),
        profiles=(
            m.ProfileCapability(
                profile_id="synthetic",
                profile_revision=1,
                tool="synthetic",
                tool_version="1",
                model="synthetic",
                model_version="1",
                allowed_stages=("restore", "asr", "translate"),
                max_width=max_width,
                max_height=max_height,
            ),
        ),
    )
    h.store.record_capabilities(
        h.operator,
        h.meta(),
        m.CapabilityUpdate(
            worker_id=current.worker_id,
            expected_version=current.version,
            capability=m.CapabilityRevision(
                revision=1, declared=None, probed=probe, approved=approved
            ),
            physical_binding=physical,
        ),
    )
    current = h.store.get_worker(h.operator, current.worker_id).worker
    h.store.update_worker(
        h.operator,
        h.meta(),
        m.WorkerUpdate(
            worker_id=current.worker_id,
            expected_version=current.version,
            address=current.address,
            port=current.port,
            network_revision=current.network_revision,
            workflow_participation=True,
            admission_state="enabled",
        ),
    )
    return h.store.get_worker(h.operator, current.worker_id).worker, auth


def plan(h, film_id, worker_id, *, revision=1):
    """Confirm the film's plan once (U1 P1': restore needs it)."""
    try:
        h.store.confirm_plan(
            h.operator,
            h.meta(),
            m.FilmPlanConfirm(
                film_id=film_id,
                input_revision=revision,
                ltx_model="standard",
                machine=worker_id,
            ),
        )
    except m.WorkflowError as exc:
        if exc.code not in ("plan_exists", "precondition_failed"):
            raise


def ensure_route(h, film_id, worker_id, revision):
    """P2-1 C10: a compute claim needs an open route to the claiming worker."""
    route = h.store.film_route(h.operator, film_id, revision)
    if route is not None:
        return
    controller, h.store.controller_id = h.store.controller_id, worker_id
    try:  # the store opens `local` routes for its controller only (OD7 hardening)
        h.store.route_film(
            h.operator,
            h.meta(),
            m.RouteOpen(
                route_id=identity(),
                film_id=film_id,
                input_revision=revision,
                worker_id=worker_id,
                kind="local",
                expected_pin_version=0,
                reason="test",
            ),
        )
    except m.WorkflowError as exc:  # e.g. a disabled worker: the claim says why
        if exc.code not in ("precondition_failed", "route_claimed"):
            raise
    finally:
        h.store.controller_id = controller


def claim_compute(h, film_id, worker, auth, *, name="restore"):
    job = stage(h, film_id, name)
    if name == "restore":
        plan(h, film_id, worker.worker_id, revision=job.input_revision)
    ensure_route(h, film_id, worker.worker_id, job.input_revision)
    command = m.ClaimRequest(
        job_id=job.job_id,
        expected_version=job.version,
        attempt_id=identity(),
        operation_id=identity(),
        worker_id=worker.worker_id,
        boot_id=worker.boot_id,
        profile_revision=1,
        config_digest=tools().config_digest,
        resource_id=worker.physical_binding.resource_id,
        tool_versions=tools(),
    )
    result = h.store.claim(auth, h.meta(), command)
    return worker, auth, command, result


def local_owner(h, claimed, *, state="running", previous=None):
    return m.LocalOwnerEvidence(
        owner_id=previous.owner_id if previous else identity(),
        generation=previous.generation if previous else 1,
        boot_id=claimed[2].boot_id,
        process_identity=previous.process_identity
        if previous
        else "synthetic-native-birth-identity",
        state=state,
        evidence=evidence(h, claimed[1].principal_id, kind="native_owner"),
    )


def resolve(h, claimed, *, outcome="succeeded", owner=None):
    current = assignment(h, claimed[3].assignment_id)
    return h.store.resolve_assignment(
        h.operator,
        h.meta(),
        m.AssignmentResolution(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="Native owner reconciled",
            operation_id=claimed[2].operation_id,
            local_owner=owner,
            evidence=(evidence(h, h.operator.principal_id, kind="reconciled"),),
            outcome=outcome,
        ),
    )


def test_business_success_does_not_release_gpu_without_same_owner_stop(h):
    film_id, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film_id, worker, auth)
    running = local_owner(h, claimed)
    h.store.finish_attempt(
        auth, h.meta(), result_command(h, claimed, local_owner=running)
    )
    assert stage(h, film_id, "restore").status == "succeeded"
    assert rows(h, "resource_owners")[0]["resolved_at"] is None
    with pytest.raises(m.WorkflowError, match="owner_unresolved"):
        claim_compute(h, film_id, worker, auth, name="asr")
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        resolve(h, claimed, owner=running)
    wrong = local_owner(h, claimed, state="stopped")
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        resolve(h, claimed, owner=wrong)
    stopped = local_owner(h, claimed, state="stopped", previous=running)
    resolve(h, claimed, owner=stopped)
    next_claim = claim_compute(h, film_id, worker, auth, name="asr")
    assert next_claim[2].attempt_id != claimed[2].attempt_id
    assert rows(h, "resource_owners")[0]["resolved_at"] is not None


def test_two_jobs_compete_for_one_persistent_physical_gpu(h):
    film1, _, _, _ = film(h)
    film2, _, _, _ = film(h)
    worker, auth = compute_worker(h)

    def submit(film_id):
        try:
            return claim_compute(h, film_id, worker, auth)[3].status
        except m.WorkflowError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(submit, (film1, film2)))
    assert sorted(result) == ["owner_unresolved", "running"]
    assert len(rows(h, "assignments")) == len(rows(h, "resource_owners")) == 1


def test_unknown_gpu_blocks_new_identity_on_same_hardware_after_restart(h):
    film1, _, _, _ = film(h)
    film2, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film1, worker, auth)
    running = local_owner(h, claimed)
    h.store.finish_attempt(
        auth,
        h.meta(),
        result_command(h, claimed, outcome="unknown", local_owner=running),
    )
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    replacement, new_auth = compute_worker(h, binding=worker.physical_binding)
    with pytest.raises(m.WorkflowError, match="owner_unresolved"):
        claim_compute(h, film2, replacement, new_auth)
    assert rows(h, "resource_owners")[0]["state"] == "unknown"
    stopped = local_owner(h, claimed, state="stopped", previous=running)
    resolve(h, claimed, outcome="failed", owner=stopped)
    claim_compute(h, film2, replacement, new_auth)


@pytest.mark.parametrize(
    "field", ["boot_id", "fencing_token", "authority_epoch", "input_hash"]
)
def test_late_or_foreign_compute_result_never_changes_owned_attempt(h, field):
    film_id, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film_id, worker, auth)
    cmd = result_command(h, claimed)
    altered = (
        identity()
        if field == "boot_id"
        else "f" * 64
        if field == "input_hash"
        else getattr(cmd, field) + 1
    )
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        h.store.finish_attempt(auth, h.meta(), cmd.model_copy(update={field: altered}))
    assert stage(h, film_id, "restore").status == "running"
    assert rows(h, "resource_owners")[0]["resolved_at"] is None


def test_worker_reboot_does_not_adopt_old_assignment(h):
    film_id, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film_id, worker, auth)
    current = h.store.get_worker(h.operator, worker.worker_id).worker
    h.store.record_boot(
        auth,
        h.meta(),
        m.BootObservation(
            worker_id=worker.worker_id,
            expected_version=current.version,
            boot_id=identity(),
            known_epoch=1,
            assignment_ids=(),
            local_owners=(),
        ),
    )
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        h.store.finish_attempt(auth, h.meta(), result_command(h, claimed))
    assert stage(h, film_id, "restore").status == "unknown"
    assert rows(h, "resource_owners")[0]["resolved_at"] is None


def test_current_profile_limits_resolution_even_with_operator_enabled_worker(h):
    film_id, _, _, auth = film(h, intake=False)

    def bigger(data):
        data["readiness"]["media_probes"][0].update(width=3840, height=2160)

    manifest = input_manifest(film_id, change=bigger)
    h.store.put_input_manifest(
        auth,
        h.meta(),
        m.InputManifestPut(film_id=film_id, expected_version=1, manifest=manifest),
    )
    worker, worker_auth = compute_worker(h)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        claim_compute(h, film_id, worker, worker_auth)
    assert rows(h, "attempts") == []


def test_gpu_failure_without_stop_is_unknown_and_expiry_keeps_owner(h):
    film_id, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film_id, worker, auth)
    result = h.store.finish_attempt(
        auth, h.meta(), result_command(h, claimed, outcome="failed")
    )
    assert result.status == "unknown"
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        resolve(h, claimed, outcome="failed")
    assert rows(h, "resource_owners")[0]["resolved_at"] is None


@pytest.mark.parametrize("part", ["subtitles", "folder", "postprocess"])
def test_final_policy_evidence_cannot_be_bypassed_by_candidate_write(h, part):
    film_id, manifest, _, _ = film(h)
    complete_return(h, film_id)
    claimed = claim(h, film_id, "scrape")

    def change(data):
        if part == "subtitles":
            data["subtitles"]["evidence"]["status"] = "unknown"
            data["postprocess"]["actual_marks"].remove("chinese_subtitles")
        elif part == "folder":
            data["postprocess"]["folder_required"] = False
        else:
            data["postprocess"]["status"] = "unknown"

    final = final_manifest(h, film_id, manifest, claimed, change=change)
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
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.finish_attempt(
            claimed[1],
            h.meta(),
            result_command(h, claimed, output_hash=final.manifest_hash),
        )
    assert h.store.get_film(h.operator, film_id).film.current_final_revision is None


def test_skip_keeps_content_proof_and_reason_after_restart(h):
    film_id, _, _, _ = film(h)
    job = stage(h, film_id, "restore")
    proof = evidence(h, h.operator.principal_id, kind="verified_reuse")
    h.store.skip_stage(
        h.operator,
        h.meta(),
        m.StageSkip(
            job_id=job.job_id,
            expected_version=job.version,
            reason="Retain verified reuse explanation",
            policy_revision=1,
            input_hash=job.input_hash,
            evidence=(proof,),
        ),
    )
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    current = stage(h, film_id, "restore")
    assert current.evidence == (proof,)
    assert current.reason == "Retain verified reuse explanation"


def test_every_policy_required_image_has_postprocess_hash_proof(h):
    film_id, manifest, _, _ = film(
        h, required_images=("poster", "fanart", "folder_cover")
    )
    complete_return(h, film_id)
    claimed = claim(h, film_id, "scrape")

    def change(data):
        # The file exists, but Miko did not process/verify it. The image's role
        # alone must not turn an incomplete candidate into a complete package.
        fanart = next(
            item["item_id"] for item in data["items"] if item["role"] == "fanart"
        )
        data["postprocess"]["required_image_item_ids"].remove(fanart)
        data["postprocess"]["items"] = [
            item for item in data["postprocess"]["items"] if item["item_id"] != fanart
        ]

    final = final_manifest(h, film_id, manifest, claimed, change=change)
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
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        h.store.finish_attempt(
            claimed[1],
            h.meta(),
            result_command(h, claimed, output_hash=final.manifest_hash),
        )


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "blocked"])
@pytest.mark.parametrize("isolation", ["boot", "revoke"])
def test_worker_reboot_preserves_success_fact_while_owner_becomes_unknown(
    h, outcome, isolation
):
    film_id, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film_id, worker, auth)
    owner = local_owner(
        h, claimed, state="stopped" if outcome == "failed" else "running"
    )
    h.store.finish_attempt(
        auth,
        h.meta(),
        result_command(
            h,
            claimed,
            outcome="unknown" if outcome == "blocked" else outcome,
            local_owner=owner,
        ),
    )
    if outcome == "blocked":
        # Public blocked results are reconciliation facts with already released
        # ownership; boot/revocation must leave that terminal case untouched too.
        resolve(
            h,
            claimed,
            outcome="blocked",
            owner=local_owner(h, claimed, state="stopped", previous=owner),
        )
    original_stage = stage(h, film_id, "restore")
    assert original_stage.status == outcome
    original_attempt = rows(h, "attempts")[0]
    original_operation = h.store.get_operation(h.operator, claimed[2].operation_id)
    current = h.store.get_worker(h.operator, worker.worker_id).worker
    if isolation == "boot":
        h.store.record_boot(
            auth,
            h.meta(),
            m.BootObservation(
                worker_id=worker.worker_id,
                expected_version=current.version,
                boot_id=identity(),
                known_epoch=1,
                assignment_ids=(),
                local_owners=(),
            ),
        )
    else:
        h.store.revoke_worker(
            h.operator,
            h.meta(),
            m.WorkerRevocation(
                worker_id=worker.worker_id,
                expected_version=current.version,
                reason="Synthetic revocation with unresolved process ownership",
            ),
        )
    assert stage(h, film_id, "restore") == original_stage
    assert rows(h, "attempts")[0] == original_attempt
    assert (
        h.store.get_operation(h.operator, claimed[2].operation_id) == original_operation
    )
    if outcome == "blocked":
        assert rows(h, "assignments")[0]["state"] == "resolved"
        assert rows(h, "resource_owners")[0]["resolved_at"] is not None
    else:
        assert rows(h, "assignments")[0]["state"] == "unknown"
        assert rows(h, "resource_owners")[0]["state"] == "unknown"
        assert rows(h, "resource_owners")[0]["resolved_at"] is None


def test_resolving_scrape_owner_does_not_republish_already_current_final(h):
    film_id, _, final = ready_film(h)
    before = h.store.get_film(h.operator, film_id).film
    record = next(
        row
        for row in rows(h, "assignments")
        if row["attempt_id"] == final.postprocess.attempt_id
    )
    current = m.AssignmentRecord.model_validate_json(record["body_json"])
    operation = next(
        row for row in rows(h, "operations") if row["attempt_id"] == current.attempt_id
    )
    h.store.resolve_assignment(
        h.operator,
        h.meta(),
        m.AssignmentResolution(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="Component exit confirmed",
            operation_id=operation["operation_id"],
            local_owner=None,
            evidence=(evidence(h, h.operator.principal_id, kind="reconciled"),),
            outcome="succeeded",
        ),
    )
    assert h.store.get_film(h.operator, film_id).film == before


def test_renew_is_same_boot_owner_only_and_deadline_is_owned_by_hub(h):
    film_id, _, _, _ = film(h)
    worker, auth = compute_worker(h)
    claimed = claim_compute(h, film_id, worker, auth)
    old = assignment(h, claimed[3].assignment_id)
    h.clock.value += timedelta(seconds=20)
    owner = local_owner(h, claimed)
    cmd = m.AssignmentRenewal(
        assignment_id=old.assignment_id,
        attempt_id=old.attempt_id,
        worker_id=old.worker_id,
        boot_id=old.boot_id,
        authority_epoch=old.authority_epoch,
        fencing_token=old.fencing_token,
        expected_version=old.version,
        local_owner=owner,
    )
    h.store.renew(auth, h.meta(), cmd)
    current = assignment(h, old.assignment_id)
    assert current.expires_at == h.clock.now() + timedelta(seconds=60)
    assert current.local_owner == owner
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        h.store.renew(
            auth,
            h.meta(),
            cmd.model_copy(
                update={
                    "expected_version": current.version,
                    "local_owner": local_owner(h, claimed),
                }
            ),
        )
    h.clock.value += timedelta(seconds=61)
    with pytest.raises(m.WorkflowError, match="stale_attempt"):
        h.store.renew(
            auth, h.meta(), cmd.model_copy(update={"expected_version": current.version})
        )
    assert rows(h, "resource_owners")[0]["resolved_at"] is None
