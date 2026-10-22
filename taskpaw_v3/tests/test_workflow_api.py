"""Workflow factories over a real temporary store, without listening sockets."""

from __future__ import annotations

import threading
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow import api
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.auth import digest_secret

PREFIX = "/v1/workflow"


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path / "workflow.db")
    try:
        yield h
    finally:
        h.store.close()


def _local_app(h, active=lambda: True):
    return api.create_local_app(
        h.store,
        control_token=h.secret.get_secret_value(),
        control_active=active,
        operator_credential_id=h.bootstrap.operator_credential_id,
    )


@pytest.fixture
def local(harness):
    with TestClient(
        _local_app(harness),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
        headers={"Authorization": "Bearer " + harness.secret.get_secret_value()},
    ) as client:
        yield client


@pytest.fixture
def worker(harness):
    with TestClient(
        api.create_worker_app(harness.store, tls=harness.bootstrap.tls),
        base_url="https://controller.example.invalid",
        client=("192.0.2.10", 12345),
    ) as client:
        yield client


def _body(h, command, meta=None):
    return {
        "meta": (meta or h.meta()).model_dump(mode="json"),
        "command": command.model_dump(mode="json"),
    }


def _error(response, code, status):
    assert response.status_code == status, response.text
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert error["code"] == code
    assert str(UUID(error["request_id"])) == error["request_id"]
    assert error["retryable"] is False
    assert not ({"detail", "traceback", "input"} & set(error))


def _registration():
    return m.WorkerRegistration(
        worker_id=identity(),
        device_id=identity(),
        name="Synthetic worker",
        address="192.0.2.10",
        port=5680,
        workflow_participation=False,
    )


def _pending_pairing(h, local):
    registration = _registration()
    response = local.post(PREFIX + "/workers", json=_body(h, registration))
    assert response.status_code == 200, response.text
    ticket = SecretStr("synthetic-pairing-ticket-only-for-api-tests")
    secret = SecretStr("synthetic-worker-credential-only-for-api-tests")
    pairing_id = identity()
    issue = m.PairingIssue(
        pairing_id=pairing_id,
        worker_id=registration.worker_id,
        device_id=registration.device_id,
        ticket_digest=digest_secret(ticket, purpose="pairing"),
        expires_at=h.clock.now() + timedelta(hours=1),
    )
    response = local.post(PREFIX + "/pairings", json=_body(h, issue))
    assert response.status_code == 200, response.text
    command = m.PairingComplete(
        pairing_id=pairing_id,
        worker_id=registration.worker_id,
        device_id=registration.device_id,
        credential_id=identity(),
        secret_digest=digest_secret(secret),
        generation=1,
        expires_at=h.clock.now() + timedelta(days=1),
    )
    body = _body(h, command)
    headers = {
        "Authorization": f"Pairing {pairing_id}.{ticket.get_secret_value()}",
        "X-Workflow-New-Secret": secret.get_secret_value(),
    }
    return command, body, headers, secret


def _pairing(h, local, worker):
    command, body, headers, secret = _pending_pairing(h, local)
    first = worker.post(PREFIX + "/enrollment/complete", json=body, headers=headers)
    assert first.status_code == 200, first.text
    return command, body, headers, secret, first


def _bearer(credential_id, secret):
    return {"Authorization": f"Bearer {credential_id}.{secret.get_secret_value()}"}


def test_local_registration_is_durable_idempotent_and_does_not_enable_worker(
    harness, local
):
    h = harness
    registration = _registration()
    body = _body(h, registration)
    first = local.post(PREFIX + "/workers", json=body)
    assert first.status_code == 200, first.text
    replay = local.post(PREFIX + "/workers", json=body)
    assert replay.json() == first.json()
    view = local.get(PREFIX + "/workers/" + registration.worker_id)
    assert view.status_code == 200, view.text
    assert view.json()["worker"]["enrollment_state"] == "pending_pairing"
    assert view.json()["worker"]["admission_state"] == "disabled"
    assert h.secret.get_secret_value() not in view.text
    assert "secret_digest" not in view.text


