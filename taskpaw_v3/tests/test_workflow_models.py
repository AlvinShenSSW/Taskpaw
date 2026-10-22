"""T01 pure contract checks; no media, services or production configuration."""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from taskpaw_v3.workflow import models as m

FIXTURES = Path(__file__).parent / "fixtures" / "workflow"
UID = "11111111-1111-4111-8111-111111111111"
DIGEST = "a" * 64
STAMP = "2026-10-19T01:00:00Z"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_canonical_json_is_stable_and_utf8():
    assert m.canonical_json({"b": [1, True], "a": "影片"}) == (
        '{"a":"影片","b":[1,true]}'
    )
    assert m.digest({"b": 2, "a": 1}) == m.digest({"a": 1, "b": 2})
    assert m.digest([1, 2]) != m.digest([2, 1])


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), {1: "x"}, {"a"}, SecretStr("do-not-print")]
)
def test_canonical_json_rejects_non_json_and_secrets(value):
    with pytest.raises((TypeError, ValueError)) as exc:
        m.canonical_json(value)
    assert "do-not-print" not in str(exc.value)


@pytest.mark.parametrize("version", [True, False, 0, 2, "1", 1.0])
def test_schema_version_is_strict(version):
    with pytest.raises(ValidationError):
        m.Model(schema_version=version)


def test_extra_fields_and_mutation_are_refused():
    with pytest.raises(ValidationError):
        m.Model(execute="anything")
    value = m.Model()
    with pytest.raises(ValidationError):
        value.schema_version = 2


@pytest.mark.parametrize("value", [True, "1", 1.0, 0, -1])
def test_command_epoch_is_strict_positive(value):
    with pytest.raises(ValidationError):
        m.CommandMeta(
            request_id=UID,
            idempotency_key="request-one",
            authority_id=UID,
            authority_epoch=value,
        )


@pytest.mark.parametrize(
    "value", ["not-a-uuid", "ABCDEFAB-1234-4123-8123-ABCDEFABCDEF", " " + UID]
)
def test_uuid_must_be_canonical(value):
    with pytest.raises(ValidationError):
        m.CommandMeta(
            request_id=value, idempotency_key="one", authority_id=UID, authority_epoch=1
        )


@pytest.mark.parametrize(
    "path",
    [
        "../movie.mkv",
        "/movie.mkv",
        "C:/movie.mkv",
        "//host/share/movie.mkv",
        "a\\b.mkv",
        "a//b",
        "a/./b",
        "a/../b",
        "CON.txt",
        "a/NUL",
        "a/name.",
        "name ",
        "name:stream",
        "a\x00b",
        "LPT¹.txt",
    ],
)
def test_windows_relative_path_ambiguities_are_refused(path):
    row = load("input-manifest-v1.json")["items"][0]
    row["relative_path"] = path
    with pytest.raises(ValidationError):
        m.ManifestItem.model_validate(row)


def test_utc_time_is_aware_and_canonical():
    value = m.TlsBinding(
        authority_id=UID, certificate_sha256=DIGEST, binding_revision=1
    )
    assert value.model_dump()["binding_revision"] == 1
    row = load("input-manifest-v1.json")["items"][0]["native_identity"]
    row["observed_at"] = "2026-10-19T10:00:00+09:00"
    identity = m.NativeIdentity.model_validate(row)
    assert identity.observed_at == datetime(2026, 10, 19, 1, tzinfo=timezone.utc)
    assert identity.model_dump(mode="json")["observed_at"] == STAMP
    for invalid in ("2026-10-19T01:00:00", 0, True):
        row["observed_at"] = invalid
        with pytest.raises(ValidationError):
            m.NativeIdentity.model_validate(row)


@pytest.mark.parametrize(
    "filename,cls",
    [
        ("input-manifest-v1.json", "InputManifest"),
        ("final-manifest-v1.json", "FinalManifest"),
        ("approval-context-v1.json", "ApprovalContext"),
    ],
)
def test_complete_fixtures_roundtrip(filename, cls):
    model_type = getattr(m, cls)
    value = model_type.model_validate(load(filename))
    assert model_type.model_validate_json(value.model_dump_json()) == value


def test_manifest_hash_sorts_members_but_binds_roles_content_and_identity():
    raw = load("input-manifest-v1.json")
    original = m.InputManifest.model_validate(raw)
    raw["items"].reverse()
    assert m.InputManifest.model_validate(raw).manifest_hash == original.manifest_hash
    for field, changed in (
        ("sha256", "b" * 64),
        ("role", "attachment"),
        ("relative_path", "other.mkv"),
    ):
        altered = deepcopy(raw)
        altered["items"][0][field] = changed
        with pytest.raises(ValidationError):
            m.InputManifest.model_validate(altered)
    altered = deepcopy(raw)
    altered["items"][0]["native_identity"]["object_id"] = "replacement"
    with pytest.raises(ValidationError):
        m.InputManifest.model_validate(altered)


def test_manifest_rejects_case_collisions_duplicate_ids_and_wrong_video_relation():
    raw = load("input-manifest-v1.json")
    for changed in ("path", "id", "relation"):
        altered = deepcopy(raw)
        if changed == "path":
            altered["items"][1]["relative_path"] = altered["items"][0][
                "relative_path"
            ].upper()
        elif changed == "id":
            altered["items"][1]["item_id"] = altered["items"][0]["item_id"]
        else:
            altered["items"][1]["video_item_id"] = UID
        altered["manifest_hash"] = m.manifest_digest(altered)
        with pytest.raises(ValidationError):
            m.InputManifest.model_validate(altered)


