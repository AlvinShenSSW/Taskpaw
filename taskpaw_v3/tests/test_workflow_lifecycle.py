"""OS ownership and atomic workflow results across real connection lifetimes."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from pydantic import SecretStr

from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow.auth import authenticate, digest_secret
from taskpaw_v3.workflow.models import ComponentGrant, WorkflowError
from taskpaw_v3.workflow.store import WorkflowStore


def grant(h):
    return ComponentGrant(
        component_id=identity(),
        principal_id=identity(),
        credential_id=identity(),
        boot_id=identity(),
        generation=1,
        role="observer",
        secret_digest=digest_secret(
            SecretStr("synthetic-component-credential-for-tests")
        ),
        expires_at=h.clock.now() + timedelta(days=1),
    )


def test_single_os_owner_and_close_reopen_are_explicit(tmp_path):
    h = Harness(tmp_path / "workflow.db")
    try:
        with pytest.raises(WorkflowError, match="authority_unavailable"):
            WorkflowStore.open(h.path, clock=h.clock)
        with pytest.raises(WorkflowError, match="authority_unavailable"):
            WorkflowStore.create(h.path, h.bootstrap, clock=h.clock)
        h.store.close()
        with pytest.raises(WorkflowError, match="database_exists"):
            WorkflowStore.create(h.path, h.bootstrap, clock=h.clock)
        h.store = WorkflowStore.open(h.path, clock=h.clock)
        h.store.verify_operator(h.operator)
    finally:
        h.store.close()


def test_result_write_failure_rolls_back_business_and_audit_and_allows_retry(
    tmp_path, monkeypatch
):
    h = Harness(tmp_path / "workflow.db")
    command = grant(h)
    meta = h.meta()
    original = h.store._insert

    def fail_result(table, **values):
        if table == "request_results":
            raise OSError("injected before durable result")
        return original(table, **values)

    try:
        monkeypatch.setattr(h.store, "_insert", fail_result)
        with pytest.raises(OSError, match="injected"):
            h.store.grant_component(h.operator, meta, command)
        assert not h.store.list_audit(h.operator, 0).events
        with pytest.raises(WorkflowError, match="unauthorized"):
            authenticate(
                h.store,
                command.credential_id,
                SecretStr("synthetic-component-credential-for-tests"),
            )
        monkeypatch.setattr(h.store, "_insert", original)
        first = h.store.grant_component(h.operator, meta, command)
        h.store.close()
        h.store = WorkflowStore.open(h.path, clock=h.clock)
        assert h.store.grant_component(h.operator, meta, command) == first
        assert len(h.store.list_audit(h.operator, 0).events) == 1
    finally:
        h.store.close()


def test_close_waits_for_active_transaction_and_durable_result(tmp_path, monkeypatch):
    h = Harness(tmp_path / "workflow.db")
    command = grant(h)
    meta = h.meta()
    in_transaction, release_transaction, closing = Event(), Event(), Event()
    original = h.store._insert

    def pause_result(table, **values):
        if table == "request_results":
            in_transaction.set()
            assert release_transaction.wait(5)
        return original(table, **values)

    def close():
        closing.set()
        h.store.close()

    monkeypatch.setattr(h.store, "_insert", pause_result)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(h.store.grant_component, h.operator, meta, command)
            assert in_transaction.wait(5)
            shutdown = pool.submit(close)
            assert closing.wait(5)
            assert not shutdown.done()
            release_transaction.set()
            first = write.result(timeout=5)
            shutdown.result(timeout=5)
        h.store = WorkflowStore.open(h.path, clock=h.clock)
        assert h.store.grant_component(h.operator, meta, command) == first
    finally:
        release_transaction.set()
        h.store.close()
