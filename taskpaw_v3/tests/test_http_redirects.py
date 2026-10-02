"""R02: real local HTTP transports refuse redirects before exposing credentials."""

from __future__ import annotations

import http.client
import io
import json
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler, HTTPSHandler, ProxyHandler, build_opener

import pytest

from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.hub.server import film_proxy, openclaw, poller
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore

POLL_TOKEN = "fake-r02-poll-only"
NOTIFY_TOKEN = "fake-r02-notify-only"


@dataclass
class Reply:
    status: int = 200
    body: bytes = b'{"ok": true, "monitors": {}, "events": [{"id": 3, "message": "fixture"}], "films": []}'
    headers: tuple[tuple[str, str], ...] = ()
    reason: str | None = None
    delay: float = 0


class Endpoint:
    def __init__(self, server_context=None, client_context=None):
        self.default = Reply()
        self.routes = {}
        self.requests = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(1)

            def log_message(self, *args):
                pass

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                endpoint.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                reply = endpoint.routes.get(self.path, endpoint.default)
                if reply.delay:
                    time.sleep(reply.delay)
                self.send_response(reply.status, reply.reason)
                for name, value in reply.headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                try:
                    self.wfile.write(reply.body)
                except (BrokenPipeError, ConnectionResetError):
                    # Immediate refusal may close before this disposable body.
                    pass

            do_GET = respond
            do_POST = respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # server_close joins request handlers as well as the serving thread.
        self.server.daemon_threads = False
        if server_context is not None:
            self.server.socket = server_context.wrap_socket(
                self.server.socket, server_side=True
            )
        self.client_context = client_context
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.start()
        scheme = "https" if server_context is not None else "http"
        self.base = f"{scheme}://127.0.0.1:{self.server.server_port}"

    def check_ready(self):
        if self.client_context is None:
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.server.server_port, timeout=1
            )
        else:
            connection = http.client.HTTPSConnection(
                "127.0.0.1",
                self.server.server_port,
                timeout=1,
                context=self.client_context,
            )
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        finally:
            connection.close()
        self.requests.clear()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


@pytest.fixture(scope="session")
def tls_contexts():
    """Public disposable fixture key, never a production credential.

    Certificate valid 2026-10-01 through 2036-09-28. Regenerate offline with:
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=localhost
      -addext subjectAltName=DNS:localhost,IP:127.0.0.1
      -keyout taskpaw_v3/tests/fixtures/redirect-test-key.pem
      -out taskpaw_v3/tests/fixtures/redirect-test-cert.pem
    Runtime tests need only stdlib ssl and trust only this local test CA.
    """
    fixtures = Path(__file__).parent / "fixtures"
    client = ssl.create_default_context(cafile=str(fixtures / "redirect-test-cert.pem"))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(
        fixtures / "redirect-test-cert.pem", fixtures / "redirect-test-key.pem"
    )
    return server, client


@pytest.fixture
def endpoints(monkeypatch, tls_contexts):
    # These fixtures must never route through the operator's outbound proxy.
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    created = []

    def make(tls=False):
        endpoint = Endpoint(*tls_contexts) if tls else Endpoint()
        created.append(endpoint)
        endpoint.check_ready()
        return endpoint

    yield make
    for endpoint in reversed(created):
        endpoint.close()


@pytest.fixture
def trusted_transport(monkeypatch, tls_contexts):
    """Real urllib handlers; only test trust/proxy discovery are replaced."""
    for module in (poller, openclaw, film_proxy):
        opener = build_opener(
            NoRedirectHandler(), ProxyHandler({}), HTTPSHandler(context=tls_contexts[1])
        )
        monkeypatch.setattr(module, "_opener", opener)


@pytest.fixture
def store(tmp_path):
    value = HubStore(tmp_path / "hub.db")
    yield value
    value.close()


def make_poller(store, source):
    sid = store.add_server("fixture-agent", "127.0.0.1", source.server.server_port)
    store.set_config("last_event_ids", json.dumps({str(sid): 2}))
    value = Poller(
        store,
        source.base + "/notify",
        get_active=lambda: True,
        get_token=lambda: NOTIFY_TOKEN,
        get_polling_token=lambda: POLL_TOKEN,
        http_timeout=1,
    )
    return value, store.list_servers()[0]


