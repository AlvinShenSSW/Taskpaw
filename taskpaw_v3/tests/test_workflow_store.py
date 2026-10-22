"""Exercise workflow commands through their public authority/credential path."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import SecretStr

from taskpaw_v3.workflow.auth import authenticate, digest_secret
from taskpaw_v3.workflow.models import (
    AuthorityBootstrap,
    CommandMeta,
    ComponentGrant,
    CredentialRevocation,
    TlsBinding,
    WorkflowError,
)
from taskpaw_v3.workflow.store import WorkflowStore


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 19, tzinfo=timezone.utc)

    def now(self):
        return self.value


def identity():
    return str(uuid4())


class Harness:
    def __init__(self, path):
        self.clock = Clock()
        self.secret = SecretStr("synthetic-operator-credential-for-tests")
        authority_id = identity()
        self.bootstrap = AuthorityBootstrap(
            authority_id=authority_id,
            epoch=1,
            operator_principal_id=identity(),
            operator_credential_id=identity(),
            operator_secret_digest=digest_secret(self.secret),
            operator_expires_at=self.clock.now() + timedelta(days=7),
            tls=TlsBinding(
                authority_id=authority_id,
                certificate_sha256="a" * 64,
                binding_revision=1,
            ),
            lease_seconds=60,
        )
        self.path = path
        self.store = WorkflowStore.create(path, self.bootstrap, clock=self.clock)
        self.operator = authenticate(
            self.store, self.bootstrap.operator_credential_id, self.secret
        )

    def meta(self, key=None):
        return CommandMeta(
            request_id=identity(),
            idempotency_key=key or identity(),
            authority_id=self.bootstrap.authority_id,
            authority_epoch=1,
        )

    def component(self, role="observer"):
        secret = SecretStr("synthetic-component-credential-for-tests")
        grant = ComponentGrant(
            component_id=identity(),
            principal_id=identity(),
            credential_id=identity(),
            boot_id=identity(),
            generation=1,
            role=role,
            secret_digest=digest_secret(secret),
            expires_at=self.clock.now() + timedelta(days=1),
        )
        result = self.store.grant_component(self.operator, self.meta(), grant)
        proof = authenticate(self.store, grant.credential_id, secret)
        return grant, result, proof, secret


@pytest.fixture
def harness(tmp_path):
    value = Harness(tmp_path / "workflow.db")
    try:
        yield value
    finally:
        value.store.close()


def test_bootstrap_grant_revoke_survives_restart_and_stale_auth_proof(harness):
    h = harness
    grant, _, proof, secret = h.component()
    h.store.list_audit(proof, 0)
    command = CredentialRevocation(
        credential_id=grant.credential_id, expected_version=1, reason="fixture retired"
    )
    meta = h.meta()
    first = h.store.revoke_credential(h.operator, meta, command)
    assert h.store.revoke_credential(h.operator, meta, command) == first
    with pytest.raises(WorkflowError, match="unauthorized"):
        h.store.list_audit(proof, 0)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    with pytest.raises(WorkflowError, match="unauthorized"):
        authenticate(h.store, grant.credential_id, secret)
    current = authenticate(h.store, h.bootstrap.operator_credential_id, h.secret)
    assert h.store.revoke_credential(current, meta, command) == first


def test_same_idempotency_key_cannot_change_credential_revoke_target(harness):
    h = harness
    grant, _, _, _ = h.component()
    another, _, _, _ = h.component()
    meta = h.meta()
    command = CredentialRevocation(
        credential_id=grant.credential_id, expected_version=1, reason="fixture"
    )
    h.store.revoke_credential(h.operator, meta, command)
    altered = command.model_copy(update={"credential_id": another.credential_id})
    with pytest.raises(WorkflowError, match="idempotency_conflict"):
        h.store.revoke_credential(h.operator, meta, altered)


def test_observer_cannot_revoke_an_operator_even_with_fresh_metadata(harness):
    h = harness
    _, _, observer, _ = h.component()
    command = CredentialRevocation(
        credential_id=h.bootstrap.operator_credential_id,
        expected_version=1,
        reason="attempt privilege escalation",
    )
    with pytest.raises(WorkflowError, match="forbidden"):
        h.store.revoke_credential(observer, h.meta(), command)
    authenticate(h.store, h.bootstrap.operator_credential_id, h.secret)


def test_failed_mutation_has_no_partial_credential_revocation(harness):
    h = harness
    grant, _, _, secret = h.component()
    with pytest.raises(WorkflowError, match="cas_conflict"):
        h.store.revoke_credential(
            h.operator,
            h.meta(),
            CredentialRevocation(
                credential_id=grant.credential_id,
                expected_version=99,
                reason="stale client",
            ),
        )
    authenticate(h.store, grant.credential_id, secret)