def test_readiness_evidence_is_required_without_sender_commit():
    raw = load("input-manifest-v1.json")
    assert "sender" not in m.canonical_json(raw)
    value = m.InputManifest.model_validate(raw)
    assert value.readiness.mode == "ordinary_directory"
    del raw["readiness"]["writer_check"]
    with pytest.raises(ValidationError):
        m.InputManifest.model_validate(raw)


def test_final_package_cannot_claim_missing_folder_or_false_chinese_subtitles():
    for changed in ("folder", "language", "empty_subtitle", "image_refresh"):
        raw = load("final-manifest-v1.json")
        if changed == "folder":
            raw["postprocess"]["folder_item_id"] = None
        elif changed == "language":
            raw["subtitles"]["language"] = "ja"
        elif changed == "empty_subtitle":
            for item in raw["items"]:
                if item["role"] == "generated_subtitle":
                    item["size_bytes"] = 0
        else:
            raw["postprocess"]["image_refresh"] = "failed"
        raw["manifest_hash"] = m.manifest_digest(raw)
        with pytest.raises(ValidationError):
            m.FinalManifest.model_validate(raw)


def test_approval_context_digest_binds_non_manifest_fields():
    raw = load("approval-context-v1.json")
    original = m.ApprovalContext.model_validate(raw)
    raw["binding"]["candidate_revision"] += 1
    with pytest.raises(ValidationError):
        m.ApprovalContext.model_validate(raw)
    raw["context_hash"] = m.context_digest(raw["binding"], raw["observations"])
    assert m.ApprovalContext.model_validate(raw).context_hash != original.context_hash


def test_workflow_error_has_bounded_nonsecret_contract():
    error = m.WorkflowError("cas_conflict", entity_id=UID, retryable=False)
    assert str(error) == "cas_conflict"
    assert error.entity_id == UID and error.retryable is False


def test_secret_inside_model_is_not_a_hashable_masked_placeholder():
    class SecretEnvelope(BaseModel):
        secret: SecretStr

    with pytest.raises(ValueError, match="secret_not_json"):
        m.digest(SecretEnvelope(secret=SecretStr("private-fixture-value")))


def test_manifest_set_order_and_receiver_timestamp_do_not_change_content():
    raw = load("input-manifest-v1.json")
    expected = raw["manifest_hash"]
    raw["readiness"]["workcopy"]["items"].reverse()
    raw["readiness"]["full_read"]["received_at"] = "2026-10-20T01:00:00Z"
    assert m.InputManifest.model_validate(raw).manifest_hash == expected
    raw["readiness"]["full_read"]["observed_at"] = "2026-10-20T01:00:00Z"
    with pytest.raises(ValidationError, match="manifest_hash_mismatch"):
        m.InputManifest.model_validate(raw)


@pytest.mark.parametrize("value", [True, -1, "1024", 1.0])
def test_manifest_size_is_a_strict_nonnegative_integer(value):
    raw = load("input-manifest-v1.json")["items"][0]
    raw["size_bytes"] = value
    with pytest.raises(ValidationError):
        m.ManifestItem.model_validate(raw)


def test_no_speech_is_evidence_backed_without_false_subtitle_mark():
    raw = load("final-manifest-v1.json")
    raw["subtitles"].update(status="no_speech", language=None, item_ids=[])
    raw["postprocess"]["actual_marks"] = ["restored"]
    raw["manifest_hash"] = m.manifest_digest(raw)
    assert m.FinalManifest.model_validate(raw).subtitles.status == "no_speech"
    raw["subtitles"]["evidence"]["status"] = "unknown"
    raw["manifest_hash"] = m.manifest_digest(raw)
    with pytest.raises(ValidationError, match="no_speech_evidence_required"):
        m.FinalManifest.model_validate(raw)


def test_context_missing_observation_and_false_ready_field_are_refused():
    raw = load("approval-context-v1.json")
    del raw["observations"]["target"]
    with pytest.raises(ValidationError):
        m.ApprovalContext.model_validate(raw)
    raw = load("approval-context-v1.json")
    raw["ready"] = True
    with pytest.raises(ValidationError):
        m.ApprovalContext.model_validate(raw)
    raw = load("approval-context-v1.json")
    raw["observations"]["target"]["status"] = "unknown"
    raw["context_hash"] = m.context_digest(raw["binding"], raw["observations"])
    assert m.ApprovalContext.model_validate(raw).observations.target.status == "unknown"


def test_ordinary_worker_defaults_to_no_workflow_participation():
    value = m.WorkerRegistration(
        worker_id=UID, device_id=UID, name="fictional", address="192.0.2.10", port=5680
    )
    assert value.workflow_participation is False
    assert value.model_dump()["role"] == "compute_worker"


def test_cross_film_context_put_and_formal_root_cleanup_are_refused():
    raw = load("approval-context-v1.json")
    with pytest.raises(ValidationError, match="context_film_mismatch"):
        m.ApprovalContextPut(
            film_id=UID,
            expected_version=1,
            expected_context_revision=0,
            binding=m.ApprovalBinding.model_validate(raw["binding"]),
            observations=m.ApprovalObservations.model_validate(raw["observations"]),
        )
    binding = raw["binding"]
    binding["target"]["root_id"] = binding["input_cleanup"]["members"][0]["root_id"]
    binding["target"]["native_identity"]["root_id"] = binding["target"]["root_id"]
    with pytest.raises(ValidationError, match="formal_target_cleanup_forbidden"):
        m.ApprovalBinding.model_validate(binding)
