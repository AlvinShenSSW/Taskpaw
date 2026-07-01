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
from taskpaw_v3.core.net import (  # re-export
    PortInUseError,
    announce_ready,
    claim_port,
    guard_bind_exposure,
    loopback_url,
    port_available,
)
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.state import load_next_id, save_next_id
from taskpaw_v3.monitors.runtime import effective_monitors  # re-export (moved to runtime)

log = logging.getLogger("taskpaw.agent")

__all__ = ["run_agent", "PortInUseError", "claim_port", "port_available",
           "ensure_port_free", "effective_monitors"]


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
    config_path: Optional[Path] = None,
    block: bool = True,
) -> GracefulShutdown:
    """Claim ports, start the network + control servers, wire shutdown.

    Ports are bound up front via claim_port() (race-free) and handed to uvicorn,
    so a taken port fails with an actionable PortInUseError, never a generic
    bind error mid-startup.
    """
    import uvicorn

    from .app import create_control_app, create_network_app

    # Refuse an unsafe network exposure at startup too — not just from the UI guard
    # — so a hand-edited agent.yaml / bootstrap can't bind wildcard/public/non-
    # loopback-without-token unguarded (#114/Kimi). Raised BEFORE any socket claim.
    guard_bind_exposure(config.bind_host, config.api_token, label="agent network API")

    # Race-free claim: hold the sockets, hand them to uvicorn.
    net_sock = claim_port(config.bind_host, config.bind_port, "agent network API")
    try:
        ctl_sock = claim_port(config.control_host, config.control_port, "agent control API")
    except PortInUseError:
        net_sock.close()
        raise

    queue = queue or build_queue(config, state_path)
    shutdown = shutdown or GracefulShutdown()

    # Build + start the monitor supervisor from the effective monitor list
    # (config.monitors + a default host_metrics per §5b), wiring events into the
    # same queue the Hub polls.
    from taskpaw_v3.agent.server.admin import MonitorAdmin
    from taskpaw_v3.monitors.registry import default_registry
    from taskpaw_v3.monitors.runtime import build_supervisor, merge_status

    # One registry, shared by the supervisor AND /control/plugins, so the endpoint
    # advertises exactly what this agent runs (Kimi).
    registry = default_registry()
    monitors = effective_monitors(config)
    # Always build the supervisor — even with no monitors — so the control API can
    # add the FIRST monitor live (#57). build_supervisor validates each spec and
    # skips ones marked enabled:false.
    supervisor = build_supervisor(registry, monitors, queue, config.machine)
    supervisor.start()
    shutdown.register("supervisor", lambda: supervisor.stop())

    # Live add/remove/update/enable/disable, persisted to config_path (#57).
    admin = MonitorAdmin(config, supervisor, registry, config_path)

    def _status_provider() -> dict:
        import platform
        # snapshot (running) + configured-but-disabled stubs, so the console
        # can list + re-enable stopped monitors (#57).
        monitors = merge_status(config, supervisor.snapshot())
        # Additively stamp each monitor with the time of its most recent local
        # event (#130) so the console's pill selector can show per-monitor
        # freshness without a second round-trip. Only monitors present in the
        # status get stamped; a monitor with no events keeps no key.
        last_seen = queue.last_event_times()
        for name, entry in monitors.items():
            if isinstance(entry, dict) and name in last_seen:
                entry["last_event_at"] = last_seen[name]
        return {
            "machine": config.machine,
            "server_id": config.server_id,
            "os": platform.platform(),
            "monitors": monitors,
        }

    net = uvicorn.Server(
        uvicorn.Config(create_network_app(config, queue, _status_provider), log_level="warning")
    )
    ctl = uvicorn.Server(
        uvicorn.Config(create_control_app(config, on_command=admin.handle,
                                          status_provider=_status_provider,
                                          registry=registry, admin=admin,
                                          events_provider=queue.recent),
                       log_level="warning")
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
    # Readiness handshake (design §3.1, #48): ONE machine-readable line on stdout
    # once the sockets are bound + servers started — the Tauri shell reads it
    # before loading the webview and injects this base_url (so a custom
    # control_port works and the UI never races the backend). All other logs go
    # to stderr (logging.basicConfig). The UI talks to the loopback CONTROL API on
    # its CONFIGURED host (so an IPv6 `::1` control_host is announced correctly).
    announce_ready("agent", loopback_url(config.control_host, config.control_port))

    if block:
        shutdown.stopped.wait()
    return shutdown
