"""Unmounted workflow HTTP factories; callers own the store and listening socket."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Generic, TypeVar
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from taskpaw_v3.core.control import add_control_guard

from . import models as m
from .auth import (
    AuthProof,
    PairingProof,
    authenticate,
    authenticate_pairing,
    parse_bearer,
)

if TYPE_CHECKING:
    from .store import WorkflowStore

PREFIX = "/v1/workflow"
MAX_BODY_BYTES = 2 * 1024 * 1024
_COMMAND = TypeVar("_COMMAND", bound=m.Model)
_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "transport_required": 403,
    "not_found": 404,
    "cas_conflict": 409,
    "idempotency_conflict": 409,
    "stale_attempt": 409,
    "stale_manifest": 409,
    "owner_unresolved": 409,
    "binding_changed": 409,
    "approval_expired": 409,
    "approval_revoked": 409,
    "precondition_failed": 409,
    "invalid_request": 422,
    "authority_unavailable": 503,
}


class _Envelope(BaseModel, Generic[_COMMAND]):
    model_config = ConfigDict(extra="forbid")
    meta: m.CommandMeta
    command: _COMMAND


def _request_id(scope: Scope) -> str:
    return scope.setdefault("state", {}).setdefault("workflow_request_id", str(uuid4()))


def _error(scope: Scope, error: m.WorkflowError) -> JSONResponse:
    # Unknown implementation error strings never cross the public boundary.
    code = error.code if error.code in _STATUS else "authority_unavailable"
    body: dict[str, object] = {
        "code": code,
        "request_id": _request_id(scope),
        "retryable": bool(error.retryable) if code == error.code else False,
    }
    if error.entity_id is not None:
        try:
            public_identity = _identifier(error.entity_id)
        except m.WorkflowError:
            public_identity = None
        if public_identity is not None:
            body["entity_id"] = public_identity
    return JSONResponse({"error": body}, status_code=_STATUS[code])


def _identifier(value: object) -> str:
    if not isinstance(value, str):
        raise m.WorkflowError("invalid_request")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise m.WorkflowError("invalid_request") from None
    return value


def _header(request: Request, name: bytes) -> str:
    values = [v for k, v in request.scope.get("headers", []) if k.lower() == name]
    if len(values) != 1:
        raise m.WorkflowError("unauthorized")
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        raise m.WorkflowError("unauthorized") from None


class _WorkflowBoundary:
    """Enforce transport/body bounds and translate only the reused guard's errors."""

    def __init__(self, app: ASGIApp, *, local: bool) -> None:
        self.app = app
        self.local = local

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        _request_id(scope)
        started = False
        pending: Message | None = None
        guard_body = bytearray()
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise m.WorkflowError("invalid_request")
            return message

        async def normalized_send(message: Message) -> None:
            nonlocal started, pending
            if message["type"] == "http.response.start" and (
                message["status"] in (401, 403)
                or (
                    self.local
                    and scope["method"] == "OPTIONS"
                    and message["status"] == 400
                )
            ):
                pending = message
                return
            if pending is not None and message["type"] == "http.response.body":
                guard_body.extend(message.get("body", b""))
                if len(guard_body) > 8192:
                    raise m.WorkflowError("authority_unavailable")
                if message.get("more_body", False):
                    return
                body = bytes(guard_body)
                try:
                    content = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    content = None
                code = None
                if pending["status"] == 400:
                    code = "forbidden"
                elif isinstance(content, dict) and content.get("error") in (
                    "control_unauthorized",
                    "control_origin_forbidden",
                ):
                    code = "unauthorized" if pending["status"] == 401 else "forbidden"
                if code is not None:
                    response = _error(scope, m.WorkflowError(code))
                    body = bytes(response.body)
                    pending["status"] = response.status_code
                pending["headers"] = [
                    (key, value)
                    for key, value in pending.get("headers", [])
                    if key.lower() != b"content-length"
                    and (code is None or key.lower() != b"content-type")
                ] + [(b"content-length", str(len(body)).encode("ascii"))]
                if code is not None:
                    pending["headers"].append((b"content-type", b"application/json"))
                await send(pending)
                started = True
                pending = None
                await send({"type": "http.response.body", "body": body})
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            if self.local:
                peer = scope.get("client")
                try:
                    local_peer = (
                        peer is not None and ipaddress.ip_address(peer[0]).is_loopback
                    )
                except ValueError:
                    local_peer = False
                if not local_peer:
                    raise m.WorkflowError("forbidden")
            elif scope.get("scheme") != "https":
                raise m.WorkflowError("transport_required")
            lengths = [
                v for k, v in scope.get("headers", []) if k.lower() == b"content-length"
            ]
            if lengths and (
                len(lengths) != 1
                or len(lengths[0]) > 20
                or not lengths[0].isdigit()
                or int(lengths[0]) > MAX_BODY_BYTES
            ):
                raise m.WorkflowError("invalid_request")
            await self.app(scope, bounded_receive, normalized_send)
        except Exception as exc:
            if started:
                # These factories return finite JSON, never a partial streaming job.
                raise RuntimeError("workflow_response_failed") from None
            error = (
                exc
                if isinstance(exc, m.WorkflowError)
                else m.WorkflowError("authority_unavailable")
            )
            await _error(scope, error)(scope, receive, send)


