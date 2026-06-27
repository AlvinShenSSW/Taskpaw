"""Agent launcher: claim ports (no TOCTOU), run both servers, wire shutdown.

Used by both the interactive (Tauri-spawned, #5) and headless service (#service)
modes — the difference is who calls `run_agent()` and who sends the stop signal.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.lifecycle import GracefulShutdown
from taskpaw_v3.core.net import PortInUseError, claim_port, port_available  # re-export
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.state import load_next_id, save_next_id

log = logging.getLogger("taskpaw.agent")

__all__ = ["run_agent", "PortInUseError", "claim_port", "port_available", "ensure_port_free"]


def ensure_port_free(host: str, port: int, what: str) -> None:
    """Advisory pre-check (claim_port does the real, race-free bind)."""
    if not port_available(host, port):
        raise PortInUseError(f"{what} port {host}:{port} is already in use.")


def build_queue(config: AgentConfig, state_path: Optional[Path]) -> EventQueue:
    """EventQueue with persisted monotonic id (constitution §3)."""
    if state_path is None:
        return EventQueue(machine=config.machine)
    return EventQueue(
        machine=config.machine,
        start_id=load_next_id(state_path),
        persist_counter=lambda n: save_next_id(state_path, n),
        on_overflow=lambda dropped: log.error(
            "Agent event queue overflow (Hub not acking?); dropped %d oldest", dropped
        ),
    )


def run_agent(
    config: AgentConfig,
    queue: EventQueue | None = None,
    shutdown: GracefulShutdown | None = None,
    state_path: Optional[Path] = None,
    block: bool = True,
) -> GracefulShutdown:
    """Claim ports, start the network + control servers, wire shutdown.

    Ports are bound up front via claim_port() (race-free) and handed to uvicorn,
    so a taken port fails with an actionable PortInUseError, never a generic
    bind error mid-startup.
    """
    import uvicorn

    from .app import create_control_app, create_network_app

    # Race-free claim: hold the sockets, hand them to uvicorn.
    net_sock = claim_port(config.bind_host, config.bind_port, "agent network API")
    try:
        ctl_sock = claim_port(config.control_host, config.control_port, "agent control API")
    except PortInUseError:
        net_sock.close()
        raise

    queue = queue or build_queue(config, state_path)
    shutdown = shutdown or GracefulShutdown()

    net = uvicorn.Server(
        uvicorn.Config(create_network_app(config, queue), log_level="warning")
    )
    ctl = uvicorn.Server(
        uvicorn.Config(create_control_app(config), log_level="warning")
    )

    def _serve(server, sock, label):
        try:
            server.run(sockets=[sock])
        except Exception as e:  # a failed server must not hang run_agent forever
            log.error("Agent %s server crashed: %s", label, e)
            shutdown.shutdown()

    net_thread = threading.Thread(target=lambda: _serve(net, net_sock, "network"), name="agent-net", daemon=True)
    ctl_thread = threading.Thread(target=lambda: _serve(ctl, ctl_sock, "control"), name="agent-ctl", daemon=True)

    def _stop_servers() -> None:
        net.should_exit = True
        ctl.should_exit = True
        for t in (net_thread, ctl_thread):
            t.join(timeout=10)
        for s in (net_sock, ctl_sock):
            try:
                s.close()
            except OSError:
                pass

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
