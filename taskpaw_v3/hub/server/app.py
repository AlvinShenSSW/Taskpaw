"""Hub FastAPI app + background poll loop.

#15 ships the headless aggregation backend: a wallclock-driven poll loop (no
drift — design carries the V2 monotonic-schedule fix) and a small read API. The
Tauri dashboard (#19) and richer endpoints come later.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import FastAPI


from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.core.lifecycle import GracefulShutdown
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore

log = logging.getLogger("taskpaw.hub")


class HubService:
    def __init__(self, config: HubConfig, store: HubStore) -> None:
        self.config = config
        self.store = store
        self.poller = Poller(
            store=store,
            openclaw_url=config.openclaw_url,
            get_active=lambda: (
                store.get_config("openclaw_enabled", "1" if config.openclaw_enabled else "0") == "1"
                and bool(store.get_config("openclaw_token", config.openclaw_token))
            ),
            get_token=lambda: store.get_config("openclaw_token", config.openclaw_token),
            get_polling_token=lambda: store.get_config("polling_token", config.polling_token),
        )
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        # Hub self-monitor (§5b.2): watch the Hub's OWN host. Its events go to the
        # outbox (forwarded to OpenClaw when active) tagged with the hub machine;
        # its snapshot is exposed in /status. No separate agent needed.
        self.self_supervisor = self._build_self_supervisor() if config.self_monitor else None

    def _build_self_supervisor(self):
        from taskpaw_v3.monitors.registry import default_registry
        from taskpaw_v3.monitors.supervisor import Supervisor

        def sink(instance_id, level, title, message, data=None, dedupe_key=None):
            active = (
                self.store.get_config("openclaw_enabled", "1" if self.config.openclaw_enabled else "0") == "1"
                and bool(self.store.get_config("openclaw_token", self.config.openclaw_token))
            )
            if not active:
                return
            import json
            self.store.enqueue_delivery(
                server_name=self.config.machine, kind="event",
                payload_json=json.dumps({"text": f"TaskPaw Event | {self.config.machine}: {message}"}),
                dedupe_key=f"{self.config.machine}:{instance_id}:{title}",
            )

        sup = Supervisor(sink=sink)
        reg = default_registry()
        plugin = reg.get("host_metrics")
        sup.register(plugin, plugin.validate_config({"name": "hub-host"}))
        return sup

    def _loop(self) -> None:
        next_due = time.monotonic()
        while self._running.is_set():
            now = time.monotonic()
            if now >= next_due:
                try:
                    self.poller.poll_once()
                except Exception as e:
                    log.error("Poll cycle failed: %s", e)
                next_due = now + max(1, self.config.poll_interval)
            time.sleep(0.5)

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(target=self._loop, name="hub-poller", daemon=True)
        self._thread.start()
        if self.self_supervisor:
            self.self_supervisor.start()

    def stop(self) -> bool:
        """Stop the poll loop. Returns True iff the thread actually exited
        (so the caller knows it's safe to close the shared store)."""
        self._running.clear()
        if self.self_supervisor:
            self.self_supervisor.stop()
        if self._thread:
            # Generous: a cycle may be mid-urlopen (<= http timeout) or in a
            # slow transaction. Cover that before the store is closed.
            self._thread.join(timeout=20)
            return not self._thread.is_alive()
        return True


def create_hub_app(config: HubConfig, store: HubStore) -> tuple[FastAPI, HubService]:
    app = FastAPI(title="TaskPaw Hub", docs_url=None, redoc_url=None)
    service = HubService(config, store)

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True, "machine": config.machine, "version": "3.0.0-dev"}

    @app.get("/status")
    def status() -> dict:
        return {
            "machine": config.machine,
            "servers": store.list_servers(),
            # Locked snapshot — the poller thread mutates this concurrently.
            "acks": service.poller.snapshot_acks(),
            # Hub's own host-health self-monitor (§5b.2).
            "self": service.self_supervisor.snapshot() if service.self_supervisor else {},
        }

    return app, service


def run_hub(config: HubConfig, store: HubStore, shutdown: GracefulShutdown | None = None,
            block: bool = True) -> GracefulShutdown:
    import uvicorn

    from taskpaw_v3.core.net import claim_port

    shutdown = shutdown or GracefulShutdown()
    sock = claim_port(config.bind_host, config.bind_port, "hub API")  # race-free
    app, service = create_hub_app(config, store)
    service.start()

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    def _serve() -> None:
        try:
            server.run(sockets=[sock])
        except Exception as e:  # a failed server must not hang run_hub forever
            log.error("Hub API server crashed: %s", e)
            shutdown.shutdown()

    server_thread = threading.Thread(target=_serve, name="hub-api", daemon=True)

    def _stop() -> None:
        stopped = service.stop()  # join the poll thread FIRST
        server.should_exit = True
        server_thread.join(timeout=10)
        try:
            sock.close()
        except OSError:
            pass
        # Closing the shared SQLite connection is only safe once BOTH the poller
        # and the API thread have truly exited.
        if stopped and not server_thread.is_alive():
            store.close()
        else:
            log.error("A hub thread is still alive; leaving the DB connection open to avoid a race")

    shutdown.register("hub", _stop)
    shutdown.install_signal_handlers()
    server_thread.start()
    log.info("Hub up on %s:%s", config.bind_host, config.bind_port)

    if block:
        shutdown.stopped.wait()
    return shutdown
