"""Synthetic ffprobe JSON; no executable or media is opened here."""

import hashlib
import json
from copy import deepcopy

import pytest

from taskpaw_v3.workflow.intake_models import (
    IntakeProfile,
    ProbeParseLimits,
    ProbePolicy,
)
from taskpaw_v3.workflow.media_facts import (
    build_ffprobe_argv,
    evaluate_eligibility,
    parse_ffprobe,
)
from taskpaw_v3.workflow.models import EvidenceRef, MediaProbe

UID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
STAMP = "2026-10-19T01:00:00Z"


def source(**stream_changes):
    stream = dict(
        index=0,
        codec_type="video",
        codec_name="h264",
        width=1920,
        height=1080,
        coded_width=1920,
        coded_height=1088,
        pix_fmt="yuv420p",
        sample_aspect_ratio="1:1",
        display_aspect_ratio="16:9",
        avg_frame_rate="30000/1001",
        r_frame_rate="30/1",
        time_base="1/90000",
        duration="10.0005",
        field_order="progressive",
        disposition={"attached_pic": 0},
    )
    stream.update(stream_changes)
    return dict(
        program_version={"version": "fixture-7"},
        format={"format_name": "mov,mp4", "duration": "12.0000"},
        streams=[stream],
    )


def parse(data):
    return parse_ffprobe(json.dumps(data).encode(), limits=ProbeParseLimits())


def evidence(payload_digest="b" * 64):
    return EvidenceRef(
        evidence_id=UID,
        kind="fixture",
        producer_id=OTHER,
        producer_version="1",
        observed_at=STAMP,
        received_at=STAMP,
        payload_digest=payload_digest,
        status="passed",
    )


def probe(data=None, item=UID):
    raw = parse(data or source())
    return MediaProbe(
        item_id=item,
        width=raw.display_width,
        height=raw.display_height,
        duration_ms=raw.duration_ms,
        video_streams=sum(value.codec_type == "video" for value in raw.streams),
        audio_streams=sum(value.codec_type == "audio" for value in raw.streams),
        evidence=evidence(raw.payload_sha256),
        raw_facts=raw,
    )


def profile(**changes):
    values = dict(
        profile_id="fixture-only",
        profile_revision=1,
        policy_revision=1,
        tool="jasna-ltx",
        tool_version="0.fixture",
        min_width=1,
        max_width=1920,
        min_height=1,
        max_height=1080,
        min_fps={"numerator": 1, "denominator": 1},
        max_fps={"numerator": 60, "denominator": 1},
        codecs=("h264",),
        pixel_formats=("yuv420p",),
        field_orders=("progressive",),
        sample_aspect_ratios=({"numerator": 1, "denominator": 1},),
        rotations=(0,),
        approval=evidence(),
    )
    values.update(changes)
    return IntakeProfile(**values)


def test_duration_uses_selected_video_not_container_and_preserves_vfr():
    value = parse(source())
    assert value.duration_ms == 10001
    assert value.duration_source == "stream"
    assert value.duration_observations[0].source == "format"
    assert value.streams[0].avg_frame_rate != value.streams[0].r_frame_rate
    assert "duration_conflict" not in value.issues
    assert "invalid_rate" not in value.issues
    assert value.streams[0].coded_height == 1088
    assert value.display_height == 1080


def test_audio_duration_is_not_video_conflict():
    data = source()
    data["streams"].append(
        dict(
            index=1,
            codec_type="audio",
            codec_name="aac",
            duration="11.5",
            channels=2,
            sample_rate="48000",
            tags={"language": "jpn"},
        )
    )
    result = parse(data)
    assert result.duration_ms == 10001
    assert "duration_conflict" not in result.issues
    assert result.streams[1].sample_rate == 48000


def test_ticks_same_scope_half_up_and_conflict_retained():
    result = parse(source(duration_ts=900045))
    assert result.duration_ms == 10001
    assert len(result.streams[0].duration_observations) == 2
    assert parse(source(duration_ts=900090)).duration_ms == 10001
    conflicting = parse(source(duration_ts=900135))
    assert conflicting.duration_ms is None
    assert conflicting.duration_source is None
    assert "duration_conflict" in conflicting.issues


