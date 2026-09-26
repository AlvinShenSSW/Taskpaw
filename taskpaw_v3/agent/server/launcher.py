"""Agent launcher: claim ports (no TOCTOU), run both servers, wire shutdown.

Used by both the interactive (Tauri-spawned, #5) and headless service (#service)
modes — the difference is who calls `run_agent()` and who sends the stop signal.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from taskpaw_v3 import __version__
from taskpaw_v3.core.auth import auth_disabled
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.datadir import set_data_dir
from taskpaw_v3.core.lifecycle import GracefulShutdown
from taskpaw_v3.core.llm import (
    llm_settings_from_config,
    set_llm_chain,
    set_llm_settings,
)
from taskpaw_v3.core.net import (  # re-export
    PortInUseError,
    announce_ready,
    claim_port,
    guard_bind_exposure,
    loopback_url,
    port_available,
    reclaim_ports_from_stale_instance,
)
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.state import load_next_id, save_next_id
from taskpaw_v3.core.tasklog import TaskLog, set_task_log
from taskpaw_v3.monitors.runtime import (
    effective_monitors,  # re-export (moved to runtime)
)
from taskpaw_v3.monitors.subs.translate import llm_chain_from_config

log = logging.getLogger("taskpaw.agent")

__all__ = [
    "run_agent",
    "PortInUseError",
    "claim_port",
    "port_available",
    "ensure_port_free",
    "effective_monitors",
]


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

    # Publish the global LLM settings (#178) BEFORE the stale-port reclaim, any
    # socket claim and the supervisor, so the first check() of any monitor reads
    # the real settings (env key first), never the unconfigured defaults (C2).
    # MonitorAdmin.update_config refreshes it after each successful save. Same
    # for the provider chain + failover switch (#192 AC1) and the data dir —
    # ONLY this run's config_path folder, never default_config_path() (C5).
    set_llm_settings(llm_settings_from_config(config))
    set_llm_chain(llm_chain_from_config(config), failover=config.llm_failover)
    set_data_dir(config_path.parent if config_path is not None else None)

    # Seamless updates/restarts: if OUR OWN previous agent backend is still holding
    # these ports (common right after installing a new version), terminate that
    # stale instance and reclaim BOTH ports. Only ever kills a positively-identified
    # TaskPaw agent backend, and only if BOTH the network AND control ports are free
    # or ours — a foreign service on either port aborts the whole reclaim so we never
    # kill the old agent when startup would fail here anyway (claim_port then fails
    # loudly as before).
    reclaim_ports_from_stale_instance(
        [
            (config.bind_host, config.bind_port, "agent network API"),
            (config.control_host, config.control_port, "agent control API"),
        ],
        role="agent",
    )

    # Race-free claim: hold the sockets, hand them to uvicorn.
    net_sock = claim_port(config.bind_host, config.bind_port, "agent network API")
    try:
        ctl_sock = claim_port(
            config.control_host, config.control_port, "agent control API"
        )
    except PortInUseError:
        net_sock.close()
        raise

    # #196 L19: no store scan/append until reclaim AND both claims succeeded.
    task_log = TaskLog(config_path.parent if config_path is not None else None)
    set_task_log(task_log)
    previous = task_log.reconcile()
    task_log.record(
        "",
        "agent.started",
        task_type="agent",
        data={"version": __version__, **previous},
    )

    # Auth-disabled visibility (#145): the guard above already refuses a
    # non-loopback bind with no token, so reaching here with auth off means a
    # loopback-only API. Warn loudly (only now the service is actually starting —
    # after the ports are claimed) so an operator knows /status and /events are
    # unauthenticated and can set a token before binding a LAN address.
    if auth_disabled(config.api_token):
        log.warning(
            "agent network API auth is DISABLED (no api_token set) — /status and "
            "/events accept any request. The bind guard keeps this loopback-only "
            "(%s); set an api_token to require a Bearer token or to bind a LAN "
            "address.",
            config.bind_host,
        )

    queue = queue if queue is not None else build_queue(config, state_path)

    def _log_failure(message: str) -> None:
        queue.add(
            monitor="tasklog",
            message=message,
            level="alert",
            title="Task log write failed",
        )

    task_log.set_on_first_failure(_log_failure)
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
        uvicorn.Config(
            create_network_app(config, queue, _status_provider), log_level="warning"
        )
    )
    ctl = uvicorn.Server(
        uvicorn.Config(
            create_control_app(
                config,
                on_command=admin.handle,
                status_provider=_status_provider,
                registry=registry,
                admin=admin,
                events_provider=queue.recent,
                films_provider=supervisor.film_page,
            ),
            log_level="warning",
        )
    )

    def _serve(server, sock, label):
        try:
            server.run(sockets=[sock])
        except Exception as e:  # a failed server must not hang run_agent forever
            log.error("Agent %s server crashed: %s", label, e)
            shutdown.shutdown()

    net_thread = threading.Thread(
        target=lambda: _serve(net, net_sock, "network"), name="agent-net", daemon=True
    )
    ctl_thread = threading.Thread(
        target=lambda: _serve(ctl, ctl_sock, "control"), name="agent-ctl", daemon=True
    )

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
    # Callbacks are LIFO: log before servers and supervisor stop (N3/W5).
    shutdown.register(
        "agent-tasklog",
        lambda: task_log.record("", "agent.stopping", task_type="agent"),
    )
    shutdown.install_signal_handlers()

    net_thread.start()
    ctl_thread.start()
    log.info(
        "Agent up: network %s:%s, control %s:%s",
        config.bind_host,
        config.bind_port,
        config.control_host,
        config.control_port,
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
