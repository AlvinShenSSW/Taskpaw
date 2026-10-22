"""Pure intake contracts; these DTOs are not native ownership evidence."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.raw_media_models import (
    DurationObservation,
    Rational,
    RawMediaFacts,
    RawStreamFacts,
)

UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-19T01:00:00Z"


def raw(**changes):
    values = dict(
        format_name="mov,mp4",
        streams=(RawStreamFacts(index=0, codec_type="video", attached_picture=False),),
        primary_video_index=0,
        display_width=None,
        display_height=None,
        duration_observations=(),
        duration_ms=None,
        duration_source=None,
        issues=(),
        ffprobe_version="fixture-1",
        payload_sha256="a" * 64,
    )
    values.update(changes)
    return RawMediaFacts(**values)


def test_rational_is_canonical():
    assert Rational(numerator=60000, denominator=2002).model_dump() == {
        "numerator": 30000,
        "denominator": 1001,
    }
    assert Rational(numerator=0, denominator=2).denominator == 1


@pytest.mark.parametrize(
    "n,d", [(True, 1), (1, False), (1, 0), (1, -1), (1.0, 1), (10**19, 1)]
)
def test_rational_rejects_coercion_and_bounds(n, d):
    with pytest.raises(ValidationError):
        Rational(numerator=n, denominator=d)


@pytest.mark.parametrize(
    "source,index,n", [("format", 0, 1), ("stream", None, 1), ("ticks", 2, -1)]
)
def test_duration_scope_is_explicit(source, index, n):
    with pytest.raises(ValidationError):
        DurationObservation(
            source=source,
            stream_index=index,
            seconds=Rational(numerator=n, denominator=1),
        )


def test_raw_stream_rates_are_positive_and_duration_is_own_scope():
    with pytest.raises(ValidationError):
        RawStreamFacts(
            index=0,
            codec_type="video",
            avg_frame_rate=Rational(numerator=0, denominator=1),
        )
    with pytest.raises(ValidationError):
        RawStreamFacts(
            index=0,
            codec_type="video",
            duration_observations=(
                DurationObservation(
                    source="stream",
                    stream_index=1,
                    seconds=Rational(numerator=1, denominator=1),
                ),
            ),
        )


def test_raw_duplicate_streams_and_attached_primary_are_rejected():
    stream = RawStreamFacts(index=0, codec_type="video", attached_picture=False)
    with pytest.raises(ValidationError):
        raw(streams=(stream, stream))
    with pytest.raises(ValidationError):
        raw(
            streams=(
                RawStreamFacts(index=0, codec_type="video", attached_picture=True),
            )
        )


def test_raw_duration_requires_source_and_selected_video():
    with pytest.raises(ValidationError):
        raw(duration_ms=123)
    with pytest.raises(ValidationError):
        raw(primary_video_index=None, duration_ms=123, duration_source="stream")


def test_raw_arrays_roundtrip_and_no_arbitrary_extra():
    value = raw()
    assert RawMediaFacts.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        raw(extra="not a fact")
    with pytest.raises(ValidationError):
        value.format_name = "other"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_probes", True),
        ("max_snapshot_operations", True),
        ("max_members", 4097),
        ("max_depth", 9),
        ("stable_seconds", 0),
    ],
)
def test_intake_policy_limits_are_strict(field, value):
    with pytest.raises(ValidationError):
        im.IntakePolicy(policy_revision=1, **{field: value})


def test_probe_birth_requires_item_and_sequence():
    with pytest.raises(ValidationError):
        im.IntakeProcessBirth(pid=10, creation_filetime_100ns=11, role="probe")


def test_owner_cannot_have_more_active_than_total_or_forget_uncertainty():
    values = dict(
        owner_boot_id=UID,
        owner_id=OTHER,
        births=(),
        observed_total=0,
        observed_active=1,
        identity_uncertain=False,
        failure_codes=(),
        observed_at=STAMP,
    )
    with pytest.raises(ValidationError):
        im.IntakeOwner(**values)
    values.update(observed_active=0, failure_codes=("open_failed",))
    with pytest.raises(ValidationError):
        im.IntakeOwner(**values)


def test_exit_proof_stopped_is_not_just_boolean():
    with pytest.raises(ValidationError):
        im.IntakeExitProof(
            operation_id=UID,
            owner_boot_id=UID,
            owner_id=OTHER,
            source_generation=1,
            state="stopped",
            births=(),
            job_active_zero=False,
            io_joined=True,
            owned_handles_closed=True,
            failure_codes=(),
            observed_at=STAMP,
        )


def test_grouping_ready_cannot_hide_unassigned_or_missing_video():
    with pytest.raises(ValidationError):
        im.PackageGrouping(
            grouping_revision=1,
            film_groups=(),
            parent_context="actor",
            status="ready",
            reasons=(),
        )
    with pytest.raises(ValidationError):
        im.FilmGroup(
            film_id=UID,
            group_key="film",
            video_member_ids=(OTHER,),
            members=(im.GroupedMember(member_id=UID, role="attachment"),),
        )


def test_eligibility_does_not_allow_empty_eligible():
    with pytest.raises(ValidationError):
        im.ProcessingEligibility(
            policy_revision=1, decision="eligible", reason="eligible", items=()
        )
    with pytest.raises(ValidationError):
        im.ItemEligibility(
            item_id=UID,
            raw_payload_digest="a" * 64,
            decision="eligible",
            reason="eligible",
            profiles=(),
        )


def test_ready_grouping_rejects_forged_volume_gap():
    group = im.FilmGroup(
        film_id=UID,
        group_key="film",
        video_member_ids=(UID, OTHER),
        members=(
            im.GroupedMember(member_id=UID, role="main_video", volume_ordinal=1),
            im.GroupedMember(member_id=OTHER, role="main_video", volume_ordinal=3),
        ),
    )
    with pytest.raises(ValidationError):
        im.PackageGrouping(
            grouping_revision=1,
            film_groups=(group,),
            parent_context=None,
            status="ready",
            reasons=(),
        )


def test_raw_duration_projection_cannot_disagree_with_its_observation():
    observation = DurationObservation(
        source="stream", stream_index=0, seconds=Rational(numerator=1, denominator=1)
    )
    stream = RawStreamFacts(
        index=0,
        codec_type="video",
        attached_picture=False,
        duration_observations=(observation,),
    )
    with pytest.raises(ValidationError):
        raw(
            streams=(stream,),
            duration_observations=(observation,),
            duration_ms=2,
            duration_source="stream",
        )


def snapshot_payload():
    payload = json.loads(
        (Path(__file__).parent / "fixtures/workflow/input-manifest-v1.json").read_text(
            encoding="utf-8"
        )
    )
    admission = im.IntakeSnapshotAdmission(
        expected_version=1,
        package_id=UID,
        source_generation=1,
        root_config_revision=1,
        grouping_revision=1,
        observation_digest="c" * 64,
        operation_id=UID,
        snapshot_id=OTHER,
        owner_boot_id=OTHER,
        policy_revision=1,
    )
    payload["source_binding"] = dict(
        schema_version=1,
        root_id=payload["items"][0]["source_root_id"],
        root_config_revision=1,
        package_id=UID,
        source_generation=1,
        snapshot_id=OTHER,
        grouping_revision=1,
        package_membership_digest="c" * 64,
        grouping_digest="d" * 64,
    )
    observation = DurationObservation(
        source="stream", stream_index=0, seconds=Rational(numerator=120, denominator=1)
    )
    facts = raw(
        streams=(
            RawStreamFacts(
                index=0,
                codec_type="video",
                attached_picture=False,
                width=1920,
                height=1080,
                duration_observations=(observation,),
            ),
            RawStreamFacts(index=1, codec_type="audio"),
        ),
        display_width=1920,
        display_height=1080,
        duration_observations=(observation,),
        duration_ms=120000,
        duration_source="stream",
    )
    payload["readiness"]["media_probes"][0]["raw_facts"] = facts.model_dump(mode="json")
    payload["manifest_hash"] = m.manifest_digest(payload)
    manifest = m.InputManifest.model_validate(payload)
    group = im.FilmGroup(
        film_id=manifest.film_id,
        group_key="film",
        video_member_ids=(manifest.items[0].item_id,),
        members=tuple(
            im.GroupedMember(
                member_id=value.item_id,
                role=value.role,
                video_member_id=value.video_item_id,
                volume_ordinal=value.volume_ordinal,
                language=value.language,
            )
            for value in manifest.items
        ),
    )
    grouping = im.PackageGrouping(
        grouping_revision=1,
        film_groups=(group,),
        parent_context="actor",
        status="ready",
        reasons=(),
    )
    copies = {
        value.source_item_id: value for value in manifest.readiness.workcopy.items
    }
    members = tuple(
        im.SnapshotMember(
            member_id=value.item_id,
            relative_path=value.relative_path,
            entry_kind="file",
            source_identity=value.native_identity,
            copy_identity=copies[value.item_id].native_identity,
            size_bytes=value.size_bytes,
            sha256=value.sha256,
        )
        for value in manifest.items
    )
    eligibility = im.ProcessingEligibility(
        policy_revision=1,
        decision="blocked",
        reason="media_unknown",
        items=(
            im.ItemEligibility(
                item_id=manifest.items[0].item_id,
                raw_payload_digest=facts.payload_sha256,
                decision="blocked",
                reason="media_unknown",
                profiles=(),
            ),
        ),
    )
    return dict(
        admission=admission,
        members=members,
        grouping=grouping,
        manifests=(manifest,),
        media_probes=manifest.readiness.media_probes,
        eligibility=eligibility,
        completion_digest="e" * 64,
        source_before=manifest.readiness.full_read,
        source_after=manifest.readiness.source_recheck,
        workcopy_verification=manifest.readiness.workcopy.verification,
        relative_work_path="intake/snapshot",
    )


def test_snapshot_roundtrip_and_no_profile_is_still_not_eligible():
    value = im.PackageSnapshot(**snapshot_payload())
    assert im.PackageSnapshot.model_validate_json(value.model_dump_json()) == value
    assert value.eligibility.decision == "blocked"


@pytest.mark.parametrize(
    "change", ["generation", "hash", "raw_digest", "group_role", "probe", "missing_raw"]
)
def test_snapshot_keeps_all_content_group_and_probe_bindings(change):
    payload = im.PackageSnapshot(**snapshot_payload()).model_dump(mode="json")
    if change == "generation":
        payload["admission"]["source_generation"] = 2
    elif change == "hash":
        payload["members"][0]["sha256"] = "0" * 64
    elif change == "raw_digest":
        payload["eligibility"]["items"][0]["raw_payload_digest"] = "0" * 64
    elif change == "group_role":
        payload["grouping"]["film_groups"][0]["members"][1].update(
            role="attachment", video_member_id=None, language=None
        )
    elif change == "probe":
        payload["media_probes"][0]["evidence"]["producer_version"] = "other"
    else:
        payload["media_probes"][0].pop("raw_facts")
    with pytest.raises(ValidationError):
        im.PackageSnapshot.model_validate(payload)


def test_same_object_copy_alias_is_rejected_across_logical_roots():
    payload = snapshot_payload()["members"][0].model_dump(mode="json")
    payload["copy_identity"] = copy.deepcopy(payload["source_identity"])
    payload["copy_identity"]["root_id"] = UID
    with pytest.raises(ValidationError, match="independent_copy"):
        im.SnapshotMember.model_validate(payload)


def test_probe_ack_matches_exact_birth_and_owner_result():
    birth = im.IntakeProcessBirth(
        pid=100, creation_filetime_100ns=200, role="probe", item_id=UID, sequence=1
    )
    ack = im.ProbeResumeAck(
        operation_id=UID, item_id=UID, sequence=1, birth=birth, owner_version=1
    )
    assert im.ProbeResumeAck.model_validate_json(ack.model_dump_json()) == ack
    with pytest.raises(ValidationError):
        im.ProbeResumeAck(
            operation_id=UID, item_id=OTHER, sequence=1, birth=birth, owner_version=1
        )
    values = dict(
        operation_id=UID,
        owner_boot_id=OTHER,
        owner_id=OTHER,
        source_generation=1,
        state="stopped",
        births=(
            im.IntakeBirthExit(
                birth=birth, signaled=True, waited=True, handle_closed=True
            ),
        ),
        job_active_zero=True,
        io_joined=True,
        owned_handles_closed=True,
        failure_codes=(),
        observed_at=STAMP,
    )
    proof = im.IntakeExitProof(**values)
    result = im.IntakeSnapshotResult(
        expected_version=1,
        operation_id=UID,
        source_generation=1,
        outcome="complete",
        reason="snapshot_complete",
        exit_proof=proof,
        snapshot=im.PackageSnapshot(**snapshot_payload()),
    )
    assert (
        im.IntakeSnapshotResult.model_validate_json(result.model_dump_json()) == result
    )
    values["state"] = "unknown"
    with pytest.raises(ValidationError):
        im.IntakeSnapshotResult(
            expected_version=1,
            operation_id=UID,
            source_generation=1,
            outcome="complete",
            reason="snapshot_complete",
            exit_proof=im.IntakeExitProof(**values),
            snapshot=result.snapshot,
        )


def root_payload():
    source = m.NativeIdentity(
        scheme="windows_file_id",
        root_id=UID,
        volume_or_export_id="fixture-volume",
        object_id="input-object",
        observed_at=STAMP,
        evidence_id=UID,
    )
    work = m.NativeIdentity(
        scheme="windows_file_id",
        root_id=OTHER,
        volume_or_export_id="fixture-volume",
        object_id="work-object",
        observed_at=STAMP,
        evidence_id=OTHER,
    )
    film_policy = m.FilmPolicy(
        policy_revision=1,
        required_stages=(
            "intake",
            "return",
            "scrape",
            "archive",
            "cleanup_input",
            "cleanup_workcopy",
        ),
        subtitle_requirement="chinese",
        required_image_roles=("poster",),
        folder_cover_required=False,
        allowed_attachment_extensions=(".txt",),
    )
    return dict(
        expected_version=0,
        root_id=UID,
        native_identity=source,
        private_path="C:/private-input",
        work_root_id=OTHER,
        work_native_identity=work,
        private_work_path="C:/private-work",
        component_id=UID,
        policy=im.IntakePolicy(
            policy_revision=1, allowed_attachment_extensions=(".txt",)
        ),
        film_policy=film_policy,
        enabled=False,
    )


def test_root_requires_explicit_film_execution_policy_and_hides_private_repr():
    value = im.IntakeRootPut(**root_payload())
    assert value.film_policy.subtitle_requirement == "chinese"
    assert "private-input" not in repr(value) and "private-work" not in repr(value)
    payload = root_payload()
    payload.pop("film_policy")
    with pytest.raises(ValidationError):
        im.IntakeRootPut(**payload)


@pytest.mark.parametrize(
    "changes", [dict(policy_revision=2), dict(allowed_attachment_extensions=(".pdf",))]
)
def test_film_execution_policy_cannot_drift_from_intake_policy(changes):
    payload = root_payload()
    payload["film_policy"] = payload["film_policy"].model_copy(update=changes)
    with pytest.raises(ValidationError, match="film_intake_policy_mismatch"):
        im.IntakeRootPut(**payload)
