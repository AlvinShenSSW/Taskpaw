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
from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from taskpaw_v3.agent.server.admin import MonitorAdmin


from taskpaw_v3.core.auth import token_ok
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.monitors.runtime import effective_monitors, monitor_name


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
            # Match the production status_provider shape: a dict keyed by monitor
            # name (supervisor.snapshot()), NOT a list — same endpoint, one wire
            # shape (Kimi). effective_monitors so it includes auto-injected ones.
            "monitors": {
                monitor_name(m): {"type_id": m.get("type_id"), "state": "unknown"}
                for m in effective_monitors(config)
            },
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
    status_provider: Optional[Callable[[], dict]] = None,
    registry: Optional[PluginRegistry] = None,
    admin: Optional["MonitorAdmin"] = None,
    events_provider: Optional[Callable[..., list[dict]]] = None,
) -> FastAPI:
    """Loopback-only control API for the local UI (agent console). CORS is opened
    for the desktop UI origins here (NOT on the network API)."""
    from fastapi import HTTPException

    from taskpaw_v3.core.cors import add_ui_cors

    app = FastAPI(title="TaskPaw Agent Control", docs_url=None, redoc_url=None)
    add_ui_cors(app)

    @app.get("/control/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.get("/control/status")
    def control_status() -> dict:
        # The agent console reads status from the loopback control API.
        if status_provider is not None:
            return status_provider()
        return {"machine": config.machine, "server_id": config.server_id, "monitors": {}}

    @app.get("/control/config")
    def get_config() -> dict:
        # Show DESIRED (pending) editable scalars over the running config when an
        # admin is wired, so the Settings form reflects unsaved-since-restart edits
        # (#43); else the plain running config. Mask the secret either way (§4.3).
        data: dict[str, Any] = admin.config_view() if admin is not None else config.model_dump()
        if data.get("api_token"):
            data["api_token"] = "***"
        return data

    @app.get("/control/events")
    def control_events(limit: int = 200, monitor: Optional[str] = None) -> dict:
        # Recent LOCAL events for the console's event log (#44) — a non-destructive
        # read (never drains the queue the Hub polls). Clamp the limit so a bad
        # query can't ask for an unbounded copy. An optional `monitor` filters to
        # one monitor's events for the console's per-monitor inline panel (#130).
        limit = max(1, min(int(limit), 1000))
        if events_provider is None:
            return {"events": []}
        events = events_provider(limit, monitor) if monitor else events_provider(limit)
        return {"events": events}

    @app.get("/control/plugins")
    def plugins() -> dict:
        # The selectable monitor services + their form schemas (the UI's "enable
        # a monitor" picker) plus named presets (moomoo is just one of these).
        from taskpaw_v3.agent.catalog import plugin_catalog, preset_catalog

        # Use the injected registry so the endpoint advertises exactly what this
        # agent can run (a custom runtime registry won't diverge) (Kimi).
        return {"plugins": plugin_catalog(registry), "presets": preset_catalog()}

    @app.post("/control/command")
    def command(body: dict) -> dict:
        if on_command is None:
            return {"ok": False, "error": "no command handler"}
        name = str(body.get("command", ""))
        return on_command(name, body)

    # ── monitor CRUD (#57): add/remove/update/enable/disable, persisted +
    # live-applied. Loopback-only (this whole app binds control_host). Mounted
    # only when an admin is wired (the headless/interactive launcher provides it).
    if admin is not None:
        def _guard(fn, *a):
            try:
                return fn(*a)
            except ValueError as e:           # unknown type / dup / invalid config
                raise HTTPException(status_code=400, detail=str(e))
            except KeyError as e:             # not registered in the supervisor
                raise HTTPException(status_code=404, detail=str(e))

        # The monitor `name` is a free-form id (V2 parity) that may contain '/',
        # which a path param can't address even URL-encoded — so the mutation
        # routes take `name` as a QUERY param, keeping every valid monitor
        # manageable (Codex #57a).
        @app.post("/control/monitors")
        def add_monitor(body: dict):
            # body = {type_id, name?, config, enabled?}
            return _guard(admin.add, body)

        @app.delete("/control/monitors")
        def remove_monitor(name: str):
            return _guard(admin.remove, name)

        @app.patch("/control/monitors")
        def update_monitor(name: str, body: dict):
            # {config?: {...}, enabled?: bool}. Apply CONFIG first: it validates,
            # so an invalid config fails (400) BEFORE enabled is touched/persisted
            # — a failed combined edit must not leave the monitor started/stopped
            # (Codex #57a). A valid config persists, then enabled (which can't fail
            # once the monitor is found).
            if "config" not in body and "enabled" not in body:
                raise HTTPException(status_code=400,
                                    detail="patch needs 'config' and/or 'enabled'")
            out: dict = {"ok": True, "name": name}
            if "config" in body:
                out = _guard(admin.update, name, body["config"])
            if "enabled" in body:
                # pass through raw — admin.set_enabled requires a real boolean
                # (rejects "false"/0 strings) → 400, not a silent enable.
                out = _guard(admin.set_enabled, name, body["enabled"])
            return out

        # Start/Stop are persisted enable/disable (V2 parity: a stopped monitor
        # stays stopped across restarts).
        @app.post("/control/monitors/start")
        def start_monitor(name: str):
            return _guard(admin.set_enabled, name, True)

        @app.post("/control/monitors/stop")
        def stop_monitor(name: str):
            return _guard(admin.set_enabled, name, False)

        @app.patch("/control/config")
        def update_config(body: dict):
            # Edit agent config (machine/ports/token) from the Settings UI (#43)
            # instead of hand-editing agent.yaml. Validated + persisted atomically;
            # returns {ok, restart_required} — port/host changes apply on restart.
            return _guard(admin.update_config, body)

    return app
