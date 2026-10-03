"""Bounded, read-only film requests. No polling, persistence or credential logging."""

from __future__ import annotations

import json
import math
from http.client import HTTPException
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.hub.server.poller import _agent_base_url

FilmResource = Literal["films", "run-films"]
ERRORS = {
    "invalid_parameters": (400, "Invalid film list parameters."),
    "unknown_server": (404, "Agent is not registered."),
    "agent_disabled": (409, "Agent is disabled."),
    "agent_offline": (503, "Agent is offline or unreachable."),
    "film_list_unavailable": (
        404,
        "Film list unavailable; the agent may need an update or the task may have no list.",
    ),
    "agent_auth_failed": (502, "Agent authentication failed; check the polling token."),
    "agent_timeout": (504, "Agent film request timed out."),
    "invalid_agent_response": (502, "Agent returned an invalid film list response."),
    "agent_request_failed": (502, "Agent film request failed."),
}


class FilmProxyError(Exception):
    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail

    @classmethod
    def for_code(cls, code: str) -> FilmProxyError:
        status, detail = ERRORS[code]
        return cls(status, code, detail)


_opener = build_opener(NoRedirectHandler())
_MAX_BODY = 1024 * 1024


def _reject_constant(value: str):
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def fetch_agent_film_page(
    server: dict,
    resource: FilmResource,
    params: dict,
    headers: dict,
    timeout: float = 5.0,
) -> dict:
    base = _agent_base_url(server["ip"], server["port"])
    allowed = {"name", "page", "size"}
    if resource == "run-films":
        allowed.add("filter")
    query = urlencode(
        {k: v for k, v in params.items() if k in allowed and v is not None}
    )
    request = Request(
        f"{base}/monitors/{resource}?{query}", headers=headers, method="GET"
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise FilmProxyError.for_code("agent_request_failed")
            body = response.read(_MAX_BODY + 1)
        if len(body) > _MAX_BODY:
            raise FilmProxyError.for_code("invalid_agent_response")
        result = json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        if not isinstance(result, dict):
            raise FilmProxyError.for_code("invalid_agent_response")
        # JSON permits escaped lone surrogates, but our UTF-8 HTTP response
        # cannot encode them. Validate at this sanitized upstream boundary.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return result
    except HTTPError as exc:
        code = (
            "film_list_unavailable"
            if exc.code == 404
            else "agent_auth_failed"
            if exc.code in (401, 403)
            else "agent_request_failed"
        )
        exc.close()
        raise FilmProxyError.for_code(code) from None
    except TimeoutError:
        raise FilmProxyError.for_code("agent_timeout") from None
    except URLError as exc:
        code = (
            "agent_timeout" if isinstance(exc.reason, TimeoutError) else "agent_offline"
        )
        raise FilmProxyError.for_code(code) from None
    except OSError:
        raise FilmProxyError.for_code("agent_offline") from None
    except (UnicodeError, ValueError, RecursionError):
        raise FilmProxyError.for_code("invalid_agent_response") from None
    except HTTPException:
        raise FilmProxyError.for_code("agent_request_failed") from None
