"""Actual frozen Hub/service qualification on an explicitly disposable Mac only.

All peers, configuration, tokens and data belong to this fixture. The real user
machine must never run this entry as a substitute for a hosted native builder.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil

from scripts import macos_release as mac
from taskpaw_v3.core.control_file import read_control_descriptor
from taskpaw_v3.core.state import FileLease, db_lease_path
from taskpaw_v3.hub.server.store import HubStore


def await_condition(check, code, seconds=35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        mac.cancellation_checkpoint()
        if check():
            return
        time.sleep(0.1)
    raise mac.BuildError("macos_hub_smoke_" + code)


class Peers:
    def __init__(self):
        self.token = secrets.token_hex(24)
        self.stop = threading.Event()
        self.slow = threading.Event()
        self.delivery_started = threading.Event()
        self.lock = threading.Lock()
        self.samples = 0
        self.deliveries = 0
        self.servers = []
        self.threads = []

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *args):
                return

            def reply(self, code, value):
                body = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # The tested shutdown intentionally cancels a pending send.
                    return

            def authorized(self):
                if self.headers.get("Authorization") != "Bearer " + fixture.token:
                    self.reply(401, {})
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                cursor = {
                    "version": 1,
                    "durable": True,
                    "server_id": "hub-native-fixture",
                    "stream_id": "1" * 32,
                    "boot_id": "2" * 32,
                    "resume_floor": 0,
                    "offered_highwater": 0,
                    "next_event_id": 1,
                }
                if self.path == "/status":
                    with fixture.lock:
                        fixture.samples += 1
                    self.reply(
                        200,
                        {
                            "machine": "controlled-agent",
                            "version": "3.10.2",
                            "server_id": "hub-native-fixture",
                            "monitors": {},
                            "event_cursor": cursor,
                        },
                    )
                elif self.path.startswith("/events?"):
                    self.reply(200, {"events": [], "event_cursor": cursor})
                else:
                    self.reply(404, {})

            def do_POST(self):
                if not self.authorized() or self.path != "/hooks/wake":
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 65536:
                    self.reply(400, {})
                    return
                self.rfile.read(length)
                fixture.delivery_started.set()
                if fixture.slow.is_set():
                    fixture.stop.wait(30)
                with fixture.lock:
                    fixture.deliveries += 1
                self.reply(200, {"ok": True})

        try:
            for _ in range(2):
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                # Non-daemon handlers are joined by server_close after stop is set.
                server.daemon_threads = False
                thread = threading.Thread(target=server.serve_forever)
                self.servers.append(server)
                self.threads.append(thread)
                thread.start()
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *args):
        self.stop.set()
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(5)
            if thread.is_alive():
                raise mac.BuildError("macos_hub_smoke_peer_cleanup")


def request(port, token, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            return None
        return json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def children(record):
    """Only the fixture's protected, creation-time-bound runtime is inspected."""
    try:
        process = psutil.Process(record["pid"])
        if abs(process.create_time() - record["created"]) > 0.01:
            raise mac.BuildError("macos_hub_smoke_runtime_identity")
        return [(p.pid, p.create_time()) for p in process.children(recursive=True)]
    except psutil.Error:
        raise mac.BuildError("macos_hub_smoke_runtime_missing") from None


def gone(identities):
    for pid, created in identities:
        try:
            if psutil.Process(pid).create_time() == created:
                return False
        except psutil.NoSuchProcess:
            continue
    return True


def released(ports, db):
    for port in ports:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise mac.BuildError("macos_hub_smoke_port_leak") from None
    with FileLease(db_lease_path(db)):
        pass


