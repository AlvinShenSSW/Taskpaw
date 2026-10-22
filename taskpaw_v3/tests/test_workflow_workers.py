"""Enrollment uses public authentication; monitoring IDs never confer authority."""

from datetime import timedelta

import pytest
from pydantic import SecretStr

from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow.auth import authenticate, authenticate_pairing, digest_secret
from taskpaw_v3.workflow.models import (
    BootObservation,
    PairingComplete,
    PairingIssue,
    WorkerRegistration,
    WorkerRetirement,
    WorkerRevocation,
    WorkflowError,
)


def enroll(h):
    worker = WorkerRegistration(
        worker_id=identity(),
        device_id=identity(),
        name="Fixture 4090",
        address="192.0.2.20",
        port=5682,
    )
    h.store.register_worker(h.operator, h.meta(), worker)
    ticket = SecretStr("synthetic-one-use-pairing-ticket-secret")
    secret = SecretStr("synthetic-new-worker-credential-secret")
    pairing = PairingIssue(
        pairing_id=identity(),
        worker_id=worker.worker_id,
        device_id=worker.device_id,
        ticket_digest=digest_secret(ticket, purpose="pairing"),
        expires_at=h.clock.now() + timedelta(minutes=5),
    )
    h.store.issue_pairing(h.operator, h.meta(), pairing)
    proof = authenticate_pairing(h.store, pairing.pairing_id, ticket, secret)
    complete = PairingComplete(
        pairing_id=pairing.pairing_id,
        worker_id=worker.worker_id,
        device_id=worker.device_id,
        credential_id=identity(),
        secret_digest=digest_secret(secret),
        generation=1,
        expires_at=h.clock.now() + timedelta(days=1),
    )
    meta = h.meta()
    result = h.store.complete_pairing(proof, meta, complete)
    auth = authenticate(h.store, complete.credential_id, secret)
    return worker, pairing, ticket, complete, secret, auth, meta, result


def test_pairing_once_and_lost_reply_requires_new_credential_proof(tmp_path):
    h = Harness(tmp_path / "workflow.db")
    try:
        worker, pairing, ticket, complete, secret, auth, meta, result = enroll(h)
        assert h.store.complete_pairing(auth, meta, complete) == result
        with pytest.raises(WorkflowError, match="unauthorized"):
            authenticate_pairing(h.store, pairing.pairing_id, ticket, secret)
        wrong = complete.model_copy(update={"credential_id": identity()})
        with pytest.raises(WorkflowError, match="idempotency_conflict"):
            h.store.complete_pairing(auth, meta, wrong)
        view = h.store.get_worker(h.operator, worker.worker_id)
        assert not view.worker.workflow_participation
        assert view.worker.enrollment_state == "pending_validation"
        assert view.worker.admission_state == "disabled"
    finally:
        h.store.close()


def test_record_boot_then_revoke_and_retire_preserves_identity_and_audit(tmp_path):
    h = Harness(tmp_path / "workflow.db")
    try:
        worker, _, _, _, _, auth, _, result = enroll(h)
        boot = BootObservation(
            worker_id=worker.worker_id,
            boot_id=identity(),
            expected_version=result.version,
            known_epoch=1,
            assignment_ids=(),
            local_owners=(),
        )
        h.store.record_boot(auth, h.meta(), boot)
        current = h.store.get_worker(h.operator, worker.worker_id).worker
        h.store.revoke_worker(
            h.operator,
            h.meta(),
            WorkerRevocation(
                worker_id=worker.worker_id,
                expected_version=current.version,
                reason="fixture retirement",
            ),
        )
        with pytest.raises(WorkflowError, match="unauthorized"):
            h.store.get_worker(auth, worker.worker_id)
        current = h.store.get_worker(h.operator, worker.worker_id).worker
        h.store.retire_worker(
            h.operator,
            h.meta(),
            WorkerRetirement(
                worker_id=worker.worker_id,
                expected_version=current.version,
                reason="fixture retirement",
            ),
        )
        final = h.store.get_worker(h.operator, worker.worker_id).worker
        assert final.device_id == worker.device_id
        assert final.enrollment_state == "retired"
        assert "complete_pairing" in {
            e.command_kind for e in h.store.list_audit(h.operator, 0).events
        }
    finally:
        h.store.close()


def test_expired_credential_stays_expired_after_clock_rollback_and_restart(tmp_path):
    h = Harness(tmp_path / "workflow.db")
    try:
        grant, _, _, secret = h.component()
        h.clock.value += timedelta(days=2)
        with pytest.raises(WorkflowError, match="unauthorized"):
            authenticate(h.store, grant.credential_id, secret)
        h.clock.value -= timedelta(days=2)
        with pytest.raises(WorkflowError, match="unauthorized"):
            authenticate(h.store, grant.credential_id, secret)
        with pytest.raises(WorkflowError, match="authority_unavailable"):
            h.component()
    finally:
        h.store.close()
