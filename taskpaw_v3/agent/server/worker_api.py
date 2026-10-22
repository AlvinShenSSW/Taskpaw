"""P2-2 (#293): the controller's worker API (port 5682, `/worker/v1` only).

A third uvicorn app, mounted only when role=controller, the workflow is
enabled and `remote_workers` is on. Every route needs the Bearer of a
`compute_worker` principal (`<credential_id>.<secret>`), except `/pair`,
which needs a pairing ticket and ignores any Bearer. Operator and component
credentials get 403; a revoked or retired worker's credential no longer
authenticates (401). Errors carry `{code}` only: no echo of a request, a
secret or a ticket. No docs or openapi route; nothing forwards to 5681.

The workflow host starts after the agent is ready, so the app reads it through
a late-bound provider and answers 503 `controller_starting` until it runs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.devices import iso
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.transfer import (
    PlanFile,
    TransferError,
    Transfers,
    parse_content_range,
)

if TYPE_CHECKING:
    from taskpaw_v3.workflow.auth import AuthProof
    from taskpaw_v3.workflow.local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.worker_api")

PREFIX = "/worker/v1"
JSON_CAP = 1 << 20  # 1 MiB
HEARTBEAT_CAP = 64 << 10  # 64 KiB
CREDENTIAL_LIFETIME = timedelta(days=3650)  # long-lived, like the local runtime's
IDLE_SECONDS = 120.0  # IR4: a stream without progress this long is dropped

# store error code → HTTP status (the body keeps the code only)
_STATUS = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "invalid_request": 422,
    "authority_unavailable": 503,
}


class ControllerHost(Protocol):
    """What the worker API needs from the controller's workflow host."""

    state: str
    runtime: LocalRuntime | None
    fleet: FleetLive | None
    transfers: Transfers | None
    dispatcher: Any


HostProvider = Callable[[], "ControllerHost | None"]


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(code)
        self.status, self.code, self.headers, self.extra = status, code, headers, extra


def coded(exc: m.WorkflowError) -> ApiError:
    return ApiError(_STATUS.get(exc.code, 409), exc.code)


def _error(status: int, code: str, headers: dict[str, str] | None = None, **extra):
    return JSONResponse({"code": code, **extra}, status_code=status, headers=headers)


class BodyCap:
    """Buffers a non-PUT body up to its cap (64 KiB heartbeat, else 1 MiB) and
    answers 413 past it; a PUT is bounded by its own Content-Range (§2)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] == "PUT":
            await self.app(scope, receive, send)
            return
        cap = HEARTBEAT_CAP if scope["path"].endswith("/heartbeat") else JSON_CAP
        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > cap:
                await _error(413, "too_large")(scope, receive, send)
                return
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class PairBody(_Strict):
    pairing_id: m.Id
    ticket: SecretStr = Field(max_length=256)
    credential_id: m.Id
    secret: SecretStr = Field(max_length=256)


class ProbeBody(_Strict):
    """The worker's probe; the server fills revision, time and evidence (R4)."""

    source: m.Text
    cpu: m.Text
    ram_mib: m.Revision
    gpu_model: m.Text
    vram_mib: m.Revision
    device_identity: m.Text
    gpu_identity: m.Text
    free_disk_bytes: m.Count
    model_ready: bool
    profiles: list[m.ProfileCapability] = Field(default=[], max_length=16)
    approved: Any = None  # never accepted from a worker (403)


class OutputFile(_Strict):
    path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0, le=10**15)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ReleasingFile(FileResponse):
    """A ranged work-copy stream whose download slot frees when it ends (R17),
    also when the worker stops reading for `idle` seconds (IR4): a dead
    connection never holds the slot."""

    def __init__(
        self,
        *args: Any,
        on_close: Callable[[], None],
        idle: float = IDLE_SECONDS,
        **kw: Any,
    ) -> None:
        super().__init__(*args, **kw)
        self._on_close, self._idle = on_close, idle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def guarded(message: Message) -> None:
            await asyncio.wait_for(send(message), self._idle)

        try:
            await super().__call__(scope, receive, guarded)
        except asyncio.TimeoutError:  # py3.10: not the builtin
            log.warning("a work-copy download stalled; its stream was dropped")
        finally:
            self._on_close()


UPLOAD_CHUNK = 4 << 20


