"""Agent launcher: claim ports (no TOCTOU), run both servers, wire shutdown.

Used by both the interactive (Tauri-spawned, #5) and headless service (#service)
modes — the difference is who calls `run_agent()` and who sends the stop signal.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from taskpaw_v3 import __version__
from taskpaw_v3.core import power, power_fleet
from taskpaw_v3.core.auth import auth_disabled
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.control import bootstrap_control, revoke_control, strip_control_env
from taskpaw_v3.core.datadir import set_data_dir
from taskpaw_v3.core.lifecycle import GracefulShutdown, StartupShutdown
from taskpaw_v3.core.llm import (
    llm_settings_from_config,
    set_llm_chain,
    set_llm_settings,
)
from taskpaw_v3.core.net import (  # re-export
    PortBindError,
    PortInUseError,
    announce_ready,
    bind_is_global,
    bind_is_wildcard,
    claim_port,
    guard_bind_exposure,
    loopback_url,
    port_available,
    reclaim_ports_from_stale_instance,
)
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.state import StateSession
from taskpaw_v3.core.tasklog import TaskLog, set_task_log
from taskpaw_v3.monitors.runtime import (
    effective_monitors,  # re-export (moved to runtime)
)
from taskpaw_v3.monitors.subs.translate import llm_chain_from_config

log = logging.getLogger("taskpaw.agent")

__all__ = [
    "run_agent",
    "PortBindError",
    "PortInUseError",
    "claim_port",
    "port_available",
    "ensure_port_free",
    "effective_monitors",
]


WORKER_API = "agent worker API"


def guard_worker_api(host: str) -> None:
    """S-1: the worker API binds the agent's LAN bind_host; a wildcard or a
    public address is refused before any socket is claimed."""
    if bind_is_wildcard(host) or bind_is_global(host):
        raise ValueError(
            f"refusing to bind the {WORKER_API} to {host!r} — use a specific "
            f"private LAN address."
        )


def ensure_port_free(host: str, port: int, what: str) -> None:
    """Advisory pre-check (claim_port does the real, race-free bind)."""
    if not port_available(host, port):
        raise PortInUseError(f"{what} port {host}:{port} is already in use.")


def build_queue(config: AgentConfig, state_path: Optional[Path]) -> EventQueue:
    """EventQueue with persisted monotonic id (constitution §3)."""
    if state_path is None:
        return EventQueue(machine=config.machine)
    session = StateSession.open(state_path, config.server_id)
    return EventQueue(
        machine=config.machine,
        start_id=session.record.next_event_id,
        persist_counter=session.reserve,
        state_session=session,
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
    so startup fails with an actionable PortBindError (PortInUseError for a
    conflict), never a generic bind error mid-startup.
    """
    import uvicorn

    from .app import create_control_app, create_network_app

    # #299 R5: a LAN bind_host DHCP took away moves to the primary private
    # address (saved with a backup) before the guard and any socket claim.
    from .network_routes import heal_on_startup

    address_changed_from = heal_on_startup(config, config_path)
    # Refuse an unsafe network exposure at startup too — not just from the UI guard
    # — so a hand-edited agent.yaml / bootstrap can't bind wildcard/public/non-
    # loopback-without-token unguarded (#114/Kimi). Raised BEFORE any socket claim.
    guard_bind_exposure(config.bind_host, config.api_token, label="agent network API")
    # P2-2 (#293): the worker API (a third server) only on a controller with
    # remote workers on; it binds bind_host, never a wildcard or public address
    workflow = config.workflow
    worker_api = (
        workflow.enabled and workflow.role == "controller" and workflow.remote_workers
    )
    if worker_api:
        guard_worker_api(config.bind_host)
    strip_control_env()

    # Publish the global LLM settings (#178) BEFORE the stale-port reclaim, any
    # socket claim and the supervisor, so the first check() of any monitor reads
    # the real settings (env key first), never the unconfigured defaults (C2).
    # MonitorAdmin.update_config refreshes it after each successful save. Same
    # for the provider chain + failover switch (#192 AC1) and the data dir —
    # ONLY this run's config_path folder, never default_config_path() (C5).
    set_llm_settings(llm_settings_from_config(config))
    set_llm_chain(llm_chain_from_config(config), failover=config.llm_failover)
    set_data_dir(config_path.parent if config_path is not None else None)
    power.set_power_settings(config.power)  # PWR-1: admin edits replace it live

    # Seamless updates/restarts: if OUR OWN previous agent backend is still holding
    # these ports (common right after installing a new version), terminate that
    # stale instance and reclaim BOTH ports. Only ever kills a positively-identified
    # TaskPaw agent backend, and only if BOTH the network AND control ports are free
    # or ours — a foreign service on either port aborts the whole reclaim so we never
    # kill the old agent when startup would fail here anyway (claim_port then fails
    # loudly as before).
    ports = [
        (config.bind_host, config.bind_port, "agent network API"),
        (config.control_host, config.control_port, "agent control API"),
    ]
    if worker_api:
        ports.append((config.bind_host, workflow.worker_api_port, WORKER_API))
    reclaim_ports_from_stale_instance(ports, role="agent")

    # Validate after stale-instance reclaim, before fresh claims or writers.
    queue = queue if queue is not None else build_queue(config, state_path)
    shutdown = shutdown or GracefulShutdown()
    net_sock = ctl_sock = api_sock = None
    try:
        net_sock = claim_port(config.bind_host, config.bind_port, "agent network API")
        ctl_sock = claim_port(
            config.control_host, config.control_port, "agent control API"
        )
        if worker_api:  # a collision fails startup loudly, like the other two
            api_sock = claim_port(
                config.bind_host, workflow.worker_api_port, WORKER_API
            )
        session = bootstrap_control(
            "agent", loopback_url(config.control_host, config.control_port), config_path
        )
    except BaseException:
        for sock in (net_sock, ctl_sock, api_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    log.error("Could not close an agent API socket")
        queue.close()
        raise

    supervisor = None
    workflow_host = None
    worker_host = None  # P2-2: role=worker
    power_sampler: Optional[power.PowerSampler] = None
    fleet: Optional[power_fleet.PowerFleet] = None
    servers: list[uvicorn.Server] = []
    threads: list[threading.Thread] = []
    task_log = None
    runtime_started = False

    def _deactivate() -> None:
        try:
            revoke_control(session)
        except Exception:
            log.error("Could not revoke agent control credentials")

    def _stop() -> None:
        try:
            if runtime_started and task_log is not None:
                task_log.record("", "agent.stopping", task_type="agent")
            if workflow_host is not None:
                workflow_host.stop()
            if worker_host is not None:  # S-7: joins its thread
                worker_host.stop()
            if supervisor is not None:
                supervisor.stop()
        finally:
            if fleet is not None:  # PWR-3: the peers' refresher
                if not fleet.stop():
                    log.error("The power peers refresher did not stop in time")
                if power_fleet.get_fleet() is fleet:
                    power_fleet.set_fleet(None)
            if power_sampler is not None:
                try:
                    power_sampler.stop()  # flushes the open 15-minute bucket
                except Exception:
                    log.error("Could not stop the power sampler")
                if power.get_sampler() is power_sampler:
                    power.set_sampler(None)
            try:
                for server in servers:
                    server.should_exit = True
                for thread in threads:
                    if (
                        thread.ident is not None
                        and thread is not threading.current_thread()
                    ):
                        thread.join(timeout=10)
                for sock in (net_sock, ctl_sock, api_sock):
                    if sock is None:
                        continue
                    try:
                        sock.close()
                    except OSError:
                        log.error("Could not close an agent API socket")
            finally:
                # Sticky close retains an admitted in-flight reservation lease.
                queue.close()

    startup = StartupShutdown(shutdown, _deactivate, _stop)
    shutdown.register("agent", startup.stop)
    try:
        startup.checkpoint()
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

        def _log_failure(message: str) -> None:
            queue.add(
                monitor="tasklog",
                message=message,
                level="alert",
                title="Task log write failed",
            )

        task_log.set_on_first_failure(_log_failure)

        # Build + start the monitor supervisor from the effective monitor list
        # (config.monitors + a default host_metrics per §5b), wiring events into the
        # same queue the Hub polls.
        from taskpaw_v3.agent.server.admin import MonitorAdmin
        from taskpaw_v3.monitors.registry import default_registry
        from taskpaw_v3.monitors.runtime import build_supervisor

        # One registry, shared by the supervisor AND /control/plugins, so the endpoint
        # advertises exactly what this agent runs (Kimi).
        registry = default_registry()
        monitors = effective_monitors(config)
        # Always build the supervisor — even with no monitors — so the control API can
        # add the FIRST monitor live (#57). build_supervisor validates each spec and
        # skips ones marked enabled:false.
        supervisor = build_supervisor(registry, monitors, queue, config.machine)

        # Live add/remove/update/enable/disable, persisted to config_path (#57).
        admin = MonitorAdmin(config, supervisor, registry, config_path)

        # PWR-1 (#287, PW01): one agent-level power sampler, built here so the
        # routes can read it and started after supervisor.start(). Its activity
        # is the GPU lease holder (a manual task's monitor type, or the
        # workflow's running stage) and otherwise the workflow's non-GPU stage.
        # `power.enabled` gates every tick, so it applies live; the Hub never
        # samples, so a machine is never counted twice.
        def _monitor_type(instance_id: str) -> Optional[str]:
            type_of = getattr(supervisor, "type_of", None)
            return type_of(instance_id) if callable(type_of) else None

        sampler = power_sampler = power.PowerSampler(
            power.get_power_settings,
            # P2-3 R14: a worker's dispatcher owns its films' energy too
            activity=power.activity_source(
                _monitor_type,
                lambda: (
                    getattr(workflow_host, "dispatcher", None)
                    or getattr(worker_host, "dispatcher", None)
                ),
            ),
            data_dir=config_path.parent if config_path is not None else None,
        )

        # PWR-3 (#289, PW06): the 功耗 tab's peers, refreshed in the background
        # with this agent's own (live) api_token; routes read only the cache.
        def _peers() -> list:
            # P2-4 R11: the controller's enrolled workers join automatically
            from taskpaw_v3.workflow.devices import power_peers

            return power_fleet.merge_peers(
                power.get_power_settings().peers, power_peers(workflow_host)
            )

        peers = fleet = power_fleet.PowerFleet(
            _peers,
            lambda: config.api_token,
            # IR06: this agent's own address entered as a peer is not summed twice
            own=lambda: (config.server_id, config.machine),
        )

        def _power_view(range_: str) -> dict:
            import platform

            view = power.power_view(sampler, range_)  # ValueError first
            return {
                **view,
                "machine": {"name": config.machine, "os": platform.system()},
                "peers": peers.snapshot(range_),
            }

        def _status_provider() -> dict:
            import platform

            # snapshot (running) + configured-but-disabled stubs, so the console
            # can list + re-enable stopped monitors (#57).
            monitors = admin.status_view()
            # Additively stamp each monitor with the time of its most recent local
            # event (#130) so the console's pill selector can show per-monitor
            # freshness without a second round-trip. Only monitors present in the
            # status get stamped; a monitor with no events keeps no key.
            last_seen = queue.last_event_times()
            for name, entry in monitors.items():
                if isinstance(entry, dict) and name in last_seen:
                    entry["last_event_at"] = last_seen[name]
            status: dict[str, Any] = {
                "machine": config.machine,
                "server_id": config.server_id,
                "os": platform.platform(),
                "version": __version__,
                "monitors": monitors,
                # PWR-1: the live estimate (null while sampling is off)
                "power": sampler.live(),
                # the retained today / week / month Wh, kept while sampling is off
                "energy": sampler.energy(),
            }
            if config.workflow.role == "worker":  # P2-3 R16
                status["workflow_worker"] = (
                    worker_host.summary() if worker_host is not None else None
                )
            if config.workflow.enabled:
                status["workflow"] = (
                    workflow_host.events.snapshot()
                    if workflow_host is not None
                    and workflow_host.state == "running"
                    and workflow_host.events is not None
                    else None
                )
            return status

        net = uvicorn.Server(
            uvicorn.Config(
                create_network_app(
                    config,
                    queue,
                    _status_provider,
                    films_provider=supervisor.film_page,
                    run_films_provider=supervisor.run_films,
                    power_history=sampler.history,
                    workflow_provider=lambda: workflow_host,
                ),
                log_level="warning",
            )
        )
        ctl = uvicorn.Server(
            uvicorn.Config(
                create_control_app(
                    config,
                    control_token=session.token,
                    control_active=session.is_active,
                    on_command=admin.handle,
                    status_provider=_status_provider,
                    registry=registry,
                    admin=admin,
                    events_provider=queue.recent,
                    films_provider=supervisor.film_page,
                    run_films_provider=supervisor.run_films,
                    prompt_responder=supervisor.respond_prompt,
                    # late-bound: the host starts after announce_ready (T06-P1)
                    workflow_provider=lambda: workflow_host,
                    power_view=_power_view,
                    worker_provider=lambda: worker_host,
                    address_changed_from=address_changed_from,
                ),
                log_level="warning",
            )
        )

        servers.extend((net, ctl))
        api = None
        if worker_api:
            from .worker_api import create_worker_app

            # S-6: no access log, so no path or header ever reaches a log
            api = uvicorn.Server(
                uvicorn.Config(
                    create_worker_app(lambda: workflow_host),
                    log_level="warning",
                    access_log=False,
                )
            )
            servers.append(api)

        def _serve(server, sock, label):
            try:
                server.run(sockets=[sock])
            except Exception:
                log.error("Agent %s server crashed", label)
                shutdown.shutdown()

        threads.extend(
            (
                threading.Thread(
                    target=lambda: _serve(net, net_sock, "network"),
                    name="agent-net",
                    daemon=True,
                ),
                threading.Thread(
                    target=lambda: _serve(ctl, ctl_sock, "control"),
                    name="agent-ctl",
                    daemon=True,
                ),
            )
        )
        if api is not None:
            threads.append(
                threading.Thread(
                    target=lambda: _serve(api, api_sock, "worker API"),
                    name="agent-worker-api",
                    daemon=True,
                )
            )
        shutdown.install_signal_handlers()
        # Cleanup is already registered even if start() fails part way through.
        startup.checkpoint()
        supervisor.start()
        runtime_started = True
        power.set_sampler(sampler)
        sampler.start()
        power_fleet.set_fleet(peers)
        peers.start()
        startup.checkpoint()
        for thread in threads:
            startup.checkpoint()
            thread.start()
            startup.checkpoint()
        deadline = time.monotonic() + 10
        while not all(server.started for server in servers):
            startup.checkpoint()
            if (
                not session.is_active()
                or any(not thread.is_alive() for thread in threads)
                or time.monotonic() >= deadline
            ):
                raise RuntimeError("Agent API startup failed")
            time.sleep(0.01)
        if not session.is_active():
            raise RuntimeError("Agent API startup failed")
        startup.checkpoint()
        log.info(
            "Agent up: network %s:%s, control %s:%s",
            config.bind_host,
            config.bind_port,
            config.control_host,
            config.control_port,
        )
        ready = {}
        if session.credential_file is not None:
            ready = {
                "control_credential_file": str(session.credential_file),
                "boot_id": session.boot_id,
            }
        announce_ready("agent", session.base_url, **ready)
        # After readiness and in its own thread: never delays or breaks the agent.
        try:
            config_dir = config_path.parent if config_path is not None else None
            if config.workflow.role == "worker":  # P2-2: no store, no events
                from taskpaw_v3.workflow.remote_worker import start_worker_host

                worker_host = start_worker_host(config.workflow, config_dir)
            else:
                from taskpaw_v3.workflow.local_host import start_local_workflow

                workflow_host = start_local_workflow(
                    config.workflow,
                    config_dir,
                    task_log=task_log,
                    event_queue=queue,
                )
        except Exception:
            log.exception("media workflow could not be started")
    except BaseException:
        shutdown.shutdown()
        raise
    finally:
        startup.finish()

    if block:
        shutdown.stopped.wait()
    return shutdown