@pytest.mark.parametrize("consumer", ["status", "events", "notify"])
def test_real_cross_port_redirect_never_requests_target(endpoints, store, consumer):
    source, target = endpoints(), endpoints()
    source.default = Reply(302, headers=(("Location", target.base + "/target"),))
    value, server = make_poller(store, source)
    failure = None
    if consumer == "status":
        result = value.fetch_status(server)
    elif consumer == "events":
        result = value.fetch_events(server)
    else:
        try:
            openclaw.send_payload(
                source.base + "/notify", NOTIFY_TOKEN, {"text": "fixture"}, timeout=1
            )
        except HTTPError as error:
            failure = error
    assert target.requests == []
    assert len(source.requests) == 1
    expected_token = NOTIFY_TOKEN if consumer == "notify" else POLL_TOKEN
    assert source.requests[0]["auth"] == f"Bearer {expected_token}"
    if consumer == "status":
        assert result == (False, None)
    elif consumer == "events":
        assert result == []
        assert value.snapshot_acks() == {server["id"]: 2}
    else:
        assert failure is not None and failure.code == 302
        failure.close()


def test_redirect_reason_location_never_enter_logs_or_outbox(endpoints, store, caplog):
    source, target = endpoints(), endpoints()
    source.default = Reply(
        302,
        body=b"fake-r02-upstream-body",
        headers=(("Location", f"javascript:{NOTIFY_TOKEN}"),),
        reason=f"{POLL_TOKEN} {NOTIFY_TOKEN}",
    )
    value, server = make_poller(store, source)
    assert value.fetch_events(server) == []
    delivery = store.enqueue_delivery("fixture-agent", "event", '{"text": "fixture"}')
    value.drain_outbox()
    row = store._conn.execute(
        "SELECT id, delivery_state, attempts, last_error FROM delivery_outbox"
    ).fetchone()
    assert row[:3] == (delivery, "failed", 1)
    assert row[3] == "HTTP Error 302: HTTP redirect refused"
    for marker in (POLL_TOKEN, NOTIFY_TOKEN, "javascript:", "fake-r02-upstream-body"):
        assert marker not in caplog.text + row[3]
    assert target.requests == []
    assert value.snapshot_acks() == {server["id"]: 2}
    assert json.loads(store.get_config("last_event_ids")) == {str(server["id"]): 2}


REDIRECT_CODES = (301, 302, 303, 307, 308)
CONSUMERS = ("status", "events", "fallback", "notify", "films", "run-films")


def invoke_refused(
    consumer, value, server, status, poll_token=POLL_TOKEN, notify_token=NOTIFY_TOKEN
):
    if consumer == "status":
        assert value.fetch_status(server) == (False, None)
    elif consumer in ("events", "fallback"):
        assert value.fetch_events(server) == []
    elif consumer == "notify":
        with pytest.raises(HTTPError) as caught:
            openclaw.send_payload(
                value.openclaw_url, notify_token, {"text": "fixture"}, timeout=1
            )
        error = caught.value
        assert error.code == status
        assert error.msg == error.reason == "HTTP redirect refused"
        assert error.filename == "" and error.headers is None
        assert str(error) == f"HTTP Error {status}: HTTP redirect refused"
        assert error.__cause__ is None and error.__suppress_context__
        error.close()
    else:
        with pytest.raises(film_proxy.FilmProxyError) as caught:
            film_proxy.fetch_agent_film_page(
                server,
                consumer,
                {"name": "fixture"},
                {"Authorization": f"Bearer {poll_token}"} if poll_token else {},
                timeout=1,
            )
        assert caught.value.status_code == 502
        assert caught.value.code == "agent_request_failed"
        assert str(caught.value) == "Agent film request failed."


