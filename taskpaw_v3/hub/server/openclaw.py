"""OpenClaw sink: POST a rendered payload to the OpenClaw wake hook.

urllib (no extra dep), with a private no-redirect opener that tests can replace.
Token in the header only — never argv, never logs.
"""

from __future__ import annotations

import json
import urllib.request

from taskpaw_v3.core.http import NoRedirectHandler

_opener = urllib.request.build_opener(NoRedirectHandler())


def send_payload(url: str, token: str, payload: dict, timeout: float = 5.0) -> None:
    """POST {"text": ...}-style payload. Raises on failure (caller handles retry)."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with _opener.open(req, timeout=timeout) as resp:
        resp.read()  # drain + close the socket (avoid FD leak)
