"""Exercise workflow commands through their public authority/credential path."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import SecretStr

from taskpaw_v3.workflow import schema
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
            # ready/service fixtures jump to real time; 7 days expired on 2026-10-26.
            operator_expires_at=self.clock.now() + timedelta(days=3650),
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


def free_pages(h, rows=3000):
    """Leave a closed store whose file is mostly free pages (#376 reclaim)."""
    conn = h.store._conn
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT INTO audit(event_id,authority_id,epoch,command_kind,created_at,"
        "body_json) VALUES(?,?,1,'filler','t',?)",
        (
            (identity(), h.bootstrap.authority_id, json.dumps({"p": "x" * 4000}))
            for _ in range(rows)
        ),
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM audit WHERE command_kind='filler'")
    conn.commit()
    h.store.close()
    return h.path.stat().st_size


def test_open_vacuums_a_mostly_free_database_once(harness, monkeypatch):
    h = harness
    size = free_pages(h)
    monkeypatch.setattr(schema, "RECLAIM_MIN_FREE_BYTES", 1 << 20)
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert h.store._conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    authenticate(h.store, h.bootstrap.operator_credential_id, h.secret)
    h.store.close()
    assert h.path.stat().st_size < size // 4
    assert not h.path.with_name(h.path.name + "-wal").exists()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    authenticate(h.store, h.bootstrap.operator_credential_id, h.secret)


def test_open_leaves_a_database_below_the_reclaim_threshold(harness):
    h = harness
    size = free_pages(h)
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    assert h.store._conn.execute("PRAGMA freelist_count").fetchone()[0] > 0
    h.store.close()
    assert h.path.stat().st_size == size


def test_failed_vacuum_is_logged_and_leaves_the_file_open(harness, monkeypatch, caplog):
    h = harness
    free_pages(h)
    monkeypatch.setattr(schema, "RECLAIM_MIN_FREE_BYTES", 1 << 20)
    conn = sqlite3.connect(h.path, isolation_level=None)

    class RefusingVacuum:
        def execute(self, sql, *args):
            if sql == "VACUUM":
                raise sqlite3.OperationalError("synthetic disk full")
            return conn.execute(sql, *args)

    try:
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        schema.reclaim_free_pages(RefusingVacuum())
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free > 0
    finally:
        conn.close()
    assert "free-page reclaim failed" in caplog.text
    h.store = WorkflowStore.open(h.path, clock=h.clock)
