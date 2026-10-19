"""Pure intake wire values. Validation does not establish native ownership."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from typing_extensions import Self

from .models import (
    Count,
    Digest,
    EvidenceRef,
    FilmPolicy,
    Id,
    InputManifest,
    MediaProbe,
    Model,
    NativeIdentity,
    RelativePath,
    Revision,
    Text,
    UtcTime,
    VersionedCommand,
)
from .raw_media_models import FieldOrder, Rational, SpatialFormat

BoundCount = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
MemberCount = Annotated[int, Field(strict=True, ge=1, le=4096)]
Reason = Literal[
    "observed",
    "stable",
    "changed",
    "partial_observation",
    "unknown_identity",
    "writer_busy",
    "writer_unknown",
    "temporary_member",
    "empty_package",
    "missing_video",
    "ambiguous_grouping",
    "volume_gap",
    "duplicate_volume",
    "subtitle_ambiguous",
    "subtitle_language_unknown",
    "unknown_attachment",
    "invalid_nfo",
    "film_id_required",
    "profile_required",
    "media_unknown",
    "eligible",
    "waiting_capacity",
    "snapshot_complete",
    "source_changed",
    "cancelled",
    "probe_failed",
    "copy_failed",
    "owner_unknown",
    "disabled",
    "limit_exceeded",
    "recovery_required",
    "permission_denied",
    "tool_missing",
]
IntakeState = Literal[
    "receiving",
    "waiting_stability",
    "snapshotting",
    "snapshot_ready",
    "operational_ready",
    "needs_attention",
    "blocked",
    "waiting_capacity",
    "unknown_owner",
    "missing",
]
MemberRole = Literal[
    "main_video",
    "source_subtitle",
    "nfo",
    "poster",
    "fanart",
    "thumb",
    "folder_cover",
    "attachment",
    "unknown",
]


def _unique(values: tuple[object, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("duplicate_intake_value")


def _ratio(value: Rational) -> tuple[int, int]:
    return value.numerator, value.denominator


def _birth_key(value: IntakeProcessBirth) -> tuple[int, int]:
    return value.pid, value.creation_filetime_100ns


class ProbeParseLimits(Model):
    max_bytes: Annotated[int, Field(strict=True, ge=64, le=8 * 1024 * 1024)] = (
        4 * 1024 * 1024
    )
    max_streams: Annotated[int, Field(strict=True, ge=1, le=128)] = 128
    max_string_length: Annotated[int, Field(strict=True, ge=32, le=16384)] = 16384
    max_depth: Annotated[int, Field(strict=True, ge=2, le=32)] = 16


class ProbePolicy(Model):
    deadline_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 30
    parse_limits: ProbeParseLimits = Field(default_factory=ProbeParseLimits)
    containers: Annotated[
        tuple[Literal["mov", "matroska", "avi", "mpegts", "mpeg", "h264", "hevc"], ...],
        Field(min_length=1, max_length=7),
    ] = ("mov", "matroska", "avi", "mpegts", "mpeg", "h264", "hevc")

    @model_validator(mode="after")
    def _containers(self) -> Self:
        _unique(self.containers)
        return self


class IntakePolicy(Model):
    policy_revision: Revision
    stable_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 60
    rescan_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 30
    max_members: MemberCount = 4096
    max_depth: Annotated[int, Field(strict=True, ge=1, le=8)] = 8
    max_snapshot_operations: Literal[1] = 1
    max_probes: Literal[1] = 1
    max_dirty_packages: Annotated[int, Field(strict=True, ge=1, le=4096)] = 256
    max_package_bytes: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)] = (
        16 * 1024**4
    )
    min_free_bytes: BoundCount = 10 * 1024**3
    operation_deadline_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = (
        21600
    )
    idle_deadline_seconds: Annotated[int, Field(strict=True, ge=1, le=3600)] = 120
    probe: ProbePolicy = Field(default_factory=ProbePolicy)
    allowed_attachment_extensions: Annotated[
        tuple[Text, ...], Field(max_length=64)
    ] = ()

    @field_validator("max_snapshot_operations", "max_probes", mode="before")
    @classmethod
    def _single(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("single_operation_required")
        return value

    @model_validator(mode="after")
    def _policy(self) -> Self:
        _unique(self.allowed_attachment_extensions)
        if any(
            not re.fullmatch(r"\.[a-z0-9]{1,12}", value)
            for value in self.allowed_attachment_extensions
        ):
            raise ValueError("invalid_attachment_extension")
        if (
            self.idle_deadline_seconds > self.operation_deadline_seconds
            or self.probe.deadline_seconds > self.operation_deadline_seconds
        ):
            raise ValueError("invalid_intake_deadline")
        return self


class IntakeProfile(Model):
    profile_id: Text
    profile_revision: Revision
    policy_revision: Revision
    tool: Text
    tool_version: Text
    min_width: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    max_width: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    min_height: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    max_height: Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
    min_fps: Rational
    max_fps: Rational
    codecs: Annotated[tuple[Text, ...], Field(min_length=1, max_length=64)]
    pixel_formats: Annotated[tuple[Text, ...], Field(min_length=1, max_length=64)]
    field_orders: Annotated[tuple[FieldOrder, ...], Field(min_length=1, max_length=6)]
    sample_aspect_ratios: Annotated[
        tuple[Rational, ...], Field(min_length=1, max_length=16)
    ]
    rotations: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=0, lt=360)], ...],
        Field(min_length=1, max_length=16),
    ]
    spatial_formats: Annotated[
        tuple[SpatialFormat, ...], Field(min_length=1, max_length=4)
    ] = ("flat",)
    approval: EvidenceRef

    @model_validator(mode="after")
    def _constraints(self) -> Self:
        if (
            self.min_width > self.max_width
            or self.min_height > self.max_height
            or self.min_fps.numerator <= 0
            or self.max_fps.numerator * self.min_fps.denominator
            < self.min_fps.numerator * self.max_fps.denominator
        ):
            raise ValueError("invalid_profile_range")
        for values in (
            self.codecs,
            self.pixel_formats,
            self.field_orders,
            self.rotations,
            tuple(_ratio(value) for value in self.sample_aspect_ratios),
        ):
            _unique(values)
        if any(value.numerator <= 0 for value in self.sample_aspect_ratios):
            raise ValueError("invalid_profile_ratio")
        return self


class ProfileBinding(Model):
    profile_id: Text
    profile_revision: Revision
    tool: Text
    tool_version: Text


class ItemEligibility(Model):
    item_id: Id
    raw_payload_digest: Digest | None
    decision: Literal["eligible", "profile_required", "blocked"]
    reason: Literal["eligible", "profile_required", "media_unknown"]
    profiles: Annotated[tuple[ProfileBinding, ...], Field(max_length=64)]

    @model_validator(mode="after")
    def _decision(self) -> Self:
        _unique(
            tuple(
                (
                    value.profile_id,
                    value.profile_revision,
                    value.tool,
                    value.tool_version,
                )
                for value in self.profiles
            )
        )
        if (self.decision == "eligible") != bool(self.profiles) or (
            self.profiles and self.raw_payload_digest is None
        ):
            raise ValueError("invalid_item_eligibility")
        if (
            self.reason
            != {
                "eligible": "eligible",
                "blocked": "media_unknown",
                "profile_required": "profile_required",
            }[self.decision]
        ):
            raise ValueError("eligibility_reason_mismatch")
        return self


class ProcessingEligibility(Model):
    policy_revision: Revision
    decision: Literal["eligible", "profile_required", "blocked"]
    reason: Literal["eligible", "profile_required", "media_unknown"]
    items: Annotated[tuple[ItemEligibility, ...], Field(max_length=4096)]

    @model_validator(mode="after")
    def _decision(self) -> Self:
        _unique(tuple(value.item_id for value in self.items))
        expected = (
            "blocked"
            if not self.items
            or any(value.decision == "blocked" for value in self.items)
            else "profile_required"
            if any(value.decision == "profile_required" for value in self.items)
            else "eligible"
        )
        if (
            self.decision != expected
            or self.reason
            != {
                "eligible": "eligible",
                "blocked": "media_unknown",
                "profile_required": "profile_required",
            }[expected]
        ):
            raise ValueError("invalid_package_eligibility")
        return self


class ObservedMember(Model):
    member_id: Id
    relative_path: RelativePath
    entry_kind: Literal["file", "directory", "reparse", "unknown"]
    native_identity: NativeIdentity | None
    size_bytes: BoundCount | None
    mtime_ns: BoundCount | None
    change_token: Text | None = None
    attributes: Annotated[
        tuple[Literal["hidden", "system", "temporary", "offline", "readonly"], ...],
        Field(max_length=5),
    ] = ()
    temporary: bool = False

    @model_validator(mode="after")
    def _member(self) -> Self:
        _unique(self.attributes)
        if len(self.relative_path.split("/")) > 8:
            raise ValueError("intake_depth_exceeded")
        return self


class GroupedMember(Model):
    member_id: Id
    role: MemberRole
    volume_ordinal: MemberCount | None = None
    video_member_id: Id | None = None
    language: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class FilmIdentity(Model):
    group_key: Text
    film_id: Id


class FilmGroup(FilmIdentity):
    video_member_ids: Annotated[tuple[Id, ...], Field(min_length=1, max_length=4096)]
    members: Annotated[tuple[GroupedMember, ...], Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def _relations(self) -> Self:
        _unique(self.video_member_ids)
        _unique(tuple(value.member_id for value in self.members))
        videos = set(self.video_member_ids)
        if videos != {
            value.member_id for value in self.members if value.role == "main_video"
        }:
            raise ValueError("group_video_mapping_mismatch")
        if any(
            value.video_member_id is not None and value.video_member_id not in videos
            for value in self.members
        ):
            raise ValueError("group_subtitle_mapping_mismatch")
        return self


class SharedMemberRef(Model):
    member_id: Id
    role: MemberRole
    film_ids: Annotated[tuple[Id, ...], Field(min_length=2, max_length=4096)]

    @model_validator(mode="after")
    def _films(self) -> Self:
        _unique(self.film_ids)
        if self.role in ("main_video", "source_subtitle", "unknown"):
            raise ValueError("invalid_shared_member_role")
        return self


class PackageGrouping(Model):
    grouping_revision: Revision
    film_groups: Annotated[tuple[FilmGroup, ...], Field(max_length=4096)]
    shared_members: Annotated[tuple[SharedMemberRef, ...], Field(max_length=4096)] = ()
    directory_member_ids: Annotated[tuple[Id, ...], Field(max_length=4096)] = ()
    unassigned_member_ids: Annotated[tuple[Id, ...], Field(max_length=4096)] = ()
    parent_context: RelativePath | None
    status: Literal["ready", "needs_attention"]
    reasons: Annotated[tuple[Reason, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def _relations(self) -> Self:
        films = {value.film_id: value for value in self.film_groups}
        _unique(tuple(value.film_id for value in self.film_groups))
        _unique(tuple(value.group_key for value in self.film_groups))
        _unique(self.directory_member_ids)
        _unique(self.unassigned_member_ids)
        _unique(self.reasons)
        assigned = tuple(
            member.member_id for film in self.film_groups for member in film.members
        )
        _unique(assigned)
        shared = tuple(value.member_id for value in self.shared_members)
        _unique(shared)
        _unique(
            (
                *assigned,
                *shared,
                *self.directory_member_ids,
                *self.unassigned_member_ids,
            )
        )
        if any(
            not set(value.film_ids).issubset(films) for value in self.shared_members
        ):
            raise ValueError("unknown_shared_film")
        if self.status == "ready" and (
            not films
            or self.reasons
            or self.unassigned_member_ids
            or any(
                value.role == "unknown"
                or (
                    value.role == "source_subtitle"
                    and (value.video_member_id is None or value.language is None)
                )
                for film in self.film_groups
                for value in film.members
            )
        ):
            raise ValueError("incomplete_ready_grouping")
        if self.status == "ready":
            for film in self.film_groups:
                ordinals = [
                    value.volume_ordinal
                    for value in film.members
                    if value.role == "main_video"
                ]
                known = [value for value in ordinals if value is not None]
                if (len(ordinals) > 1 and len(known) != len(ordinals)) or (
                    known
                    and (
                        len(set(known)) != len(known)
                        or set(known) != set(range(1, max(known) + 1))
                    )
                ):
                    raise ValueError("incomplete_ready_volumes")
        if self.status == "needs_attention" and not self.reasons:
            raise ValueError("grouping_reason_required")
        return self


class NfoHint(Model):
    member_id: Id
    identifiers: Annotated[tuple[Text, ...], Field(max_length=16)]
    valid: bool


class IntakeRootPut(Model):
    expected_version: Count
    root_id: Id
    native_identity: NativeIdentity
    private_path: Text = Field(repr=False)
    work_root_id: Id
    work_native_identity: NativeIdentity
    private_work_path: Text = Field(repr=False)
    component_id: Id
    policy: IntakePolicy
    film_policy: FilmPolicy
    profiles: Annotated[tuple[IntakeProfile, ...], Field(max_length=64)] = ()
    enabled: bool

    @model_validator(mode="after")
    def _roots(self) -> Self:
        if (
            self.film_policy.policy_revision != self.policy.policy_revision
            or self.film_policy.allowed_attachment_extensions
            != self.policy.allowed_attachment_extensions
        ):
            raise ValueError("film_intake_policy_mismatch")
        if (
            self.native_identity.root_id != self.root_id
            or self.work_native_identity.root_id != self.work_root_id
            or self.root_id == self.work_root_id
        ):
            raise ValueError("intake_root_identity_mismatch")
        if (
            self.native_identity.scheme,
            self.native_identity.volume_or_export_id,
            self.native_identity.object_id,
        ) == (
            self.work_native_identity.scheme,
            self.work_native_identity.volume_or_export_id,
            self.work_native_identity.object_id,
        ):
            raise ValueError("overlapping_intake_roots")
        _unique(
            tuple((value.profile_id, value.profile_revision) for value in self.profiles)
        )
        if any(
            value.policy_revision != self.policy.policy_revision
            for value in self.profiles
        ):
            raise ValueError("profile_policy_mismatch")
        return self


class IntakeObservation(Model):
    root_id: Id
    root_config_revision: Revision
    package_id: Id
    expected_package_version: Count
    package_native_identity: NativeIdentity | None
    relative_path: RelativePath
    parent_context: RelativePath | None
    observed_at: UtcTime
    coverage: Literal["complete", "partial", "unknown"]
    trigger: Literal[
        "startup", "notification", "rescan", "overflow", "reconnect", "manual"
    ]
    members: Annotated[tuple[ObservedMember, ...], Field(max_length=4096)]
    observation_digest: Digest
    grouping: PackageGrouping | None
    writer_state: Literal["clear", "busy", "unknown"]

    @model_validator(mode="after")
    def _members(self) -> Self:
        _unique(tuple(value.member_id for value in self.members))
        _unique(tuple(value.relative_path.casefold() for value in self.members))
        if (
            self.package_native_identity
            and self.package_native_identity.root_id != self.root_id
        ):
            raise ValueError("package_root_mismatch")
        if any(
            value.native_identity and value.native_identity.root_id != self.root_id
            for value in self.members
        ):
            raise ValueError("member_root_mismatch")
        if self.grouping and self.grouping.parent_context != self.parent_context:
            raise ValueError("parent_context_mismatch")
        return self


class IntakeSnapshotAdmission(VersionedCommand):
    package_id: Id
    source_generation: Revision
    root_config_revision: Revision
    grouping_revision: Revision
    observation_digest: Digest
    operation_id: Id
    snapshot_id: Id
    owner_boot_id: Id
    policy_revision: Revision


class IntakeProcessBirth(Model):
    pid: Annotated[int, Field(strict=True, gt=0, le=2**32 - 1)]
    creation_filetime_100ns: Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
    role: Literal["launcher", "helper", "probe", "member"]
    item_id: Id | None = None
    sequence: MemberCount | None = None

    @model_validator(mode="after")
    def _probe(self) -> Self:
        if (self.role == "probe") != (
            self.item_id is not None and self.sequence is not None
        ) or ((self.item_id is None) != (self.sequence is None)):
            raise ValueError("invalid_probe_birth_binding")
        return self


class IntakeOwner(Model):
    owner_boot_id: Id
    owner_id: Id
    births: Annotated[tuple[IntakeProcessBirth, ...], Field(max_length=4100)]
    observed_total: Annotated[int, Field(strict=True, ge=0, le=4100)]
    observed_active: Annotated[int, Field(strict=True, ge=0, le=5)]
    identity_uncertain: bool
    failure_codes: Annotated[
        tuple[
            Literal[
                "open_failed",
                "identity_failed",
                "wait_failed",
                "close_failed",
                "query_failed",
                "budget_exceeded",
                "io_failed",
                "timeout",
                "ack_failed",
            ],
            ...,
        ],
        Field(max_length=9),
    ]
    observed_at: UtcTime

    @model_validator(mode="after")
    def _budget(self) -> Self:
        _unique(tuple(_birth_key(value) for value in self.births))
        _unique(self.failure_codes)
        if (
            self.observed_active > self.observed_total
            or len(self.births) > self.observed_total
            or (self.failure_codes and not self.identity_uncertain)
        ):
            raise ValueError("inconsistent_intake_owner")
        probes = tuple(value.sequence for value in self.births if value.role == "probe")
        _unique(probes)
        return self


class ProbeBirthNotice(Model):
    operation_id: Id
    item_id: Id
    sequence: MemberCount
    birth: IntakeProcessBirth

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if (
            self.birth.role != "probe"
            or self.birth.item_id != self.item_id
            or self.birth.sequence != self.sequence
        ):
            raise ValueError("probe_notice_mismatch")
        return self


class ProbeResumeAck(ProbeBirthNotice):
    owner_version: Revision


class IntakeOwnerObservation(VersionedCommand):
    operation_id: Id
    source_generation: Revision
    owner_boot_id: Id
    owner: IntakeOwner

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if self.owner.owner_boot_id != self.owner_boot_id:
            raise ValueError("owner_boot_mismatch")
        return self


class IntakeBirthExit(Model):
    birth: IntakeProcessBirth
    signaled: bool
    waited: bool
    handle_closed: bool


class IntakeExitProof(Model):
    operation_id: Id
    owner_boot_id: Id
    owner_id: Id
    source_generation: Revision
    state: Literal["stopped", "unknown", "no_process_created"]
    births: Annotated[tuple[IntakeBirthExit, ...], Field(max_length=4100)]
    job_active_zero: bool
    io_joined: bool
    owned_handles_closed: bool
    failure_codes: Annotated[tuple[Text, ...], Field(max_length=16)]
    observed_at: UtcTime

    @model_validator(mode="after")
    def _exit(self) -> Self:
        _unique(tuple(_birth_key(value.birth) for value in self.births))
        _unique(self.failure_codes)
        if self.state != "unknown" and (
            self.failure_codes
            or not self.io_joined
            or not self.owned_handles_closed
            or any(
                not (value.signaled and value.waited and value.handle_closed)
                for value in self.births
            )
        ):
            raise ValueError("incomplete_intake_exit")
        if self.state == "stopped" and (not self.job_active_zero or not self.births):
            raise ValueError("incomplete_intake_exit")
        if self.state == "no_process_created" and self.births:
            raise ValueError("created_process_not_exempt")
        return self


class SnapshotMember(Model):
    member_id: Id
    relative_path: RelativePath
    entry_kind: Literal["file", "directory"]
    source_identity: NativeIdentity
    copy_identity: NativeIdentity
    size_bytes: BoundCount
    sha256: Digest | None

    @model_validator(mode="after")
    def _copy(self) -> Self:
        if (self.entry_kind == "file") != (self.sha256 is not None) or (
            self.entry_kind == "directory" and self.size_bytes != 0
        ):
            raise ValueError("snapshot_member_hash_required")
        source, copy = self.source_identity, self.copy_identity
        if (source.scheme, source.volume_or_export_id, source.object_id) == (
            copy.scheme,
            copy.volume_or_export_id,
            copy.object_id,
        ):
            raise ValueError("independent_copy_required")
        return self


class PackageSnapshot(Model):
    admission: IntakeSnapshotAdmission
    members: Annotated[tuple[SnapshotMember, ...], Field(min_length=1, max_length=4096)]
    grouping: PackageGrouping
    manifests: Annotated[
        tuple[InputManifest, ...], Field(min_length=1, max_length=4096)
    ]
    media_probes: Annotated[
        tuple[MediaProbe, ...], Field(min_length=1, max_length=4096)
    ]
    eligibility: ProcessingEligibility
    completion_digest: Digest
    source_before: EvidenceRef
    source_after: EvidenceRef
    workcopy_verification: EvidenceRef
    relative_work_path: RelativePath

    @model_validator(mode="after")
    def _package(self) -> Self:
        _unique(tuple(value.member_id for value in self.members))
        _unique(tuple(value.relative_path.casefold() for value in self.members))
        _unique(tuple(value.film_id for value in self.manifests))
        _unique(tuple(value.item_id for value in self.media_probes))
        if (
            self.grouping.status != "ready"
            or self.grouping.grouping_revision != self.admission.grouping_revision
        ):
            raise ValueError("snapshot_grouping_mismatch")
        if {value.film_id for value in self.manifests} != {
            value.film_id for value in self.grouping.film_groups
        }:
            raise ValueError("snapshot_films_mismatch")
        files = {
            value.member_id: value
            for value in self.members
            if value.entry_kind == "file"
        }
        if {
            value.member_id for value in self.members if value.entry_kind == "directory"
        } != set(self.grouping.directory_member_ids):
            raise ValueError("snapshot_directories_mismatch")
        if {
            item.item_id for manifest in self.manifests for item in manifest.items
        } != set(files):
            raise ValueError("snapshot_member_coverage")
        for manifest in self.manifests:
            film_group = next(
                value
                for value in self.grouping.film_groups
                if value.film_id == manifest.film_id
            )
            relations = {
                value.member_id: (
                    value.role,
                    value.video_member_id,
                    value.language,
                    value.volume_ordinal,
                )
                for value in film_group.members
            }
            for value in self.grouping.shared_members:
                if manifest.film_id in value.film_ids:
                    relations[value.member_id] = (value.role, None, None, None)
            if {
                value.item_id: (
                    value.role,
                    value.video_item_id,
                    value.language,
                    value.volume_ordinal,
                )
                for value in manifest.items
            } != relations:
                raise ValueError("snapshot_film_relations_mismatch")
            binding = manifest.source_binding
            if binding is None or (
                binding.package_id,
                binding.source_generation,
                binding.snapshot_id,
                binding.grouping_revision,
                binding.root_config_revision,
            ) != (
                self.admission.package_id,
                self.admission.source_generation,
                self.admission.snapshot_id,
                self.admission.grouping_revision,
                self.admission.root_config_revision,
            ):
                raise ValueError("snapshot_source_binding_mismatch")
            if any(
                item.sha256 != files[item.item_id].sha256
                or item.size_bytes != files[item.item_id].size_bytes
                for item in manifest.items
            ):
                raise ValueError("snapshot_content_mismatch")
        videos = {
            member
            for film in self.grouping.film_groups
            for member in film.video_member_ids
        }
        if {value.item_id for value in self.media_probes} != videos or {
            value.item_id for value in self.eligibility.items
        } != videos:
            raise ValueError("snapshot_video_coverage")
        probes = {value.item_id: value for value in self.media_probes}
        for manifest in self.manifests:
            if any(
                probes.get(value.item_id) != value
                for value in manifest.readiness.media_probes
            ):
                raise ValueError("snapshot_probe_mismatch")
        for item in self.eligibility.items:
            raw = probes[item.item_id].raw_facts
            if raw is None or item.raw_payload_digest != raw.payload_sha256:
                raise ValueError("snapshot_raw_evidence_required")
        if self.eligibility.policy_revision != self.admission.policy_revision or any(
            value.status != "passed"
            for value in (
                self.source_before,
                self.source_after,
                self.workcopy_verification,
            )
        ):
            raise ValueError("snapshot_verification_required")
        return self


class IntakeSnapshotResult(VersionedCommand):
    operation_id: Id
    source_generation: Revision
    outcome: Literal[
        "complete",
        "receiving",
        "needs_attention",
        "blocked",
        "waiting_capacity",
        "cancelled",
        "failed",
    ]
    reason: Reason
    exit_proof: IntakeExitProof
    snapshot: PackageSnapshot | None

    @model_validator(mode="after")
    def _result(self) -> Self:
        if (self.exit_proof.operation_id, self.exit_proof.source_generation) != (
            self.operation_id,
            self.source_generation,
        ):
            raise ValueError("snapshot_exit_binding_mismatch")
        if (self.outcome == "complete") != (self.snapshot is not None):
            raise ValueError("snapshot_result_required")
        if self.snapshot:
            admission = self.snapshot.admission
            if (
                admission.operation_id,
                admission.source_generation,
                admission.owner_boot_id,
            ) != (
                self.operation_id,
                self.source_generation,
                self.exit_proof.owner_boot_id,
            ) or self.exit_proof.state != "stopped":
                raise ValueError("snapshot_result_binding_mismatch")
        return self


class IntakeCancel(VersionedCommand):
    operation_id: Id
    reason: Reason


class IntakeGroupingPut(VersionedCommand):
    package_id: Id
    source_generation: Revision
    grouping: PackageGrouping


class IntakeOperationView(Model):
    operation_id: Id
    version: Revision
    admission: IntakeSnapshotAdmission
    owner: IntakeOwner | None
    exit_proof: IntakeExitProof | None
    publication_allowed: bool
    state: Literal[
        "admitted", "running", "stopping", "completed", "cancelled", "failed", "unknown"
    ]
    result: IntakeSnapshotResult | None


class IntakeView(Model):
    package_id: Id
    root_id: Id
    root_config_revision: Revision
    version: Revision
    source_generation: Revision
    relative_path: RelativePath
    parent_context: RelativePath | None
    state: IntakeState
    reason: Reason
    observed_at: UtcTime
    last_complete_scan_at: UtcTime | None
    grouping: PackageGrouping | None
    media_probes: Annotated[tuple[MediaProbe, ...], Field(max_length=4096)] = ()
    eligibility: ProcessingEligibility | None
    operation_id: Id | None


class IntakeListQuery(Model):
    root_id: Id | None = None
    states: Annotated[tuple[IntakeState, ...], Field(max_length=10)] = ()
    after_package_id: Id | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 50


class IntakePage(Model):
    items: Annotated[tuple[IntakeView, ...], Field(max_length=200)]
    next_package_id: Id | None
