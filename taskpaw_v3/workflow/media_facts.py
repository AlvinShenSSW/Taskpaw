"""Parse complete successful ffprobe stdout, never run or authorize a process.

The native caller must establish successful exit and complete bounded stdout.
Absent optional fields in such output stay unknown, except absent spatial side
data/tags, which ffprobe omits when none exists. Unrecognized side data prevents
assuming a flat picture. No filename or hardware label participates.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import PureWindowsPath
from typing import Literal, cast

from .intake_models import (
    IntakeProfile,
    ItemEligibility,
    ProbeParseLimits,
    ProbePolicy,
    ProcessingEligibility,
    ProfileBinding,
)
from .models import MediaProbe
from .raw_media_models import (
    DurationObservation,
    FieldOrder,
    ProbeIssue,
    Rational,
    RawMediaFacts,
    RawStreamFacts,
    RotationObservation,
    SpatialFormat,
    agreed_duration_ms,
)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_probe_key")
        value[key] = item
    return value


def _constant(value: str) -> object:
    raise ValueError("nonfinite_probe_number")


def _json_integer(value: str) -> int:
    if len(value) > 20:
        raise ValueError("probe_integer_too_large")
    return int(value)


def _bound(value: object, limits: ProbeParseLimits, depth: int = 0) -> None:
    if depth > limits.max_depth:
        raise ValueError("probe_depth_exceeded")
    if isinstance(value, str) and len(value) > limits.max_string_length:
        raise ValueError("probe_string_exceeded")
    if isinstance(value, dict):
        if len(value) > 128:
            raise ValueError("probe_object_exceeded")
        for key, item in value.items():
            _bound(key, limits, depth + 1)
            _bound(item, limits, depth + 1)
    elif isinstance(value, list):
        if len(value) > 128:
            raise ValueError("probe_array_exceeded")
        for item in value:
            _bound(item, limits, depth + 1)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("probe_object_required")
    return value


def _text(value: object, maximum: int = 256) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError("invalid_probe_text")
    return value


def _number(value: object) -> Fraction | None:
    if type(value) is int:
        return Fraction(value) if abs(value) <= 10**18 else None
    if isinstance(value, Decimal):
        exponent = value.as_tuple().exponent
        if (
            not value.is_finite()
            or value.adjusted() > 18
            or not isinstance(exponent, int)
            or exponent < -18
        ):
            return None
        return Fraction(value)
    if type(value) is str and re.fullmatch(r"-?\d{1,18}(?:\.\d{1,18})?", value):
        return Fraction(value)
    return None


def _rational(value: Fraction) -> Rational | None:
    if abs(value.numerator) > 10**18 or value.denominator > 10**18:
        return None
    return Rational(numerator=value.numerator, denominator=value.denominator)


def _ratio(
    value: object, issues: list[ProbeIssue], issue: ProbeIssue, separator: str = "/"
) -> Rational | None:
    if value is None:
        return None
    result = None
    if type(value) is str and re.fullmatch(
        r"\d{1,18}" + re.escape(separator) + r"\d{1,18}", value
    ):
        numerator, denominator = (int(part) for part in value.split(separator))
        if numerator > 0 and denominator > 0:
            result = _rational(Fraction(numerator, denominator))
    if result is None:
        issues.append(issue)
    return result


def _integer(
    value: object,
    maximum: int,
    issues: list[ProbeIssue],
    issue: ProbeIssue,
    *,
    string: bool = False,
) -> int | None:
    if value is None:
        return None
    if string and type(value) is str and re.fullmatch(r"\d{1,18}", value):
        value = int(value)
    if type(value) is int and 0 < value <= maximum:
        return value
    issues.append(issue)
    return None


def _duration(
    value: object, source: str, index: int | None, issues: list[ProbeIssue]
) -> DurationObservation | None:
    if value is None:
        return None
    number = _number(value)
    seconds = _rational(number) if number is not None and number >= 0 else None
    if seconds is None:
        issues.append("invalid_duration")
        return None
    return DurationObservation(
        source=cast("Literal['format', 'stream', 'ticks']", source),
        stream_index=index,
        seconds=seconds,
    )


def _rotation(
    value: object, source: str, matrix: str | None, issues: list[ProbeIssue]
) -> RotationObservation:
    number = _number(value)
    degrees = _rational(number) if number is not None else None
    if degrees is None:
        issues.append("unknown_rotation" if value is None else "invalid_rotation")
    return RotationObservation(
        source=cast("Literal['tag', 'display_matrix']", source),
        degrees=degrees,
        display_matrix=matrix,
    )


def rotation_degrees(stream: RawStreamFacts) -> Fraction | None:
    """Return an unambiguous recorded angle; absence means no rotation metadata."""
    values: set[Fraction] = set()
    for observation in stream.rotations:
        if observation.degrees is None:
            return None
        values.add(
            Fraction(observation.degrees.numerator, observation.degrees.denominator)
            % 360
        )
    return (
        next(iter(values)) if len(values) == 1 else Fraction(0) if not values else None
    )


def _stream(data: dict[str, object], issues: list[ProbeIssue]) -> RawStreamFacts:
    index = data.get("index")
    if type(index) is not int or not 0 <= index <= 65535:
        raise ValueError("invalid_probe_stream_index")
    kind = data.get("codec_type", "unknown")
    if kind not in ("video", "audio", "subtitle", "data", "attachment", "unknown"):
        kind = "unknown"
    tags = _object(data.get("tags", {}))
    side_data = data.get("side_data_list", [])
    if not isinstance(side_data, list) or len(side_data) > 16:
        raise ValueError("invalid_probe_side_data")
    rotations: list[RotationObservation] = []
    if "rotate" in tags:
        rotations.append(_rotation(tags["rotate"], "tag", None, issues))
    spatial: set[SpatialFormat] = set()
    known_flat = {
        "Display Matrix",
        "Mastering display metadata",
        "Content light level metadata",
        "CPB properties",
        "DOVI configuration record",
        "Ambient viewing environment",
    }
    for item in side_data:
        side = _object(item)
        side_type = side.get("side_data_type")
        if side_type == "Display Matrix":
            rotations.append(
                _rotation(
                    side.get("rotation"),
                    "display_matrix",
                    _text(side.get("displaymatrix"), 16384),
                    issues,
                )
            )
        if side_type == "Stereo 3D":
            spatial.add("stereo3d")
        elif side_type == "Spherical Mapping":
            spatial.add("spherical")
        elif type(side_type) is not str or side_type not in known_flat:
            spatial.add("unknown")
    for key in ("stereo_mode", "projection", "spherical"):
        if key in tags:
            spatial.add("unknown")
    spatial_format: SpatialFormat = (
        next(iter(spatial))
        if len(spatial) == 1
        else "flat"
        if not spatial
        else "unknown"
    )
    if spatial_format == "unknown":
        issues.append("unknown_spatial")
    disposition = _object(data.get("disposition", {})).get("attached_pic")
    attached = (
        bool(disposition)
        if type(disposition) is int and disposition in (0, 1)
        else None
    )
    if kind == "video" and attached is None:
        issues.append("invalid_disposition")
    time_base = _ratio(data.get("time_base"), issues, "invalid_time_base")
    durations = []
    duration = _duration(data.get("duration"), "stream", index, issues)
    if duration:
        durations.append(duration)
    ticks = data.get("duration_ts")
    if ticks is not None:
        number = _number(ticks)
        if number is None or number < 0 or number.denominator != 1 or time_base is None:
            issues.append("invalid_duration")
        else:
            seconds = _rational(
                number * Fraction(time_base.numerator, time_base.denominator)
            )
            if seconds is None:
                issues.append("invalid_duration")
            else:
                durations.append(
                    DurationObservation(
                        source="ticks", stream_index=index, seconds=seconds
                    )
                )
    order = data.get("field_order", "unknown")
    if order not in ("progressive", "tt", "bb", "tb", "bt", "unknown"):
        order = "unknown"
    if kind == "video" and order == "unknown":
        issues.append("unknown_field_order")
    result = RawStreamFacts(
        index=index,
        codec_type=cast(
            "Literal['video', 'audio', 'subtitle', 'data', 'attachment', 'unknown']",
            kind,
        ),
        codec_name=_text(data.get("codec_name")),
        width=_integer(data.get("width"), 1_000_000, issues, "invalid_dimensions"),
        height=_integer(data.get("height"), 1_000_000, issues, "invalid_dimensions"),
        coded_width=_integer(
            data.get("coded_width"), 1_000_000, issues, "invalid_dimensions"
        ),
        coded_height=_integer(
            data.get("coded_height"), 1_000_000, issues, "invalid_dimensions"
        ),
        pixel_format=_text(data.get("pix_fmt")),
        sample_aspect_ratio=_ratio(
            data.get("sample_aspect_ratio"), issues, "invalid_ratio", ":"
        ),
        display_aspect_ratio=_ratio(
            data.get("display_aspect_ratio"), issues, "invalid_ratio", ":"
        ),
        avg_frame_rate=_ratio(data.get("avg_frame_rate"), issues, "invalid_rate"),
        r_frame_rate=_ratio(data.get("r_frame_rate"), issues, "invalid_rate"),
        time_base=time_base,
        field_order=cast(FieldOrder, order),
        spatial_format=spatial_format,
        rotations=tuple(rotations),
        attached_picture=attached,
        duration_observations=tuple(durations),
        channels=_integer(data.get("channels"), 1024, issues, "invalid_audio"),
        sample_rate=_integer(
            data.get("sample_rate"), 10_000_000, issues, "invalid_audio", string=True
        ),
        language=_text(tags.get("language"), 64),
    )
    known_angles = {
        Fraction(value.degrees.numerator, value.degrees.denominator) % 360
        for value in rotations
        if value.degrees is not None
    }
    if len(known_angles) > 1:
        issues.append("rotation_conflict")
    return result


def parse_ffprobe(payload: bytes, *, limits: ProbeParseLimits) -> RawMediaFacts:
    if type(payload) is not bytes or not payload or len(payload) > limits.max_bytes:
        raise ValueError("probe_payload_bound")
    try:
        data = json.loads(
            payload,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=Decimal,
            parse_int=_json_integer,
        )
        _bound(data, limits)
    except (
        UnicodeError,
        RecursionError,
        json.JSONDecodeError,
        InvalidOperation,
    ) as exc:
        raise ValueError("invalid_probe_json") from exc
    data = _object(data)
    stream_values = data.get("streams")
    if not isinstance(stream_values, list) or len(stream_values) > limits.max_streams:
        raise ValueError("probe_stream_bound")
    issues: list[ProbeIssue] = []
    streams = tuple(
        sorted(
            (_stream(_object(value), issues) for value in stream_values),
            key=lambda value: value.index,
        )
    )
    format_value = _object(data.get("format", {}))
    format_duration = _duration(format_value.get("duration"), "format", None, issues)
    durations = tuple(
        value for stream in streams for value in stream.duration_observations
    )
    if format_duration:
        durations = (format_duration, *durations)
    videos = [
        stream
        for stream in streams
        if stream.codec_type == "video" and stream.attached_picture is not True
    ]
    primary = (
        videos[0] if len(videos) == 1 and videos[0].attached_picture is False else None
    )
    if not videos:
        issues.append("missing_video")
    elif len(videos) > 1:
        issues.append("multiple_videos")
    width = height = duration_ms = None
    duration_source = None
    if primary:
        if primary.width is None or primary.height is None:
            issues.append("missing_dimensions")
        angle = rotation_degrees(primary)
        if (
            primary.width
            and primary.height
            and primary.sample_aspect_ratio
            and angle in (0, 90, 180, 270)
        ):
            display = Fraction(
                primary.width * primary.sample_aspect_ratio.numerator,
                primary.sample_aspect_ratio.denominator,
            )
            if display.denominator == 1 and 0 < display <= 1_000_000:
                width, height = int(display), primary.height
                if angle in (90, 270):
                    width, height = height, width
        same_scope = primary.duration_observations
        agreed = agreed_duration_ms(same_scope)
        if same_scope and agreed is None:
            issues.append("duration_conflict")
        elif same_scope:
            duration_ms = agreed
            duration_source = cast("Literal['stream', 'ticks']", same_scope[0].source)
        else:
            issues.append("missing_duration")
    version = _text(_object(data.get("program_version", {})).get("version"))
    if version is None:
        issues.append("missing_version")
    return RawMediaFacts(
        format_name=_text(format_value.get("format_name")),
        streams=streams,
        primary_video_index=primary.index if primary else None,
        display_width=width,
        display_height=height,
        duration_observations=durations,
        duration_ms=duration_ms,
        duration_source=duration_source,
        issues=tuple(dict.fromkeys(issues)),
        ffprobe_version=version,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
    )


def build_ffprobe_argv(
    executable: str, private_path: str, *, policy: ProbePolicy
) -> tuple[str, ...]:
    for path in (executable, private_path):
        if (
            type(path) is not str
            or len(path) > 32767
            or any(ord(char) < 32 for char in path)
        ):
            raise ValueError("invalid_probe_local_path")
        native = PureWindowsPath(path)
        if (
            not native.is_absolute()
            or len(native.drive) != 2
            or native.drive[1] != ":"
            or ":" in str(native)[2:]
            or ".." in native.parts
        ):
            raise ValueError("probe_local_absolute_path_required")
    entries = (
        "program_version=version:format=format_name,duration:"
        "stream=index,codec_type,codec_name,width,height,coded_width,coded_height,pix_fmt,"
        "sample_aspect_ratio,display_aspect_ratio,avg_frame_rate,r_frame_rate,time_base,"
        "field_order,duration,duration_ts,channels,sample_rate:stream_disposition=attached_pic:"
        "stream_tags=rotate,language,stereo_mode,projection,spherical:"
        "stream_side_data=side_data_type,rotation,displaymatrix"
    )
    return (
        executable,
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(policy.containers),
        "-show_program_version",
        "-show_format",
        "-show_streams",
        "-show_entries",
        entries,
        "-of",
        "json",
        "-i",
        private_path,
    )


def evaluate_eligibility(
    probes: tuple[MediaProbe, ...],
    profiles: tuple[IntakeProfile, ...],
    *,
    policy_revision: int,
) -> ProcessingEligibility:
    # Raw evidence is produced by the native caller; configuration authorization
    # is enforced by the public store, not by constructing this pure DTO.
    rows: list[ItemEligibility] = []
    for probe in probes:
        raw = probe.raw_facts
        stream = (
            next(
                (
                    value
                    for value in raw.streams
                    if value.index == raw.primary_video_index
                ),
                None,
            )
            if raw
            else None
        )
        matches: list[ProfileBinding] = []
        decision = "profile_required"
        if (
            raw is None
            or probe.evidence.status != "passed"
            or raw.ffprobe_version is None
            or not raw.streams
            or "missing_video" in raw.issues
        ):
            decision = "blocked"
        elif stream is not None:
            if (
                not stream.width
                or not stream.height
                or not raw.duration_ms
                or not stream.codec_name
                or not stream.pixel_format
                or stream.avg_frame_rate is None
                or stream.r_frame_rate is None
                or stream.sample_aspect_ratio is None
            ):
                decision = "blocked"
            elif (
                stream.spatial_format != "unknown"
                and rotation_degrees(stream) is not None
            ):
                for profile in profiles:
                    if (
                        profile.policy_revision != policy_revision
                        or profile.approval.status != "passed"
                    ):
                        continue
                    if not (
                        profile.min_width <= stream.width <= profile.max_width
                        and profile.min_height <= stream.height <= profile.max_height
                    ):
                        continue
                    low, high = (
                        Fraction(
                            profile.min_fps.numerator, profile.min_fps.denominator
                        ),
                        Fraction(
                            profile.max_fps.numerator, profile.max_fps.denominator
                        ),
                    )
                    if not all(
                        low <= Fraction(rate.numerator, rate.denominator) <= high
                        for rate in (stream.avg_frame_rate, stream.r_frame_rate)
                    ):
                        continue
                    if (
                        stream.codec_name not in profile.codecs
                        or stream.pixel_format not in profile.pixel_formats
                        or stream.field_order not in profile.field_orders
                        or stream.sample_aspect_ratio
                        not in profile.sample_aspect_ratios
                        or rotation_degrees(stream) not in profile.rotations
                        or stream.spatial_format not in profile.spatial_formats
                    ):
                        continue
                    matches.append(
                        ProfileBinding(
                            profile_id=profile.profile_id,
                            profile_revision=profile.profile_revision,
                            tool=profile.tool,
                            tool_version=profile.tool_version,
                        )
                    )
                if matches:
                    decision = "eligible"
        rows.append(
            ItemEligibility(
                item_id=probe.item_id,
                raw_payload_digest=raw.payload_sha256 if raw else None,
                decision=cast(
                    "Literal['eligible', 'profile_required', 'blocked']", decision
                ),
                reason=cast(
                    "Literal['eligible', 'profile_required', 'media_unknown']",
                    "media_unknown" if decision == "blocked" else decision,
                ),
                profiles=tuple(
                    sorted(
                        matches,
                        key=lambda item: (
                            item.profile_id,
                            item.profile_revision,
                            item.tool,
                            item.tool_version,
                        ),
                    )
                ),
            )
        )
    overall = (
        "blocked"
        if not rows or any(row.decision == "blocked" for row in rows)
        else "profile_required"
        if any(row.decision == "profile_required" for row in rows)
        else "eligible"
    )
    return ProcessingEligibility(
        policy_revision=policy_revision,
        decision=cast("Literal['eligible', 'profile_required', 'blocked']", overall),
        reason=cast(
            "Literal['eligible', 'profile_required', 'media_unknown']",
            "media_unknown" if overall == "blocked" else overall,
        ),
        items=tuple(rows),
    )