class BootBody(_Strict):
    boot_id: m.Id
    assignment_ids: list[m.Id] = Field(default=[], max_length=64)
    local_owners: list[m.LocalOwnerEvidence] = Field(default=[], max_length=64)


@dataclass(frozen=True)
class Caller:
    host: ControllerHost
    rt: LocalRuntime
    fleet: FleetLive
    auth: AuthProof = field(repr=False)
    me: m.WorkerSelf

    @property
    def worker_id(self) -> str:
        return self.me.worker.worker_id


def _running(provider: HostProvider) -> tuple[ControllerHost, LocalRuntime, FleetLive]:
    host = provider()
    if (
        host is None
        or host.state != "running"
        or host.runtime is None
        or host.fleet is None
    ):
        raise ApiError(503, "controller_starting")
    return host, host.runtime, host.fleet


def create_worker_app(
    provider: HostProvider, *, idle_seconds: float = IDLE_SECONDS
) -> FastAPI:
    from taskpaw_v3.workflow.auth import (
        authenticate,
        authenticate_pairing,
        digest_secret,
        parse_bearer,
    )

    app = FastAPI(
        title="TaskPaw Worker API", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(BodyCap)

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.headers, **exc.extra)

    @app.exception_handler(TransferError)
    async def _transfer_error(_request: Request, exc: TransferError) -> JSONResponse:
        headers = (
            {"Retry-After": str(exc.extra["retry_after"])}
            if "retry_after" in exc.extra
            else None
        )
        extra = {k: v for k, v in exc.extra.items() if k != "retry_after"}
        return _error(exc.status, exc.code, headers, **extra)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, _exc: RequestValidationError):
        return _error(422, "invalid_request")

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException):
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(exc.status_code, codes.get(exc.status_code, "http_error"))

    def caller(request: Request) -> Caller:
        try:
            credential_id, secret = parse_bearer(request.headers.get("authorization"))
        except m.WorkflowError:
            raise ApiError(401, "unauthorized") from None
        host, rt, fleet = _running(provider)
        try:
            auth = authenticate(rt.store, credential_id, secret)
            me = rt.store.authenticated_worker(auth)
        except m.WorkflowError as exc:
            raise coded(exc) from None
        if me.worker.worker_id == rt.identity.worker_id:  # 本机 is not remote
            raise ApiError(403, "forbidden")
        return Caller(host, rt, fleet, auth, me)

    authed = Depends(caller)

    def self_view(me: m.WorkerSelf, rt: LocalRuntime) -> dict[str, Any]:
        return {
            "worker": me.worker.model_dump(mode="json"),
            "boot_reconciled": me.boot_reconciled,
            "server_time": iso(rt.clock.now()),
        }

    @app.post(f"{PREFIX}/pair")
    def pair(body: PairBody) -> dict[str, Any]:
        """R2: ticket → credential; the worker chose and stored its secret first."""
        _, rt, _ = _running(provider)
        try:
            proof = authenticate_pairing(
                rt.store, body.pairing_id, body.ticket, body.secret
            )
            worker_id, device_id, generation = rt.store.pairing_slot(
                rt.operator, body.pairing_id
            )
            rt.store.complete_pairing(
                proof,
                rt.meta(),
                m.PairingComplete(
                    pairing_id=body.pairing_id,
                    worker_id=worker_id,
                    device_id=device_id,
                    credential_id=body.credential_id,
                    secret_digest=digest_secret(body.secret),
                    generation=generation,
                    expires_at=rt.clock.now() + CREDENTIAL_LIFETIME,
                ),
            )
        except m.WorkflowError as exc:
            raise coded(exc) from None
        log.info("worker %s paired (generation %d)", worker_id, generation)
        return {
            "worker_id": worker_id,
            "device_id": device_id,
            "generation": generation,
        }

    @app.get(f"{PREFIX}/self")
    def self_(c: Caller = authed) -> dict[str, Any]:
        return self_view(c.me, c.rt)

    @app.post(f"{PREFIX}/capabilities")
    def capabilities(body: ProbeBody, c: Caller = authed) -> dict[str, Any]:
        """R4: the worker's probe as a new revision; this de-verifies it (store)."""
        if "approved" in body.model_fields_set:
            raise ApiError(403, "forbidden")
        rt = c.rt
        for attempt in range(2):  # the server fills expected_version; one CAS retry
            me = c.me if attempt == 0 else rt.store.authenticated_worker(c.auth)
            old = me.worker.capability
            revision = (old.revision if old else 0) + 1
            now = rt.clock.now()
            probe = m.HardwareProbe(
                revision=revision,
                observed_at=now,
                evidence=rt.evidence("hardware_probe", producer=c.auth),
                **body.model_dump(exclude={"approved", "profiles"}),
                profiles=tuple(body.profiles),
            )
            try:
                result = rt.store.record_capabilities(
                    c.auth,
                    rt.meta(),
                    m.CapabilityUpdate(
                        worker_id=me.worker.worker_id,
                        expected_version=me.worker.version,
                        capability=m.CapabilityRevision(
                            revision=revision,
                            declared=None,
                            probed=probe,
                            approved=None,
                        ),
                        physical_binding=None,
                    ),
                )
            except m.WorkflowError as exc:
                if exc.code == "cas_conflict" and attempt == 0:
                    continue
                raise coded(exc) from None
            return {"revision": revision, "status": result.status}
        raise ApiError(409, "cas_conflict")  # pragma: no cover - loop returns

    @app.post(f"{PREFIX}/boot")
    def boot(body: BootBody, c: Caller = authed) -> dict[str, Any]:
        """R6: a boot; the same `boot_id` is re-posted until reconciled (C6)."""
        rt = c.rt
        for attempt in range(2):
            me = c.me if attempt == 0 else rt.store.authenticated_worker(c.auth)
            try:
                result = rt.store.record_boot(
                    c.auth,
                    rt.meta(),
                    m.BootObservation(
                        worker_id=me.worker.worker_id,
                        expected_version=me.worker.version,
                        boot_id=body.boot_id,
                        known_epoch=1,
                        assignment_ids=tuple(body.assignment_ids),
                        local_owners=tuple(body.local_owners),
                    ),
                )
            except m.WorkflowError as exc:
                if exc.code == "cas_conflict" and attempt == 0:
                    continue
                raise coded(exc) from None
            return {
                "status": result.status,
                "reconciled": result.status == "reconciled",
            }
        raise ApiError(409, "cas_conflict")  # pragma: no cover - loop returns

    @app.post(f"{PREFIX}/heartbeat")
    def heartbeat(body: Heartbeat, c: Caller = authed) -> dict[str, Any]:
        """R7: into FleetLive only (no store write, no renew, C5); the reply
        carries the worker's routes, the global pause and what to release."""
        rt, now = c.rt, c.rt.clock.now()
        c.fleet.observe(c.worker_id, body, now)
        try:
            routes = [
                r
                for r in rt.store.list_routes(rt.operator)
                if r.worker_id == c.worker_id
            ]
            held = _held(rt, c.worker_id) if body.staged else set()
        except m.WorkflowError as exc:
            raise coded(exc) from None
        routed = {(r.film_id, r.input_revision) for r in routes}
        dispatcher = getattr(c.host, "dispatcher", None)
        status = dispatcher.status() if dispatcher is not None else None
        return {
            "server_time": iso(now),
            "paused": bool(getattr(status, "paused", False)),
            "routes": [
                {
                    "route_id": r.route_id,
                    "film": r.film_id,
                    "rev": r.input_revision,
                    "position": position,
                }
                for position, r in enumerate(routes)
            ],
            "cancel": [],  # producers arrive with P2-3/P2-4
            "release": [
                {"film": item.film, "rev": item.rev}
                for item in body.staged
                if (item.film, item.rev) not in routed | held
            ],
            "reconcile_needed": c.me.worker.boot_id != body.boot_id
            or not c.me.boot_reconciled,
        }

    # -- transfer (R11-R17) -------------------------------------------------
    def transfers_of(c: Caller) -> Transfers:
        transfers = getattr(c.host, "transfers", None)
        if transfers is None:
            raise ApiError(503, "controller_starting")
        return transfers

    def own_attempt(c: Caller, attempt_id: str) -> m.WorkerAttempt:
        """C7/R14: current, unresolved and the caller's own (else 409)."""
        try:
            return c.rt.store.worker_attempt(c.auth, attempt_id)
        except m.WorkflowError as exc:
            raise coded(exc) from None

    @app.get(f"{PREFIX}/films/{{film_id}}/{{revision}}")
    def film_envelope(film_id: str, revision: int, c: Caller = authed) -> JSONResponse:
        from taskpaw_v3.workflow.transfer import envelope

        try:
            value = envelope(c.rt, c.worker_id, film_id, revision)
        except m.WorkflowError as exc:
            raise coded(exc) from None
        response = JSONResponse(value)
        if len(response.body) > JSON_CAP:
            raise ApiError(413, "too_large")
        return response

    @app.get(f"{PREFIX}/films/{{film_id}}/{{revision}}/items/{{item_id}}")
    def film_item(
        film_id: str, revision: int, item_id: str, c: Caller = authed
    ) -> FileResponse:
        from taskpaw_v3.workflow.transfer import item_file

        transfers = transfers_of(c)
        try:
            path, copy = item_file(c.rt, c.worker_id, film_id, revision, item_id)
        except m.WorkflowError as exc:
            raise coded(exc) from None
        transfers.acquire(c.worker_id, "down")
        return _ReleasingFile(
            path,
            media_type="application/octet-stream",
            headers={"X-Sha256": copy.sha256, "X-Size": str(copy.size_bytes)},
            on_close=lambda: transfers.release(c.worker_id, "down"),
            idle=idle_seconds,
        )

    @app.post(f"{PREFIX}/attempts/{{attempt_id}}/outputs")
    def post_outputs(
        attempt_id: str, files: list[OutputFile], c: Caller = authed
    ) -> dict[str, Any]:
        own_attempt(c, attempt_id)
        return transfers_of(c).put_plan(
            attempt_id, [PlanFile(f.path, f.size, f.sha256) for f in files]
        )

    @app.get(f"{PREFIX}/attempts/{{attempt_id}}/outputs")
    def get_outputs(attempt_id: str, c: Caller = authed) -> dict[str, Any]:
        own_attempt(c, attempt_id)
        return transfers_of(c).view(attempt_id)

    @app.put(f"{PREFIX}/attempts/{{attempt_id}}/files/{{path:path}}")
    async def put_file(
        attempt_id: str, path: str, request: Request, c: Caller = authed
    ) -> dict[str, Any]:
        """R13: one streaming PUT from the received offset to the end."""
        transfers = transfers_of(c)
        await run_in_threadpool(own_attempt, c, attempt_id)
        offset, total = parse_content_range(request.headers.get("content-range"))
        expected = total - offset
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > expected):
            raise ApiError(413, "too_large")
        transfers.acquire(c.worker_id, "up")
        try:
            upload = await run_in_threadpool(
                transfers.open_upload, attempt_id, path, offset, total
            )
            try:
                got, buffer, more = 0, bytearray(), True
                while more:
                    try:  # IR4: a body without progress frees the slot
                        message = await asyncio.wait_for(
                            request.receive(), idle_seconds
                        )
                    except asyncio.TimeoutError:  # py3.10: not the builtin
                        log.warning("an upload stalled; its stream was dropped")
                        more = False
                        break
                    if message["type"] == "http.disconnect":
                        more = False  # IR11: keep what arrived, no 500
                        break
                    chunk = message.get("body", b"")
                    more = message.get("more_body", False)
                    got += len(chunk)
                    if got > expected:
                        await run_in_threadpool(upload.truncate)
                        raise ApiError(413, "too_large")
                    buffer += chunk
                    if len(buffer) >= UPLOAD_CHUNK:
                        await run_in_threadpool(upload.write, bytes(buffer))
                        buffer.clear()
                if buffer:
                    await run_in_threadpool(upload.write, bytes(buffer))
                if got < expected:
                    raise ApiError(409, "offset_mismatch", received=offset + got)
                return await run_in_threadpool(upload.finish)
            finally:
                await run_in_threadpool(upload.close)
        finally:
            transfers.release(c.worker_id, "up")

    return app


def _held(rt: LocalRuntime, worker_id: str) -> set[tuple[str, int]]:
    """Film revisions with an unresolved assignment of this worker."""
    held = set()
    for open_ in rt.store.list_open_assignments(rt.operator):
        if open_.assignment.worker_id == worker_id:
            stage = rt.store.get_stage(rt.operator, open_.assignment.job_id)
            held.add((stage.film_id, stage.input_revision))
    return held
