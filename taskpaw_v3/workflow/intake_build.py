"""Build immutable evidence from held, copied objects and bounded probe facts."""

from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

from taskpaw_v3 import __version__

from . import intake_models as im
from . import models as m
from .intake_copy import PrivateCopy
from .intake_files import HeldPackage, IntakeFileError, _key
from .intake_protocol import IntakeWorkRequest
from .intake_snapshot import completion_digest
from .manifest import observation_digest
from .media_facts import evaluate_eligibility
from .raw_media_models import RawMediaFacts


def check_observation(request: IntakeWorkRequest, source: HeldPackage) -> None:
    """Call before copying and again before creating a completion candidate."""
    source.recheck()
    observation = request.observation
    if (
        observation.package_native_identity is None
        or source.package_identity is None
        or _key(observation.package_native_identity) != _key(source.package_identity)
        or observation_digest(
            observation.members,
            parent_context=observation.parent_context,
            grouping=observation.grouping,
        )
        != observation.observation_digest
        or set(source.entries) != {v.relative_path for v in observation.members}
    ):
        raise IntakeFileError("source_changed")
    for observed in observation.members:
        actual = source.entries[observed.relative_path]
        if (
            observed.native_identity is None
            or _key(observed.native_identity) != _key(actual.identity)
            or observed.size_bytes != actual.size_bytes
            or observed.mtime_ns != actual.mtime_ns
            or observed.entry_kind != ("directory" if actual.directory else "file")
            or observed.temporary != actual.temporary
            or observed.change_token is not None
            and observed.change_token != str(actual.change_filetime)
        ):
            raise IntakeFileError("source_changed")


