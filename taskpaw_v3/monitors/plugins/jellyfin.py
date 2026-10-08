"""`jellyfin` monitor — is a Jellyfin media server up and healthy? (#257)

Probes two unauthenticated Jellyfin endpoints over HTTP:
- `/health` — healthy only when it answers 200 with the body `Healthy`.
- `/System/Info/Public` — confirms the peer really is Jellyfin (`ProductName`)
  and supplies its version / server name.

`tcp_check` can only say a port is open; this says whether what is listening is
a healthy Jellyfin. Passive: it never starts or stops the server, and never
sends credentials. Redirects are refused and environment proxies are ignored —
the monitor always talks to the configured host directly.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)

# urlopen failures: URLError/timeout (OSError), a bad URL (ValueError), and
# InvalidURL / a malformed response (HTTPException, NOT an OSError) — all must
# become a clean "no answer", never a raised check error.
_NET_ERRORS = (OSError, ValueError, http.client.HTTPException)
_MAX_BODY = 64 * 1024
_HEALTHY_BODY = "Healthy"


class JellyfinConfig(BaseMonitorConfig):
    base_url: str = Field(
        "http://127.0.0.1:8096",
        title="Base URL",
        description="Jellyfin address, e.g. http://127.0.0.1:8096 "
        "(http/https only; no credentials, query or fragment).",
    )
    # Request deadline = the shared BaseMonitorConfig.timeout (one knob, not two).

    @field_validator("base_url")
    @classmethod
    def _base_url_valid(cls, v: str) -> str:
        # urlsplit tolerates leading whitespace and strips newlines, so reject
        # them up front instead of silently probing a rewritten URL.
        if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("base_url must not contain whitespace")
        try:
            parts = urlsplit(v)
            parts.port  # noqa: B018 — parsing the port validates it
        except ValueError as e:
            raise ValueError("base_url is not a valid URL") from e
        if parts.scheme not in ("http", "https"):
            raise ValueError("base_url must start with http:// or https://")
        if not parts.hostname:
            raise ValueError("base_url must include a host")
        if "@" in parts.netloc:
            raise ValueError("base_url must not contain credentials")
        if "?" in v or "#" in v:
            raise ValueError("base_url must not contain a query or fragment")
        return v.rstrip("/")


@dataclass(frozen=True)
class _Reply:
    """One HTTP answer (any status), body capped at `_MAX_BODY`."""

    status: int
    body: bytes


@dataclass(frozen=True)
class JellyfinProbe:
    """Raw result of one probe. `None` = no HTTP answer (transport failure)."""

    health: Optional[_Reply]
    info: Optional[_Reply]
    response_ms: Optional[float]


def _build_opener() -> urllib.request.OpenerDirector:
    # The empty ProxyHandler suppresses the default one, which would send a
    # loopback probe to http_proxy/https_proxy from the environment.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirectHandler()
    )


_OPENER = _build_opener()


def _read_capped(resp: Any, deadline: float) -> bytes:
    """Read up to `_MAX_BODY` bytes, giving up at `deadline`.

    The socket timeout is per operation, so a peer dripping one byte at a time
    would keep a plain read() alive far beyond it; read1() returns after each
    socket read so the deadline is checked between them."""
    chunks: list[bytes] = []
    size = 0
    while size < _MAX_BODY:
        if time.monotonic() >= deadline:
            raise TimeoutError("response body deadline exceeded")
        chunk = resp.read1(_MAX_BODY - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _fetch(
    url: str, timeout: float, opener: Optional[urllib.request.OpenerDirector] = None
) -> Optional[_Reply]:
    """GET `url`; any HTTP answer (incl. 4xx/5xx and a refused redirect) is a
    `_Reply`, a transport failure or an overrun body deadline is None."""
    deadline = time.monotonic() + timeout
    try:
        try:
            resp = (opener or _OPENER).open(url, timeout=timeout)
        except urllib.error.HTTPError as e:
            # A refused redirect carries no body (and no file object to read).
            if 300 <= e.code < 400:
                return _Reply(e.code, b"")
            with e:
                return _Reply(e.code, _read_capped(e, deadline))
        with resp:
            return _Reply(resp.status, _read_capped(resp, deadline))
    except _NET_ERRORS:
        return None


def probe(
    base_url: str,
    timeout: float,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> JellyfinProbe:
    """One observation: `/health`, then (only if that answered) the public info."""
    started = time.monotonic()
    health = _fetch(f"{base_url}/health", timeout, opener)
    response_ms = round((time.monotonic() - started) * 1000, 1)
    if health is None:
        return JellyfinProbe(None, None, None)
    info = _fetch(f"{base_url}/System/Info/Public", timeout, opener)
    return JellyfinProbe(health, info, response_ms)


def _cap(s: str, n: int = 80) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace").strip()


def _info_object(reply: _Reply) -> Optional[dict]:
    """The public-info JSON object if `reply` identifies a Jellyfin server."""
    if reply.status != 200:
        return None
    try:
        data = json.loads(reply.body.decode("utf-8"))
    # ValueError: UnicodeDecodeError / JSONDecodeError; RecursionError: nesting
    # too deep for the decoder (it is a RuntimeError, not a ValueError).
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    product = data.get("ProductName")
    if not isinstance(product, str) or "jellyfin" not in product.lower():
        return None
    return data


def _str_field(info: dict, key: str) -> str:
    value = info.get(key)
    return _cap(value) if isinstance(value, str) else ""


def evaluate(result: JellyfinProbe) -> MonitorStatus:
    """Map a probe to a status (the #257 design's state decision table)."""
    health = result.health
    metrics: dict[str, Any] = {
        "reachable": health is not None,
        "healthy": False,
        "health_status": health.status if health else None,
        "version": "",
        "server_name": "",
        "response_ms": result.response_ms,
    }
    if health is None:  # row 1
        return MonitorStatus(state="error", detail="unreachable", metrics=metrics)

    health_text = _text(health.body)
    healthy = health.status == 200 and health_text == _HEALTHY_BODY
    metrics["healthy"] = healthy
    health_summary = _cap(f"{health.status} {health_text}".strip())

    info_reply = result.info
    if info_reply is None or info_reply.status >= 500:  # row 6
        return MonitorStatus(
            state="degraded",
            detail=f"server info unavailable (health {health_summary})",
            metrics=metrics,
        )
    info = _info_object(info_reply)
    if info is None:  # row 5
        return MonitorStatus(
            state="error",
            detail=f"not a Jellyfin server (info HTTP {info_reply.status})",
            metrics=metrics,
        )

    version = _str_field(info, "Version")
    server_name = _str_field(info, "ServerName")
    metrics["version"] = version
    metrics["server_name"] = server_name
    if not healthy:  # row 4 (before row 3: unhealthy outranks the wizard)
        return MonitorStatus(
            state="degraded", detail=f"unhealthy: {health_summary}", metrics=metrics
        )
    # Only an explicit false degrades; an older server may lack the field.
    if info.get("StartupWizardCompleted") is False:  # row 3
        return MonitorStatus(
            state="degraded", detail="setup wizard not completed", metrics=metrics
        )
    label = " ".join(p for p in (server_name, version) if p)  # row 2
    return MonitorStatus(
        state="ok", detail=f"healthy — {label}" if label else "healthy", metrics=metrics
    )


class JellyfinInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: JellyfinConfig) -> None:
        super().__init__(instance_id, config)
        self._prev_state: Optional[str] = None

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: JellyfinConfig = self.config  # type: ignore[assignment]
        status = evaluate(probe(cfg.base_url, cfg.timeout))
        prev, state = self._prev_state, status.state
        message = f"{cfg.base_url}: {status.detail}"
        # One event per transition. No dedupe_key: the supervisor drops every
        # later event with a seen key, which would swallow a second outage.
        if state != prev:
            # Alert/warn on a bad state at startup too, not only on a change.
            if state == "error":
                emit("alert", f"{cfg.name} down", message)
            elif state == "degraded":
                emit("warn", f"{cfg.name} degraded", message)
            elif prev is not None:
                emit("done", f"{cfg.name} healthy", message)
        self._prev_state = state
        return status


class JellyfinPlugin(MonitorPlugin):
    type_id = "jellyfin"
    display_name = "Jellyfin"
    category = "service"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return JellyfinConfig

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return JellyfinInstance(instance_id, config)  # type: ignore[arg-type]
