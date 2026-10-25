"""Bounded OpenClaw delivery uses an owner independent of Agent sampling."""

from __future__ import annotations

import json
import ssl
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from taskpaw_v3.hub.server import upstream_worker as upstream


@pytest.fixture
def endpoint():
    handlers = []

    def create(action=None, *, tls=False):
        requests = []
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(
                    {
                        "auth": self.headers.get("Authorization"),
                        "body": self.rfile.read(int(self.headers["Content-Length"])),
                    }
                )
                if action:
                    action(self, release)
                else:
                    self.send_response(204)
                    self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = False
        if tls:
            fixtures = Path(__file__).parent / "fixtures"
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(
                fixtures / "redirect-test-cert.pem", fixtures / "redirect-test-key.pem"
            )
            server.socket = context.wrap_socket(server.socket, server_side=True)
        owner = threading.Thread(target=server.serve_forever)
        owner.start()
        handlers.append((server, owner, release))
        scheme = "https" if tls else "http"
        return f"{scheme}://127.0.0.1:{server.server_port}/notify", requests

    yield create
    for server, owner, release in handlers:
        release.set()
        server.shutdown()
        server.server_close()
        owner.join(2)
        assert not owner.is_alive()


def request(url, timeout=2):
    return {
        "kind": "openclaw",
        "url": url,
        "headers": {"Authorization": "Bearer fake-delivery-token"},
        "payload": {"text": "notification fixture"},
        "timeout": timeout,
    }


def test_owned_openclaw_success_preserves_post_body_and_auth(endpoint):
    url, seen = endpoint()
    transport = upstream.Transport()
    try:
        result = transport.request(request(url), 3)
        assert result == {"ok": True}, result
        assert seen == [
            {
                "auth": "Bearer fake-delivery-token",
                "body": json.dumps({"text": "notification fixture"}).encode(),
            }
        ]
        assert transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


@pytest.mark.parametrize("kind", ["length", "stream", "metadata"])
def test_owned_openclaw_rejects_oversized_response(endpoint, kind):
    def action(handler, release):
        handler.send_response(200)
        if kind == "length":
            handler.send_header("Content-Length", str(64 * 1024 + 1))
        elif kind == "metadata":
            handler.send_header("X-Padding", "x" * 9000)
        handler.end_headers()
        if kind == "stream":
            handler.wfile.write(b"x" * (64 * 1024 + 1))

    url, _ = endpoint(action)
    transport = upstream.Transport()
    try:
        result = transport.request(request(url), 3)
        assert result == {
            "ok": False,
            "reason": "http_metadata_oversize"
            if kind == "metadata"
            else "body_oversize",
        }
        assert transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


@pytest.mark.parametrize("status", [302, 401, 503])
def test_owned_openclaw_refuses_redirect_and_failure_without_leaking_token(
    endpoint, status
):
    target, forwarded = endpoint()

    def action(handler, release):
        handler.send_response(status, "fake-delivery-token")
        handler.send_header("Location", target)
        handler.end_headers()

    url, seen = endpoint(action)
    transport = upstream.Transport()
    try:
        result = transport.request(request(url), 3)
        assert result == {
            "ok": False,
            "reason": "http_auth" if status == 401 else "http_refused",
            "status_code": status,
        }
        assert len(seen) == 1 and forwarded == []
        assert "fake-delivery-token" not in str(result)
        assert transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


def test_owned_openclaw_slow_drip_has_parent_deadline_and_no_live_resources(
    endpoint, monkeypatch
):
    def action(handler, release):
        handler.send_response(200)
        handler.end_headers()
        while not release.wait(0.03):
            try:
                handler.wfile.write(b"x")
                handler.wfile.flush()
            except OSError:
                break

    url, seen = endpoint(action)
    transport = upstream.Transport()
    records = []
    cleanup = transport._cleanup

    def record_cleanup(rec, cancel):
        records.append(rec)
        return cleanup(rec, cancel)

    monkeypatch.setattr(transport, "_cleanup", record_cleanup)
    started = time.monotonic()
    try:
        result = transport.request(request(url, timeout=0.3), 0.5)
        assert seen and result == {"ok": False, "reason": "upstream_deadline"}
        assert time.monotonic() - started < 1.8
        assert transport.clean()
        assert len(records) == 1
        owned = records[0]
        assert owned.proc.poll() is not None
        assert all(not thread.is_alive() for thread in owned.threads)
        assert owned.proc.stdin.closed and owned.proc.stdout.closed
        assert owned.keeper == -1
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


