"""OpenClaw sink: POST a rendered payload to the OpenClaw wake hook.

urllib (no extra dep), with a private no-redirect opener that tests can replace.
Token in the header only — never argv, never logs.
"""

from __future__ import annotations

import json
import urllib.request

from taskpaw_v3.core.http import NoRedirectHandler

from .upstream_worker import OPENCLAW_BYTES, UpstreamError, _Handler, _HTTPSHandler

_opener = urllib.request.build_opener(NoRedirectHandler(), _Handler(), _HTTPSHandler())


def send_payload(url: str, token: str, payload: dict, timeout: float = 5.0) -> None:
    """Bounded response for direct tests/settings; worker owns delivery deadline.

    ``timeout`` here is urllib's socket timeout. The production DeliveryWorker
    uses an owned helper process to also bound DNS and slow response streams.
    """
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                raise ValueError("http_refused")
            if resp.headers.get("Content-Encoding", "identity").lower() not in (
                "identity",
                "",
            ):
                raise ValueError("http_encoding")
            length = resp.headers.get("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > OPENCLAW_BYTES
            ):
                raise ValueError("body_oversize")
            size = 0
            while True:
                part = resp.read1(min(16384, OPENCLAW_BYTES + 1 - size))
                if not part:
                    break
                size += len(part)
                if size > OPENCLAW_BYTES:
                    raise ValueError("body_oversize")
            if length is not None and size != int(length):
                raise ValueError("http_incomplete")
    except UpstreamError as exc:
        raise ValueError(exc.reason) from None
