"""Real local peers exercise the Hub's sampler/sender ownership boundary."""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.hub.server import upstream_worker
from taskpaw_v3.hub.server.app import HubService
from taskpaw_v3.hub.server.store import HubStore


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    wake = threading.Event()
    while not predicate():
        remaining = deadline - time.monotonic()
        assert remaining > 0, "controlled peer did not reach the expected state"
        wake.wait(min(0.02, remaining))


class Peer:
    def __init__(self, kind, *, blocked=False):
        self.kind = kind
        self.release = threading.Event()
        if not blocked:
            self.release.set()
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.status_reads = 0
        self.event_reads = []
        self.requests = []
        self.active = 0
        self.max_active = 0
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer fake-agent-token":
                    self.send_error(401)
                    return
                cursor = {
                    "version": 1,
                    "durable": True,
                    "server_id": f"fixture-{peer.server.server_port}",
                    "stream_id": "1" * 32,
                    "boot_id": "2" * 32,
                    "resume_floor": 0,
                    "offered_highwater": 0,
                    "next_event_id": 1,
                }
                parsed = urlsplit(self.path)
                with peer.lock:
                    if parsed.path == "/status":
                        peer.status_reads += 1
                        body = {"machine": "healthy", "event_cursor": cursor}
                    elif parsed.path == "/events":
                        peer.event_reads.append(parse_qs(parsed.query))
                        body = {"events": [], "event_cursor": cursor}
                    else:
                        self.send_error(404)
                        return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                with peer.lock:
                    peer.requests.append(
                        (self.headers.get("Authorization"), json.loads(raw))
                    )
                    peer.active += 1
                    peer.max_active = max(peer.max_active, peer.active)
                peer.entered.set()
                try:
                    deadline = time.monotonic() + 30
                    while not peer.release.wait(0.02):
                        # A cancelled helper closes the socket even when this
                        # fake OpenClaw has not yet released its response barrier.
                        ready, _, _ = select.select([self.connection], [], [], 0)
                        if ready and self.connection.recv(1, socket.MSG_PEEK) == b"":
                            return
                        if time.monotonic() >= deadline:
                            return
                    self.send_response(204)
                    self.end_headers()
                except OSError:
                    return  # Cancellation is the peer behaviour under test.
                finally:
                    with peer.lock:
                        peer.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.owner = threading.Thread(target=self.server.serve_forever, daemon=False)
        self.owner.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/hooks/wake"

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.owner.join(2)
        assert not self.owner.is_alive()
        assert self.active == 0
        assert self.server.socket.fileno() == -1


@pytest.fixture
def peers():
    opened = []

    def create(kind, **kwargs):
        peer = Peer(kind, **kwargs)
        opened.append(peer)
        return peer

    yield create
    for peer in opened:
        peer.close()


def service_for(tmp_path, sink, *, enabled=True, token="fake-openclaw-token"):
    store = HubStore(tmp_path / "hub.db")
    service = HubService(
        HubConfig(
            data_dir=str(tmp_path),
            poll_interval=1,
            self_monitor=False,
            openclaw_enabled=enabled,
            openclaw_url=sink.url,
            openclaw_token=token,
            polling_token="fake-agent-token",
            write_status_md=False,
            status_log_retention_days=0,
        ),
        store,
    )
    return store, service


def enqueue(store, count):
    for number in range(count):
        store.enqueue_delivery("fixture", "event", json.dumps({"text": str(number)}))


