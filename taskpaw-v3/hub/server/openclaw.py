"""OpenClaw sink: POST a rendered payload to the OpenClaw wake hook.

urllib (no extra dep) so tests can monkeypatch `urllib.request.urlopen` like the
V2 suite. Token in the header only — never argv, never logs.
"""

from __future__ import annotations

import json
import urllib.request


def send_payload(url: str, token: str, payload: dict, timeout: float = 5.0) -> None:
    """POST {"text": ...}-style payload. Raises on failure (caller handles retry)."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=timeout)
