"""New source fences bind the digest without rewriting historical manifests."""

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from taskpaw_v3.workflow import models as m

FIXTURES = Path(__file__).parent / "fixtures" / "workflow"


def legacy(name="input-manifest-v1.json"):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def binding():
    return {
        "schema_version": 1,
        "root_id": str(uuid4()),
        "root_config_revision": 1,
        "package_id": str(uuid4()),
        "source_generation": 1,
        "snapshot_id": str(uuid4()),
        "grouping_revision": 1,
        "package_membership_digest": "c" * 64,
        "grouping_digest": "d" * 64,
    }


@pytest.mark.parametrize(
    "name,model",
    [
        ("input-manifest-v1.json", m.InputManifest),
        ("final-manifest-v1.json", m.FinalManifest),
    ],
)
def test_historical_manifest_bytes_and_digest_remain_valid(name, model):
    path = FIXTURES / name
    original = path.read_bytes()
    payload = json.loads(original)
    parsed = model.model_validate(payload)
    assert m.manifest_digest(parsed) == payload["manifest_hash"]
    assert parsed.model_dump(mode="json") == payload
    assert path.read_bytes() == original


def test_source_binding_is_hashed_and_roundtrips_without_changing_input_items():
    payload = legacy()
    original_items = copy.deepcopy(payload["items"])
    original_hash = payload["manifest_hash"]
    payload["source_binding"] = binding()
    payload["manifest_hash"] = m.manifest_digest(payload)
    parsed = m.InputManifest.model_validate(payload)
    assert parsed.source_binding.source_generation == 1
    assert parsed.manifest_hash != original_hash
    assert parsed.model_dump(mode="json")["items"] == original_items
    assert m.InputManifest.model_validate_json(parsed.model_dump_json()) == parsed


def test_explicit_absent_binding_keeps_historical_wire_shape_and_digest():
    payload = legacy()
    with_absent = {**payload, "source_binding": None}
    parsed = m.InputManifest.model_validate(with_absent)
    assert parsed.source_binding is None
    assert parsed.model_dump(mode="json") == payload
    assert m.manifest_digest(parsed) == payload["manifest_hash"]
    assert m.manifest_digest(with_absent) == payload["manifest_hash"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("root_id", "10000000-0000-0000-0000-000000000001"),
        ("root_config_revision", 2),
        ("package_id", "10000000-0000-0000-0000-000000000002"),
        ("source_generation", 2),
        ("snapshot_id", "10000000-0000-0000-0000-000000000003"),
        ("grouping_revision", 2),
        ("package_membership_digest", "e" * 64),
        ("grouping_digest", "f" * 64),
    ],
)
def test_changing_any_source_binding_field_invalidates_the_manifest(field, value):
    payload = legacy()
    payload["source_binding"] = binding()
    payload["manifest_hash"] = m.manifest_digest(payload)
    m.InputManifest.model_validate(payload)
    payload["source_binding"][field] = value
    with pytest.raises(ValidationError, match="manifest_hash_mismatch"):
        m.InputManifest.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("root_id", "relative-root"),
        ("root_config_revision", True),
        ("source_generation", 0),
        ("grouping_revision", -1),
        ("package_membership_digest", "A" * 64),
        ("unexpected", "extra"),
    ],
)
def test_binding_values_are_strict_and_bounded(field, value):
    payload = binding()
    payload[field] = value
    with pytest.raises(ValidationError):
        m.InputSourceBinding.model_validate(payload)
