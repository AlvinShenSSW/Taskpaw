"""Public SQLite proof only; synthetic snapshot/owner DTOs, no native claims."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_operations import exit_proof, owner, record
from taskpaw_v3.tests.test_workflow_intake_publication import snapshot_fixture
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.intake_snapshot import validate_snapshot
from taskpaw_v3.workflow.media_facts import evaluate_eligibility

intake = fixtures.intake


def policy_resnapshot(intake):
    h, adapter, root, observed, first = snapshot_fixture(intake, eligible=True)
    h.store.finish_intake_snapshot(adapter, h.meta(), first)
    old = first.snapshot
    root = root.model_copy(
        update={
            "expected_version": 1,
            "policy": root.policy.model_copy(update={"policy_revision": 2}),
            "film_policy": root.film_policy.model_copy(update={"policy_revision": 2}),
            "profiles": tuple(
                p.model_copy(update={"policy_revision": 2}) for p in root.profiles
            ),
        }
    )
    h.store.configure_intake_root(h.operator, h.meta(), root)
    start = h.clock.now()
    for seconds in (0, 60):
        h.clock.value += timedelta(seconds=seconds)
        view = h.store.get_intake(adapter, observed.package_id)
        observed = observed.model_copy(
            update={
                "expected_package_version": view.version,
                "root_config_revision": 2,
                "observed_at": h.clock.now(),
                "trigger": "rescan",
            }
        )
        h.store.observe_intake(adapter, h.meta(), observed)
    view = h.store.get_intake(adapter, observed.package_id)
    admission = old.admission.model_copy(
        update={
            "expected_version": view.version,
            "operation_id": identity(),
            "snapshot_id": identity(),
            "source_generation": view.source_generation,
            "root_config_revision": 2,
            "policy_revision": 2,
        }
    )
    h.store.begin_intake_snapshot(adapter, h.meta(), admission)
    native_owner = owner(h, admission)
    recorded = record(h, adapter, admission, native_owner)
    work_path = f"intake/{admission.operation_id}/snapshot"
    manifests = []
    for value in old.manifests:
        data = value.model_dump(mode="json")
        data.update(revision=2, policy_revision=2)
        data["source_binding"].update(
            source_generation=admission.source_generation,
            snapshot_id=admission.snapshot_id,
            root_config_revision=2,
        )
        data["readiness"].update(
            policy_revision=2,
            stable_started_at=start.isoformat(),
            stable_ended_at=h.clock.now().isoformat(),
        )
        paths = {i.item_id: i.relative_path for i in value.items}
        for item in data["readiness"]["workcopy"]["items"]:
            item["relative_path"] = f"{work_path}/{paths[item['source_item_id']]}"
        data["readiness"]["workcopy"]["manifest_hash"] = m.digest(
            data["readiness"]["workcopy"]["items"]
        )
        data["readiness"] = m.ReadinessEvidence.model_validate(
            data["readiness"]
        ).model_dump(mode="json")
        data["manifest_hash"] = m.manifest_digest(data)
        manifests.append(m.InputManifest.model_validate(data))
    data = old.model_dump(mode="json", exclude={"completion_digest"})
    data.update(
        admission=admission.model_dump(mode="json"),
        manifests=[v.model_dump(mode="json") for v in manifests],
        relative_work_path=work_path,
        eligibility=evaluate_eligibility(
            old.media_probes, root.profiles, policy_revision=2
        ).model_dump(mode="json"),
    )
    data["completion_digest"] = m.digest(data)
    snapshot = im.PackageSnapshot.model_validate(data)
    validate_snapshot(
        snapshot,
        admission,
        observed,
        root,
        producer_id=intake[1].principal_id,
        now=h.clock.now(),
    )
    assert snapshot.eligibility.decision == "eligible"
    command = im.IntakeSnapshotResult(
        expected_version=recorded.version,
        operation_id=admission.operation_id,
        source_generation=admission.source_generation,
        outcome="complete",
        reason="snapshot_complete",
        snapshot=snapshot,
        exit_proof=exit_proof(
            h, admission, native_owner, source_generation=admission.source_generation
        ),
    )
    return h, adapter, observed, command


def test_policy_change_cannot_leave_proven_exit_unrecorded(intake):
    h, adapter, observed, command = policy_resnapshot(intake)
    error = None
    try:
        h.store.finish_intake_snapshot(adapter, h.meta(), command)
    except m.WorkflowError as exc:
        error = str(exc)
    view = h.store.get_intake_operation(adapter, command.operation_id)
    print(
        {
            "error": error,
            "state": view.state,
            "exit": view.exit_proof,
            "package": h.store.get_intake(adapter, observed.package_id).state,
            "new_policy": command.snapshot.manifests[0].policy_revision,
            "film_policy": h.store.get_film(
                adapter, command.snapshot.manifests[0].film_id
            ).film.policy.policy_revision,
        }
    )
    assert view.exit_proof is not None, (
        "public policy bump loses stopped proof and retains the only slot"
    )
    assert view.result.snapshot == command.snapshot
    package = h.store.get_intake(adapter, observed.package_id)
    assert package.state == "blocked" and package.reason == "publication_blocked"
    h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": package.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    assert h.store.get_intake(adapter, observed.package_id).state == "blocked"
    assert (
        h.store.get_film(
            adapter, command.snapshot.manifests[0].film_id
        ).film.current_input_revision
        == 1
    )


@pytest.mark.parametrize("change", ["dirty", "reconfigure", "disable"])
@pytest.mark.parametrize("persistence_failure", [False, True])
def test_dirty_after_admission_records_no_process_and_releases_slot(
    intake, monkeypatch, change, persistence_failure
):
    from taskpaw_v3.tests.test_workflow_intake_operations import prepare
    from taskpaw_v3.workflow import intake_owner
    from taskpaw_v3.workflow import intake_protocol as wire
    from taskpaw_v3.workflow.intake_discovery_models import IntakeSourceNotice
    from taskpaw_v3.workflow.intake_runner import SnapshotRunner

    h, grant, adapter, root = intake
    observed, _ = prepare(intake)
    controller = SnapshotRunner(
        h.store,
        adapter,
        meta=h.meta,
        owner_boot_id=grant.boot_id,
        clock=h.clock.now,
        ffprobe=wire.ProbeArtifact(
            executable="C:/synthetic/never.exe",
            sha256="0" * 64,
            size_bytes=1,
            version="fixture",
        ),
    )
    original = h.store.begin_intake_snapshot
    admitted = []

    def changed(auth, meta, command):
        result = original(auth, meta, command)
        admitted.append(command.operation_id)
        view = h.store.get_intake(adapter, observed.package_id)
        if change != "dirty":
            h.store.configure_intake_root(
                h.operator,
                h.meta(),
                root.model_copy(
                    update={"expected_version": 1, "enabled": change != "disable"}
                ),
            )
            return result
        h.store.record_intake_source_notice(
            adapter,
            h.meta(),
            IntakeSourceNotice(
                expected_version=view.version,
                package_id=view.package_id,
                root_config_revision=view.root_config_revision,
                source_generation=view.source_generation,
                observed_at=h.clock.now(),
                kind="dirty",
                trigger="notification",
            ),
        )
        return result

    def never_launch(*args, **kwargs):
        raise AssertionError("no process creation permitted")

    monkeypatch.setattr(h.store, "begin_intake_snapshot", changed)
    monkeypatch.setattr(intake_owner, "start_snapshot_suspended", never_launch)
    if persistence_failure:
        finish = h.store.finish_intake_snapshot

        def unavailable(*args, **kwargs):
            raise m.WorkflowError("authority_unavailable")

        monkeypatch.setattr(h.store, "finish_intake_snapshot", unavailable)
        with pytest.raises(m.WorkflowError, match="authority_unavailable"):
            controller.run(observed.package_id)
        assert controller.has_pending_settlement
        assert controller.active_operation_id == admitted[0]
        assert h.store.get_intake_operation(adapter, admitted[0]).exit_proof is None
        monkeypatch.setattr(h.store, "finish_intake_snapshot", finish)
        result = controller.retry_settlement()
    else:
        result = controller.run(observed.package_id)
    assert result.operation_id == admitted[0]
    assert result.exit_proof.state == "no_process_created"
    assert result.owner is None and controller.active_operation_id is None
    # A different package can now use the global durable slot. The same
    # dirty package is not used as a proxy for global progress.
    from taskpaw_v3.tests.test_workflow_store import identity
    from taskpaw_v3.workflow.store import WorkflowStore

    monkeypatch.setattr(h.store, "begin_intake_snapshot", original)
    root_version = h.store.get_intake(adapter, observed.package_id).root_config_revision
    h.store.configure_intake_root(
        h.operator,
        h.meta(),
        root.model_copy(
            update={
                "expected_version": root_version,
                "enabled": True,
            }
        ),
    )
    fresh = fixtures.observation(
        h,
        root,
        root_config_revision=root_version + 1,
        relative_path="Performer/SECOND-002",
        package_native_identity=fixtures.native(h, root.root_id, "second-package"),
    )
    first = h.store.observe_intake(adapter, h.meta(), fresh)
    h.clock.value += timedelta(seconds=60)
    fresh = fresh.model_copy(
        update={"expected_package_version": first.version, "observed_at": h.clock.now()}
    )
    stable = h.store.observe_intake(adapter, h.meta(), fresh)
    second = im.IntakeSnapshotAdmission(
        expected_version=stable.version,
        package_id=fresh.package_id,
        source_generation=1,
        root_config_revision=root_version + 1,
        grouping_revision=1,
        observation_digest=fresh.observation_digest,
        operation_id=identity(),
        snapshot_id=identity(),
        owner_boot_id=grant.boot_id,
        policy_revision=1,
    )
    accepted = h.store.begin_intake_snapshot(adapter, h.meta(), second)
    assert accepted.operation_id == second.operation_id
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert (
        h.store.get_intake_operation(adapter, admitted[0]).exit_proof.state
        == "no_process_created"
    )
