"""Agent launcher: port-in-use guard, run both servers, wire graceful shutdown.

Used by both the interactive (Tauri-spawned, #5) and headless service (#service)
modes — the difference is who calls `run_agent()` and who sends the stop signal.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import AgentConfig
from core.lifecycle import GracefulShutdown
from core.protocol import EventQueue

log = logging.getLogger("taskpaw.agent")


class PortInUseError(RuntimeError):
    pass


def port_available(host: str, port: int) -> bool:
    """True if (host, port) can be bound right now."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def ensure_port_free(host: str, port: int, what: str) -> None:
    """Refuse to start on a taken port with an actionable message."""
    if not port_available(host, port):
        raise PortInUseError(
            f"{what} port {host}:{port} is already in use. Another TaskPaw "
            f"instance, a V2 agent (default 5678), or another service may hold "
            f"it. Stop it or change the port in agent.yaml before starting."
        )


def run_agent(
    config: AgentConfig,
    queue: EventQueue | None = None,
    shutdown: GracefulShutdown | None = None,
    block: bool = True,
) -> GracefulShutdown:
    """Validate ports, start the network + control servers, wire shutdown.

    Returns the GracefulShutdown handle. When `block` is True, waits until
    shutdown; otherwise returns immediately (servers run in background threads).
    """
    import uvicorn  # local import so importing this module stays light/testable

    from .app import create_control_app, create_network_app

    ensure_port_free(config.bind_host, config.bind_port, "agent network API")
    ensure_port_free(config.control_host, config.control_port, "agent control API")

    queue = queue or EventQueue(machine=config.machine)
    shutdown = shutdown or GracefulShutdown()

    net = uvicorn.Server(
        uvicorn.Config(
            create_network_app(config, queue),
            host=config.bind_host,
            port=config.bind_port,
            log_level="warning",
        )
    )
    ctl = uvicorn.Server(
        uvicorn.Config(
            create_control_app(config),
            host=config.control_host,
            port=config.control_port,
            log_level="warning",
        )
    )

    net_thread = threading.Thread(target=net.run, name="agent-net", daemon=True)
    ctl_thread = threading.Thread(target=ctl.run, name="agent-ctl", daemon=True)

    def _stop_servers() -> None:
        net.should_exit = True
        ctl.should_exit = True
        for t in (net_thread, ctl_thread):
            t.join(timeout=5)

    shutdown.register("agent-servers", _stop_servers)
    shutdown.install_signal_handlers()

    net_thread.start()
    ctl_thread.start()
    log.info(
        "Agent up: network %s:%s, control %s:%s",
        config.bind_host, config.bind_port, config.control_host, config.control_port,
    )

    if block:
        shutdown.stopped.wait()
    return shutdown
