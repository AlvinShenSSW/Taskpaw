"""Nonsecret, fixed Agent startup failure protocol for the desktop shell."""

from __future__ import annotations

import errno
import json
import socket
from pathlib import Path

from taskpaw_v3.core.net import PortInUseError
from taskpaw_v3.core.state import StateError, state_paths

STARTUP_CODES = frozenset(
    {
        "migration_required",
        "initialization_required",
        "state_recovery_required",
        "state_lease_unavailable",
        "config_invalid",
        "config_unwritable",
        "port_in_use",
        "bind_address_unavailable",
        "startup_failed",
    }
)


def emit_startup_error(code: str) -> None:
    if code not in STARTUP_CODES:
        raise ValueError("unknown startup failure code")
    print(
        json.dumps({"taskpaw_startup_error": 1, "role": "agent", "code": code}),
        flush=True,
    )


def startup_code(exc: BaseException, *, state_path: Path | None = None) -> str:
    if isinstance(exc, StateError):
        if exc.reason == "lease_held_or_unavailable":
            return "state_lease_unavailable"
        if exc.reason in {"migration_required", "initialization_required"}:
            if state_path is None:
                return exc.reason
            primary, anchor, _ = state_paths(state_path)
            try:
                # A partial record pair must never invite desktop initialization.
                if exc.reason == "migration_required" and not (
                    anchor.exists() or anchor.is_symlink()
                ):
                    return exc.reason
                if not any(
                    p.exists() or p.is_symlink() for p in (primary, anchor)
                ) and not any(
                    any(p.parent.glob(p.name + ".fault-*")) for p in (primary, anchor)
                ):
                    return "initialization_required"
            except OSError:
                return "state_recovery_required"
        return "state_recovery_required"

    errors: list[OSError] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        if isinstance(current, OSError):
            errors.append(current)
        current = current.__cause__ or current.__context__
    if any(
        error.errno in {errno.EADDRNOTAVAIL, 10049}
        or getattr(error, "winerror", None) == 10049
        or isinstance(error, socket.gaierror)
        for error in errors
    ):
        return "bind_address_unavailable"
    if any(
        error.errno in {errno.EADDRINUSE, 10048}
        or getattr(error, "winerror", None) == 10048
        for error in errors
    ):
        return "port_in_use"
    if isinstance(exc, PortInUseError) and not errors:
        return "port_in_use"
    return "startup_failed"