def _app() -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.exception_handler(m.WorkflowError)
    async def workflow_error(request: Request, exc: m.WorkflowError) -> JSONResponse:
        return _error(request.scope, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(request.scope, m.WorkflowError("invalid_request"))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error(
            request.scope,
            m.WorkflowError(
                "not_found" if exc.status_code == 404 else "invalid_request"
            ),
        )

    return app


async def _envelope(
    request: Request, command_type: type[_COMMAND]
) -> _Envelope[_COMMAND]:
    try:
        raw = await request.json()
        if isinstance(raw, dict) and isinstance(raw.get("meta"), dict):
            request.scope["state"]["workflow_request_id"] = _identifier(
                raw["meta"].get("request_id")
            )
        return _Envelope[command_type].model_validate(raw)  # type: ignore[valid-type]
    except (ValidationError, ValueError, UnicodeDecodeError, RecursionError):
        raise m.WorkflowError("invalid_request") from None


async def _worker_auth(store: WorkflowStore, request: Request) -> AuthProof:
    credential_id, secret = parse_bearer(_header(request, b"authorization"))

    def verify() -> AuthProof:
        proof = authenticate(store, credential_id, secret)
        store.verify_worker(proof)
        return proof

    return await run_in_threadpool(verify)


def _write_route(
    app: FastAPI,
    store: WorkflowStore,
    path: str,
    method: str,
    command_type: type[m.Model],
    command_name: str,
    *,
    local_auth: AuthProof | None = None,
    id_field: str | None = None,
) -> None:
    async def endpoint(request: Request) -> JSONResponse:
        proof = (
            local_auth if local_auth is not None else await _worker_auth(store, request)
        )
        envelope = await _envelope(request, command_type)
        if id_field is not None and _identifier(request.path_params["id"]) != getattr(
            envelope.command, id_field
        ):
            raise m.WorkflowError("invalid_request")
        # This dispatch table is fixed in source; no client-supplied method names.
        result = await run_in_threadpool(
            getattr(store, command_name), proof, envelope.meta, envelope.command
        )
        return JSONResponse(jsonable_encoder(result))

    app.add_api_route(PREFIX + path, endpoint, methods=[method])


def _read_route(
    app: FastAPI,
    store: WorkflowStore,
    path: str,
    method_name: str,
    *,
    local_auth: AuthProof | None = None,
) -> None:
    async def endpoint(request: Request) -> JSONResponse:
        proof = (
            local_auth if local_auth is not None else await _worker_auth(store, request)
        )
        identity = _identifier(request.path_params["id"])
        result = await run_in_threadpool(getattr(store, method_name), proof, identity)
        return JSONResponse(jsonable_encoder(result))

    app.add_api_route(PREFIX + path, endpoint, methods=["GET"])


def create_worker_app(store: WorkflowStore, *, tls: m.TlsBinding) -> FastAPI:
    """Require the explicit authority binding; HTTPS scheme is not TLS deployment."""
    try:
        binding = m.TlsBinding.model_validate(tls)
    except ValidationError:
        raise m.WorkflowError("transport_required") from None
    if binding != store.get_authority_binding():
        raise m.WorkflowError("transport_required")
    app = _app()

    @app.post(PREFIX + "/enrollment/complete")
    async def enrollment(request: Request) -> JSONResponse:
        header = _header(request, b"authorization")
        proof: AuthProof | PairingProof
        if header.startswith("Pairing "):
            # Reuse only bearer UUID/secret spelling validation, not credential auth.
            pairing_id, ticket = parse_bearer("Bearer " + header[len("Pairing ") :])
            new_secret = SecretStr(_header(request, b"x-workflow-new-secret"))
            proof = await run_in_threadpool(
                authenticate_pairing, store, pairing_id, ticket, new_secret
            )
        else:
            proof = await _worker_auth(store, request)
        envelope = await _envelope(request, m.PairingComplete)
        if (
            isinstance(proof, PairingProof)
            and proof.pairing_id != envelope.command.pairing_id
        ) or (
            isinstance(proof, AuthProof)
            and proof.credential_id != envelope.command.credential_id
        ):
            raise m.WorkflowError("invalid_request")
        result = await run_in_threadpool(
            store.complete_pairing, proof, envelope.meta, envelope.command
        )
        return JSONResponse(jsonable_encoder(result))

    for path, dto, command, field in (
        ("/boot", m.BootObservation, "record_boot", None),
        ("/claims", m.ClaimRequest, "claim", None),
        ("/assignments/{id}/renew", m.AssignmentRenewal, "renew", "assignment_id"),
        ("/attempts/{id}/result", m.AttemptResult, "finish_attempt", "attempt_id"),
    ):
        _write_route(app, store, path, "POST", dto, command, id_field=field)
    _read_route(app, store, "/films/{id}", "get_film")
    _read_route(app, store, "/operations/{id}", "get_operation")
    app.add_middleware(_WorkflowBoundary, local=False)
    return app


def create_local_app(
    store: WorkflowStore,
    *,
    control_token: str,
    control_active: Callable[[], bool],
    operator_credential_id: str,
) -> FastAPI:
    """Map the local control token to the bootstrap operator, never a body role."""
    if not isinstance(control_token, str) or not control_token:
        raise m.WorkflowError("unauthorized")
    proof = authenticate(store, operator_credential_id, SecretStr(control_token))
    store.verify_operator(proof)
    app = _app()
    for path, method, dto, command, field in (
        ("/component-grants", "POST", m.ComponentGrant, "grant_component", None),
        (
            "/credentials/{id}/revoke",
            "POST",
            m.CredentialRevocation,
            "revoke_credential",
            "credential_id",
        ),
        ("/workers", "POST", m.WorkerRegistration, "register_worker", None),
        ("/workers/{id}", "PATCH", m.WorkerUpdate, "update_worker", "worker_id"),
        (
            "/workers/{id}/capabilities",
            "PUT",
            m.CapabilityUpdate,
            "record_capabilities",
            "worker_id",
        ),
        ("/pairings", "POST", m.PairingIssue, "issue_pairing", None),
        (
            "/workers/{id}/revoke",
            "POST",
            m.WorkerRevocation,
            "revoke_worker",
            "worker_id",
        ),
        (
            "/workers/{id}/retire",
            "POST",
            m.WorkerRetirement,
            "retire_worker",
            "worker_id",
        ),
        (
            "/films/{id}/approval-context",
            "PUT",
            m.ApprovalContextPut,
            "put_approval_context",
            "film_id",
        ),
        ("/films/{id}/approvals", "POST", m.ApprovalCreate, "approve", "film_id"),
        (
            "/approvals/{id}/revoke",
            "POST",
            m.ApprovalRevocation,
            "revoke_approval",
            "approval_id",
        ),
        ("/stages/{id}/retry", "POST", m.StageRetry, "retry_stage", "job_id"),
        ("/stages/{id}/skip", "POST", m.StageSkip, "skip_stage", "job_id"),
        (
            "/assignments/{id}/unknown",
            "POST",
            m.UnknownObservation,
            "mark_unknown",
            "assignment_id",
        ),
        (
            "/assignments/{id}/resolve",
            "POST",
            m.AssignmentResolution,
            "resolve_assignment",
            "assignment_id",
        ),
    ):
        _write_route(
            app, store, path, method, dto, command, local_auth=proof, id_field=field
        )
    for path, method in (
        ("/films/{id}", "get_film"),
        ("/workers/{id}", "get_worker"),
        ("/operations/{id}", "get_operation"),
    ):
        _read_route(app, store, path, method, local_auth=proof)

    @app.get(PREFIX + "/audit")
    async def audit(request: Request) -> JSONResponse:
        query = request.query_params
        if any(key not in {"after", "limit"} for key in query):
            raise m.WorkflowError("invalid_request")
        values = {}
        for key, default, lower, upper in (
            ("after", "0", 0, (1 << 63) - 1),
            ("limit", "100", 1, 200),
        ):
            raw = query.getlist(key)
            value = raw[0] if raw else default
            if (
                len(raw) > 1
                or len(value) > 20
                or not value.isascii()
                or not value.isdigit()
            ):
                raise m.WorkflowError("invalid_request")
            values[key] = int(value)
            if not lower <= values[key] <= upper:
                raise m.WorkflowError("invalid_request")
        result = await run_in_threadpool(
            store.list_audit, proof, values["after"], values["limit"]
        )
        return JSONResponse(jsonable_encoder(result))

    # No unauthenticated probe is registered; empty ping_path cannot match a path.
    add_control_guard(
        app, control_token=control_token, is_active=control_active, ping_path=""
    )
    app.add_middleware(_WorkflowBoundary, local=True)
    return app
