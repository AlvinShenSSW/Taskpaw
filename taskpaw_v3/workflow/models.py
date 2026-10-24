"""Version-one workflow values. No I/O, credentials, schedulers or executors."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Annotated, Literal, get_origin
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretBytes,
    SecretStr,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import Self

from taskpaw_v3.workflow.raw_media_models import RawMediaFacts


class WorkflowError(ValueError):
    """Stable, non-secret domain error shared by store and transport adapters."""

    def __init__(
        self, code: str, *, entity_id: str | None = None, retryable: bool = False
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("invalid_error_code")
        self.code = code
        self.entity_id = entity_id
        self.retryable = retryable
        super().__init__(code)


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        _reject_secrets(value.model_dump(mode="python"))
        return _json_value(value.model_dump(mode="json"))
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(type(key) is str for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("non_json_value")


def _reject_secrets(value: object) -> None:
    if isinstance(value, (SecretStr, SecretBytes)):
        raise ValueError("secret_not_json")
    if isinstance(value, dict):
        for item in value.values():
            _reject_secrets(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_secrets(item)


def canonical_json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _id(value: object) -> str:
    if type(value) is not str:
        raise ValueError("canonical_uuid_required")
    try:
        valid = str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("canonical_uuid_required")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("sha256_required")
    return value


def _time(value: object) -> datetime:
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("aware_time_required") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("aware_time_required")
    return value.astimezone(timezone.utc)


def _time_json(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


def _relative(value: object) -> str:
    if type(value) is not str or not value or len(value) > 1024:
        raise ValueError("relative_path_required")
    if any(char in value for char in '\\:\x00<>"|?*') or any(
        ord(c) < 32 for c in value
    ):
        raise ValueError("ambiguous_relative_path")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("noncanonical_relative_path")
    for part in value.split("/"):
        if (
            not part
            or part in (".", "..")
            or part.endswith((".", " "))
            or _RESERVED.match(part)
        ):
            raise ValueError("ambiguous_relative_path")
    return value


Id = Annotated[str, BeforeValidator(_id)]
Digest = Annotated[str, BeforeValidator(_digest)]
UtcTime = Annotated[
    datetime,
    BeforeValidator(_time),
    PlainSerializer(_time_json, return_type=str, when_used="json"),
]
Revision = Annotated[int, Field(strict=True, gt=0)]
Count = Annotated[int, Field(strict=True, ge=0)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=2048)]
RelativePath = Annotated[str, BeforeValidator(_relative)]
Stage = Literal[
    "intake",
    "restore",
    "asr",
    "translate",
    "return",
    "scrape",
    "archive",
    "cleanup_input",
    "cleanup_workcopy",
]
StageStatus = Literal[
    "pending", "running", "succeeded", "skipped", "failed", "unknown", "blocked"
]
Role = Literal[
    "operator",
    "compute_worker",
    "intake_adapter",
    "receiver_adapter",
    "scrape_adapter",
    "archive_adapter",
    "cleanup_adapter",
    "observer",
]
ComponentRole = Literal[
    "intake_adapter",
    "receiver_adapter",
    "scrape_adapter",
    "archive_adapter",
    "cleanup_adapter",
    "observer",
]
ItemRole = Literal[
    "main_video",
    "source_subtitle",
    "generated_subtitle",
    "nfo",
    "poster",
    "fanart",
    "thumb",
    "folder_cover",
    "attachment",
]
ProofStatus = Literal["passed", "failed", "unknown"]
CleanupScope = Literal["input", "workcopy"]
EnrollmentState = Literal[
    "pending_pairing", "pending_validation", "verified", "retired"
]
AdmissionState = Literal["disabled", "enabled", "draining", "revoked"]


class Model(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported_schema_version")
        return value

    @field_validator("*", mode="before")
    @classmethod
    def _arrays(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields.get(info.field_name or "")
        if isinstance(value, list) and field and get_origin(field.annotation) is tuple:
            return tuple(value)
        return value


class CommandMeta(Model):
    request_id: Id
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)]
    authority_id: Id
    authority_epoch: Revision


class CommandResult(Model):
    request_id: Id
    result_id: Id
    entity_id: Id
    version: Revision
    status: Text
    audit_id: Revision
    operation_id: Id | None = None
    assignment_id: Id | None = None
    attempt_id: Id | None = None
    context_revision: Revision | None = None
    context_hash: Digest | None = None


class TlsBinding(Model):
    authority_id: Id
    certificate_sha256: Digest
    binding_revision: Revision


class AuthorityBootstrap(Model):
    authority_id: Id
    epoch: Revision
    operator_principal_id: Id
    operator_credential_id: Id
    operator_secret_digest: Digest = Field(repr=False)
    operator_expires_at: UtcTime
    tls: TlsBinding
    lease_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)]

    @model_validator(mode="after")
    def _authority(self) -> Self:
        if self.tls.authority_id != self.authority_id:
            raise ValueError("authority_binding_mismatch")
        return self


class NativeIdentity(Model):
    scheme: Literal["windows_file_id", "posix_inode", "export_object_id"]
    root_id: Id
    volume_or_export_id: Text
    object_id: Text
    observed_at: UtcTime
    evidence_id: Id


Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
LTX_STAGE_NAMES = 8  # Jasna has four; anything beyond this bound is not a stage


class RestoreTiming(Model):
    """U1e: the wall-clock seconds Taskpaw saw each Jasna phase as the current
    one while restoring (`timing.PhaseTimer`), summed over every LTX run and
    volume. `ltx` is the four restore-mode stages (`ltx_stages`, by name) only;
    the speed test (`speed_test`) is counted apart. `wall` is how long the Jasna
    children ran."""

    scan: Seconds = 0.0
    basicvsr: Seconds = 0.0
    ltx: Seconds = 0.0
    speed_test: Seconds = 0.0
    ltx_stages: dict[Annotated[str, Field(min_length=1, max_length=32)], Seconds] = (
        Field(default_factory=dict, max_length=LTX_STAGE_NAMES)
    )
    ltx_runs: Count = 0
    wall: Seconds = 0.0


Watts = Annotated[float, Field(ge=0, le=100_000, allow_inf_nan=False)]
WattHours = Annotated[float, Field(ge=0, le=10_000_000, allow_inf_nan=False)]


class AttemptEnergy(Model):
    """PWR-2 (#288, PW04/PW05): the machine's estimated energy the film owned
    between the attempt's claim and its resolution (`power.EnergyDelta`).
    Every figure is an estimate (估算)."""

    wh: WattHours = 0.0
    gpu_wh: WattHours = 0.0
    cpu_wh: WattHours = 0.0
    other_wh: WattHours = 0.0
    seconds: Seconds = 0.0
    avg_w: Watts | None = None
    peak_w: Watts | None = None


class EvidenceSummary(Model):
    item_ids: tuple[Id, ...] = ()
    note: Text | None = None
    item_count: Count | None = None
    bytes_read: Count | None = None
    active_writers: bool | None = None
    # U1e: a restore success's film timing; absent elsewhere and in older evidence
    timing: RestoreTiming | None = None
    # PWR-2: every resolved compute / component attempt's energy, failed ones
    # included; absent without a power reading and in older evidence (PW05).
    # Older builds cannot read it back (strict validation; PW14, accepted).
    energy: AttemptEnergy | None = None

    @model_serializer(mode="wrap")
    def _historical_wire(self, handler: SerializerFunctionWrapHandler) -> dict:
        payload = handler(self)
        if self.timing is None:
            payload.pop("timing", None)
        if self.energy is None:
            payload.pop("energy", None)
        return payload


class EvidenceRef(Model):
    evidence_id: Id
    kind: Text
    producer_id: Id
    producer_version: Text
    observed_at: UtcTime
    received_at: UtcTime
    payload_digest: Digest
    status: ProofStatus
    summary: EvidenceSummary = Field(default_factory=EvidenceSummary)

    @model_validator(mode="after")
    def _bound(self) -> Self:
        if len(canonical_json(self.summary).encode("utf-8")) > 16384:
            raise ValueError("evidence_too_large")
        return self


class MediaProbe(Model):
    item_id: Id
    width: Revision | None
    height: Revision | None
    duration_ms: Count | None
    video_streams: Count
    audio_streams: Count
    evidence: EvidenceRef
    raw_facts: RawMediaFacts | None = None

    @model_serializer(mode="wrap")
    def _historical_wire(self, handler: SerializerFunctionWrapHandler) -> dict:
        payload = handler(self)
        if self.raw_facts is None:
            payload.pop("raw_facts", None)
        return payload

    @model_validator(mode="after")
    def _success(self) -> Self:
        if self.raw_facts is not None:
            raw = self.raw_facts
            if self.evidence.payload_digest != raw.payload_sha256:
                raise ValueError("media_probe_payload_mismatch")
            if (
                self.width != raw.display_width
                or self.height != raw.display_height
                or self.duration_ms != raw.duration_ms
                or self.video_streams
                != sum(stream.codec_type == "video" for stream in raw.streams)
                or self.audio_streams
                != sum(stream.codec_type == "audio" for stream in raw.streams)
            ):
                raise ValueError("media_probe_projection_mismatch")
        if self.evidence.status == "passed" and (
            self.width is None
            or self.height is None
            or self.duration_ms is None
            or self.video_streams == 0
        ):
            raise ValueError("incomplete_media_probe")
        return self


class ManifestItem(Model):
    item_id: Id
    role: ItemRole
    relative_path: RelativePath
    size_bytes: Count
    sha256: Digest
    source_root_id: Id
    native_identity: NativeIdentity | None
    parent_item_id: Id | None = None
    volume_ordinal: Revision | None = None
    video_item_id: Id | None = None
    language: Text | None = None

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if self.native_identity and self.native_identity.root_id != self.source_root_id:
            raise ValueError("item_root_identity_mismatch")
        if self.role in ("source_subtitle", "generated_subtitle") and (
            not self.video_item_id or not self.language
        ):
            raise ValueError("subtitle_mapping_required")
        return self


class WorkcopyItem(Model):
    source_item_id: Id
    root_id: Id
    relative_path: RelativePath
    size_bytes: Count
    sha256: Digest
    native_identity: NativeIdentity | None


class WorkcopyEvidence(Model):
    manifest_hash: Digest
    items: tuple[WorkcopyItem, ...]
    verification: EvidenceRef


class ReadinessEvidence(Model):
    mode: Literal["ordinary_directory"] = "ordinary_directory"
    policy_revision: Revision
    stable_started_at: UtcTime
    stable_ended_at: UtcTime
    membership_digest: Digest
    writer_check: EvidenceRef
    media_probes: tuple[MediaProbe, ...]
    full_read: EvidenceRef
    workcopy: WorkcopyEvidence
    source_recheck: EvidenceRef

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.stable_ended_at <= self.stable_started_at:
            raise ValueError("invalid_stability_window")
        if (
            self.writer_check.status == "passed"
            and self.writer_check.summary.active_writers is not False
        ):
            raise ValueError("writer_evidence_required")
        return self


class FilmPolicy(Model):
    policy_revision: Revision
    required_stages: tuple[Stage, ...]
    subtitle_requirement: Literal["chinese", "original", "none"]
    skippable_stages: tuple[Literal["restore", "asr", "translate"], ...] = ()
    skip_evidence_kinds: tuple[Text, ...] = ()
    required_image_roles: tuple[
        Literal["poster", "fanart", "thumb", "folder_cover"], ...
    ]
    folder_cover_required: bool
    allowed_attachment_extensions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def _mandatory(self) -> Self:
        if not {
            "intake",
            "return",
            "scrape",
            "archive",
            "cleanup_input",
            "cleanup_workcopy",
        }.issubset(self.required_stages):
            raise ValueError("mandatory_stages_required")
        if "poster" not in self.required_image_roles:
            raise ValueError("poster_required")
        return self


class FilmCreate(Model):
    film_id: Id
    intake_session_id: Id
    display_name: Text
    policy: FilmPolicy


class FilmRecord(FilmCreate):
    current_input_revision: Revision | None = None
    current_final_revision: Revision | None = None
    current_context_revision: Revision | None = None
    context_state: Literal["ready", "blocked", "invalidated"] | None = None
    version: Revision
    created_at: UtcTime
    # P3 E3 (#304): the operator deleted the task; terminal, the readers skip it
    removed_at: UtcTime | None = None

    @model_serializer(mode="wrap")
    def _legacy_shape(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        if self.removed_at is None:
            value.pop("removed_at", None)
        return value


def _members(items: tuple[ManifestItem, ...]) -> None:
    if not items or len(items) > 4096:
        raise ValueError("manifest_item_bound")
    by_id = {item.item_id: item for item in items}
    if len(by_id) != len(items):
        raise ValueError("duplicate_item_id")
    paths = {item.relative_path.casefold() for item in items}
    if len(paths) != len(items):
        raise ValueError("case_colliding_path")
    if not any(item.role == "main_video" for item in items):
        raise ValueError("main_video_required")
    for item in items:
        if item.video_item_id and (
            item.video_item_id not in by_id
            or by_id[item.video_item_id].role != "main_video"
        ):
            raise ValueError("invalid_video_relation")
        seen = {item.item_id}
        parent = item.parent_item_id
        while parent:
            if parent not in by_id or parent in seen:
                raise ValueError("invalid_parent_relation")
            seen.add(parent)
            parent = by_id[parent].parent_item_id


def _content(value: object, *, root: bool = True) -> object:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        result = {
            key: _content(item, root=False)
            for key, item in value.items()
            if key != "received_at" and not (root and key == "manifest_hash")
        }
        # Only these additive optional fields have an omitted-None wire form.
        # Keep historical manifests and their receipt-bound hashes unchanged.
        if {"film_id", "revision", "items", "readiness"} <= value.keys() and value.get(
            "source_binding"
        ) is None:
            result.pop("source_binding", None)
        if {
            "item_id",
            "width",
            "height",
            "duration_ms",
            "video_streams",
            "audio_streams",
            "evidence",
        } <= value.keys() and value.get("raw_facts") is None:
            result.pop("raw_facts", None)
        # These are sets of identities, not the order of business operations.
        for key, identity in (
            ("items", "item_id"),
            ("items", "source_item_id"),
            ("media_probes", "item_id"),
        ):
            items = result.get(key)
            if isinstance(items, list) and all(
                isinstance(item, dict) and identity in item for item in items
            ):
                result[key] = sorted(items, key=lambda item: item[identity])
        for key in (
            "item_ids",
            "video_item_ids",
            "volume_item_ids",
            "required_image_item_ids",
            "actual_marks",
        ):
            members = result.get(key)
            if isinstance(members, list) and all(type(item) is str for item in members):
                result[key] = sorted(members)
        return result
    if isinstance(value, (list, tuple)):
        return [_content(item, root=False) for item in value]
    return value


def manifest_digest(value: object) -> str:
    return digest(_content(value))


class InputSourceBinding(Model):
    """Exact intake snapshot identity; it contains no recursive manifest hash."""

    root_id: Id
    root_config_revision: Revision
    package_id: Id
    source_generation: Revision
    snapshot_id: Id
    grouping_revision: Revision
    package_membership_digest: Digest
    grouping_digest: Digest


class InputManifest(Model):
    film_id: Id
    revision: Revision
    items: tuple[ManifestItem, ...]
    readiness: ReadinessEvidence
    policy_revision: Revision
    manifest_hash: Digest
    source_binding: InputSourceBinding | None = None

    @model_serializer(mode="wrap")
    def _legacy_shape(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        value: dict[str, object] = handler(self)
        if self.source_binding is None:
            value.pop("source_binding", None)
        return value

    @model_validator(mode="after")
    def _manifest(self) -> Self:
        _members(self.items)
        if self.readiness.policy_revision != self.policy_revision:
            raise ValueError("readiness_policy_mismatch")
        videos = {item.item_id for item in self.items if item.role == "main_video"}
        probes = [probe.item_id for probe in self.readiness.media_probes]
        if set(probes) != videos or len(set(probes)) != len(probes):
            raise ValueError("media_probe_mapping_required")
        copied = {item.source_item_id: item for item in self.readiness.workcopy.items}
        if len(copied) != len(self.readiness.workcopy.items) or set(copied) != {
            item.item_id for item in self.items
        }:
            raise ValueError("workcopy_mapping_required")
        if any(
            (copied[item.item_id].sha256, copied[item.item_id].size_bytes)
            != (item.sha256, item.size_bytes)
            for item in self.items
        ):
            raise ValueError("workcopy_content_mismatch")
        if manifest_digest(self) != self.manifest_hash:
            raise ValueError("manifest_hash_mismatch")
        return self


class ToolVersions(Model):
    tool: Text
    tool_version: Text
    model: Text
    model_version: Text
    profile: Text
    profile_revision: Revision
    config_digest: Digest


class RestorationResult(Model):
    status: Literal["applied", "reused", "not_requested", "failed", "unknown"]
    video_item_ids: tuple[Id, ...]
    evidence: EvidenceRef


class SubtitleResult(Model):
    status: Literal["present", "no_speech", "not_requested", "failed", "unknown"]
    language: Text | None
    item_ids: tuple[Id, ...]
    evidence: EvidenceRef

    @model_validator(mode="after")
    def _subtitle(self) -> Self:
        if self.status == "present" and (not self.item_ids or not self.language):
            raise ValueError("subtitle_result_required")
        if self.status in ("no_speech", "not_requested") and self.item_ids:
            raise ValueError("unexpected_subtitle_items")
        if self.status == "no_speech" and self.evidence.status != "passed":
            raise ValueError("no_speech_evidence_required")
        return self


class PostprocessItem(Model):
    item_id: Id
    sha256: Digest
    status: ProofStatus


class PostprocessEvidence(Model):
    attempt_id: Id
    tool_version: Text
    config_digest: Digest
    status: ProofStatus
    full_process: bool
    items: tuple[PostprocessItem, ...]
    nfo_item_id: Id
    poster_item_id: Id
    required_image_item_ids: tuple[Id, ...]
    actual_marks: tuple[
        Literal["restored", "uncensored", "chinese_subtitles", "4k"], ...
    ]
    image_refresh: Literal["fresh", "refreshed", "not_needed", "failed", "unknown"]
    folder_required: bool
    folder_item_id: Id | None
    parent_context: RelativePath | None
    volume_item_ids: tuple[Id, ...]
    evidence: EvidenceRef

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.status == "passed" and (
            not self.full_process
            or self.evidence.status != "passed"
            or self.image_refresh in ("failed", "unknown")
            or any(item.status != "passed" for item in self.items)
            or (
                self.folder_required
                and (not self.folder_item_id or not self.parent_context)
            )
        ):
            raise ValueError("incomplete_postprocess")
        return self


class FinalManifest(Model):
    film_id: Id
    revision: Revision
    input_revision: Revision
    input_hash: Digest
    items: tuple[ManifestItem, ...]
    output_root_id: Id
    output_relative_path: RelativePath
    tool_versions: ToolVersions
    restoration: RestorationResult
    subtitles: SubtitleResult
    postprocess: PostprocessEvidence
    media_probes: tuple[MediaProbe, ...]
    manifest_hash: Digest

    @model_validator(mode="after")
    def _manifest(self) -> Self:
        _members(self.items)
        by_id = {item.item_id: item for item in self.items}
        pp = self.postprocess
        for key, role in (
            (pp.nfo_item_id, "nfo"),
            (pp.poster_item_id, "poster"),
            (pp.folder_item_id, "folder_cover"),
        ):
            if key and (
                key not in by_id
                or by_id[key].role != role
                or by_id[key].size_bytes == 0
            ):
                raise ValueError("postprocess_role_mismatch")
        outputs = {item.item_id: item for item in pp.items}
        required = {pp.nfo_item_id, pp.poster_item_id, *pp.required_image_item_ids}
        if pp.folder_required and pp.folder_item_id:
            required.add(pp.folder_item_id)
        if len(outputs) != len(pp.items) or not required.issubset(outputs):
            raise ValueError("postprocess_items_missing")
        if any(
            key not in by_id or value.sha256 != by_id[key].sha256
            for key, value in outputs.items()
        ):
            raise ValueError("postprocess_hash_mismatch")
        if any(
            key not in by_id
            or by_id[key].role not in ("poster", "fanart", "thumb", "folder_cover")
            for key in pp.required_image_item_ids
        ):
            raise ValueError("postprocess_image_role_mismatch")
        for key in self.subtitles.item_ids:
            if (
                key not in by_id
                or by_id[key].role not in ("source_subtitle", "generated_subtitle")
                or by_id[key].size_bytes == 0
                or by_id[key].language != self.subtitles.language
            ):
                raise ValueError("subtitle_result_mismatch")
        if "chinese_subtitles" in pp.actual_marks and (
            self.subtitles.status != "present"
            or not self.subtitles.language
            or not self.subtitles.language.lower().startswith("zh")
            or self.subtitles.evidence.status != "passed"
        ):
            raise ValueError("false_chinese_subtitle_mark")
        if ("restored" in pp.actual_marks or "uncensored" in pp.actual_marks) and (
            self.restoration.status not in ("applied", "reused")
            or self.restoration.evidence.status != "passed"
        ):
            raise ValueError("false_restoration_mark")
        videos = {item.item_id for item in self.items if item.role == "main_video"}
        if (
            set(self.restoration.video_item_ids) - videos
            or set(pp.volume_item_ids) - videos
        ):
            raise ValueError("invalid_final_video_relation")
        if (
            self.restoration.status in ("applied", "reused")
            and set(self.restoration.video_item_ids) != videos
        ):
            raise ValueError("restoration_video_mapping_required")
        if {probe.item_id for probe in self.media_probes} != videos or len(
            self.media_probes
        ) != len(videos):
            raise ValueError("final_media_probe_mapping_required")
        if "4k" in pp.actual_marks and not all(
            probe.evidence.status == "passed"
            and probe.width is not None
            and probe.width >= 3840
            for probe in self.media_probes
        ):
            raise ValueError("false_resolution_mark")
        if manifest_digest(self) != self.manifest_hash:
            raise ValueError("manifest_hash_mismatch")
        return self


class HardwareDeclaration(Model):
    revision: Revision
    source: Text
    observed_at: UtcTime
    cpu: Text
    ram_mib: Revision
    gpu_model: Text
    vram_mib: Revision


class ProfileCapability(Model):
    profile_id: Text
    profile_revision: Revision
    tool: Text
    tool_version: Text
    model: Text
    model_version: Text
    allowed_stages: tuple[Literal["restore", "asr", "translate"], ...]
    max_width: Revision
    max_height: Revision


class HardwareProbe(HardwareDeclaration):
    evidence: EvidenceRef
    device_identity: Text
    gpu_identity: Text
    free_disk_bytes: Count
    model_ready: bool
    # P2-2 (#293): a remote worker's proposed profiles (its executors' tools);
    # the operator approves a subset. Absent from older rows and local probes.
    profiles: tuple[ProfileCapability, ...] = ()
    # P2-2 (#293, CX9): a fingerprint of the worker's executor settings; a
    # change needs 核验 again. Absent from older rows and local probes.
    config_digest: Digest | None = None

    @model_serializer(mode="wrap")
    def _historical_wire(self, handler: SerializerFunctionWrapHandler) -> dict:
        payload = handler(self)
        if not self.profiles:
            payload.pop("profiles", None)
        if self.config_digest is None:
            payload.pop("config_digest", None)
        return payload


class ApprovedCapability(Model):
    revision: Revision
    probe_revision: Revision
    approved_by: Id
    approved_at: UtcTime
    profiles: tuple[ProfileCapability, ...]


class CapabilityRevision(Model):
    revision: Revision
    declared: HardwareDeclaration | None
    probed: HardwareProbe | None
    approved: ApprovedCapability | None

    @model_validator(mode="after")
    def _approved(self) -> Self:
        if self.approved and (
            not self.probed
            or self.probed.evidence.status != "passed"
            or self.approved.probe_revision != self.probed.revision
            or not self.approved.profiles
        ):
            raise ValueError("approved_probe_required")
        if (
            self.approved
            and self.declared
            and self.probed
            and (
                self.declared.gpu_model != self.probed.gpu_model
                or self.declared.vram_mib != self.probed.vram_mib
            )
        ):
            raise ValueError("hardware_declaration_conflict")
        return self


class PhysicalGpuBinding(Model):
    resource_id: Id
    device_id: Id
    device_identity: Text
    gpu_identity: Text
    capability_revision: Revision
    evidence: EvidenceRef


class WorkerRegistration(Model):
    worker_id: Id
    device_id: Id
    monitoring_reference: Text | None = None
    name: Text
    address: Text
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    network_revision: Revision = 1
    workflow_participation: bool = False
    role: Literal["compute_worker"] = "compute_worker"


class WorkerRecord(WorkerRegistration):
    authorization_revision: Revision
    enrollment_state: EnrollmentState
    admission_state: AdmissionState
    boot_id: Id | None
    version: Revision
    capability: CapabilityRevision | None = None
    physical_binding: PhysicalGpuBinding | None = None


class PairingRecord(Model):
    pairing_id: Id
    worker_id: Id
    device_id: Id
    ticket_digest: Digest = Field(repr=False)
    expires_at: UtcTime
    consumed_at: UtcTime | None
    credential_id: Id | None
    request_digest: Digest | None


class CredentialRecord(Model):
    credential_id: Id
    principal_id: Id
    worker_id: Id | None
    component_id: Id | None
    boot_id: Id | None
    secret_digest: Digest = Field(repr=False)
    generation: Revision
    role: Role
    issued_at: UtcTime
    expires_at: UtcTime
    revoked_at: UtcTime | None
    version: Revision


class StageRecord(Model):
    job_id: Id
    film_id: Id
    stage: Stage
    input_revision: Revision
    input_hash: Digest
    dependency_job_ids: tuple[Id, ...]
    status: StageStatus
    attempt_id: Id | None
    version: Revision
    evidence: tuple[EvidenceRef, ...] = ()
    reason: Text | None = None


class AttemptRecord(Model):
    attempt_id: Id
    job_id: Id
    attempt_number: Revision
    tool_versions: ToolVersions
    input_hash: Digest
    owner_principal_id: Id
    worker_id: Id | None
    boot_id: Id
    authority_epoch: Revision
    fencing_token: Revision
    status: StageStatus
    started_at: UtcTime
    ended_at: UtcTime | None
    error_code: Text | None
    operation_id: Id
    evidence: tuple[EvidenceRef, ...]


class LocalOwnerEvidence(Model):
    owner_id: Id
    generation: Revision
    boot_id: Id
    process_identity: Text
    state: Literal["running", "stopped", "unknown"]
    evidence: EvidenceRef


class AssignmentRecord(Model):
    assignment_id: Id
    job_id: Id
    attempt_id: Id
    owner_principal_id: Id
    worker_id: Id | None
    boot_id: Id
    authority_epoch: Revision
    fencing_token: Revision
    resource_id: Id | None
    claimed_at: UtcTime
    renewed_at: UtcTime
    expires_at: UtcTime
    status: Literal["reserved", "running", "unknown", "resolved"]
    local_owner: LocalOwnerEvidence | None
    version: Revision


class ResourceOwnerRecord(Model):
    resource_id: Id
    assignment_id: Id
    attempt_id: Id
    generation: Revision
    state: Literal["reserved", "running", "unknown"]
    local_owner: LocalOwnerEvidence | None


class VerifiedItem(Model):
    item_id: Id
    size_bytes: Count
    sha256: Digest
    evidence: EvidenceRef


class ArtifactReceipt(Model):
    receipt_id: Id
    receiver_id: Id
    operation_id: Id
    attempt_id: Id
    input_hash: Digest
    output_manifest_hash: Digest
    status: ProofStatus
    items: tuple[VerifiedItem, ...]
    durable_reference: Id
    accepted_at: UtcTime

    @model_validator(mode="after")
    def _received(self) -> Self:
        if not self.items or len({item.item_id for item in self.items}) != len(
            self.items
        ):
            raise ValueError("receipt_members_required")
        if self.status == "passed" and any(
            item.evidence.status != "passed" for item in self.items
        ):
            raise ValueError("incomplete_receipt")
        return self


class CleanupMember(Model):
    item_id: Id
    root_id: Id
    relative_path: RelativePath
    size_bytes: Count
    sha256: Digest
    native_identity: NativeIdentity

    @model_validator(mode="after")
    def _root(self) -> Self:
        if self.native_identity.root_id != self.root_id:
            raise ValueError("cleanup_root_identity_mismatch")
        return self


class CleanupManifest(Model):
    scope: CleanupScope
    source_manifest_hash: Digest
    members: tuple[CleanupMember, ...]

    @model_validator(mode="after")
    def _members(self) -> Self:
        if not self.members or len(self.members) > 4096:
            raise ValueError("cleanup_member_bound")
        if len({item.item_id for item in self.members}) != len(self.members):
            raise ValueError("duplicate_cleanup_member")
        if len(
            {(item.root_id, item.relative_path.casefold()) for item in self.members}
        ) != len(self.members):
            raise ValueError("duplicate_cleanup_path")
        return self


class TargetIdentity(Model):
    root_id: Id
    host_identity: Text
    export_identity: Text
    native_identity: NativeIdentity
    relative_path: RelativePath
    expected_absent: bool

    @model_validator(mode="after")
    def _root(self) -> Self:
        if self.root_id != self.native_identity.root_id:
            raise ValueError("target_root_identity_mismatch")
        return self


class ApprovalBinding(Model):
    film_id: Id
    input_revision: Revision
    input_hash: Digest
    final_revision: Revision
    final_hash: Digest
    policy_revision: Revision
    postprocess_digest: Digest
    metadata_revision: Revision
    candidate_revision: Revision
    actor_ids: tuple[Id, ...]
    target: TargetIdentity
    input_cleanup: CleanupManifest
    input_cleanup_hash: Digest
    workcopy_cleanup: CleanupManifest
    workcopy_cleanup_hash: Digest

    @model_validator(mode="after")
    def _cleanup(self) -> Self:
        if (
            self.input_cleanup.scope != "input"
            or self.workcopy_cleanup.scope != "workcopy"
        ):
            raise ValueError("cleanup_scope_mismatch")
        if (
            digest(self.input_cleanup) != self.input_cleanup_hash
            or digest(self.workcopy_cleanup) != self.workcopy_cleanup_hash
        ):
            raise ValueError("cleanup_hash_mismatch")
        if (
            self.input_cleanup.source_manifest_hash != self.input_hash
            or self.workcopy_cleanup.source_manifest_hash != self.final_hash
        ):
            raise ValueError("cleanup_source_mismatch")
        if any(
            item.root_id == self.target.root_id
            for item in (*self.input_cleanup.members, *self.workcopy_cleanup.members)
        ):
            raise ValueError("formal_target_cleanup_forbidden")
        return self


class ApprovalObservations(Model):
    metadata: EvidenceRef
    candidate: EvidenceRef
    target: EvidenceRef
    input_cleanup: EvidenceRef
    workcopy_cleanup: EvidenceRef


def context_digest(binding: object, observations: object) -> str:
    return digest(
        {"binding": _json_value(binding), "observations": _json_value(observations)}
    )


class ApprovalContext(Model):
    film_id: Id
    context_revision: Revision
    binding: ApprovalBinding
    observations: ApprovalObservations
    context_hash: Digest

    @model_validator(mode="after")
    def _context(self) -> Self:
        if self.binding.film_id != self.film_id:
            raise ValueError("context_film_mismatch")
        if context_digest(self.binding, self.observations) != self.context_hash:
            raise ValueError("context_hash_mismatch")
        return self


class ApprovalRecord(Model):
    approval_id: Id
    context_revision: Revision
    context_hash: Digest
    binding: ApprovalBinding
    binding_hash: Digest
    operator_principal_id: Id
    issued_at: UtcTime
    expires_at: UtcTime
    authority_epoch: Revision
    revoked_at: UtcTime | None
    revocation_reason: Text | None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if (
            self.expires_at <= self.issued_at
            or digest(self.binding) != self.binding_hash
        ):
            raise ValueError("invalid_approval_envelope")
        return self


class OwnedTargetItem(Model):
    item_id: Id
    relative_path: RelativePath
    native_identity: NativeIdentity | None


class ArchiveOperation(Model):
    operation_id: Id
    approval_id: Id
    binding_hash: Digest
    attempt_id: Id
    adapter_id: Id
    target: TargetIdentity
    status: Literal["admitted", "running", "unknown", "verified", "failed"]
    owned_items: tuple[OwnedTargetItem, ...]
    error_code: Text | None
    evidence: tuple[EvidenceRef, ...]
    version: Revision


class ArchiveReceipt(Model):
    receipt_id: Id
    operation_id: Id
    approval_id: Id
    producer_id: Id
    producer_version: Text
    target: TargetIdentity
    target_directory_identity: NativeIdentity
    final_manifest_hash: Digest
    items: tuple[VerifiedItem, ...]
    commit_marker: Digest
    durable_reference: Id
    accepted_at: UtcTime

    @model_validator(mode="after")
    def _verified(self) -> Self:
        if not self.items or len({item.item_id for item in self.items}) != len(
            self.items
        ):
            raise ValueError("archive_items_required")
        if any(item.evidence.status != "passed" for item in self.items):
            raise ValueError("archive_item_unverified")
        if self.target_directory_identity.root_id != self.target.root_id:
            raise ValueError("archive_directory_identity_mismatch")
        return self


class SourceObservation(Model):
    manifest_hash: Digest
    membership_digest: Digest
    items: tuple[CleanupMember, ...]
    writer_check: EvidenceRef
    content_check: EvidenceRef
    identity_check: EvidenceRef
    membership_check: EvidenceRef
    shared_references_released: EvidenceRef


class CleanupIntent(Model):
    operation_id: Id
    approval_id: Id
    archive_receipt_id: Id
    scope: CleanupScope
    manifest: CleanupManifest
    manifest_hash: Digest
    source_observation: SourceObservation
    version: Revision

    @model_validator(mode="after")
    def _manifest(self) -> Self:
        if (
            self.scope != self.manifest.scope
            or digest(self.manifest) != self.manifest_hash
        ):
            raise ValueError("cleanup_intent_mismatch")
        return self


class CleanupItemResult(Model):
    item_id: Id
    status: Literal[
        "pending",
        "deleted",
        "already_absent",
        "blocked_changed",
        "blocked_writing",
        "blocked_identity",
        "failed",
        "unknown",
    ]
    evidence: EvidenceRef
    error_code: Text | None
    recorded_at: UtcTime


class VersionedCommand(Model):
    expected_version: Revision


class ComponentGrant(Model):
    component_id: Id
    principal_id: Id
    credential_id: Id
    boot_id: Id
    generation: Revision
    role: ComponentRole
    secret_digest: Digest = Field(repr=False)
    expires_at: UtcTime


class CredentialRevocation(VersionedCommand):
    credential_id: Id
    reason: Text


class WorkerUpdate(VersionedCommand):
    worker_id: Id
    address: Text
    port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    network_revision: Revision
    workflow_participation: bool
    admission_state: AdmissionState


class CapabilityUpdate(VersionedCommand):
    worker_id: Id
    capability: CapabilityRevision
    physical_binding: PhysicalGpuBinding | None


class PairingIssue(Model):
    pairing_id: Id
    worker_id: Id
    device_id: Id
    expires_at: UtcTime
    ticket_digest: Digest = Field(repr=False)


class PairingComplete(Model):
    pairing_id: Id
    worker_id: Id
    device_id: Id
    credential_id: Id
    secret_digest: Digest = Field(repr=False)
    generation: Revision
    expires_at: UtcTime


class BootObservation(VersionedCommand):
    worker_id: Id
    boot_id: Id
    known_epoch: Revision
    assignment_ids: tuple[Id, ...]
    local_owners: tuple[LocalOwnerEvidence, ...]


class WorkerRevocation(VersionedCommand):
    worker_id: Id
    reason: Text


class WorkerRetirement(WorkerRevocation):
    pass


class InputManifestPut(VersionedCommand):
    film_id: Id
    manifest: InputManifest

    @model_validator(mode="after")
    def _film(self) -> Self:
        if self.film_id != self.manifest.film_id:
            raise ValueError("manifest_film_mismatch")
        return self


class FinalManifestPut(VersionedCommand):
    film_id: Id
    attempt_id: Id
    manifest: FinalManifest

    @model_validator(mode="after")
    def _film(self) -> Self:
        if (
            self.film_id != self.manifest.film_id
            or self.attempt_id != self.manifest.postprocess.attempt_id
        ):
            raise ValueError("final_attempt_mismatch")
        return self


class ClaimRequest(VersionedCommand):
    job_id: Id
    attempt_id: Id
    operation_id: Id
    worker_id: Id | None
    boot_id: Id
    profile_revision: Revision
    config_digest: Digest
    resource_id: Id | None
    tool_versions: ToolVersions


class AssignmentRenewal(VersionedCommand):
    assignment_id: Id
    worker_id: Id | None
    boot_id: Id
    attempt_id: Id
    authority_epoch: Revision
    fencing_token: Revision
    local_owner: LocalOwnerEvidence | None


class AttemptResult(AssignmentRenewal):
    job_id: Id
    input_hash: Digest
    outcome: Literal["succeeded", "failed", "unknown"]
    result_manifest_hash: Digest | None
    artifact_receipt_id: Id | None
    evidence: tuple[EvidenceRef, ...]
    error_code: Text | None


class UnknownObservation(VersionedCommand):
    assignment_id: Id
    attempt_id: Id
    reason: Text
    operation_id: Id
    local_owner: LocalOwnerEvidence | None
    evidence: tuple[EvidenceRef, ...]


class AssignmentResolution(UnknownObservation):
    outcome: Literal["succeeded", "failed", "blocked"]


class StageRetry(VersionedCommand):
    job_id: Id
    reason: Text


class StageSkip(StageRetry):
    policy_revision: Revision
    input_hash: Digest
    evidence: tuple[EvidenceRef, ...]


class ApprovalContextPut(VersionedCommand):
    film_id: Id
    expected_context_revision: Count
    binding: ApprovalBinding
    observations: ApprovalObservations

    @model_validator(mode="after")
    def _film(self) -> Self:
        if self.film_id != self.binding.film_id:
            raise ValueError("context_film_mismatch")
        return self


class ApprovalCreate(VersionedCommand):
    film_id: Id
    approval_id: Id
    context_revision: Revision
    context_hash: Digest
    binding: ApprovalBinding
    expires_at: UtcTime

    @model_validator(mode="after")
    def _film(self) -> Self:
        if self.film_id != self.binding.film_id:
            raise ValueError("approval_film_mismatch")
        return self


class ApprovalRevocation(VersionedCommand):
    approval_id: Id
    reason: Text


class ArchiveAdmission(VersionedCommand):
    film_id: Id
    operation_id: Id
    approval_id: Id
    binding_hash: Digest
    adapter_id: Id


class ArchiveFailure(VersionedCommand):
    """A terminal archive outcome; `expected_version` is the operation's version."""

    operation_id: Id
    error_code: Text
    evidence: EvidenceRef


class ArchivedPackageRetirement(VersionedCommand):
    """Retire an intake package once every linked film cleaned its input."""

    package_id: Id


class CleanupAdmission(VersionedCommand):
    film_id: Id
    operation_id: Id
    approval_id: Id
    archive_receipt_id: Id
    scope: CleanupScope
    manifest: CleanupManifest
    manifest_hash: Digest
    source_observation: SourceObservation

    @model_validator(mode="after")
    def _manifest(self) -> Self:
        if (
            self.scope != self.manifest.scope
            or digest(self.manifest) != self.manifest_hash
        ):
            raise ValueError("cleanup_admission_mismatch")
        return self


class CleanupResult(VersionedCommand):
    operation_id: Id
    items: tuple[CleanupItemResult, ...]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if not self.items or len({item.item_id for item in self.items}) != len(
            self.items
        ):
            raise ValueError("cleanup_result_members_required")
        return self


class AuditRecord(Model):
    audit_id: Revision
    entity_id: Id
    principal_id: Id
    command_kind: Text
    request_id: Id
    observed_at: UtcTime
    result_id: Id


class AuditPage(Model):
    events: tuple[AuditRecord, ...]
    next_after: Count


LtxTier = Literal["standard", "good", "high"]


class FilmPlanConfirm(Model):
    """U1 P1': confirm one film's plan for its current input revision.

    `ltx_model` is None only for a film whose inputs are all restored already.
    """

    film_id: Id
    input_revision: Revision
    ltx_model: LtxTier | None
    machine: Id | None  # P2-1: None = 自动
    actors: tuple[Text, ...] = ()  # the film's actors as the operator saw them


class FilmPlan(Model):
    """A confirmed plan; immutable, bound to one input revision.

    `backfilled`: written by the v3 upgrade, not chosen by the operator (U04)."""

    film_id: Id
    input_revision: Revision
    ltx_model: LtxTier | None
    machine: Id | None  # the operator's choice at confirm; None = 自动 (P2-1)
    confirmed_at: UtcTime
    backfilled: bool = False
    actors: tuple[Text, ...] = ()  # snapshot at confirm time; () when unknown


RouteKind = Literal["auto", "pinned", "local"]
RoutedStage = Literal["restore", "asr"]


class FilmPin(Model):
    """P2-1 C2: the mutable, CAS'd machine choice of one plan; None = 自动."""

    film_id: Id
    input_revision: Revision
    worker_id: Id | None
    version: Revision
    updated_at: UtcTime


class FilmRoute(Model):
    """P2-1 C9: a film revision routed to one worker. "Claimed" is derived from
    unresolved assignments; `reason` is the persisted human-readable choice (V1)."""

    route_id: Id
    film_id: Id
    input_revision: Revision
    worker_id: Id
    kind: RouteKind
    state: Literal["open", "closed"]
    reason: Text
    routed_at: UtcTime
    closed_at: UtcTime | None = None
    close_reason: Text | None = None
    version: Revision


class RouteOpen(Model):
    """Route a film revision (operator). `expected_pin_version` is the pin row's
    version the decision saw (0: no pin row); `local` routes skip the pin rules."""

    route_id: Id
    film_id: Id
    input_revision: Revision
    worker_id: Id
    kind: RouteKind
    expected_pin_version: Count
    reason: Text


class RouteWithdraw(VersionedCommand):
    """Close an unclaimed open route; `expected_version` is the route's."""

    route_id: Id
    reason: Text


class PinSet(VersionedCommand):
    """改派: change a plan's pin; `expected_version` is the pin's.
    `withdraw_route_id`: the film's open route on a 失联 worker, withdrawn in
    the same transaction when it is still the open route (P2-4 §9 CX4)."""

    film_id: Id
    input_revision: Revision
    worker_id: Id | None
    reason: Text
    withdraw_route_id: Id | None = None


class FilmRemove(VersionedCommand):
    """删除此任务 (P3 E3): a terminal `removed` mark on a film with no work in
    flight and nothing archived; `expected_version` is the film's."""

    film_id: Id
    reason: Text


class StoppedAttestation(VersionedCommand):
    """判定已停止: the operator attests an unresolved compute assignment stopped;
    `expected_version` is the assignment's."""

    assignment_id: Id
    attempt_id: Id
    operation_id: Id
    reason: Text
    evidence: tuple[EvidenceRef, ...]


class RoutingProfile(Model):
    stage: Literal["restore", "asr", "translate"]
    max_width: Revision
    max_height: Revision


class RoutingWorker(Model):
    """A worker as the scheduler sees it in the store (heartbeats come apart)."""

    worker_id: Id
    name: Text
    enrollment_state: EnrollmentState
    admission_state: AdmissionState
    participation: bool
    boot_reconciled: bool
    gpu_model: Text | None
    vram_mib: Count
    profiles: tuple[RoutingProfile, ...]


class RoutingFilm(Model):
    """A film revision's routing facts: the current revision with GPU work left
    or an open route, or an older revision an open route still holds."""

    film_id: Id
    input_revision: Revision
    display_name: Text
    created_at: UtcTime
    planned: bool
    confirmed_at: UtcTime | None
    ltx_model: LtxTier | None
    pin_worker_id: Id | None
    pin_version: Count
    restore: StageStatus
    asr: StageStatus
    translate: StageStatus
    # the worker, stage and claim time of the film's unresolved compute assignment
    claimed_by: Id | None = None
    claimed_stage: Literal["restore", "asr", "translate"] | None = None
    claimed_at: UtcTime | None = None
    max_width: Count = 0
    max_height: Count = 0
    duration_ms: Count | None = None
    source_bytes: Count = 0
    # CX7: the claim's own source check (dirty, rescan, disabled root) passes;
    # an inadmissible film is neither routed nor held by an unclaimed route
    source_admissible: bool = True


class RateSample(Model):
    """One succeeded restore/ASR attempt on a worker: the prediction's history."""

    stage: RoutedStage
    worker_id: Id
    gpu_model: Text | None
    tier: LtxTier | None
    max_width: Count
    max_height: Count
    duration_ms: Count
    wall_seconds: Seconds
    avg_w: Watts | None
    ended_at: UtcTime


class RoutingSnapshot(Model):
    films: tuple[RoutingFilm, ...]  # current revisions
    # CX5: the older revisions open routes still hold, with their own facts
    older_revisions: tuple[RoutingFilm, ...] = ()
    routes: tuple[FilmRoute, ...]  # open routes only
    workers: tuple[RoutingWorker, ...]
    samples: tuple[RateSample, ...]  # newest first


class UnplannedFilm(Model):
    """A film whose current-revision restore waits for a plan (待处理)."""

    film_id: Id
    input_revision: Revision
    display_name: Text
    created_at: UtcTime


class PendingComputeJob(Model):
    """A current-revision local stage whose dependencies are complete.

    `ltx_model`: the planned LTX tier of a restore job (U1 P4'), read once by
    the list that offered the job; None for other stages or no tier.
    """

    job_id: Id
    film_id: Id
    display_name: Text
    stage: Literal[
        "restore",
        "asr",
        "translate",
        "return",
        "scrape",
        "archive",
        "cleanup_input",
        "cleanup_workcopy",
    ]
    input_revision: Revision
    version: Revision
    film_created_at: UtcTime
    ltx_model: LtxTier | None = None


class OpenAssignment(Model):
    """An unresolved assignment, its stage and the owner's component (if any)."""

    assignment: AssignmentRecord
    stage: Stage
    owner_component_id: Id | None


class FilmView(Model):
    film: FilmRecord
    stages: tuple[StageRecord, ...]
    context: ApprovalContext | None


class WorkerView(Model):
    worker: WorkerRecord
    assignments: tuple[AssignmentRecord, ...]


class WorkerAttempt(Model):
    """A current, unresolved compute attempt of the calling worker (C7)."""

    attempt_id: Id
    job_id: Id
    film_id: Id
    input_revision: Revision
    stage: Literal["restore", "asr", "translate"]


class WorkerSelf(Model):
    """A remote worker's own record as its credential reads it (P2-2 `GET /self`)."""

    worker: WorkerRecord
    boot_reconciled: bool


class WorkflowApproval(Model):
    """The newest approval of the film's current approval context."""

    approval_id: Id
    context_revision: Revision
    binding: ApprovalBinding
    binding_hash: Digest
    expires_at: UtcTime
    revoked: bool


class WorkflowArchiveOperation(Model):
    operation_id: Id
    approval_id: Id
    status: Literal["admitted", "running", "unknown", "verified", "failed"]
    version: Revision
    resolved: bool
    error_code: Text | None


class WorkflowCleanupItem(Model):
    item_id: Id
    status: Literal[
        "pending",
        "deleted",
        "already_absent",
        "blocked_changed",
        "blocked_writing",
        "blocked_identity",
        "failed",
        "unknown",
    ]


class WorkflowCleanupOperation(Model):
    operation_id: Id
    approval_id: Id
    scope: CleanupScope
    version: Revision
    status: Literal["admitted", "running", "unknown", "verified"]
    resolved: bool
    items: tuple[WorkflowCleanupItem, ...]


class FilmWorkflow(Model):
    """Approval, archive and cleanup state of one film (F02-P1 D09)."""

    film_id: Id
    version: Revision
    input_revision: Revision | None
    context_revision: Revision | None
    context_state: Literal["ready", "blocked", "invalidated"] | None
    approval: WorkflowApproval | None
    archive_operations: tuple[WorkflowArchiveOperation, ...]
    archive_receipt_id: Id | None
    archive_receipt_approval_id: Id | None
    cleanup_operations: tuple[WorkflowCleanupOperation, ...]
    scrape_status: StageStatus | None
    archive_status: StageStatus | None
    cleanup_input_status: StageStatus | None
    cleanup_workcopy_status: StageStatus | None
    package_id: Id | None
    intake_operation_id: Id | None
    snapshot_id: Id | None
    snapshot_work_path: RelativePath | None
    package_version: Revision | None = None
    package_path: RelativePath | None = None  # under the INBOX root
    package_current: bool = False


class BoardStage(Model):
    """One current-revision stage, its last attempt's note (T06-P1 D02) and its
    own reason, e.g. why it was skipped or blocked (H2 G4)."""

    stage: Stage
    status: StageStatus
    note: Text | None = None
    reason: Text | None = None
    # U1e: the current attempt's times and a restore's phase timing;
    # `first_started_at`: the earliest start among this revision's attempts of
    # the stage, so a failed attempt's time still counts toward 全程
    started_at: UtcTime | None = None
    ended_at: UtcTime | None = None
    first_started_at: UtcTime | None = None
    timing: RestoreTiming | None = None
    # PWR-2: one entry per resolved attempt of this revision, oldest first;
    # None where the attempt has no energy (cut off by a crash) → 「部分」
    attempt_energy: tuple[AttemptEnergy | None, ...] = ()
    # the same attempts' ids, in the same order: the running attempt's live
    # figure is added only when its id is not among them (Codex PWR-2/3 r2)
    attempt_ids: tuple[str, ...] = ()
    # P2-4 R10: the same attempts' workers, in the same order (the machines
    # its energy ran on); None for an attempt with no worker
    worker_ids: tuple[str | None, ...] = ()


class BoardFilm(Model):
    """One film on the 媒体入库 board, read in one locked snapshot (T06-P1 D03, N01).

    `approval_active`: the current context revision has an unrevoked approval
    that no archive operation has used yet (confirmed, waiting to archive).
    `archive_error`: the error code of the film's newest archive operation.
    `package_path` / `package_films`: the INBOX package of the current input
    revision and how many films' current inputs it holds (H3 D06).
    """

    film_id: Id
    display_name: Text
    created_at: UtcTime
    final_name: Text | None  # the current final's film directory name
    stages: tuple[BoardStage, ...]
    approval_active: bool
    archive_open: bool
    archive_error: Text | None
    input_revision: Revision | None = None
    package_path: Text | None = None  # under the INBOX root
    package_films: Count = 0
    # U1 V': the current revision's confirmed plan
    planned: bool = False
    plan_ltx_model: LtxTier | None = None
    plan_machine: Id | None = None
    # U1e: when the operator confirmed it (queue wait); a backfilled plan has none
    plan_confirmed_at: UtcTime | None = None
    plan_backfilled: bool = False
    # V3 §7: the newest archive receipt, both manifests and the largest probe
    receipt_id: Text | None = None
    input_hash: Digest | None = None
    final_revision: Revision | None = None
    final_hash: Digest | None = None
    max_width: Count = 0
    max_height: Count = 0
    # PWR-2: the current input's summed probe duration (the prediction's film-hour)
    duration_ms: Count | None = None


class PackageFilm(Model):
    """A film whose current input revision is linked to an intake package."""

    film_id: Id
    archive_status: StageStatus | None = None
    cleanup_input_status: StageStatus | None
    cleanup_workcopy_status: StageStatus | None


class ExecutionOperation(Model):
    operation_id: Id
    film_id: Id
    kind: Stage
    job_id: Id | None
    attempt_id: Id | None
    status: Literal["admitted", "running", "unknown", "verified", "failed", "resolved"]
    version: Revision


class OperationView(Model):
    archive: ArchiveOperation | None = None
    cleanup: CleanupIntent | None = None
    execution: ExecutionOperation | None = None

    @model_validator(mode="after")
    def _one_operation(self) -> Self:
        if (
            sum(
                item is not None
                for item in (self.archive, self.cleanup, self.execution)
            )
            != 1
        ):
            raise ValueError("one_operation_required")
        return self
