"""Validate a complete candidate against its immutable admitted observations."""

from datetime import datetime

from . import intake_models as im
from . import models as m
from .film_store import native_identity_key
from .media_facts import evaluate_eligibility


def completion_digest(snapshot: im.PackageSnapshot) -> str:
    return m.digest(snapshot.model_dump(mode="json", exclude={"completion_digest"}))


def validate_snapshot(
    snapshot: im.PackageSnapshot,
    admission: im.IntakeSnapshotAdmission,
    observation: im.IntakeObservation,
    config: im.IntakeRootPut,
    *,
    producer_id: str,
    now: datetime,
) -> None:
    def require(condition: bool) -> None:
        if not condition:
            raise m.WorkflowError("snapshot_binding_changed")

    require(
        snapshot.admission == admission and snapshot.grouping == observation.grouping
    )
    require(snapshot.completion_digest == completion_digest(snapshot))
    require(snapshot.relative_work_path == f"intake/{admission.operation_id}/snapshot")
    observed = {item.member_id: item for item in observation.members}
    require(set(observed) == {item.member_id for item in snapshot.members})
    copied = {item.member_id: item for item in snapshot.members}
    for member in snapshot.members:
        original = observed[member.member_id]
        require(
            (
                member.relative_path,
                member.entry_kind,
                member.size_bytes,
                native_identity_key(member.source_identity),
            )
            == (
                original.relative_path,
                original.entry_kind,
                original.size_bytes,
                native_identity_key(original.native_identity),
            )
        )
        require(
            member.source_identity.root_id == config.root_id
            and member.copy_identity.root_id == config.work_root_id
        )
    proofs = [
        snapshot.source_before,
        snapshot.source_after,
        snapshot.workcopy_verification,
    ]
    for manifest in snapshot.manifests:
        binding = manifest.source_binding
        require(binding is not None)
        assert binding is not None
        require(
            binding.root_id == config.root_id
            and binding.package_membership_digest == admission.observation_digest
            and binding.grouping_digest == m.digest(snapshot.grouping)
        )
        require(manifest.policy_revision == config.policy.policy_revision)
        readiness = manifest.readiness
        require(readiness.membership_digest == admission.observation_digest)
        require(readiness.workcopy.manifest_hash == m.digest(readiness.workcopy.items))
        require(
            (readiness.stable_ended_at - readiness.stable_started_at).total_seconds()
            >= config.policy.stable_seconds
            and readiness.stable_ended_at <= now
            and readiness.stable_ended_at >= observation.observed_at
        )
        proofs.extend(
            (
                readiness.writer_check,
                readiness.full_read,
                readiness.workcopy.verification,
                readiness.source_recheck,
            )
        )
        for item in manifest.items:
            member = copied[item.item_id]
            require(
                item.source_root_id == config.root_id
                and native_identity_key(item.native_identity)
                == native_identity_key(member.source_identity)
                and item.relative_path == member.relative_path
            )
        for copy_item in readiness.workcopy.items:
            member = copied[copy_item.source_item_id]
            require(
                copy_item.root_id == config.work_root_id
                and copy_item.relative_path
                == f"{snapshot.relative_work_path}/{member.relative_path}"
                and native_identity_key(copy_item.native_identity)
                == native_identity_key(member.copy_identity)
            )
        require(readiness.writer_check.summary.active_writers is False)
    require(
        all(
            proof.status == "passed"
            and proof.producer_id == producer_id
            and proof.observed_at <= now
            for proof in proofs
        )
    )
    require(
        all(
            probe.evidence.producer_id == producer_id
            and probe.evidence.observed_at <= now
            for probe in snapshot.media_probes
        )
    )
    require(
        snapshot.eligibility
        == evaluate_eligibility(
            snapshot.media_probes,
            config.profiles,
            policy_revision=config.policy.policy_revision,
        )
    )