def test_pairing_uses_dedicated_headers_then_new_credential_for_replay(
    harness, local, worker
):
    command, body, headers, secret, first = _pairing(harness, local, worker)
    replay = worker.post(
        PREFIX + "/enrollment/complete",
        json=body,
        headers=_bearer(command.credential_id, secret),
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    reused_ticket = worker.post(
        PREFIX + "/enrollment/complete", json=body, headers=headers
    )
    _error(reused_ticket, "unauthorized", 401)
    assert (
        secret.get_secret_value() not in first.text + replay.text + reused_ticket.text
    )


def test_worker_boot_uses_real_store_and_route_body_identity_is_checked(
    harness, local, worker
):
    command, _, _, secret, _ = _pairing(harness, local, worker)
    headers = _bearer(command.credential_id, secret)
    view = harness.store.get_worker(harness.operator, command.worker_id)
    boot_id = identity()
    observation = m.BootObservation(
        worker_id=command.worker_id,
        expected_version=view.worker.version,
        boot_id=boot_id,
        known_epoch=1,
        assignment_ids=(),
        local_owners=(),
    )
    response = worker.post(
        PREFIX + "/boot", json=_body(harness, observation), headers=headers
    )
    assert response.status_code == 200, response.text
    renewal = m.AssignmentRenewal(
        assignment_id=identity(),
        expected_version=1,
        worker_id=command.worker_id,
        boot_id=boot_id,
        attempt_id=identity(),
        authority_epoch=1,
        fencing_token=1,
        local_owner=None,
    )
    body = _body(harness, renewal)
    response = worker.post(
        PREFIX + f"/assignments/{identity()}/renew",
        json=body,
        headers=headers,
    )
    _error(response, "invalid_request", 422)
    assert response.json()["error"]["request_id"] == body["meta"]["request_id"]


@pytest.mark.parametrize("forwarded", [False, True])
def test_worker_factory_refuses_http_even_with_forwarded_https(harness, forwarded):
    headers = {"X-Forwarded-Proto": "https"} if forwarded else {}
    with TestClient(
        api.create_worker_app(harness.store, tls=harness.bootstrap.tls),
        base_url="http://controller.example.invalid",
        headers=headers,
    ) as client:
        _error(client.post(PREFIX + "/claims", json={}), "transport_required", 403)


@pytest.mark.parametrize(
    "change", ["authority_id", "certificate_sha256", "binding_revision"]
)
def test_worker_factory_rejects_tls_binding_mismatch(harness, change):
    values = harness.bootstrap.tls.model_dump()
    values[change] = {
        "authority_id": identity(),
        "certificate_sha256": "b" * 64,
        "binding_revision": 2,
    }[change]
    with pytest.raises(m.WorkflowError, match="transport_required"):
        api.create_worker_app(harness.store, tls=m.TlsBinding.model_validate(values))


@pytest.mark.parametrize("client", [("192.0.2.12", 1), ("localhost", 1), None])
def test_local_factory_refuses_nonliteral_or_missing_loopback_peer(harness, client):
    with TestClient(
        _local_app(harness),
        client=client,
        headers={"Authorization": "Bearer " + harness.secret.get_secret_value()},
    ) as test_client:
        _error(test_client.get(PREFIX + "/audit"), "forbidden", 403)


def test_local_guard_errors_share_workflow_envelope_and_keep_origin_rules(
    harness, local
):
    _error(
        local.get(PREFIX + "/audit", headers={"Authorization": "Bearer wrong"}),
        "unauthorized",
        401,
    )
    _error(
        local.get(
            PREFIX + "/audit", headers={"Origin": "https://evil.example.invalid"}
        ),
        "forbidden",
        403,
    )
    _error(
        local.get(
            PREFIX + "/audit",
            headers=[
                ("Authorization", "Bearer wrong"),
                ("Authorization", "Bearer wrong"),
            ],
        ),
        "unauthorized",
        401,
    )
    with TestClient(
        _local_app(harness, active=lambda: False),
        client=("127.0.0.1", 1),
        headers={"Authorization": "Bearer " + harness.secret.get_secret_value()},
    ) as client:
        _error(client.get(PREFIX + "/audit"), "unauthorized", 401)


def test_local_factory_requires_bootstrap_operator_credential(harness):
    with pytest.raises(m.WorkflowError, match="unauthorized"):
        api.create_local_app(
            harness.store,
            control_token="",
            control_active=lambda: True,
            operator_credential_id=harness.bootstrap.operator_credential_id,
        )
    grant, _, _, secret = harness.component()
    with pytest.raises(m.WorkflowError, match="forbidden"):
        api.create_local_app(
            harness.store,
            control_token=secret.get_secret_value(),
            control_active=lambda: True,
            operator_credential_id=grant.credential_id,
        )


@pytest.mark.parametrize("role", ["operator", "observer", "scrape_adapter"])
def test_noncompute_credentials_cannot_use_worker_queries(harness, worker, role):
    if role == "operator":
        credential_id, secret = harness.bootstrap.operator_credential_id, harness.secret
    else:
        grant, _, _, secret = harness.component(role)
        credential_id = grant.credential_id
    response = worker.get(
        PREFIX + f"/films/{identity()}", headers=_bearer(credential_id, secret)
    )
    _error(response, "forbidden", 403)


@pytest.mark.parametrize(
    "header", [None, "Bearer monitor-token", "Bearer ", "Pairing invalid.ticket"]
)
def test_worker_rejects_monitor_missing_and_wrong_credentials(harness, worker, header):
    response = worker.post(
        PREFIX + "/claims",
        json={},
        headers={} if header is None else {"Authorization": header},
    )
    _error(response, "unauthorized", 401)


def test_worker_surface_has_no_local_or_adapter_execution_routes(worker):
    for path in (
        "/audit",
        "/workers",
        "/component-grants",
        "/pairings",
        "/archive",
        "/cleanup",
        "/commands",
    ):
        assert worker.post(PREFIX + path, json={}).status_code == 404
    assert worker.get("/control").status_code == 404
    assert worker.get("/openapi.json").status_code == 404


def test_local_rejects_extra_fields_and_keeps_error_input_out_of_response(
    harness, local
):
    body = _body(harness, _registration())
    body["role"] = "operator"
    body["secret"] = "synthetic-private-value-must-not-echo"
    response = local.post(PREFIX + "/workers", json=body)
    _error(response, "invalid_request", 422)
    assert body["secret"] not in response.text
    assert response.json()["error"]["request_id"] == body["meta"]["request_id"]


@pytest.mark.parametrize("chunked", [False, True])
def test_actual_body_limit_rejects_declared_and_chunked_oversize(local, chunked):
    content = b"x" * (2 * 1024 * 1024 + 1)
    response = local.post(
        PREFIX + "/workers",
        content=iter([content[:1024], content[1024:]]) if chunked else content,
        headers={"Content-Type": "application/json"},
    )
    _error(response, "invalid_request", 422)


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=bad", "after=-1", "after=1.5", "limit=1&limit=2"],
)
def test_audit_pagination_is_bounded_and_unambiguous(local, query):
    _error(local.get(PREFIX + "/audit?" + query), "invalid_request", 422)