def build_snapshot(
    request: IntakeWorkRequest,
    *,
    source: HeldPackage,
    copy: PrivateCopy,
    facts: dict[str, RawMediaFacts],
) -> im.PackageSnapshot:
    check_observation(request, source)
    if (
        copy.source is not source
        or not copy._copied
        or copy.closed
        or set(copy.entries) != set(source.entries)
    ):
        raise IntakeFileError("copy_incomplete")
    if set(facts) != set(request.video_ids):
        raise IntakeFileError("probe_coverage")
    now = datetime.now(timezone.utc)
    observation, admission, config = (
        request.observation,
        request.admission,
        request.root,
    )
    grouping = observation.grouping
    assert grouping is not None
    if (
        observation.observed_at - request.stable_started_at
    ).total_seconds() < config.policy.stable_seconds:
        raise IntakeFileError("source_not_stable")
    relative_work_path = f"intake/{admission.operation_id}/snapshot"
    by_id = {member.member_id: member for member in observation.members}
    members = tuple(
        im.SnapshotMember(
            member_id=observed.member_id,
            relative_path=observed.relative_path,
            entry_kind="directory" if value.source.directory else "file",
            source_identity=value.source.identity,
            copy_identity=value.copy.identity,
            size_bytes=value.source.size_bytes,
            sha256=value.sha256,
        )
        for observed in observation.members
        for value in (copy.entries[observed.relative_path],)
    )
    files = {
        member.member_id: member for member in members if member.entry_kind == "file"
    }
    summary = m.EvidenceSummary(
        item_count=len(files), bytes_read=sum(v.size_bytes for v in files.values())
    )
    content_digest = m.digest(members)

    def evidence(
        kind: str,
        payload_digest: str = content_digest,
        *,
        status: m.ProofStatus = "passed",
        detail: m.EvidenceSummary = summary,
    ) -> m.EvidenceRef:
        return m.EvidenceRef(
            evidence_id=str(uuid4()),
            kind=kind,
            producer_id=request.producer_id,
            producer_version=__version__,
            observed_at=now,
            received_at=now,
            payload_digest=payload_digest,
            status=status,
            summary=detail,
        )

    writer = evidence(
        "windows_share_read",
        detail=m.EvidenceSummary(active_writers=False, item_count=len(files)),
    )
    source_before = evidence("held_source_full_read")
    source_after = evidence("held_source_recheck")
    verified = evidence("independent_copy_sha256")
    probes = tuple(
        m.MediaProbe(
            item_id=item_id,
            width=raw.display_width,
            height=raw.display_height,
            duration_ms=raw.duration_ms,
            video_streams=sum(v.codec_type == "video" for v in raw.streams),
            audio_streams=sum(v.codec_type == "audio" for v in raw.streams),
            evidence=evidence(
                "ffprobe",
                raw.payload_sha256,
                status="passed"
                if raw.display_width is not None
                and raw.display_height is not None
                and raw.duration_ms is not None
                and any(v.codec_type == "video" for v in raw.streams)
                else "unknown",
                detail=m.EvidenceSummary(item_ids=(item_id,)),
            ),
            raw_facts=raw,
        )
        for item_id in request.video_ids
        for raw in (facts[item_id],)
    )
    probe_by_id = {v.item_id: v for v in probes}
    revisions = {v.film_id: v.input_revision for v in request.input_revisions}
    binding = m.InputSourceBinding(
        root_id=config.root_id,
        root_config_revision=admission.root_config_revision,
        package_id=admission.package_id,
        source_generation=admission.source_generation,
        snapshot_id=admission.snapshot_id,
        grouping_revision=admission.grouping_revision,
        package_membership_digest=admission.observation_digest,
        grouping_digest=m.digest(grouping),
    )
    manifests = []
    for group in grouping.film_groups:
        relations = list(group.members)
        relations.extend(
            im.GroupedMember(member_id=v.member_id, role=v.role)
            for v in grouping.shared_members
            if group.film_id in v.film_ids
        )
        items = tuple(
            m.ManifestItem(
                item_id=relation.member_id,
                role=cast(m.ItemRole, relation.role),
                relative_path=by_id[relation.member_id].relative_path,
                size_bytes=files[relation.member_id].size_bytes,
                sha256=cast(str, files[relation.member_id].sha256),
                source_root_id=config.root_id,
                native_identity=files[relation.member_id].source_identity,
                volume_ordinal=relation.volume_ordinal,
                video_item_id=relation.video_member_id,
                language=relation.language,
            )
            for relation in relations
        )
        work_items = tuple(
            m.WorkcopyItem(
                source_item_id=item.item_id,
                root_id=config.work_root_id,
                relative_path=f"{relative_work_path}/{item.relative_path}",
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                native_identity=files[item.item_id].copy_identity,
            )
            for item in items
        )
        readiness = m.ReadinessEvidence(
            policy_revision=config.policy.policy_revision,
            stable_started_at=request.stable_started_at,
            stable_ended_at=observation.observed_at,
            membership_digest=admission.observation_digest,
            writer_check=writer,
            media_probes=tuple(probe_by_id[v] for v in group.video_member_ids),
            full_read=source_before,
            source_recheck=source_after,
            workcopy=m.WorkcopyEvidence(
                manifest_hash=m.digest(work_items),
                items=work_items,
                verification=verified,
            ),
        )
        payload = dict(
            schema_version=1,
            film_id=group.film_id,
            revision=revisions[group.film_id],
            items=items,
            readiness=readiness,
            policy_revision=config.policy.policy_revision,
            source_binding=binding,
        )
        manifests.append(
            m.InputManifest.model_validate(
                {**payload, "manifest_hash": m.manifest_digest(payload)}
            )
        )
    snapshot = im.PackageSnapshot(
        admission=admission,
        members=members,
        grouping=grouping,
        manifests=tuple(manifests),
        media_probes=probes,
        eligibility=evaluate_eligibility(
            probes, config.profiles, policy_revision=config.policy.policy_revision
        ),
        completion_digest="0" * 64,
        source_before=source_before,
        source_after=source_after,
        workcopy_verification=verified,
        relative_work_path=relative_work_path,
    )
    return snapshot.model_copy(
        update={"completion_digest": completion_digest(snapshot)}
    )
