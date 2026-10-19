"""Raw observations remain bound to their projection and historical hashes."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.raw_media_models import RawMediaFacts

FIXTURE = Path(__file__).parent / "fixtures/workflow/input-manifest-v1.json"


def historical_manifest():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def raw_facts():
    observation = {
        "source": "stream",
        "stream_index": 0,
        "seconds": {"numerator": 120, "denominator": 1},
    }
    return RawMediaFacts.model_validate(
        {
            "format_name": "matroska,webm",
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "attached_picture": False,
                    "duration_observations": [observation],
                },
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ],
            "primary_video_index": 0,
            "display_width": 1920,
            "display_height": 1080,
            "duration_observations": [observation],
            "duration_ms": 120000,
            "duration_source": "stream",
            "issues": [],
            "ffprobe_version": "synthetic-1",
            "payload_sha256": "a" * 64,
        }
    ).model_dump(mode="json")


def probe_payload():
    probe = historical_manifest()["readiness"]["media_probes"][0]
    probe["raw_facts"] = raw_facts()
    return probe


def test_legacy_probe_omits_new_field_in_all_serializers():
    original = historical_manifest()["readiness"]["media_probes"][0]
    probe = m.MediaProbe.model_validate(original)
    assert probe.raw_facts is None
    assert probe.model_dump(mode="json") == original
    assert json.loads(probe.model_dump_json()) == original
    assert "raw_facts" not in probe.model_dump()


def test_explicit_absent_raw_facts_does_not_change_historical_manifest_hash():
    original = historical_manifest()
    payload = copy.deepcopy(original)
    payload["readiness"]["media_probes"][0]["raw_facts"] = None
    assert m.manifest_digest(payload) == original["manifest_hash"]
    parsed = m.InputManifest.model_validate(payload)
    assert parsed.model_dump(mode="json") == original
    assert m.manifest_digest(parsed) == original["manifest_hash"]


def test_complete_raw_facts_roundtrip_and_change_the_manifest_digest():
    payload = historical_manifest()
    old_hash = payload["manifest_hash"]
    payload["readiness"]["media_probes"][0] = probe_payload()
    payload["manifest_hash"] = m.manifest_digest(payload)
    parsed = m.InputManifest.model_validate(payload)
    assert parsed.manifest_hash != old_hash
    assert parsed.readiness.media_probes[0].raw_facts.payload_sha256 == "a" * 64
    assert parsed.model_dump(mode="json") == payload
    assert m.InputManifest.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "field,value",
    [
        ("width", 1280),
        ("height", 720),
        ("duration_ms", 120001),
        ("video_streams", 2),
        ("audio_streams", 0),
    ],
)
def test_raw_projection_cannot_disagree_with_workflow_probe(field, value):
    payload = probe_payload()
    payload[field] = value
    with pytest.raises(ValidationError, match="media_probe_projection_mismatch"):
        m.MediaProbe.model_validate(payload)


def test_raw_payload_digest_must_match_its_evidence():
    payload = probe_payload()
    payload["raw_facts"]["payload_sha256"] = "b" * 64
    with pytest.raises(ValidationError, match="media_probe_payload_mismatch"):
        m.MediaProbe.model_validate(payload)


def test_attached_cover_counts_as_a_video_stream_but_is_not_the_primary_video():
    payload = probe_payload()
    payload["raw_facts"]["streams"].append(
        {"index": 2, "codec_type": "video", "attached_picture": True}
    )
    payload["video_streams"] = 2
    parsed = m.MediaProbe.model_validate(payload)
    assert parsed.raw_facts.primary_video_index == 0
    assert parsed.video_streams == 2


def test_unknown_projection_stays_unknown_and_cannot_gain_passed_status():
    payload = probe_payload()
    payload["raw_facts"].update(display_width=None, display_height=None)
    payload.update(width=None, height=None)
    payload["evidence"]["status"] = "unknown"
    parsed = m.MediaProbe.model_validate(payload)
    assert parsed.width is None and parsed.height is None
    payload["evidence"]["status"] = "passed"
    with pytest.raises(ValidationError, match="incomplete_media_probe"):
        m.MediaProbe.model_validate(payload)


@pytest.mark.parametrize(
    "field,value", [("ffprobe_version", "synthetic-2"), ("format_name", "mp4")]
)
def test_raw_observations_are_covered_by_manifest_hash(field, value):
    payload = historical_manifest()
    payload["readiness"]["media_probes"][0] = probe_payload()
    payload["manifest_hash"] = m.manifest_digest(payload)
    m.InputManifest.model_validate(payload)
    payload["readiness"]["media_probes"][0]["raw_facts"][field] = value
    with pytest.raises(ValidationError, match="manifest_hash_mismatch"):
        m.InputManifest.model_validate(payload)