def test_delivery_worker_stop_cancels_owned_request_and_closes_every_owner(endpoint):
    from taskpaw_v3.hub.server.delivery_worker import DeliveryWorker

    entered = threading.Event()

    def action(handler, release):
        entered.set()
        release.wait(5)

    url, seen = endpoint(action)
    calls = []

    def drain(send):
        calls.append(1)
        send(url, "fake-token", {"text": "fixture"}, 5)

    worker = DeliveryWorker(drain)
    assert worker.start()
    thread = worker._thread
    assert thread is not None and not thread.daemon
    assert entered.wait(3)
    started = time.monotonic()
    worker.request_stop()
    assert worker.cancelled()
    assert worker.stop(timeout=2)
    assert time.monotonic() - started < 2
    assert worker.stopped and worker.clean()
    assert not thread.is_alive()
    assert len(seen) == 1 and calls == [1]


def test_delivery_worker_restart_is_idempotent_and_never_overlaps(endpoint):
    from taskpaw_v3.hub.server.delivery_worker import DeliveryWorker

    url, seen = endpoint()
    sent = threading.Event()

    def drain(send):
        send(url, "fake-token", {"text": "fixture"}, 5)
        sent.set()

    worker = DeliveryWorker(drain, interval=30)
    previous = None
    try:
        for total in (1, 2, 3):
            sent.clear()
            assert worker.start()
            owner = worker._thread
            assert owner is not previous
            assert worker.start()
            assert worker._thread is owner
            assert sent.wait(3)
            assert len(seen) == total
            assert worker.stop(timeout=2)
            assert worker.stopped and worker.clean()
            assert not owner.is_alive()
            previous = owner
    finally:
        assert worker.stop(timeout=2)


def test_delivery_worker_rejects_simultaneous_sender_and_stops_new_admissions(endpoint):
    from taskpaw_v3.hub.server.delivery_worker import DeliveryWorker

    entered = threading.Event()

    def action(handler, release):
        entered.set()
        release.wait(5)

    url, seen = endpoint(action)
    worker = DeliveryWorker(lambda send: send(url, "", {"text": "fixture"}, 5))
    try:
        assert worker.start()
        assert entered.wait(3)
        with pytest.raises(upstream.UpstreamError, match="delivery_busy"):
            worker.send_payload(url, "", {"text": "second"}, 5)
        worker.request_stop()
        assert worker.stop(timeout=2)
        with pytest.raises(upstream.UpstreamError, match="helper_cancelled"):
            worker.send_payload(url, "", {"text": "third"}, 5)
        assert len(seen) == 1
    finally:
        assert worker.stop(timeout=2)


def test_delivery_worker_parent_deadline_includes_cleanup_reserve(monkeypatch):
    from taskpaw_v3.hub.server.delivery_worker import DeliveryWorker

    worker = DeliveryWorker(lambda send: None, timeout=5)
    observed = []

    def send(request, timeout):
        observed.append((request, timeout))
        return {"ok": True}

    monkeypatch.setattr(worker._transport, "request", send)
    worker.send_payload("http://127.0.0.1:1/hook", "fake-token", {"text": "a"}, 60)
    assert observed[0][0]["timeout"] == observed[0][1] == 4
    assert worker.stop(timeout=2)