def test_stalled_sender_does_not_delay_two_agents_and_stop_releases_all_owners(
    tmp_path, peers, monkeypatch
):
    sink = peers("openclaw", blocked=True)
    agents = [peers("agent"), peers("agent")]
    store, service = service_for(tmp_path, sink)
    ids = [
        store.add_server(f"agent-{index}", "127.0.0.1", agent.server.server_port)
        for index, agent in enumerate(agents)
    ]
    enqueue(store, 10)
    # Keep the barrier open well beyond two sampling cycles. Cancellation must
    # work independently of timeout; production's shorter cap has transport tests.
    service.poller.http_timeout = 20
    service.delivery._request_timeout = 15
    records = []
    cleanup = upstream_worker.Transport._cleanup

    def capture_cleanup(owner, record, cancel):
        records.append(record)
        return cleanup(owner, record, cancel)

    monkeypatch.setattr(upstream_worker.Transport, "_cleanup", capture_cleanup)
    try:
        service.start()
        assert sink.entered.wait(5)
        poll_thread, sender_thread = service._thread, service.delivery._thread
        service.start()
        assert service._thread is poll_thread
        assert service.delivery._thread is sender_thread
        wait_until(lambda: all(agent.status_reads >= 2 for agent in agents))
        assert not sink.release.is_set()
        assert len(sink.requests) == sink.max_active == 1
        assert store.notification_health()["pending_count"] == 10
        wait_until(
            lambda: all(
                service.poller.snapshot_statuses()[sid]["online"] for sid in ids
            )
        )
        assert all(agent.event_reads for agent in agents)
        assert all(
            query.get("cursor_stream") == ["1" * 32]
            for agent in agents
            for query in agent.event_reads
        )

        started = time.monotonic()
        assert service.stop()
        assert time.monotonic() - started < 5
        assert not poll_thread.is_alive() and not sender_thread.is_alive()
        assert service.poller.stopped()
        assert service.delivery.clean() and service.delivery.stopped
        wait_until(lambda: sink.active == 0)
        assert len(sink.requests) == 1
        assert store.notification_health()["pending_count"] == 10
        assert store.notification_health()["last_success_at"] is None
        assert records
        for record in records:
            assert record.proc.poll() is not None
            assert all(not owner.is_alive() for owner in record.threads)
            assert record.proc.stdin.closed and record.proc.stdout.closed
            assert record.keeper == -1
    finally:
        assert service.stop()
        store.close()


def test_cancelled_queue_survives_database_reopen_and_service_restart(tmp_path, peers):
    sink = peers("openclaw", blocked=True)
    store, service = service_for(tmp_path, sink)
    enqueue(store, 10)
    try:
        service.start()
        assert sink.entered.wait(5)
        assert service.stop()
        wait_until(lambda: sink.active == 0)
        assert store.notification_health()["pending_count"] == 10
        assert [body["text"] for _, body in sink.requests] == ["0"]
        store.close()
        store, service = service_for(tmp_path, sink)
        assert store.notification_health()["pending_count"] == 10
        sink.release.set()
        service.start()
        # Observe ten serial helpers plus a normal 24–36s first retry window.
        # This fixture allowance is not a production delivery deadline.
        wait_until(
            lambda: store.notification_health()["pending_count"] == 0, timeout=90
        )
        assert store.notification_health()["last_success_at"] is not None
        assert sink.max_active == 1
        texts = [body["text"] for _, body in sink.requests]
        # Retry due times can change order; only the cancelled first send repeats.
        assert Counter(texts) == Counter(["0", *map(str, range(10))])
        assert service.stop()

        previous = service.poller
        service.start()
        assert service.poller is not previous
        running = service._thread, service.delivery._thread
        service.start()
        assert running == (service._thread, service.delivery._thread)
        assert service.stop()
        assert all(not owner.is_alive() for owner in running)
    finally:
        assert service.stop()
        store.close()


@pytest.mark.parametrize("initial", ["disabled", "missing_token"])
def test_delivery_toggles_pause_backlog_while_sampling_continues(
    tmp_path, peers, initial
):
    sink = peers("openclaw")
    agent = peers("agent")
    store, service = service_for(
        tmp_path,
        sink,
        enabled=initial != "disabled",
        token="" if initial == "missing_token" else "fake-openclaw-token",
    )
    store.add_server("agent", "127.0.0.1", agent.server.server_port)
    enqueue(store, 2)
    try:
        service.start()
        wait_until(lambda: agent.status_reads >= 2)
        assert sink.requests == []
        assert service.notification_health()["state"] == initial
        assert store.notification_health()["pending_count"] == 2
        store.set_configs({"openclaw_enabled": "1", "openclaw_token": "new-fake-token"})
        wait_until(lambda: store.notification_health()["pending_count"] == 0)
        assert len(sink.requests) == 2
        assert all(auth == "Bearer new-fake-token" for auth, _ in sink.requests)

        store.set_config("openclaw_enabled", "0")
        enqueue(store, 2)
        before = agent.status_reads
        wait_until(lambda: agent.status_reads >= before + 2)
        assert len(sink.requests) == 2
        assert store.notification_health()["pending_count"] == 2
        store.set_configs({"openclaw_enabled": "1", "openclaw_token": ""})
        before = agent.status_reads
        wait_until(lambda: agent.status_reads >= before + 2)
        assert len(sink.requests) == 2
        assert service.notification_health()["state"] == "missing_token"
        store.set_config("openclaw_token", "restored-fake-token")
        wait_until(lambda: store.notification_health()["pending_count"] == 0)
        assert len(sink.requests) == 4 and sink.max_active == 1
        assert all(
            auth == "Bearer restored-fake-token" for auth, _ in sink.requests[2:]
        )
    finally:
        assert service.stop()
        store.close()
