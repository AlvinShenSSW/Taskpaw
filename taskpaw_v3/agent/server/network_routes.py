"""#299 R4–R6: 网络与访问 on the loopback control API.

`GET /control/network` shows the saved switch, address and ports, this
machine's LAN addresses, a self-healed address change, whether a token is set
(and its last four characters when it is long enough) and the firewall rule.
`POST /control/network/token` generates a new token (not saved — the step's
save writes it through `update_config`) or reveals the saved one; both answer
`Cache-Control: no-store` and are the only responses that carry a token.
`POST /control/network/firewall` asks Windows to add the inbound rule.
"""

from __future__ import annotations

import logging
import secrets
import string
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from taskpaw_v3.core import lan
from taskpaw_v3.core.config import AgentConfig, save_yaml

if TYPE_CHECKING:
    from taskpaw_v3.agent.server.admin import MonitorAdmin

log = logging.getLogger("taskpaw.agent.network")

TOKEN_PREFIX = "tp_"
TOKEN_LENGTH = 32
_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
NO_STORE = {"Cache-Control": "no-store"}
HINT_MIN = 16


def new_token() -> str:
    """`tp_` + 32 base62 characters from the OS CSPRNG."""
    return TOKEN_PREFIX + "".join(secrets.choice(_BASE62) for _ in range(TOKEN_LENGTH))


def token_hint(token: str) -> Optional[str]:
    """The last four characters, only for a token long enough to keep secret."""
    token = (token or "").strip()
    return token[-4:] if len(token) >= HINT_MIN else None


def firewall_ports(bind_port: int, workflow: Any) -> list[int]:
    """R6: the network port, plus the worker API port while GPU workers may
    connect to this 主控."""
    ports = [int(bind_port)]
    if (
        workflow.enabled
        and workflow.role == "controller"
        and workflow.remote_workers
        and int(workflow.worker_api_port) not in ports
    ):
        ports.append(int(workflow.worker_api_port))
    return ports


def heal_on_startup(config: AgentConfig, config_path: Optional[Path]) -> Optional[str]:
    """R5: a LAN `bind_host` this machine no longer has moves to the default
    route's LAN address before any socket is claimed; saved with a backup.
    The old address is returned for the 网络与访问 notice. Review IR1: with
    no routed LAN address yet (Wi-Fi / DHCP not up, only a virtual or VPN
    adapter) nothing moves and nothing is saved — the saved address is kept
    for this run (its bind then fails with the startup dialog, and the next
    start heals once the network is up)."""
    if config_path is None:
        return None
    new = lan.healed_bind_host(config.lan_access, config.bind_host)
    if new is None:
        if lan.stale_bind(config.lan_access, config.bind_host):
            log.warning(
                "the saved LAN address is not on this machine and no LAN "
                "address has a default route yet; it is kept, not saved"
            )
        return None
    old = config.bind_host
    config.bind_host = new
    try:
        save_yaml(config, config_path, backup=True)
    except OSError:
        log.error("could not save the changed LAN address; it applies this run only")
    log.warning("the LAN address changed; the network API binds the new primary one")
    return old


def register_network_routes(
    app: FastAPI,
    admin: Optional["MonitorAdmin"],
    *,
    address_changed_from: Optional[str] = None,
) -> None:
    from taskpaw_v3.agent import firewall

    def unavailable() -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "设置不可用"})

    def ports() -> list[int]:
        assert admin is not None
        return firewall_ports(admin.desired("bind_port"), admin.desired_workflow())

    @app.get("/control/network")
    def network_view() -> Any:
        if admin is None:
            return unavailable()
        token = str(admin.desired("api_token") or "")
        return {
            "lan_access": bool(admin.desired("lan_access")),
            "bind_host": admin.desired("bind_host"),
            "bind_port": admin.desired("bind_port"),
            "control_port": admin.desired("control_port"),
            "addresses": [a.view() for a in lan.local_addresses()],
            "address_changed_from": address_changed_from,
            "running_bind_host": admin.running().bind_host,
            "token_set": bool(token.strip()),
            "api_token_hint": token_hint(token),
            "firewall": firewall.status(
                ports(), bind_host=str(admin.desired("bind_host") or "")
            ),
        }

    @app.post("/control/network/token")
    async def network_token(request: Request) -> Any:
        if admin is None:
            return unavailable()
        try:
            body = await request.json()
        except ValueError:
            body = None
        action = body.get("action") if isinstance(body, dict) else None
        if action == "generate":
            token = new_token()
        elif action == "reveal":
            token = str(admin.desired("api_token") or "")
        else:
            return JSONResponse(
                status_code=422,
                content={"detail": "操作不正确", "fields": {"action": "操作不正确"}},
            )
        return JSONResponse({"token": token}, headers=NO_STORE)

    @app.post("/control/network/firewall")
    def network_firewall() -> Any:
        if admin is None:
            return unavailable()
        result = firewall.request_allow(ports())
        log.info("firewall rule request: %s", result)
        return {"result": result}