def test_owned_openclaw_accepts_rendered_message_larger_than_agent_request_cap(
    endpoint,
):
    url, seen = endpoint()
    value = request(url)
    value["payload"] = {"text": "\n" * (32 * 1024)}
    transport = upstream.Transport()
    try:
        assert transport.request(value, 3) == {"ok": True}
        assert json.loads(seen[0]["body"]) == value["payload"]
        assert transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.URLError("fake-token-host"), "network_unreachable"),
        (urllib.error.URLError(TimeoutError("fake-token")), "socket_timeout"),
        (ConnectionRefusedError("fake-token"), "network_unreachable"),
        (TimeoutError("fake-token"), "socket_timeout"),
    ],
)
def test_helper_preserves_sanitized_network_failure_reason(error, reason):
    class Opener:
        def open(self, *args, **kwargs):
            raise error

    result = upstream.handle_request(request("http://127.0.0.1:1"), Opener())
    assert result == {"ok": False, "reason": reason}


@pytest.mark.parametrize("oversized", [False, True])
def test_owned_openclaw_https_retains_tls_validation_and_metadata_bound(
    endpoint, monkeypatch, oversized
):
    monkeypatch.setenv(
        "SSL_CERT_FILE",
        str(Path(__file__).parent / "fixtures" / "redirect-test-cert.pem"),
    )

    def action(handler, release):
        handler.send_response(200)
        if oversized:
            handler.send_header("X-Padding", "x" * 9000)
        handler.end_headers()

    url, seen = endpoint(action, tls=True)
    transport = upstream.Transport()
    try:
        result = transport.request(request(url), 3)
        assert result == (
            {"ok": False, "reason": "http_metadata_oversize"}
            if oversized
            else {"ok": True}
        )
        assert len(seen) == 1 and transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


def test_owned_openclaw_hanging_dns_is_terminated_by_parent_deadline(monkeypatch):
    code = (
        "import socket,time; "
        "socket.getaddrinfo=lambda *a,**k: time.sleep(30); "
        "from taskpaw_v3.hub.server import upstream_worker; "
        "raise SystemExit(upstream_worker.main())"
    )
    monkeypatch.setattr(upstream, "worker_argv", lambda: [sys.executable, "-c", code])
    transport = upstream.Transport()
    started = time.monotonic()
    try:
        result = transport.request(request("http://127.0.0.1:1"), 0.5)
        assert result == {"ok": False, "reason": "upstream_deadline"}
        assert time.monotonic() - started < 1.8
        assert transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


@pytest.mark.parametrize("kind", ["status", "events", "openclaw"])
def test_owned_request_limit_remains_bounded_by_kind(kind):
    value = request("http://127.0.0.1:1")
    value["kind"] = kind
    value["payload"] = {
        "text": "x"
        * (
            upstream.OPENCLAW_REQUEST_BYTES
            if kind == "openclaw"
            else upstream.REQUEST_BYTES
        )
    }
    transport = upstream.Transport()
    try:
        assert transport.request(value, 1) == {"ok": False, "reason": "request_invalid"}
        assert transport.clean()
    finally:
        transport.cancel()


@pytest.mark.parametrize("owned", [False, True])
def test_openclaw_does_not_accept_truncated_success_response(endpoint, owned):
    from taskpaw_v3.hub.server import openclaw

    def action(handler, release):
        handler.send_response(200)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.write(b"short")

    url, _ = endpoint(action)
    if not owned:
        with pytest.raises(ValueError, match="http_incomplete"):
            openclaw.send_payload(url, "", {"text": "fixture"}, 1)
        return
    transport = upstream.Transport()
    try:
        assert transport.request(request(url), 3) == {
            "ok": False,
            "reason": "http_incomplete",
        }
        assert transport.clean()
    finally:
        transport.cancel()
        assert transport.retry_cleanup()


def test_direct_openclaw_metadata_failure_is_settings_safe(endpoint):
    from taskpaw_v3.hub.server.settings import openclaw_test

    def action(handler, release):
        handler.send_response(200)
        handler.send_header("X-Padding", "x" * 9000)
        handler.end_headers()

    url, _ = endpoint(action)
    assert openclaw_test(url, "", timeout=1) == {"ok": False, "error": "unreachable"}