def half_ms_straddle():
    # #349 (MKON-151-4K): ffprobe prints the stream duration with six decimals, so a
    # tick duration 0.1 us below a half-millisecond is printed onto the boundary.
    return source(
        duration="7711.203500", duration_ts=77112034999, time_base="1/10000000"
    )


def test_print_rounding_across_half_ms_keeps_exact_ticks():
    result = parse(half_ms_straddle())
    assert "duration_conflict" not in result.issues
    assert result.duration_ms == 7711203
    assert result.duration_source == "ticks"
    # Workers on the previous release re-validate these facts with the unchanged
    # equal-milliseconds rule, so only the exact observation may remain.
    assert [v.source for v in result.streams[0].duration_observations] == ["ticks"]
    assert type(result).model_validate(result.model_dump()) == result


def test_beyond_print_precision_is_still_conflict():
    # 10.000500 s vs 10.0004989 s: 1.1 us apart and on both sides of 10000.5 ms.
    result = parse(
        source(duration="10.000500", duration_ts=100004989, time_base="1/10000000")
    )
    assert "duration_conflict" in result.issues
    assert result.duration_ms is None


def test_print_rounding_straddle_probe_is_eligible():
    value = probe(half_ms_straddle())
    assert value.duration_ms == 7711203
    result = evaluate_eligibility((value,), (profile(),), policy_revision=1)
    assert result.decision == "eligible"


def test_no_implicit_container_duration_fallback():
    data = source()
    del data["streams"][0]["duration"]
    assert parse(data).duration_ms is None
    data["streams"][0]["duration_ts"] = 900045
    assert parse(data).duration_source == "ticks"


def test_attached_picture_and_multiple_main_streams():
    data = source()
    cover = deepcopy(data["streams"][0])
    cover.update(index=1, disposition={"attached_pic": 1}, width=200, height=300)
    data["streams"].insert(0, cover)
    assert parse(data).primary_video_index == 0
    cover["disposition"]["attached_pic"] = 0
    assert parse(data).primary_video_index is None
    assert "multiple_videos" in parse(data).issues


@pytest.mark.parametrize(
    "changes",
    [
        dict(width=True),
        dict(width=0),
        dict(avg_frame_rate="0/0"),
        dict(r_frame_rate="not-a-rate"),
        dict(field_order="strange"),
        dict(disposition={"attached_pic": True}),
    ],
)
def test_unknown_fields_never_become_1080_defaults(changes):
    value = parse(source(**changes))
    assert value.issues
    if "width" in changes:
        assert value.display_width is None


def test_rotation_and_matrix_unknown_preserve_evidence():
    result = parse(source(tags={"rotate": "90"}))
    assert (result.display_width, result.display_height) == (1080, 1920)
    result = parse(
        source(
            tags={"rotate": "90"},
            side_data_list=[
                {
                    "side_data_type": "Display Matrix",
                    "rotation": 180,
                    "displaymatrix": "matrix fixture",
                }
            ],
        )
    )
    assert "rotation_conflict" in result.issues
    assert result.display_width is None
    assert result.streams[0].rotations[1].display_matrix == "matrix fixture"
    result = parse(
        source(
            side_data_list=[
                {"side_data_type": "Display Matrix", "displaymatrix": "matrix only"}
            ]
        )
    )
    assert "unknown_rotation" in result.issues


@pytest.mark.parametrize(
    "payload",
    [
        b'{"streams":[],"streams":[]}',
        b'{"streams":[],"value":NaN}',
        b'{"streams":[],"value":Infinity}',
        b'{"streams":',
        b"[]",
        b'{"streams":[{"index":true}]}',
    ],
)
def test_malformed_or_ambiguous_json_rejected(payload):
    with pytest.raises(ValueError):
        parse_ffprobe(payload, limits=ProbeParseLimits())


def test_parser_limits_and_exact_received_hash():
    payload = json.dumps(source(), indent=2).encode()
    assert (
        parse_ffprobe(payload, limits=ProbeParseLimits()).payload_sha256
        == hashlib.sha256(payload).hexdigest()
    )
    with pytest.raises(ValueError):
        parse_ffprobe(payload, limits=ProbeParseLimits(max_bytes=64))
    with pytest.raises(ValueError):
        parse_ffprobe(
            b'{"streams":[],"extra":"' + b"x" * 33 + b'"}',
            limits=ProbeParseLimits(max_string_length=32),
        )
    data = source()
    data["streams"].append(dict(index=1, codec_type="audio"))
    with pytest.raises(ValueError):
        parse_ffprobe(json.dumps(data).encode(), limits=ProbeParseLimits(max_streams=1))


