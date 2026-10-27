"""Bounded ffprobe facts, independent of workflow identities and storage."""

from __future__ import annotations

from math import gcd
from typing import Annotated, Literal, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

Integer = Annotated[int, Field(strict=True, ge=-(10**18), le=10**18)]
Nonnegative = Annotated[int, Field(strict=True, ge=0, le=10**18)]
Positive = Annotated[int, Field(strict=True, gt=0, le=10**18)]
Dimension = Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
StreamIndex = Annotated[int, Field(strict=True, ge=0, le=65535)]
RawText = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
FieldOrder = Literal["progressive", "tt", "bb", "tb", "bt", "unknown"]
SpatialFormat = Literal["flat", "stereo3d", "spherical", "unknown"]
ProbeIssue = Literal[
    "missing_video",
    "multiple_videos",
    "missing_dimensions",
    "invalid_dimensions",
    "invalid_rate",
    "invalid_ratio",
    "invalid_time_base",
    "invalid_duration",
    "duration_conflict",
    "missing_duration",
    "invalid_rotation",
    "rotation_conflict",
    "unknown_rotation",
    "unknown_field_order",
    "invalid_disposition",
    "invalid_audio",
    "missing_version",
    "unknown_spatial",
]


class RawModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _arrays(cls, value: object, info: ValidationInfo) -> object:
        field = cls.model_fields.get(info.field_name or "")
        if isinstance(value, list) and field and get_origin(field.annotation) is tuple:
            return tuple(value)
        return value


class Rational(RawModel):
    numerator: Integer
    denominator: Positive

    @model_validator(mode="after")
    def _reduce(self) -> Rational:
        divisor = gcd(self.numerator, self.denominator)
        object.__setattr__(self, "numerator", self.numerator // divisor)
        object.__setattr__(self, "denominator", self.denominator // divisor)
        return self


class DurationObservation(RawModel):
    source: Literal["format", "stream", "ticks"]
    stream_index: StreamIndex | None
    seconds: Rational

    @model_validator(mode="after")
    def _scope(self) -> DurationObservation:
        if (self.source == "format") != (
            self.stream_index is None
        ) or self.seconds.numerator < 0:
            raise ValueError("invalid_duration_scope")
        return self


class RotationObservation(RawModel):
    source: Literal["tag", "display_matrix"]
    degrees: Rational | None
    display_matrix: Annotated[str, Field(max_length=16384)] | None


class RawStreamFacts(RawModel):
    index: StreamIndex
    codec_type: Literal["video", "audio", "subtitle", "data", "attachment", "unknown"]
    codec_name: RawText | None = None
    width: Dimension | None = None
    height: Dimension | None = None
    coded_width: Dimension | None = None
    coded_height: Dimension | None = None
    pixel_format: RawText | None = None
    sample_aspect_ratio: Rational | None = None
    display_aspect_ratio: Rational | None = None
    avg_frame_rate: Rational | None = None
    r_frame_rate: Rational | None = None
    time_base: Rational | None = None
    field_order: FieldOrder = "unknown"
    spatial_format: SpatialFormat = "unknown"
    rotations: Annotated[tuple[RotationObservation, ...], Field(max_length=16)] = ()
    attached_picture: bool | None = None
    duration_observations: Annotated[
        tuple[DurationObservation, ...], Field(max_length=2)
    ] = ()
    channels: Annotated[int, Field(strict=True, gt=0, le=1024)] | None = None
    sample_rate: Annotated[int, Field(strict=True, gt=0, le=10_000_000)] | None = None
    language: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @model_validator(mode="after")
    def _relations(self) -> RawStreamFacts:
        for ratio in (
            self.sample_aspect_ratio,
            self.display_aspect_ratio,
            self.avg_frame_rate,
            self.r_frame_rate,
            self.time_base,
        ):
            if ratio is not None and ratio.numerator <= 0:
                raise ValueError("positive_ratio_required")
        if any(
            observation.stream_index != self.index or observation.source == "format"
            for observation in self.duration_observations
        ):
            raise ValueError("invalid_stream_duration_scope")
        if len(
            {observation.source for observation in self.duration_observations}
        ) != len(self.duration_observations):
            raise ValueError("duplicate_duration_source")
        return self


class RawMediaFacts(RawModel):
    schema_version: Literal[1] = 1
    format_name: RawText | None
    streams: Annotated[tuple[RawStreamFacts, ...], Field(max_length=128)]
    primary_video_index: StreamIndex | None
    display_width: Dimension | None
    display_height: Dimension | None
    duration_observations: Annotated[
        tuple[DurationObservation, ...], Field(max_length=257)
    ]
    duration_ms: Nonnegative | None
    duration_source: Literal["stream", "ticks"] | None
    issues: Annotated[tuple[ProbeIssue, ...], Field(max_length=32)]
    ffprobe_version: RawText | None
    payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported_schema_version")
        return value

    @model_validator(mode="after")
    def _relations(self) -> RawMediaFacts:
        if len({stream.index for stream in self.streams}) != len(self.streams):
            raise ValueError("duplicate_stream_index")
        if len(set(self.issues)) != len(self.issues):
            raise ValueError("duplicate_probe_issue")
        videos = [
            stream
            for stream in self.streams
            if stream.codec_type == "video" and stream.attached_picture is not True
        ]
        if self.primary_video_index is not None and (
            len(videos) != 1
            or videos[0].index != self.primary_video_index
            or videos[0].attached_picture is not False
        ):
            raise ValueError("invalid_primary_video")
        if (self.duration_ms is None) != (self.duration_source is None) or (
            self.duration_ms is not None and self.primary_video_index is None
        ):
            raise ValueError("invalid_derived_duration")
        if self.duration_ms is not None:
            observations = videos[0].duration_observations
            milliseconds = {
                (2 * value.seconds.numerator * 1000 + value.seconds.denominator)
                // (2 * value.seconds.denominator)
                for value in observations
            }
            if milliseconds != {self.duration_ms} or self.duration_source not in {
                value.source for value in observations
            }:
                raise ValueError("duration_projection_mismatch")
        if (self.display_width is None) != (self.display_height is None) or (
            self.display_width is not None and self.primary_video_index is None
        ):
            raise ValueError("invalid_display_dimensions")
        expected = tuple(
            observation
            for stream in self.streams
            for observation in stream.duration_observations
        )
        if (
            tuple(
                observation
                for observation in self.duration_observations
                if observation.source != "format"
            )
            != expected
        ):
            raise ValueError("duration_observation_mismatch")
        if (
            len(
                [
                    value
                    for value in self.duration_observations
                    if value.source == "format"
                ]
            )
            > 1
        ):
            raise ValueError("duplicate_format_duration")
        return self
