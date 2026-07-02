"""Hub FastAPI app + background poll loop.

#15 ships the headless aggregation backend: a wallclock-driven poll loop (no
drift — design carries the V2 monotonic-schedule fix) and a small read API. The
Tauri dashboard (#19) and richer endpoints come later.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import sqlite3
import threading
import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from taskpaw_v3.core.auth import token_ok
from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.core.lifecycle import GracefulShutdown
from taskpaw_v3.core.net import guard_bind_exposure
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore

log = logging.getLogger("taskpaw.hub")

# A DNS hostname: dot-separated labels of alnum/hyphen (no scheme, port, path, or
# URL metacharacters). IP literals are validated separately via ipaddress (#124).
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)


def _unauthorized() -> JSONResponse:
    # Mirror the agent's network app: 401 + WWW-Authenticate, no body leakage (#106).
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer realm="TaskPaw"'},
    )


def _guard_bind_exposure(config: HubConfig) -> None:
    """Refuse to start an unsafe network exposure (#114), mirroring the agent's
    UI guard (constitution §2: no public/WAN exposure, never default to all
    interfaces). Raised BEFORE the socket is claimed so a bad bind never opens.

    - A wildcard/all-interfaces bind (0.0.0.0, :: and any spelling) is refused
      outright — even with a token — so the Hub never listens on every nic.
    - A globally-routable (public/WAN) address is refused even WITH a token — the
      Hub is LAN + Bearer only (constitution §2: no public/WAN exposure) (Codex).
    - A non-loopback bind with an empty api_token is refused: /status (each
      agent's snapshot) and /events would be reachable off-host unauthenticated.

    The actual rules + host normalization live in the shared
    core.net.guard_bind_exposure, so the Hub startup, agent startup, and agent UI
    guards can't drift.
    """
    guard_bind_exposure(config.bind_host, config.api_token, label="Hub API")


class HubService:
    def __init__(self, config: HubConfig, store: HubStore) -> None:
        self.config = config
        self.store = store
        self.poller = Poller(
            store=store,
            openclaw_url=config.openclaw_url,
            get_active=lambda: (
                store.get_config(
                    "openclaw_enabled", "1" if config.openclaw_enabled else "0"
                )
                == "1"
                and bool(store.get_config("openclaw_token", config.openclaw_token))
            ),
            get_token=lambda: store.get_config("openclaw_token", config.openclaw_token),
            get_polling_token=lambda: store.get_config(
                "polling_token", config.polling_token
            ),
            # Seed a restarted Hub's status.md as ONLINE only for agents whose last
            # success is within ~2 polls; older successes render OFFLINE (#38).
            seed_fresh_seconds=max(2 * config.poll_interval, 60),
        )
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        # Hub self-monitor (§5b.2): watch the Hub's OWN host. Its events go to the
        # outbox (forwarded to OpenClaw when active) tagged with the hub machine;
        # its snapshot is exposed in /status. No separate agent needed.
        self.self_supervisor = (
            self._build_self_supervisor() if config.self_monitor else None
        )

    def _build_self_supervisor(self):
        from taskpaw_v3.monitors.registry import default_registry
        from taskpaw_v3.monitors.supervisor import Supervisor

        def sink(instance_id, level, title, message, data=None, dedupe_key=None):
            active = self.store.get_config(
                "openclaw_enabled", "1" if self.config.openclaw_enabled else "0"
            ) == "1" and bool(
                self.store.get_config("openclaw_token", self.config.openclaw_token)
            )
            if not active:
                return
            import json

            # No dedupe_key: these are edge-triggered incidents (alert on breach,
            # done on recovery). A stable title-based key would let the permanent
            # outbox unique index suppress every future recurrence of the same
            # metric. Each transition is a distinct delivery.
            self.store.enqueue_delivery(
                server_name=self.config.machine,
                kind="event",
                payload_json=json.dumps(
                    {"text": f"TaskPaw Event | {self.config.machine}: {message}"}
                ),
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
                # status.md / pruning are OpenClaw-compat side outputs — a failure
                # here must never stall or kill the poll loop (#38).
                try:
                    self._refresh_compat_outputs()
                except Exception as e:
                    log.error("status.md/prune failed: %s", e)
                next_due = now + max(1, self.config.poll_interval)
            time.sleep(0.5)

    def _refresh_compat_outputs(self) -> None:
        """Write status.md and prune old status_log rows (OpenClaw compat, #38)."""
        if self.config.status_log_retention_days:
            self.store.prune_status_logs(self.config.status_log_retention_days)
        if self.config.write_status_md:
            from datetime import datetime
            from pathlib import Path

            from taskpaw_v3.hub.server.status_md import write_status_md

            path = Path(self.config.data_dir).expanduser() / "status.md"
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # From the poller's in-memory snapshot (current reachability + last
            # good status), like V2 — status_log holds only successful polls.
            write_status_md(path, self.poller.status_snapshot(), now)

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(
            target=self._loop, name="hub-poller", daemon=True
        )
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
    from taskpaw_v3.core.cors import add_ui_cors

    app = FastAPI(title="TaskPaw Hub", docs_url=None, redoc_url=None)
    add_ui_cors(app)  # the Hub dashboard UI talks to this API (design §3.2)
    service = HubService(config, store)

    def _auth(request: Request) -> bool:
        # Bearer-gate the read API (#106). Empty api_token = auth disabled (V2
        # parity), mirroring the agent's network app.
        return token_ok(config.api_token, request.headers.get("Authorization"))

    @app.get("/ping")
    def ping() -> dict:
        # Open by design — trivial reachability probe, no sensitive data.
        return {"ok": True, "machine": config.machine, "version": "3.0.0-dev"}

    @app.get("/status")
    def status(request: Request):
        if not _auth(request):
            return _unauthorized()
        # Attach each server's latest poll snapshot (#96) so the dashboard can show
        # per-machine state + metrics + last_seen. Additive: the existing
        # id/name/ip/port/enabled keys are preserved; `online`/`last_seen`/`snapshot`
        # are added. `snapshot` is the agent's parsed /status (None if never polled).
        snaps = service.poller.snapshot_statuses()
        servers = []
        for s in store.list_servers():
            snap = snaps.get(s["id"], {})
            # A disabled server isn't polled, so its snapshot goes stale — force
            # online=False so the dashboard never shows a disabled machine as live
            # (Codex). last_seen/snapshot stay for last-known reference.
            servers.append(
                {
                    **s,
                    "online": bool(snap.get("online", False)) and bool(s["enabled"]),
                    "last_seen": snap.get("last_seen"),
                    "snapshot": snap.get("snapshot"),
                }
            )
        return {
            "machine": config.machine,
            "servers": servers,
            # Locked snapshot — the poller thread mutates this concurrently.
            "acks": service.poller.snapshot_acks(),
            # Hub's own host-health self-monitor (§5b.2).
            "self": service.self_supervisor.snapshot()
            if service.self_supervisor
            else {},
        }

    @app.get("/events")
    def events(
        request: Request,
        server: Optional[int] = None,
        level: Optional[str] = None,
        limit: int = 200,
    ):
        if not _auth(request):
            return _unauthorized()
        # Durable event history aggregated from all polled agents (#44), newest
        # first, optionally filtered by server id / level. Clamp the limit.
        limit = max(1, min(int(limit), 1000))
        return {
            "events": store.recent_events(server_id=server, level=level, limit=limit)
        }

    # ── manage the polled agents from the dashboard (#124) — same Bearer gate as
    # the read API (#106): open on loopback, token-required on a LAN Hub (#114). ──
    from fastapi import HTTPException

    def _valid_name(v) -> str:
        # Must be a string — don't str()-coerce a list/dict into its repr (Kimi).
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(
                status_code=400, detail="name must be a non-blank string"
            )
        return v.strip()

    def _valid_ip(v) -> str:
        # Must be an IP literal or a plain hostname — NOT a URL, host:port, or a
        # path. Otherwise poller._agent_base_url builds a malformed/unintended URL
        # (e.g. "192.168.1.80:5678" → http://[192.168.1.80:5678]:5680) — a
        # correctness bug and a mild SSRF vector (Kimi).
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(
                status_code=400, detail="ip/host must be a non-blank string"
            )
        h = v.strip().strip("[]")  # allow a bracketed IPv6 literal
        try:
            ipaddress.ip_address(h)
            return h
        except ValueError:
            pass
        if _HOSTNAME_RE.match(h):
            return h
        raise HTTPException(
            status_code=400,
            detail="ip/host must be an IP address or hostname (no port, path, or scheme)",
        )

    def _valid_port(v) -> int:
        # Reject bool (an int subclass) and non-integers (3.14) — silently
        # truncating them would store a port the wire value never named. Parse via
        # int() (NOT str.isdigit(), which accepts Unicode digits like "⁵" that
        # int() then rejects → 500 instead of 400) (Kimi).
        if isinstance(v, bool):
            raise HTTPException(status_code=400, detail="port must be an integer")
        if isinstance(v, int):
            p = v
        elif isinstance(v, str):
            try:
                p = int(v.strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="port must be an integer")
        else:
            raise HTTPException(status_code=400, detail="port must be an integer")
        if not (1 <= p <= 65535):
            raise HTTPException(
                status_code=400, detail="port must be between 1 and 65535"
            )
        return p

    def _valid_enabled(v) -> bool:
        if not isinstance(v, bool):
            raise HTTPException(status_code=400, detail="'enabled' must be a boolean")
        return v

    @app.post("/servers")
    def add_server(request: Request, body: dict):
        if not _auth(request):
            return _unauthorized()  # same 401 shape as the read API (Kimi)
        name = _valid_name(body.get("name"))
        ip = _valid_ip(body.get("ip"))
        port = _valid_port(body.get("port", 5680))
        # Explicit name-exists precheck → a clear 400 that doesn't rely on which
        # constraint IntegrityError happens to be (Kimi); the UNIQUE index is still
        # the authoritative backstop against a race.
        if any(s["name"] == name for s in store.list_servers()):
            raise HTTPException(
                status_code=400, detail=f"a server named {name!r} already exists"
            )
        try:
            sid = store.add_server(name, ip, port)
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=400, detail=f"a server named {name!r} already exists"
            )
        log.info("hub: registered agent %r at %s:%s (#124)", name, ip, port)
        return {"ok": True, "id": sid, **(store.get_server(sid) or {})}

    @app.patch("/servers/{sid}")
    def update_server(request: Request, sid: int, body: dict):
        if not _auth(request):
            return _unauthorized()
        if store.get_server(sid) is None:
            raise HTTPException(status_code=404, detail=f"no server with id {sid}")
        # Validate EVERYTHING (incl. enabled) before any store write, so a rejected
        # request never leaves a partial edit persisted (Codex/Kimi).
        name = _valid_name(body["name"]) if "name" in body else None
        ip = _valid_ip(body["ip"]) if "ip" in body else None
        port = _valid_port(body["port"]) if "port" in body else None
        enabled = _valid_enabled(body["enabled"]) if "enabled" in body else None
        try:
            # One transactional UPDATE (name/ip/port/enabled together) — no partial
            # edit if something fails between writes (Kimi).
            store.update_server(sid, name=name, ip=ip, port=port, enabled=enabled)
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="that name is already in use")
        # Re-read: if the row was removed concurrently, report 404 rather than a
        # misleading 200 with an empty body (Kimi).
        srv = store.get_server(sid)
        if srv is None:
            raise HTTPException(status_code=404, detail=f"no server with id {sid}")
        log.info(
            "hub: updated agent id=%s → %s:%s enabled=%s (#124)",
            sid,
            srv["ip"],
            srv["port"],
            bool(srv["enabled"]),
        )
        return {"ok": True, **srv}

    @app.delete("/servers/{sid}")
    def delete_server(request: Request, sid: int):
        if not _auth(request):
            return _unauthorized()
        if not store.remove_server(sid):
            raise HTTPException(status_code=404, detail=f"no server with id {sid}")
        log.info("hub: removed agent id=%s (#124)", sid)
        return {"ok": True}

    @app.patch("/config")
    def update_config(request: Request, body: dict):
        if not _auth(request):
            return _unauthorized()
        if "polling_token" in body:
            tok = body["polling_token"]
            if tok is None:
                tok = ""  # JSON null = clear; never store the string "None" (Kimi)
            if not isinstance(tok, str):
                raise HTTPException(
                    status_code=400, detail="polling_token must be a string"
                )
            # No control chars (\r \n \t …) — they'd be injected into the poller's
            # Authorization header, corrupting the request / enabling injection (Kimi).
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in tok):
                raise HTTPException(
                    status_code=400,
                    detail="polling_token must not contain control characters",
                )
            # Live: the poller reads polling_token per cycle (get_polling_token),
            # so no Hub restart is needed to apply it (#124).
            store.set_config("polling_token", tok)
            log.info(
                "hub: polling_token %s via dashboard (#124)",
                "set" if tok else "cleared",
            )
        return {"ok": True}

    return app, service


def run_hub(
    config: HubConfig,
    store: HubStore,
    shutdown: GracefulShutdown | None = None,
    block: bool = True,
) -> GracefulShutdown:
    import uvicorn

    from taskpaw_v3.core.net import announce_ready, claim_port, loopback_url

    shutdown = shutdown or GracefulShutdown()
    _guard_bind_exposure(config)  # refuse an unsafe LAN exposure before binding (#114)
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
            log.error(
                "A hub thread is still alive; leaving the DB connection open to avoid a race"
            )

    shutdown.register("hub", _stop)
    shutdown.install_signal_handlers()
    server_thread.start()
    log.info("Hub up on %s:%s", config.bind_host, config.bind_port)
    # §3.1 readiness handshake (#48) — one stdout line the Tauri shell reads
    # before loading the webview; injects this loopback base_url (custom port
    # supported). A wildcard/IPv6 bind maps to the reachable loopback host so the
    # local dashboard hits the socket that's actually listening.
    announce_ready("hub", loopback_url(config.bind_host, config.bind_port))

    if block:
        shutdown.stopped.wait()
    return shutdown