def test_argv_fixed_fields_no_extra_execution_or_version_process():
    args = build_ffprobe_argv(
        "C:/tools/ffprobe.exe", "C:/owned/影片.mkv", policy=ProbePolicy()
    )
    assert args[0] == "C:/tools/ffprobe.exe"
    assert args[-1] == "C:/owned/影片.mkv"
    assert "-show_program_version" in args
    assert "-show_entries" in args
    assert "-show_frames" not in args and "-show_packets" not in args
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    for bad in ("https://host/movie", "-show_frames", "\\\\host\\movie", "C:/a\x00b"):
        with pytest.raises(ValueError):
            build_ffprobe_argv("C:/tools/ffprobe.exe", bad, policy=ProbePolicy())


def test_no_profile_is_not_gpu_authorization_and_all_matches_retained():
    item = probe()
    assert (
        evaluate_eligibility((item,), (), policy_revision=1).decision
        == "profile_required"
    )
    result = evaluate_eligibility(
        (item,), (profile(), profile(profile_id="second")), policy_revision=1
    )
    assert result.decision == "eligible"
    assert {match.profile_id for match in result.items[0].profiles} == {
        "fixture-only",
        "second",
    }
    assert result.items[0].raw_payload_digest == item.raw_facts.payload_sha256


def test_all_volumes_and_explicit_actual_sizes_required():
    first = probe()
    large = probe(source(width=2560, height=1440), item=OTHER)
    assert (
        evaluate_eligibility((first, large), (profile(),), policy_revision=1).decision
        == "profile_required"
    )
    assert (
        evaluate_eligibility(
            (large,), (profile(max_width=3840, max_height=2160),), policy_revision=1
        ).decision
        == "eligible"
    )
    assert (
        evaluate_eligibility((first,), (profile(),), policy_revision=2).decision
        == "profile_required"
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(field_order="tt"),
        dict(sample_aspect_ratio="4:3"),
        dict(tags={"rotate": "90"}),
    ],
)
def test_unqualified_special_media_has_no_default_profile(changes):
    item = probe(source(**changes))
    assert (
        evaluate_eligibility((item,), (profile(),), policy_revision=1).decision
        == "profile_required"
    )


@pytest.mark.parametrize(
    "side_data,expected",
    [
        ([{"side_data_type": "Stereo 3D"}], "stereo3d"),
        ([{"side_data_type": "Spherical Mapping"}], "spherical"),
        ([{"side_data_type": "Unrecognized future data"}], "unknown"),
        ([{"rotation": 0}], "unknown"),
        ([{"side_data_type": "CPB properties"}], "flat"),
    ],
)
def test_spatial_observations_and_unknown_side_data_are_conservative(
    side_data, expected
):
    data = source(side_data_list=side_data)
    item = probe(data)
    assert item.raw_facts.streams[0].spatial_format == expected
    assert evaluate_eligibility((item,), (profile(),), policy_revision=1).decision == (
        "eligible" if expected == "flat" else "profile_required"
    )
    if expected != "unknown":
        assert (
            evaluate_eligibility(
                (item,), (profile(spatial_formats=(expected,)),), policy_revision=1
            ).decision
            == "eligible"
        )


def test_legacy_probe_cannot_bypass_raw_facts_gate():
    legacy = MediaProbe(
        item_id=UID,
        width=1920,
        height=1080,
        duration_ms=1000,
        video_streams=1,
        audio_streams=0,
        evidence=evidence(),
    )
    assert (
        evaluate_eligibility((legacy,), (profile(),), policy_revision=1).decision
        == "blocked"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"streams":[],"extra":1e99999999999999999999999}',
        b'{"streams":[],"extra":' + b"[" * 40 + b"0" + b"]" * 40 + b"}",
    ],
)
def test_pathological_numbers_and_depth_are_bounded_failures(payload):
    with pytest.raises(ValueError):
        parse_ffprobe(payload, limits=ProbeParseLimits())
