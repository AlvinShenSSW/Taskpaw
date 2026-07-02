"""Bearer-token auth shared by the agent's network API.

V2 parity: an empty configured token means auth is disabled (default). When set,
the client must send `Authorization: Bearer <token>`. Comparison is
constant-time. Callers must ensure a failed check never drains the event queue.
"""

from __future__ import annotations

import hmac
from typing import Optional


def token_ok(configured_token: str, auth_header: Optional[str]) -> bool:
    """Return True if the request is authorized.

    Empty/whitespace configured token → auth disabled → always True.
    """
    token = (configured_token or "").strip()
    if not token:
        return True
    expected = f"Bearer {token}"
    return hmac.compare_digest(expected, auth_header or "")


def auth_disabled(configured_token: str) -> bool:
    """True when no token is configured, i.e. `token_ok` will accept any request
    (V2 parity). Used to warn loudly at startup and to drive a UI banner (#145) —
    the network bind guard (`core.net.guard_bind_exposure`) still refuses a
    non-loopback bind in this state, so a running auth-disabled API is loopback
    only. Intentionally the exact negation of the `token_ok` short-circuit, so the
    two can never disagree about what "auth is off" means.
    """
    return not (configured_token or "").strip()