@pytest.mark.parametrize(
    ("reason", "error", "reported"),
    [
        ("http_auth", "token", "http_auth"),
        ("http_refused", "http", "http_refused"),
        ("http_incomplete", "http", "http_incomplete"),
        ("upstream_deadline", "timeout", "upstream_deadline"),
        ("socket_timeout", "timeout", "socket_timeout"),
        ("delivery_busy", "unreachable", "delivery_busy"),
        ("helper_cancelled", "unreachable", "helper_cancelled"),
        ("fake-secret-from-untrusted-error", "unreachable", "upstream_failed"),
    ],
)
def test_manual_notification_safely_maps_owned_sender_errors(reason, error, reported):
    from taskpaw_v3.hub.server.settings import openclaw_test

    def send(*args):
        raise upstream.UpstreamError(reason)

    assert openclaw_test("http://127.0.0.1:1", "fake-token", send=send) == {
        "ok": False,
        "error": error,
        "reason": reported,
    }


def test_manual_notification_route_uses_injected_sender(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.settings import HubSettings, register_hub_settings
    from taskpaw_v3.hub.server.store import HubStore

    store = HubStore(tmp_path / "hub.db")
    seen = []

    def send(*args):
        seen.append(args)

    try:
        config = HubConfig()
        app = FastAPI()
        register_hub_settings(
            app,
            HubSettings(config, config, store),
            boot_id=None,
            on_restart=None,
            send=send,
        )
        with TestClient(app) as client:
            result = client.post(
                "/config/openclaw/test",
                json={"url": "http://127.0.0.1:1/hook", "token": "fake-token"},
            )
        assert result.status_code == 200 and result.json()["ok"]
        assert seen == [
            (
                "http://127.0.0.1:1/hook",
                "fake-token",
                {"text": "TaskPaw Hub 测试通知"},
                5.0,
            )
        ]
    finally:
        store.close()


def test_manual_test_shares_worker_busy_and_cancellation_admission(endpoint):
    from taskpaw_v3.hub.server.delivery_worker import DeliveryWorker
    from taskpaw_v3.hub.server.settings import openclaw_test

    entered = threading.Event()

    def action(handler, release):
        entered.set()
        release.wait(5)

    url, seen = endpoint(action)
    worker = DeliveryWorker(lambda send: send(url, "", {"text": "fixture"}, 5))
    try:
        assert worker.start()
        assert entered.wait(3)
        assert openclaw_test(url, "", send=worker.send_payload) == {
            "ok": False,
            "error": "unreachable",
            "reason": "delivery_busy",
        }
        assert worker.stop(timeout=2)
        assert openclaw_test(url, "", send=worker.send_payload) == {
            "ok": False,
            "error": "unreachable",
            "reason": "helper_cancelled",
        }
        assert len(seen) == 1
    finally:
        assert worker.stop(timeout=2)


def test_stop_waits_for_manual_sender_cleanup_as_well_as_worker_thread(
    endpoint, monkeypatch
):
    from taskpaw_v3.hub.server.delivery_worker import DeliveryWorker
    from taskpaw_v3.hub.server.settings import openclaw_test

    entered = threading.Event()

    def action(handler, release):
        entered.set()
        release.wait(5)

    url, _ = endpoint(action)
    idle = threading.Event()
    worker = DeliveryWorker(lambda send: idle.set(), interval=30)
    cleanup = worker._transport._cleanup
    outcomes = []

    def delayed_cleanup(rec, cancel):
        threading.Event().wait(0.2)
        return cleanup(rec, cancel)

    monkeypatch.setattr(worker._transport, "_cleanup", delayed_cleanup)
    caller = threading.Thread(
        target=lambda: outcomes.append(openclaw_test(url, "", send=worker.send_payload))
    )
    try:
        assert worker.start() and idle.wait(2)
        caller.start()
        assert entered.wait(3)
        assert worker.stop(timeout=2)
        caller.join(1)
        assert not caller.is_alive()
        assert outcomes == [
            {"ok": False, "error": "unreachable", "reason": "helper_cancelled"}
        ]
        assert worker.stopped and worker.clean()
    finally:
        worker.request_stop()
        if caller.ident is not None:
            caller.join(2)
        assert worker.stop(timeout=2)
