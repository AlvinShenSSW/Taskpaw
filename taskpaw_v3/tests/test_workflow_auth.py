"""Execution credentials have no monitoring/empty-token compatibility mode."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from taskpaw_v3.workflow.auth import (
    AuthProof,
    PairingProof,
    digest_secret,
    parse_bearer,
    require_role,
)
from taskpaw_v3.workflow.models import WorkflowError


def test_credential_digest_is_domain_separated_and_never_plaintext():
    secret = SecretStr("synthetic-workflow-secret-for-tests-only")
    credential = digest_secret(secret)
    pairing = digest_secret(secret, purpose="pairing")
    assert len(credential) == 64 and len(pairing) == 64
    assert credential != pairing
    assert credential == digest_secret(secret)
    assert secret.get_secret_value() not in credential


@pytest.mark.parametrize(
    "value", ["", " ", "short", "x" * 257, "x" * 32 + "\n", "密" * 32]
)
def test_empty_weak_or_ambiguous_credentials_are_refused(value):
    with pytest.raises(WorkflowError, match="unauthorized") as exc:
        digest_secret(SecretStr(value))
    assert str(exc.value) == "unauthorized"


def test_bearer_contains_explicit_credential_identity_and_secret():
    identity = str(uuid4())
    raw = "synthetic-workflow-secret-for-tests-only"
    credential_id, secret = parse_bearer(f"Bearer {identity}.{raw}")
    assert credential_id == identity
    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == raw
    assert raw not in repr(secret)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer monitor-token",
        "Basic abc",
        "Bearer ",
        "Bearer invalid-id." + "x" * 32,
        "Bearer 00000000-0000-0000-0000-000000000001.short",
        "Bearer 00000000-0000-0000-0000-000000000001." + "x" * 32 + ".extra",
        "Bearer 00000000-0000-0000-0000-000000000001." + "x" * 32 + " ",
    ],
)
def test_monitoring_or_malformed_headers_cannot_identify_workflow_principal(header):
    with pytest.raises(WorkflowError, match="unauthorized") as exc:
        parse_bearer(header)
    assert str(exc.value) == "unauthorized"


def test_authentication_proofs_hide_even_the_credential_hash():
    credential_hash = "a" * 64
    proof = AuthProof(str(uuid4()), str(uuid4()), 1, str(uuid4()), credential_hash)
    pairing = PairingProof(str(uuid4()), str(uuid4()), credential_hash, "b" * 64)
    assert credential_hash not in repr(proof)
    assert credential_hash not in repr(pairing)
    with pytest.raises((AttributeError, TypeError)):
        proof.generation = 2


@pytest.mark.parametrize(
    "role,command",
    [
        ("compute_worker", "approve"),
        ("compute_worker", "begin_cleanup"),
        ("compute_worker", "accept_archive_receipt"),
        ("archive_adapter", "approve"),
        ("observer", "claim"),
        ("monitor", "claim"),
        ("operator", "accept_archive_receipt"),
        ("operator", "shell"),
    ],
)
def test_role_does_not_grant_unrelated_component_authority(role, command):
    with pytest.raises(WorkflowError, match="forbidden"):
        require_role(role, command)


def test_approved_operator_and_receiving_adapter_have_distinct_authority():
    require_role("operator", "approve")
    require_role("archive_adapter", "accept_archive_receipt")
    require_role("receiver_adapter", "accept_artifact_receipt")
