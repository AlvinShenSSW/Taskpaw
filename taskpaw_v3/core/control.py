"""Credential-gated local control, separate from the LAN polling Bearer.

The outer ASGI guard rejects origin/header errors before CORS, body parsing or
routing. Every production startup creates a new wire key after claiming ports.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from taskpaw_v3.core.control_file import (
    ControlCredentialError,
    ControlDescriptor,
    CredentialLease,
    read_control_descriptor,
    validate_control_base,
    validate_control_token,
)
from taskpaw_v3.core.cors import UI_ORIGINS

__all__ = [
    "ControlCredentialError",
    "ControlDescriptor",
    "ControlSession",
    "add_control_guard",
    "bootstrap_control",
    "read_control_descriptor",
    "revoke_control",
    "strip_control_env",
    "without_control_env",
]

log = logging.getLogger("taskpaw.control")


def _control_env(name: str) -> bool:
    key = name.upper()
    return key.startswith("TASKPAW_CONTROL_") or key == "TASKPAW_UI_TOKEN"


def without_control_env(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Copy an environment without controller credentials/discovery settings."""
    source = os.environ if base is None else base
    return {key: value for key, value in source.items() if not _control_env(key)}


def strip_control_env(environ: Optional[MutableMapping[str, str]] = None) -> None:
    """Remove each controller variable before tasks spawn; never reset the env."""
    source = os.environ if environ is None else environ
    unsupported = any(key.upper() == "TASKPAW_CONTROL_TOKEN" for key in source)
    for key in list(source):
        if _control_env(key):
            source.pop(key, None)
    if unsupported:
        log.warning(
            "Static control credentials are unsupported; using a new runtime key"
        )


@dataclass
class ControlSession:
    role: str
    base_url: str
    boot_id: str
    token: str = field(repr=False)
    credential_file: Optional[Path] = None
    _lease: Optional[CredentialLease] = field(default=None, repr=False)
    _active: threading.Event = field(default_factory=threading.Event, repr=False)

    def __post_init__(self) -> None:
        validate_control_token(self.token)
        self._active.set()

    def is_active(self) -> bool:
        return self._active.is_set()


def bootstrap_control(
    role: str, base_url: str, config_path: Optional[Path]
) -> ControlSession:
    """Publish a fresh key. Caller must already own both listening sockets."""
    validate_control_base(base_url)
    if role not in {"agent", "hub"}:
        raise ControlCredentialError("invalid_control_descriptor")
    token, boot_id = secrets.token_urlsafe(32), secrets.token_hex(16)
    if config_path is None:
        return ControlSession(role, base_url, boot_id, token)
    descriptor = ControlDescriptor(1, role, base_url, boot_id, token)
    lease = CredentialLease(Path(config_path).parent, f"{role}.control.json")
    try:
        lease.publish(descriptor)
    except Exception:
        lease.close()
        raise
    return ControlSession(role, base_url, boot_id, token, lease.path, lease)


def revoke_control(session: ControlSession) -> None:
    """Invalidate first; remove only this boot's file; always close its lease."""
    session._active.clear()
    lease, session._lease = session._lease, None
    if lease is not None:
        try:
            lease.revoke(session.boot_id, session.role)
        finally:
            lease.close()


class _ControlGuard:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        is_active: Callable[[], bool],
        ping_path: str,
    ) -> None:
        self._expected = b"Bearer " + token.encode("ascii")
        self._is_active = is_active
        self._ping_path = ping_path
        self._cors = CORSMiddleware(
            app,
            allow_origins=UI_ORIGINS,
            allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._cors(scope, receive, send)
            return
        headers = scope.get("headers", [])
        origins = [value for key, value in headers if key.lower() == b"origin"]
        origin: Optional[str] = None
        if origins:
            try:
                origin = origins[0].decode("ascii")
            except UnicodeDecodeError:
                origin = None
            if len(origins) != 1 or origin not in UI_ORIGINS:
                await self._reject(
                    scope, receive, send, 403, "control_origin_forbidden"
                )
                return
        if scope["method"] == "OPTIONS":
            preflights = [
                value
                for key, value in headers
                if key.lower() == b"access-control-request-method"
            ]
            if origin is None or len(preflights) != 1:
                await self._reject(
                    scope, receive, send, 403, "control_origin_forbidden"
                )
                return
            await self._cors(scope, receive, send)
            return
        probe = scope["path"] == self._ping_path and scope["method"] in {"GET", "HEAD"}
        if not probe:
            auth = [value for key, value in headers if key.lower() == b"authorization"]
            if (
                len(auth) != 1
                or not self._is_active()
                or not hmac.compare_digest(auth[0], self._expected)
            ):
                await self._reject(
                    scope, receive, send, 401, "control_unauthorized", origin
                )
                return
        await self._cors(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        error: str,
        origin: Optional[str] = None,
    ) -> None:
        headers = {}
        if status == 401:
            headers["WWW-Authenticate"] = 'Bearer realm="TaskPaw Control"'
        if origin in UI_ORIGINS:
            headers["Access-Control-Allow-Origin"] = str(origin)
            headers["Vary"] = "Origin"
        detail = (
            "Local control credentials are missing or no longer valid."
            if status == 401
            else "This origin is not allowed to control TaskPaw."
        )
        await JSONResponse(
            {"error": error, "detail": detail}, status_code=status, headers=headers
        )(scope, receive, send)


def add_control_guard(
    app: FastAPI,
    *,
    control_token: str,
    is_active: Callable[[], bool],
    ping_path: str,
) -> None:
    """Install origin validation outside CORS; do not add another outer CORS."""
    validate_control_token(control_token)
    app.add_middleware(
        _ControlGuard, token=control_token, is_active=is_active, ping_path=ping_path
    )
