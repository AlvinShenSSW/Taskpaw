"""Bounded redirect refusal for authenticated Hub HTTP requests."""

from __future__ import annotations

from typing import Any, NoReturn
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request


class NoRedirectHandler(HTTPRedirectHandler):
    """Refuse before urllib parses Location or echoes upstream error text."""

    def http_error_302(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any
    ) -> NoReturn:
        try:
            # Never drain an untrusted redirect body or retain its response.
            fp.close()
        finally:
            # Keep the status for legacy fallback/film mappings. Even cleanup
            # failure must expose only this fixed reason, not upstream text.
            # HTTPError accepts absent headers at runtime; its stub requires Message.
            raise HTTPError("", code, "HTTP redirect refused", None, None) from None  # type: ignore[arg-type]

    # Inherited aliases retain the base implementation, and Python 3.10 has no
    # default 308 handler. Supply every supported redirect code explicitly.
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
