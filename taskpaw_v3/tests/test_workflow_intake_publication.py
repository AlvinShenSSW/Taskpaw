"""A copied package is committed through the public journal as one transaction."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_models import snapshot_payload
from taskpaw_v3.tests.test_workflow_intake_operations import exit_proof, owner, record
from taskpaw_v3.tests.test_workflow_media_facts import probe, profile
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.manifest import observation_digest
from taskpaw_v3.workflow.media_facts import evaluate_eligibility
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake


def test_cancelled_complete_candidate_is_history_without_operational_ready(intake):
    h, adapter, _, observed, command = snapshot_fixture(intake, eligible=True)
    cancelled = h.store.cancel_intake_snapshot(
        adapter,
        h.meta(),
        im.IntakeCancel(
            expected_version=command.expected_version,
            operation_id=command.operation_id,
            reason="cancelled",
        ),
    )
    h.store.finish_intake_snapshot(
        adapter,
        h.meta(),
        command.model_copy(update={"expected_version": cancelled.version}),
    )
    view = h.store.get_intake(adapter, observed.package_id)
    assert view.state == "waiting_stability"
    assert view.eligibility is None
    assert (
        h.store.get_intake_operation(adapter, command.operation_id).result.snapshot
        == command.snapshot
    )
    for manifest in command.snapshot.manifests:
        with pytest.raises(m.WorkflowError):
            h.store.get_film(adapter, manifest.film_id)


def snapshot_fixture(intake, *, eligible=False, allow_test_reuse=False):
    h, grant, adapter, root = intake
    h.clock.value += timedelta(hours=1)
    data = snapshot_payload()
    original = data["manifests"][0]
    root = root.model_copy(
        update={
            "root_id": original.items[0].source_root_id,
            "native_identity": original.items[0].native_identity.model_copy(
                update={"object_id": "input-root"}
            ),
            "work_root_id": original.readiness.workcopy.items[0].root_id,
            "work_native_identity": original.readiness.workcopy.items[
                0
            ].native_identity.model_copy(update={"object_id": "work-root"}),
            "profiles": (profile(),) if eligible else (),
        }
    )
    if allow_test_reuse:
        root = root.model_copy(
            update={
                "film_policy": root.film_policy.model_copy(
                    update={
                        "skippable_stages": ("restore", "asr", "translate"),
                        "skip_evidence_kinds": ("verified_reuse",),
                    }
                )
            }
        )
    grouping = data["grouping"]
    observed = im.IntakeObservation(
        root_id=root.root_id,
        root_config_revision=1,
        package_id=data["admission"].package_id,
        expected_package_version=0,
        package_native_identity=root.native_identity.model_copy(
            update={"object_id": "package"}
        ),
        relative_path="actor/film",
        parent_context=grouping.parent_context,
        observed_at=h.clock.now(),
        coverage="complete",
        trigger="startup",
        writer_state="clear",
        members=tuple(
            im.ObservedMember(
                member_id=v.member_id,
                relative_path=v.relative_path,
                entry_kind=v.entry_kind,
                native_identity=v.source_identity,
                size_bytes=v.size_bytes,
                mtime_ns=1,
            )
            for v in data["members"]
        ),
        observation_digest="0" * 64,
        grouping=grouping,
    )
    observed = observed.model_copy(
        update={
            "observation_digest": observation_digest(
                observed.members,
                parent_context=observed.parent_context,
                grouping=grouping,
            )
        }
    )
    h.store.configure_intake_root(h.operator, h.meta(), root)
    first = h.store.observe_intake(adapter, h.meta(), observed)
    start = h.clock.now()
    h.clock.value += timedelta(seconds=60)
    observed = observed.model_copy(
        update={"expected_package_version": first.version, "observed_at": h.clock.now()}
    )
    second = h.store.observe_intake(adapter, h.meta(), observed)
    admission = data["admission"].model_copy(
        update={
            "expected_version": second.version,
            "owner_boot_id": grant.boot_id,
            "observation_digest": observed.observation_digest,
        }
    )
    h.store.begin_intake_snapshot(adapter, h.meta(), admission)
    native_owner = owner(h, admission)
    recorded = record(h, adapter, admission, native_owner)

    def own_evidence(value):
        if isinstance(value, dict):
            return {
                k: grant.principal_id if k == "producer_id" else own_evidence(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [own_evidence(v) for v in value]
        return value

    payload = own_evidence(original.model_dump(mode="json"))
    if eligible:
        payload["readiness"]["media_probes"] = [
            own_evidence(
                probe()
                .model_copy(
                    update={"item_id": original.readiness.media_probes[0].item_id}
                )
                .model_dump(mode="json")
            )
        ]
    payload["readiness"]["stable_started_at"] = start.isoformat()
    payload["readiness"]["stable_ended_at"] = h.clock.now().isoformat()
    payload["readiness"]["membership_digest"] = observed.observation_digest
    relative_work_path = f"intake/{admission.operation_id}/snapshot"
    source_paths = {v["item_id"]: v["relative_path"] for v in payload["items"]}
    for value in payload["readiness"]["workcopy"]["items"]:
        value["relative_path"] = (
            f"{relative_work_path}/{source_paths[value['source_item_id']]}"
        )
    payload["readiness"] = m.ReadinessEvidence.model_validate(
        payload["readiness"]
    ).model_dump(mode="json")
    payload["readiness"]["workcopy"]["manifest_hash"] = m.digest(
        payload["readiness"]["workcopy"]["items"]
    )
    payload["source_binding"].update(
        root_id=root.root_id,
        package_membership_digest=observed.observation_digest,
        grouping_digest=m.digest(grouping),
    )
    payload["manifest_hash"] = m.manifest_digest(payload)
    manifest = m.InputManifest.model_validate(payload)
    data.update(
        admission=admission,
        manifests=(manifest,),
        media_probes=manifest.readiness.media_probes,
        eligibility=evaluate_eligibility(
            manifest.readiness.media_probes, root.profiles, policy_revision=1
        ),
        source_before=manifest.readiness.full_read,
        source_after=manifest.readiness.source_recheck,
        workcopy_verification=manifest.readiness.workcopy.verification,
        relative_work_path=relative_work_path,
    )
    candidate = im.PackageSnapshot(**data)
    candidate = candidate.model_copy(
        update={
            "completion_digest": m.digest(
                candidate.model_dump(mode="json", exclude={"completion_digest"})
            )
        }
    )
    result = im.IntakeSnapshotResult(
        expected_version=recorded.version,
        operation_id=admission.operation_id,
        source_generation=1,
        outcome="complete",
        reason="snapshot_complete",
        exit_proof=exit_proof(h, admission, native_owner),
        snapshot=candidate,
    )
    return h, adapter, root, observed, result


def test_eligible_snapshot_atomically_publishes_film_and_readable_manifest(intake):
    h, adapter, _, observed, command = snapshot_fixture(intake, eligible=True)
    meta = h.meta()
    completed = h.store.finish_intake_snapshot(adapter, meta, command)
    assert h.store.finish_intake_snapshot(adapter, meta, command) == completed
    for manifest in command.snapshot.manifests:
        view = h.store.get_film(adapter, manifest.film_id)
        assert view.film.current_input_revision == manifest.revision
        assert len(view.stages) == 9 and view.stages[0].status == "succeeded"
        assert (
            h.store.get_input_manifest(adapter, manifest.film_id, manifest.revision)
            == manifest
        )
    assert h.store.get_intake(adapter, observed.package_id).state == "operational_ready"


def test_snapshot_publication_rolls_back_all_records_on_stage_insert_failure(
    intake, monkeypatch
):
    h, adapter, _, observed, command = snapshot_fixture(intake, eligible=True)
    import sqlite3

    original = h.store._insert

    def fail(table, **values):
        if table == "stages" and values["stage"] == "restore":
            raise sqlite3.OperationalError("synthetic write failure")
        return original(table, **values)

    with monkeypatch.context() as patch:
        patch.setattr(h.store, "_insert", fail)
        with pytest.raises(m.WorkflowError, match="authority_unavailable"):
            h.store.finish_intake_snapshot(adapter, h.meta(), command)
    assert h.store.get_intake(adapter, observed.package_id).state == "snapshotting"
    assert (
        h.store.get_intake_operation(adapter, command.operation_id).state == "running"
    )
    with pytest.raises(m.WorkflowError, match="not_found"):
        h.store.get_film(h.operator, command.snapshot.manifests[0].film_id)
    assert (
        h.store.finish_intake_snapshot(adapter, h.meta(), command).status == "completed"
    )


def test_unknown_media_snapshot_is_saved_but_not_published_for_compute(intake):
    h, adapter, _, observed, command = snapshot_fixture(intake)
    meta = h.meta()
    done = h.store.finish_intake_snapshot(adapter, meta, command)
    assert done.status == "completed"
    assert h.store.finish_intake_snapshot(adapter, meta, command) == done
    view = h.store.get_intake(adapter, observed.package_id)
    assert view.media_probes == command.snapshot.media_probes
    assert view.eligibility == command.snapshot.eligibility
    assert view.state == "blocked" and view.operation_id is None
    with pytest.raises(m.WorkflowError, match="not_found"):
        h.store.get_film(h.operator, command.snapshot.manifests[0].film_id)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert (
        h.store.get_intake(adapter, observed.package_id).media_probes
        == view.media_probes
    )


@pytest.mark.parametrize(
    "change", ["digest", "group_digest", "member_identity", "work_root", "eligibility"]
)
def test_complete_snapshot_must_match_admitted_source_and_authorized_profile(
    intake, change
):
    h, adapter, _, _, command = snapshot_fixture(intake)
    payload = command.model_dump(mode="json")
    snapshot = payload["snapshot"]
    if change == "digest":
        snapshot["completion_digest"] = "0" * 64
    elif change == "group_digest":
        value = snapshot["manifests"][0]
        value["source_binding"]["grouping_digest"] = "0" * 64
        value["manifest_hash"] = m.manifest_digest(value)
    elif change == "member_identity":
        snapshot["members"][0]["source_identity"]["object_id"] = "substituted-object"
    elif change == "work_root":
        snapshot["members"][0]["copy_identity"]["root_id"] = snapshot["members"][0][
            "source_identity"
        ]["root_id"]
    else:
        snapshot["eligibility"]["reason"] = "profile_required"
        snapshot["eligibility"]["decision"] = "profile_required"
        for item in snapshot["eligibility"]["items"]:
            item.update(reason="profile_required", decision="profile_required")
    if change != "digest":
        value = im.PackageSnapshot.model_validate(snapshot)
        snapshot["completion_digest"] = m.digest(
            value.model_dump(mode="json", exclude={"completion_digest"})
        )
    changed = im.IntakeSnapshotResult.model_validate(payload)
    with pytest.raises(m.WorkflowError, match="snapshot_binding_changed"):
        h.store.finish_intake_snapshot(adapter, h.meta(), changed)
    assert (
        h.store.get_intake_operation(adapter, command.operation_id).state == "running"
    )


def test_source_change_during_copy_retains_historical_result_without_current_publication(
    intake,
):
    h, adapter, _, observed, command = snapshot_fixture(intake)
    current = h.store.get_intake(adapter, observed.package_id)
    changed = observed.model_copy(
        update={
            "expected_package_version": current.version,
            "coverage": "partial",
            "observed_at": h.clock.now(),
        }
    )
    h.store.observe_intake(adapter, h.meta(), changed)
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    view = h.store.get_intake(adapter, observed.package_id)
    assert (
        view.source_generation == 2
        and view.state == "receiving"
        and view.operation_id is None
    )
    assert h.store.get_intake_operation(adapter, command.operation_id).result == command
    assert not view.media_probes


@pytest.mark.parametrize(
    "change", ["operation_path", "copy_path", "copy_digest", "membership"]
)
def test_private_copy_is_bound_to_exact_operation_member_paths_and_digest(
    intake, change
):
    h, adapter, _, _, command = snapshot_fixture(intake, eligible=True)
    payload = command.model_dump(mode="json")
    snapshot = payload["snapshot"]
    manifest = snapshot["manifests"][0]
    if change == "operation_path":
        snapshot["relative_work_path"] = "intake/another-operation/snapshot"
    elif change == "copy_path":
        manifest["readiness"]["workcopy"]["items"][0]["relative_path"] = (
            "unrelated/existing.mp4"
        )
        manifest["readiness"]["workcopy"]["manifest_hash"] = m.digest(
            manifest["readiness"]["workcopy"]["items"]
        )
    elif change == "copy_digest":
        manifest["readiness"]["workcopy"]["manifest_hash"] = "e" * 64
    else:
        manifest["readiness"]["membership_digest"] = "f" * 64
    manifest["manifest_hash"] = m.manifest_digest(manifest)
    candidate = im.PackageSnapshot.model_validate(snapshot)
    snapshot["completion_digest"] = m.digest(
        candidate.model_dump(mode="json", exclude={"completion_digest"})
    )
    changed = im.IntakeSnapshotResult.model_validate(payload)
    with pytest.raises(m.WorkflowError, match="snapshot_binding_changed"):
        h.store.finish_intake_snapshot(adapter, h.meta(), changed)
    assert (
        h.store.get_intake_operation(adapter, command.operation_id).state == "running"
    )