@pytest.mark.parametrize("status", REDIRECT_CODES)
@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize(
    "location_kind", ["port", "host", "scheme-relative", "relative", "loop", "https"]
)
def test_real_redirect_matrix(
    endpoints, store, trusted_transport, caplog, status, consumer, location_kind
):
    source, target = endpoints(), endpoints(tls=location_kind == "https")
    # The same trusted urllib transport can reach the target, including TLS.
    # Certificate rejection cannot supply a false zero-request result.
    with openclaw._opener.open(target.base + "/health", timeout=1) as response:
        assert response.status == 200
        response.read()
    target.requests.clear()
    value, server = make_poller(store, source)
    path = (
        "/events"
        if consumer == "fallback"
        else "/events?ack=2"
        if consumer == "events"
        else "/status"
        if consumer == "status"
        else "/notify"
        if consumer == "notify"
        else f"/monitors/{consumer}?name=fixture"
    )
    locations = {
        "port": target.base + "/target",
        "host": source.base.replace("127.0.0.1", "localhost") + "/target",
        "scheme-relative": "//127.0.0.1:" + str(target.server.server_port) + "/target",
        "relative": "/target",
        "loop": source.base + path,
        "https": target.base + "/target",
    }
    source.default = Reply(
        status,
        body=b"fake-r02-upstream-body",
        headers=(("Location", locations[location_kind]),),
        reason=f"{POLL_TOKEN} {NOTIFY_TOKEN}",
    )
    if consumer == "fallback":
        source.routes["/events?ack=2"] = Reply(404)
    invoke_refused(consumer, value, server, status)
    assert target.requests == []
    assert [request["path"] for request in source.requests] == (
        ["/events?ack=2", "/events"] if consumer == "fallback" else [path]
    )
    token = NOTIFY_TOKEN if consumer == "notify" else POLL_TOKEN
    assert all(request["auth"] == f"Bearer {token}" for request in source.requests)
    assert source.requests[-1]["method"] == ("POST" if consumer == "notify" else "GET")
    assert value.snapshot_acks() == {server["id"]: 2}
    assert json.loads(store.get_config("last_event_ids")) == {str(server["id"]): 2}
    assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    assert store._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone() == (
        0,
    )
    for marker in (POLL_TOKEN, NOTIFY_TOKEN, "fake-r02-upstream-body", "/target"):
        assert marker not in caplog.text


@pytest.mark.parametrize("status", REDIRECT_CODES)
def test_real_https_source_refuses_http_target(
    endpoints, store, trusted_transport, status
):
    source, target = endpoints(tls=True), endpoints()
    value, server = make_poller(store, source)
    # A normal HTTPS POST works before the redirect is enabled.
    openclaw.send_payload(
        value.openclaw_url, NOTIFY_TOKEN, {"text": "fixture"}, timeout=1
    )
    assert source.requests[-1]["method"] == "POST"
    assert source.requests[-1]["auth"] == f"Bearer {NOTIFY_TOKEN}"
    source.requests.clear()
    source.default = Reply(status, headers=(("Location", target.base + "/target"),))
    invoke_refused("notify", value, server, status)
    assert target.requests == []
    assert len(source.requests) == 1
    assert source.requests[0]["method"] == "POST"


@pytest.mark.parametrize("status", REDIRECT_CODES)
@pytest.mark.parametrize("close_fails", [False, True])
def test_redirect_response_closed_without_reading_or_header_parsing(
    status, close_fails
):
    class UnreadHeaders(Message):
        def __contains__(self, name):
            raise AssertionError("Redirect headers must not be inspected")

        def __getitem__(self, name):
            raise AssertionError("Redirect headers must not be parsed")

    headers = UnreadHeaders()
    headers["Location"] = f"javascript:{NOTIFY_TOKEN}"
    response = io.BytesIO(b"fake-r02-upstream-body")
    response.code = status
    response.msg = NOTIFY_TOKEN
    response.info = lambda: headers
    response.geturl = lambda: "http://fixture.invalid/source"
    counts = {"read": 0, "close": 0, "requests": 0}

    def read(*args):
        counts["read"] += 1
        raise AssertionError("Redirect response must not be drained")

    def close():
        counts["close"] += 1
        if close_fails:
            raise OSError(NOTIFY_TOKEN)
        io.BytesIO.close(response)

    response.read = read
    response.close = close

    class Transport(BaseHandler):
        handler_order = 100

        def http_open(self, request):
            counts["requests"] += 1
            return response

    opener = build_opener(NoRedirectHandler(), ProxyHandler({}), Transport())
    try:
        with pytest.raises(HTTPError) as caught:
            opener.open("http://fixture.invalid/source", timeout=1)
        assert counts == {"read": 0, "close": 1, "requests": 1}
        error = caught.value
        assert error.code == status
        assert error.headers is None and error.filename == ""
        assert error.reason == "HTTP redirect refused"
        assert error.__cause__ is None and error.__suppress_context__
        assert NOTIFY_TOKEN not in str(error) + repr(error)
        assert response.closed or close_fails
        error.close()
        assert counts["close"] == 1
    finally:
        io.BytesIO.close(response)