def test_closed_store_has_generic_error_without_native_details(harness, local):
    harness.store.close()
    response = local.get(PREFIX + "/audit")
    _error(response, "authority_unavailable", 503)
    assert str(harness.path) not in response.text
    assert "sqlite" not in response.text.lower()


def test_store_callbacks_run_outside_the_asgi_event_loop(harness, monkeypatch):
    threads = {}
    original = harness.store.register_worker

    def observed(*args, **kwargs):
        threads["store"] = threading.get_ident()
        return original(*args, **kwargs)

    monkeypatch.setattr(harness.store, "register_worker", observed)
    app = _local_app(harness)

    @app.middleware("http")
    async def observe_loop(request, call_next):
        threads["loop"] = threading.get_ident()
        return await call_next(request)

    with TestClient(
        app,
        client=("127.0.0.1", 1),
        headers={"Authorization": "Bearer " + harness.secret.get_secret_value()},
    ) as client:
        response = client.post(
            PREFIX + "/workers", json=_body(harness, _registration())
        )
        assert response.status_code == 200, response.text
    assert threads["loop"] != threads["store"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "different_digest"])
def test_rejected_pairing_secret_does_not_consume_ticket(harness, local, worker, fault):
    (
        _,
        body,
        headers,
        _,
    ) = _pending_pairing(harness, local)
    changed = dict(headers)
    if fault == "missing":
        changed.pop("X-Workflow-New-Secret")
    elif fault == "different_digest":
        changed["X-Workflow-New-Secret"] = "another-synthetic-worker-secret-for-tests"
    else:
        changed = list(changed.items()) + [("X-Workflow-New-Secret", "duplicate-value")]
    rejected = worker.post(PREFIX + "/enrollment/complete", json=body, headers=changed)
    _error(rejected, "unauthorized", 401)
    accepted = worker.post(PREFIX + "/enrollment/complete", json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text


def test_unexpected_store_exception_and_private_error_identifiers_are_not_exposed(
    harness, local, monkeypatch, caplog
):
    private = "synthetic-private-error-contents"

    def unavailable(*args, **kwargs):
        raise RuntimeError(private)

    monkeypatch.setattr(harness.store, "list_audit", unavailable)
    response = local.get(PREFIX + "/audit")
    _error(response, "authority_unavailable", 503)
    assert private not in response.text + caplog.text

    def private_id(*args, **kwargs):
        raise m.WorkflowError("not_found", entity_id=private)

    monkeypatch.setattr(harness.store, "list_audit", private_id)
    response = local.get(PREFIX + "/audit")
    _error(response, "not_found", 404)
    assert "entity_id" not in response.json()["error"]
    assert private not in response.text + caplog.text


def test_guard_error_normalization_preserves_allowed_origin(local):
    origin = "http://tauri.localhost"
    response = local.get(
        PREFIX + "/audit", headers={"Origin": origin, "Authorization": "Bearer wrong"}
    )
    _error(response, "unauthorized", 401)
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert int(response.headers["Content-Length"]) == len(response.content)


def test_invalid_local_cors_preflight_has_workflow_error_envelope(local):
    response = local.options(
        PREFIX + "/workers",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "TRACE",
        },
    )
    _error(response, "forbidden", 403)
    assert response.headers["Access-Control-Allow-Origin"] == "http://tauri.localhost"
    assert response.headers["Content-Type"] == "application/json"


def test_deep_invalid_json_is_request_error_not_authority_outage(local):
    response = local.post(
        PREFIX + "/workers",
        content=b"[" * 5000 + b"0" + b"]" * 5000,
        headers={"Content-Type": "application/json"},
    )
    _error(response, "invalid_request", 422)


def test_invalid_request_id_is_not_reflected(harness, local):
    body = _body(harness, _registration())
    body["meta"]["request_id"] = "synthetic-private-invalid-request-id"
    response = local.post(PREFIX + "/workers", json=body)
    _error(response, "invalid_request", 422)
    assert body["meta"]["request_id"] not in response.text
