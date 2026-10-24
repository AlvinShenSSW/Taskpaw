"""Fixed, bounded workflow reads; no control requests, persistence or polling."""

from __future__ import annotations

import json
from http.client import HTTPException
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.hub.server.film_proxy import (
    _MAX_BODY as _MAX_JSON,
)
from taskpaw_v3.hub.server.film_proxy import (
    ERRORS as FILM_ERRORS,
)
from taskpaw_v3.hub.server.film_proxy import (
    FilmProxyError,
    _finite_float,
    _reject_constant,
)
from taskpaw_v3.hub.server.poller import _agent_base_url
from taskpaw_v3.workflow.board import _IMAGE_TYPES

WorkflowResource = Literal["films", "film", "plans", "poster", "cover"]
_MAX_IMAGE = 8 * 1024 * 1024
_opener = build_opener(NoRedirectHandler())
_ERRORS = {
    **FILM_ERRORS,
    "invalid_parameters": (400, "Invalid workflow request parameters."),
    "workflow_unavailable": (404, "Workflow or film unavailable on this agent."),
    "agent_timeout": (504, "Agent workflow request timed out."),
    "invalid_agent_response": (502, "Agent returned an invalid workflow response."),
}


class WorkflowProxyError(FilmProxyError):
    @classmethod
    def for_code(cls, code: str) -> WorkflowProxyError:
        status, detail = _ERRORS[code]
        return cls(status, code, detail)


def fetch_agent_workflow(
    server: dict,
    resource: WorkflowResource,
    headers: dict,
    *,
    film_id: str | None = None,
    timeout: float = 5.0,
    done_page: int = 1,
) -> tuple[bytes, str]:
    """Return validated JSON or an allowed image; never forward response headers."""
    if resource in ("films", "plans"):
        path = f"/workflow/{resource}"
        if resource == "films" and done_page != 1:
            path += f"?done_page={done_page}"
    elif resource in ("film", "poster", "cover"):
        if (
            not film_id
            or film_id in (".", "..")
            or any(c in film_id for c in ("/", "\\"))
            or any(not c.isprintable() for c in film_id)
        ):
            raise WorkflowProxyError.for_code("invalid_parameters")
        path = f"/workflow/films/{quote(film_id, safe='')}"
        if resource != "film":
            path += f"/{resource}"
    else:
        raise WorkflowProxyError.for_code("invalid_parameters")
    image = resource in ("poster", "cover")
    limit = _MAX_IMAGE if image else _MAX_JSON
    base = _agent_base_url(server["ip"], server["port"])
    request = Request(f"{base}{path}", headers=headers, method="GET")
    try:
        with _opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise WorkflowProxyError.for_code("agent_request_failed")
            media_type = "application/json"
            if image:
                media_type = response.headers.get_content_type()
                if media_type not in _IMAGE_TYPES.values():
                    raise WorkflowProxyError.for_code("invalid_agent_response")
            body = response.read(limit + 1)
        if len(body) > limit:
            raise WorkflowProxyError.for_code("invalid_agent_response")
        if not image:
            result = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_constant,
                parse_float=_finite_float,
            )
            if not isinstance(result, dict):
                raise WorkflowProxyError.for_code("invalid_agent_response")
            # Match the film proxy's Unicode/finite-number response boundary.
            json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return body, media_type
    except HTTPError as exc:
        code = (
            "workflow_unavailable"
            if exc.code == 404
            else "agent_auth_failed"
            if exc.code in (401, 403)
            else "agent_request_failed"
        )
        exc.close()
        raise WorkflowProxyError.for_code(code) from None
    except TimeoutError:
        raise WorkflowProxyError.for_code("agent_timeout") from None
    except URLError as exc:
        code = (
            "agent_timeout" if isinstance(exc.reason, TimeoutError) else "agent_offline"
        )
        raise WorkflowProxyError.for_code(code) from None
    except OSError:
        raise WorkflowProxyError.for_code("agent_offline") from None
    except (UnicodeError, ValueError, RecursionError):
        raise WorkflowProxyError.for_code("invalid_agent_response") from None
    except HTTPException:
        raise WorkflowProxyError.for_code("agent_request_failed") from None