@pytest.mark.parametrize("status", REDIRECT_CODES)
@pytest.mark.parametrize(
    "headers_kind", ["missing", "malformed", "unsupported", "duplicate", "uri"]
)
def test_real_hostile_redirect_fields_are_fixed_failures(
    endpoints, store, trusted_transport, caplog, status, headers_kind
):
    source, target = endpoints(), endpoints()
    locations = {
        "missing": (),
        "malformed": (("Location", f"http://[{NOTIFY_TOKEN}"),),
        "unsupported": (("Location", f"javascript:{NOTIFY_TOKEN}"),),
        "duplicate": (
            ("Location", target.base + "/target"),
            ("Location", NOTIFY_TOKEN),
        ),
        "uri": (("URI", target.base + "/target?" + NOTIFY_TOKEN),),
    }
    source.default = Reply(
        status,
        body=b"fake-r02-upstream-body",
        headers=locations[headers_kind],
        reason=f"{POLL_TOKEN} {NOTIFY_TOKEN}",
    )
    value, server = make_poller(store, source)
    for consumer in ("status", "events", "notify", "films", "run-films"):
        invoke_refused(consumer, value, server, status)
    delivery = store.enqueue_delivery("fixture-agent", "event", '{"text": "fixture"}')
    value.drain_outbox()
    row = store._conn.execute(
        "SELECT id, delivery_state, attempts, last_error FROM delivery_outbox"
    ).fetchone()
    assert row == (delivery, "failed", 1, f"HTTP Error {status}: HTTP redirect refused")
    assert len(source.requests) == 6 and target.requests == []
    assert value.snapshot_acks() == {server["id"]: 2}
    for marker in (
        POLL_TOKEN,
        NOTIFY_TOKEN,
        "javascript:",
        "/target",
        "fake-r02-upstream-body",
    ):
        assert marker not in caplog.text + row[3]


@pytest.mark.parametrize("status", REDIRECT_CODES)
@pytest.mark.parametrize("fallback", [False, True])
def test_redirect_keeps_status_and_ack_until_direct_recovery(
    endpoints, store, status, fallback
):
    source, target = endpoints(), endpoints()
    source.default = Reply(body=b'{"monitors": {}, "version": "last-good"}')
    value, server = make_poller(store, source)
    sid = server["id"]
    value._poll_server(server, active=False)
    before = value.snapshot_statuses()[sid]
    status_rows = store._conn.execute("SELECT * FROM status_log").fetchall()
    redirect = Reply(status, headers=(("Location", target.base + "/target"),))
    source.default = redirect
    if fallback:
        source.routes["/events?ack=2"] = Reply(404)
    source.requests.clear()
    value._poll_server(server, active=True)
    after = value.snapshot_statuses()[sid]
    assert after["online"] is False
    assert after["snapshot"] == before["snapshot"]
    assert after["last_seen"] == before["last_seen"]
    assert store._conn.execute("SELECT * FROM status_log").fetchall() == status_rows
    assert value.snapshot_acks() == {sid: 2}
    assert json.loads(store.get_config("last_event_ids")) == {str(sid): 2}
    assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    assert store._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone() == (
        0,
    )
    assert [r["path"] for r in source.requests] == (
        ["/status", "/events?ack=2", "/events"]
        if fallback
        else ["/status", "/events?ack=2"]
    )
    assert target.requests == []

    source.default = Reply()
    source.routes.clear()
    if fallback:
        source.routes["/events?ack=2"] = Reply(404)
    value._poll_server(server, active=True)
    assert value.snapshot_statuses()[sid]["online"] is True
    assert value.snapshot_acks() == {sid: 3}
    assert json.loads(store.get_config("last_event_ids")) == {str(sid): 3}
    assert store._conn.execute("SELECT event_id FROM events").fetchall() == [(3,)]
    assert store._conn.execute(
        "SELECT delivery_state, dedupe_key FROM delivery_outbox"
    ).fetchall() == [("pending", f"{sid}:3")]


def outbox_row(store):
    columns = (
        "id",
        "payload_json",
        "delivery_state",
        "attempts",
        "last_error",
        "next_attempt_at",
        "dedupe_key",
    )
    row = store._conn.execute(
        "SELECT " + ",".join(columns) + " FROM delivery_outbox"
    ).fetchone()
    return dict(zip(columns, row)) if row else None