def smoke(sidecar, plan, isolation, env, version):
    isolation.require()  # Before temporary files, sockets, launchctl or any Popen.
    root = Path(tempfile.mkdtemp(prefix="taskpaw-hub-smoke-")).resolve()
    safe_to_remove = True
    home = root / "HOME"
    config = home / "Library/Application Support/TaskPaw/hub.yaml"
    config.parent.mkdir(parents=True, mode=0o700)
    # Intermediate mkdir parents use umask; ensure the fixture home is private.
    home.chmod(0o700)
    app = home / "Applications/TaskPaw Hub.app"
    app.parent.mkdir(mode=0o700)
    shutil.copytree(sidecar.parents[2], app, symlinks=True)
    executable = app / "Contents/MacOS/taskpaw-backend"
    runtime_env = mac.child_env(env, runtime=True)
    runtime_env.update(HOME=str(home), PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    reservations = [socket.socket(), socket.socket()]
    try:
        for sock in reservations:
            sock.bind(("127.0.0.1", 0))
        read_port, control_port = (sock.getsockname()[1] for sock in reservations)
    finally:
        for sock in reservations:
            sock.close()
    db = home / "data/hub.db"
    token = secrets.token_hex(24)
    service_root = config.parent / "HubService"
    record_path = service_root / "runtime.json"
    descriptor_path = config.with_name("hub.control.json")

    def manage(action):
        isolation.require()
        output, _ = mac.tool(
            "hub_service_" + action,
            [str(executable), "hub-service", action],
            env=runtime_env,
            cwd=root,
            timeout=110,
            exact_output=True,
        )
        try:
            record = json.loads(output)
            if record.get("ok") is not True:
                raise ValueError
            return record
        except (ValueError, AttributeError):
            raise mac.BuildError("macos_hub_smoke_manager_invalid") from None

    def runtime():
        record = json.loads(record_path.read_bytes())
        if record.get("state") != "running":
            raise mac.BuildError("macos_hub_smoke_not_running")
        return record

    def current_status():
        value = request(read_port, token, "/status")
        if not isinstance(value, dict) or value.get("machine") != "hub-native-smoke":
            return False
        servers = value.get("servers", [])
        return bool(
            len(servers) == 1
            and servers[0].get("online") is True
            and servers[0].get("event_channel", {}).get("state") == "ready"
            and value.get("notifications", {}).get("last_success_at")
        )

    try:
        with Peers() as peers:
            config.write_text(
                json.dumps(
                    {
                        "machine": "hub-native-smoke",
                        "bind_port": read_port,
                        "control_port": control_port,
                        "api_token": token,
                        "polling_token": peers.token,
                        "poll_interval": 1,
                        "data_dir": str(db.parent),
                        "openclaw_enabled": True,
                        "openclaw_token": peers.token,
                        "openclaw_url": f"http://127.0.0.1:{peers.servers[1].server_port}/hooks/wake",
                    }
                )
            )
            config.chmod(0o600)
            store = HubStore(db)
            try:
                store.add_server(
                    "controlled-agent", "127.0.0.1", peers.servers[0].server_port
                )
                store.record_hub_event(
                    "hub", "native-smoke", "Fixture event", "info", True
                )
            finally:
                store.close()
            try:
                safe_to_remove = False
                installed = manage("install")
                if (
                    installed.get("state") != "running"
                    or installed.get("version") != version
                ):
                    raise mac.BuildError("macos_hub_smoke_install_identity")
                ping = request(read_port, token, "/ping")
                if not isinstance(ping, dict) or ping.get("version") != version:
                    raise mac.BuildError("macos_hub_smoke_version")
                initial = runtime()
                descriptor = read_control_descriptor(descriptor_path)
                if (
                    descriptor.role != "hub"
                    or descriptor.base_url != f"http://127.0.0.1:{control_port}"
                ):
                    raise mac.BuildError("macos_hub_smoke_descriptor")
                await_condition(current_status, "poll_or_delivery")
                await_condition(
                    lambda: (db.parent / "status.md").is_file(), "status_file"
                )
                # Repeated installation of identical bytes must not restart.
                manage("install")
                if runtime()["created"] != initial["created"]:
                    raise mac.BuildError("macos_hub_smoke_reinstall_restarted")
                # A short-lived read-only client disconnects; polling continues.
                if request(control_port, descriptor.control_token, "/status") is None:
                    raise mac.BuildError("macos_hub_smoke_client_read")
                before = peers.samples
                await_condition(
                    lambda: peers.samples >= before + 2, "client_disconnect"
                )
                previous_children = children(initial)
                manage("restart")
                restarted = read_control_descriptor(descriptor_path)
                if restarted.boot_id == descriptor.boot_id:
                    raise mac.BuildError("macos_hub_smoke_restart_identity")
                await_condition(lambda: gone(previous_children), "restart_helpers")
                await_condition(current_status, "restart_poll")
                # Crash only the owned inner runtime, using PID + creation proof.
                crashed = runtime()
                crash_children = children(crashed)
                process = psutil.Process(crashed["pid"])
                if process.create_time() != crashed["created"]:
                    raise mac.BuildError("macos_hub_smoke_runtime_identity")
                process.send_signal(signal.SIGKILL)
                await_condition(
                    lambda: (
                        record_path.exists()
                        and json.loads(record_path.read_bytes()).get("state")
                        == "running"
                        and json.loads(record_path.read_bytes()).get("created")
                        != crashed["created"]
                    ),
                    "crash_recovery",
                    60,
                )
                await_condition(lambda: gone(crash_children), "crash_helpers")
                await_condition(current_status, "crash_poll")
                # Hold a fake delivery while proving independent polling and stop.
                peers.slow.set()
                peers.delivery_started.clear()
                store = HubStore(db)
                try:
                    store.record_hub_event(
                        "hub", "native-smoke", "Slow fixture event", "info", True
                    )
                finally:
                    store.close()
                await_condition(peers.delivery_started.is_set, "slow_send_start")
                before = peers.samples
                await_condition(lambda: peers.samples >= before + 2, "slow_send_poll")
                pending_children = children(runtime())
                manage("stop")
                await_condition(lambda: gone(pending_children), "stop_helpers")
                released((read_port, control_port), db)
                if descriptor_path.exists():
                    raise mac.BuildError("macos_hub_smoke_descriptor_leak")
                preserved_config = config.read_bytes()
                manage("uninstall")
                safe_to_remove = True
                if not db.is_file() or config.read_bytes() != preserved_config:
                    raise mac.BuildError("macos_hub_smoke_data_removed")
                store = HubStore(db)
                try:
                    if len(store.recent_hub_events()) < 2:
                        raise mac.BuildError("macos_hub_smoke_history_missing")
                finally:
                    store.close()
                return {
                    "role": "hub",
                    "service_lifecycle_verified": True,
                    "polling_and_outbox_verified": True,
                    "crash_recovery_verified": True,
                    "slow_send_stop_and_resource_release_verified": True,
                    "client_disconnect_verified": True,
                    "native_window_close_verified": False,
                    "login_reboot_verified": False,
                    "runtime_sha256": hashlib.sha256(
                        executable.read_bytes()
                    ).hexdigest(),
                }
            finally:
                if not safe_to_remove:
                    # Never remove a runtime or fixture directory after uncertain stop.
                    manage("stop")
                    released((read_port, control_port), db)
                    manage("uninstall")
                    safe_to_remove = True
    finally:
        if safe_to_remove:
            shutil.rmtree(root)
