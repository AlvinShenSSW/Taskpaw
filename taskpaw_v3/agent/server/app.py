"""V3 agent FastAPI apps.

Two surfaces, two ports (design §3.2):
- **network app** (`/ping /status /events`): Bearer-gated read API the Hub polls;
  bound to the LAN nic. This is the ONLY thing exposed off-box.
- **control app** (`/control/*`): start/stop, config — bound to loopback only,
  for the local UI client. Never exposed to the network.

#15 keeps `/status` static (machine info + configured monitor stubs); real
monitors arrive with the plugin supervisor in #17.
"""

from __future__ import annotations

import platform
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


from taskpaw_v3.core.auth import token_ok
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.protocol import EventQueue


def _unauthorized() -> JSONResponse:
    # 401 must NOT touch the event queue (V2 parity).
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="TaskPaw"'},
    )


def create_network_app(
    config: AgentConfig,
    queue: EventQueue,
    status_provider: Optional[Callable[[], dict]] = None,
) -> FastAPI:
    app = FastAPI(title="TaskPaw Agent", docs_url=None, redoc_url=None)

    def _auth(request: Request) -> bool:
        return token_ok(config.api_token, request.headers.get("Authorization"))

    @app.get("/ping")
    def ping() -> dict:
        # Open by design — trivial reachability probe, no sensitive data.
        return {"ok": True, "machine": config.machine, "version": "3.0.0-dev"}

    @app.get("/status")
    def status(request: Request):
        if not _auth(request):
            return _unauthorized()
        if status_provider is not None:
            return status_provider()
        return {
            "machine": config.machine,
            "server_id": config.server_id,
            "os": platform.platform(),
            "monitors": [
                {"type_id": m.get("type_id"), "name": m.get("name"), "state": "unknown"}
                for m in config.monitors
            ],
        }

    @app.get("/events")
    def events(request: Request, ack: Optional[int] = None):
        if not _auth(request):
            return _unauthorized()
        return queue.payload(ack_id=ack)

    return app


def create_control_app(
    config: AgentConfig,
    on_command: Optional[Callable[[str, dict], dict]] = None,
) -> FastAPI:
    """Loopback-only control API. #15 ships a minimal surface; #17/#5 extend it."""
    app = FastAPI(title="TaskPaw Agent Control", docs_url=None, redoc_url=None)

    @app.get("/control/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.get("/control/config")
    def get_config() -> dict:
        # Mask the secret — never echo the real token (design §4.3).
        data: dict[str, Any] = config.model_dump()
        if data.get("api_token"):
            data["api_token"] = "***"
        return data

    @app.post("/control/command")
    def command(body: dict) -> dict:
        if on_command is None:
            return {"ok": False, "error": "no command handler"}
        name = str(body.get("command", ""))
        return on_command(name, body)

    return app