def make_due(store):
    store._conn.execute(
        "UPDATE delivery_outbox SET next_attempt_at=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    store._conn.commit()


@pytest.mark.parametrize("status", REDIRECT_CODES)
def test_redirect_outbox_retains_row_retries_then_recovers(endpoints, store, status):
    source, target = endpoints(), endpoints()
    source.default = Reply(status, headers=(("Location", target.base + "/target"),))
    value, server = make_poller(store, source)
    payload = '{"text": "fixture"}'
    delivery = store.enqueue_delivery(
        "fixture-agent", "event", payload, dedupe_key="fixture:3"
    )
    for attempt in (1, 2):
        make_due(store)
        value.drain_outbox()
        row = outbox_row(store)
        assert (row["id"], row["payload_json"], row["dedupe_key"]) == (
            delivery,
            payload,
            "fixture:3",
        )
        assert (row["delivery_state"], row["attempts"]) == ("failed", attempt)
        assert row["last_error"] == f"HTTP Error {status}: HTTP redirect refused"
        assert datetime.fromisoformat(row["next_attempt_at"]) > datetime.now(
            timezone.utc
        )
        assert value.snapshot_acks() == {server["id"]: 2}
        assert json.loads(store.get_config("last_event_ids")) == {str(server["id"]): 2}
        assert target.requests == []
    source.default = Reply(204, body=b"")
    make_due(store)
    value.drain_outbox()
    assert outbox_row(store) is None
    assert len(source.requests) == 3 and target.requests == []
    assert all(r["method"] == "POST" for r in source.requests)
    assert all(
        r["body"] == json.dumps({"text": "fixture"}).encode() for r in source.requests
    )


@pytest.mark.parametrize("status", REDIRECT_CODES)
def test_redirect_at_attempt_cap_has_one_safe_dead_letter_alert(
    endpoints, store, status
):
    source, target = endpoints(), endpoints()
    source.default = Reply(
        status,
        headers=(("Location", target.base + "/target?" + NOTIFY_TOKEN),),
        reason=NOTIFY_TOKEN,
    )
    value, _server = make_poller(store, source)
    store.enqueue_delivery("fixture-agent", "event", '{"text": "fixture"}', attempts=9)
    alerts = []
    value.emit_local_alert = alerts.append
    value.drain_outbox()
    value.drain_outbox()
    row = outbox_row(store)
    assert (row["delivery_state"], row["attempts"]) == ("dead_letter", 10)
    assert row["last_error"] == f"HTTP Error {status}: HTTP redirect refused"
    assert len(alerts) == 1 and "HTTP redirect refused" in alerts[0]
    assert NOTIFY_TOKEN not in alerts[0] + row["last_error"]
    assert len(source.requests) == 1 and target.requests == []


def test_redirect_device_and_notification_do_not_stall_healthy_device(endpoints, store):
    bad, healthy, target = endpoints(), endpoints(), endpoints()
    bad.default = Reply(302, headers=(("Location", target.base + "/target"),))
    value, bad_server = make_poller(store, bad)
    good_id = store.add_server("healthy-agent", "127.0.0.1", healthy.server.server_port)
    value.last_event_ids[good_id] = 2
    value._persist_acks()
    first = store.enqueue_delivery(
        "fixture-agent", "event", '{"text": "prior"}', dedupe_key="prior"
    )
    value.poll_once()
    value.poll_once()
    assert value.snapshot_acks() == {bad_server["id"]: 2, good_id: 3}
    assert json.loads(store.get_config("last_event_ids")) == {
        str(bad_server["id"]): 2,
        str(good_id): 3,
    }
    assert store._conn.execute("SELECT server_id, event_id FROM events").fetchall() == [
        (good_id, 3)
    ]
    rows = store._conn.execute(
        "SELECT id, delivery_state, attempts, dedupe_key FROM delivery_outbox ORDER BY id"
    ).fetchall()
    assert rows == [
        (first, "failed", 1, "prior"),
        (first + 1, "failed", 1, f"{good_id}:3"),
    ]
    assert store._conn.execute("SELECT server_id FROM status_log").fetchall() == [
        (good_id,),
        (good_id,),
    ]
    assert [r["path"] for r in healthy.requests] == [
        "/status",
        "/events?ack=2",
        "/status",
        "/events?ack=3",
    ]
    assert target.requests == []


@pytest.mark.parametrize("tls", [False, True])
@pytest.mark.parametrize("status", [200, 201, 204])
@pytest.mark.parametrize("token", ["", NOTIFY_TOKEN])
def test_direct_notification_keeps_method_body_auth_and_timeout(
    endpoints, trusted_transport, monkeypatch, tls, status, token
):
    source = endpoints(tls=tls)
    source.default = Reply(status, body=b"")
    real_open = openclaw._opener.open
    timeouts = []

    def observed(request, *, timeout):
        timeouts.append(timeout)
        return real_open(request, timeout=timeout)

    monkeypatch.setattr(openclaw._opener, "open", observed)
    payload = {"text": "fixture 翻译"}
    openclaw.send_payload(source.base + "/notify", token, payload, timeout=0.7)
    assert timeouts == [0.7]
    assert len(source.requests) == 1
    assert source.requests[0] == {
        "method": "POST",
        "path": "/notify",
        "auth": f"Bearer {token}" if token else None,
        "body": json.dumps(payload).encode("utf-8"),
    }


@pytest.mark.parametrize("token", ["", POLL_TOKEN])
def test_direct_status_events_fallback_and_films_keep_request_contract(
    endpoints, store, monkeypatch, token
):
    source = endpoints()
    value, server = make_poller(store, source)
    value.get_polling_token = lambda: token
    value.http_timeout = 0.7
    timeouts = []
    for module in (poller, film_proxy):
        real_open = module._opener.open

        def observed(request, *, timeout, _open=real_open):
            timeouts.append(timeout)
            return _open(request, timeout=timeout)

        monkeypatch.setattr(module._opener, "open", observed)
    assert value.fetch_status(server)[0] is True
    assert value.fetch_events(server) == [{"id": 3, "message": "fixture"}]
    source.routes["/events?ack=2"] = Reply(404)
    source.routes["/events"] = Reply(body=b'[{"id":3,"message":"legacy"}]')
    assert value.fetch_events(server) == [{"id": 3, "message": "legacy"}]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for resource in ("films", "run-films"):
        assert (
            film_proxy.fetch_agent_film_page(
                server, resource, {"name": "fixture"}, headers, timeout=0.7
            )["films"]
            == []
        )
    assert timeouts == [0.7] * 6
    assert [r["path"] for r in source.requests] == [
        "/status",
        "/events?ack=2",
        "/events?ack=2",
        "/events",
        "/monitors/films?name=fixture",
        "/monitors/run-films?name=fixture",
    ]
    assert all(
        r["auth"] == (f"Bearer {token}" if token else None) for r in source.requests
    )
    assert all(r["method"] == "GET" for r in source.requests)


@pytest.mark.parametrize("status", REDIRECT_CODES)
@pytest.mark.parametrize("consumer", CONSUMERS)
def test_empty_token_still_refuses_redirect(endpoints, store, status, consumer):
    source, target = endpoints(), endpoints()
    source.default = Reply(status, headers=(("Location", target.base + "/target"),))
    if consumer == "fallback":
        source.routes["/events?ack=2"] = Reply(404)
    value, server = make_poller(store, source)
    value.get_polling_token = lambda: ""
    invoke_refused(consumer, value, server, status, poll_token="", notify_token="")
    assert target.requests == []
    assert len(source.requests) == (2 if consumer == "fallback" else 1)
    assert all(r["auth"] is None for r in source.requests)


@pytest.mark.parametrize("consumer", ["status", "events", "notify", "films"])
def test_direct_response_timeout_keeps_existing_failure_mapping(
    endpoints, store, consumer
):
    source = endpoints()
    source.default = Reply(delay=0.08)
    value, server = make_poller(store, source)
    value.http_timeout = 0.02
    if consumer == "status":
        assert value.fetch_status(server) == (False, None)
    elif consumer == "events":
        assert value.fetch_events(server) == []
    elif consumer == "notify":
        with pytest.raises((TimeoutError, URLError)):
            openclaw.send_payload(
                value.openclaw_url, NOTIFY_TOKEN, {"text": "fixture"}, timeout=0.02
            )
    else:
        with pytest.raises(film_proxy.FilmProxyError) as caught:
            film_proxy.fetch_agent_film_page(
                server, "films", {"name": "fixture"}, {}, timeout=0.02
            )
        assert caught.value.status_code == 504
        assert caught.value.code == "agent_timeout"
    assert len(source.requests) == 1
    assert value.snapshot_acks() == {server["id"]: 2}
