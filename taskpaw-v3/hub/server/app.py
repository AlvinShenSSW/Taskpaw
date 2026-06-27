"""Hub FastAPI app + background poll loop.

#15 ships the headless aggregation backend: a wallclock-driven poll loop (no
drift — design carries the V2 monotonic-schedule fix) and a small read API. The
Tauri dashboard (#19) and richer endpoints come later.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import HubConfig
from core.lifecycle import GracefulShutdown
from hub.server.poller import Poller
from hub.server.store import HubStore

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
        )
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

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

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=5)


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
            "acks": service.poller.last_event_ids,
        }

    return app, service


def run_hub(config: HubConfig, store: HubStore, shutdown: GracefulShutdown | None = None,
            block: bool = True) -> GracefulShutdown:
    import uvicorn

    shutdown = shutdown or GracefulShutdown()
    app, service = create_hub_app(config, store)
    service.start()

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.bind_host, port=config.bind_port, log_level="warning")
    )
    server_thread = threading.Thread(target=server.run, name="hub-api", daemon=True)

    def _stop() -> None:
        service.stop()
        server.should_exit = True
        server_thread.join(timeout=5)
        store.close()

    shutdown.register("hub", _stop)
    shutdown.install_signal_handlers()
    server_thread.start()
    log.info("Hub up on %s:%s", config.bind_host, config.bind_port)

    if block:
        shutdown.stopped.wait()
    return shutdown
