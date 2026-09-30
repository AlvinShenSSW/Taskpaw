"""#210: on-demand reads are isolated from polling and never forward secrets."""

import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.hub.server.app import create_hub_app
from taskpaw_v3.hub.server.store import HubStore


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from taskpaw_v3.hub.server import film_proxy

    store = HubStore(tmp_path / "hub.db")
    sid = store.add_server("agent", "::1", 5680)
    app, service = create_hub_app(
        HubConfig(
            self_monitor=False, api_token="client-token", polling_token="poll-token"
        ),
        store,
    )
    snapshots = {sid: {"online": True, "snapshot": {"version": "3.9.7"}}}
    monkeypatch.setattr(service.poller, "snapshot_statuses", lambda: snapshots)
    opener = Mock()
    monkeypatch.setattr(film_proxy, "_opener", opener)
    yield TestClient(app), store, service, sid, snapshots, opener
    store.close()


class Response(io.BytesIO):
    def __init__(self, body=b'{"films": [], "additive": {"x": 1}}', status=200):
        super().__init__(body)
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


AUTH = {"Authorization": "Bearer client-token"}


@pytest.mark.parametrize("resource", ["films", "run-films"])
@pytest.mark.parametrize("ip", ["::1", "10.0.0.2"])
def test_hub_films_target_payload_token_and_no_side_effects(hub, resource, ip):
    client, store, service, sid, snapshots, opener = hub
    store.update_server(sid, ip=ip)
    before = (
        service.poller.snapshot_acks(),
        json.dumps(snapshots),
        store.latest_statuses(),
        store.recent_events(),
    )
    for token in (None, "rotated-token", ""):
        if token is not None:
            store.set_config("polling_token", token)
        response = Response()
        opener.open.return_value = response
        result = client.get(
            f"/servers/{sid}/monitors/{resource}",
            params={"name": "AV/翻译 & #?", "size": 99},
            headers=AUTH,
        )
        assert result.status_code == 200
        assert result.json() == {"films": [], "additive": {"x": 1}}
        request = opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        assert url.netloc == ("[::1]:5680" if ip == "::1" else "10.0.0.2:5680")
        assert url.path == f"/monitors/{resource}"
        expected = {"name": ["AV/翻译 & #?"], "size": ["50"]}
        if resource == "run-films":
            expected.update(filter=["done"], page=["1"])
        assert parse_qs(url.query) == expected
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == (
            f"Bearer {token if token is not None else 'poll-token'}"
            if token != ""
            else None
        )
        assert opener.open.call_args.kwargs == {"timeout": service.poller.http_timeout}
        assert response.closed and response.read_sizes == [1024 * 1024 + 1]
    assert opener.open.call_count == 3
    assert before == (
        service.poller.snapshot_acks(),
        json.dumps(snapshots),
        store.latest_statuses(),
        store.recent_events(),
    )


@pytest.mark.parametrize("resource", ["films", "run-films"])
@pytest.mark.parametrize(
    "condition,status,code",
    [
        ("unknown", 404, "unknown_server"),
        ("disabled", 409, "agent_disabled"),
        ("offline", 503, "agent_offline"),
        ("missing", 503, "agent_offline"),
        ("invalid", 400, "invalid_parameters"),
        ("invalid_sid", 400, "invalid_parameters"),
    ],
)
def test_hub_films_preflight_and_auth_first(hub, resource, condition, status, code):
    client, store, _, sid, snapshots, opener = hub
    params = {"name": "task"}
    if condition == "unknown":
        sid = 999
    elif condition == "disabled":
        store.set_server_enabled(sid, False)
    elif condition == "offline":
        snapshots[sid]["online"] = False
    elif condition == "missing":
        snapshots.clear()
    elif condition == "invalid":
        params = {"page": "bad"}
    elif condition == "invalid_sid":
        sid = "bad"
    path = f"/servers/{sid}/monitors/{resource}"
    assert client.get(path, params=params).status_code == 401
    result = client.get(path, params=params, headers=AUTH)
    assert result.status_code == status
    assert result.json()["error"] == code
    opener.open.assert_not_called()


@pytest.mark.parametrize("resource", ["films", "run-films"])
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"name": " "},
        *({"name": "x", "page": v} for v in (0, -1, "true", "x", "1.5")),
        *({"name": "x", "size": v} for v in ("x", "false", "1.5")),
    ],
)
def test_hub_films_bad_parameters(hub, resource, params):
    client, _, _, sid, _, opener = hub
    result = client.get(
        f"/servers/{sid}/monitors/{resource}", params=params, headers=AUTH
    )
    assert result.status_code == 400
    assert result.json() == {
        "error": "invalid_parameters",
        "detail": "Invalid film list parameters.",
    }
    opener.open.assert_not_called()


@pytest.mark.parametrize(
    "status,expected,code",
    [
        (404, 404, "film_list_unavailable"),
        (401, 502, "agent_auth_failed"),
        (403, 502, "agent_auth_failed"),
        (302, 502, "agent_request_failed"),
        (500, 502, "agent_request_failed"),
    ],
)
def test_hub_films_http_errors_closed_sanitized_no_retry(
    hub, caplog, status, expected, code
):
    client, _, _, sid, _, opener = hub
    body = io.BytesIO(b"SECRET-UPSTREAM-BODY")
    opener.open.side_effect = HTTPError(
        "http://secret-url", status, "secret-error", {}, body
    )
    result = client.get(f"/servers/{sid}/monitors/films?name=task", headers=AUTH)
    assert result.status_code == expected and result.json()["error"] == code
    assert body.closed
    assert opener.open.call_count == 1
    for secret in (
        "SECRET-UPSTREAM-BODY",
        "secret-url",
        "secret-error",
        "client-token",
        "poll-token",
    ):
        assert secret not in result.text + caplog.text


@pytest.mark.parametrize(
    "error,status,code",
    [
        (TimeoutError("secret"), 504, "agent_timeout"),
        (URLError(TimeoutError("secret")), 504, "agent_timeout"),
        (URLError("secret"), 503, "agent_offline"),
        (OSError("secret"), 503, "agent_offline"),
    ],
)
def test_hub_films_transport_errors(hub, error, status, code):
    client, _, _, sid, _, opener = hub
    opener.open.side_effect = error
    result = client.get(f"/servers/{sid}/monitors/films?name=x", headers=AUTH)
    assert result.status_code == status and result.json()["error"] == code
    assert "secret" not in result.text
    assert opener.open.call_count == 1


@pytest.mark.parametrize(
    "body,status,code",
    [
        (b"\xff", 200, "invalid_agent_response"),
        (b"{", 200, "invalid_agent_response"),
        (b"[]", 200, "invalid_agent_response"),
        (b"null", 200, "invalid_agent_response"),
        (b'{"n": NaN}', 200, "invalid_agent_response"),
        (b'{"n": Infinity}', 200, "invalid_agent_response"),
        (b'{"n": -Infinity}', 200, "invalid_agent_response"),
        (b" " * (1024 * 1024 + 1), 200, "invalid_agent_response"),
        (b"{}", 204, "agent_request_failed"),
        (b"{}", 302, "agent_request_failed"),
    ],
    ids=[
        "utf8",
        "json",
        "array",
        "null",
        "nan",
        "infinity",
        "negative-infinity",
        "oversize",
        "204",
        "302",
    ],
)
def test_hub_films_invalid_bodies_close(hub, body, status, code):
    client, _, _, sid, _, opener = hub
    response = Response(body, status)
    opener.open.return_value = response
    result = client.get(f"/servers/{sid}/monitors/films?name=x", headers=AUTH)
    assert result.status_code == 502 and result.json()["error"] == code
    assert response.closed
    assert opener.open.call_count == 1


def test_film_proxy_redirect_never_issues_second_request():
    from email.message import Message
    from urllib.request import BaseHandler, build_opener

    from taskpaw_v3.hub.server import film_proxy

    calls = []

    class RedirectingTransport(BaseHandler):
        handler_order = 100

        def http_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            headers["Location"] = "http://other.invalid/steal"
            result = Response(b"secret", 302)
            result.code = 302
            result.msg = "Found"
            result.info = lambda: headers
            result.geturl = lambda: request.full_url
            return result

    # Exercise urllib's real redirect/error handler chain without a bound socket.
    opener = build_opener(film_proxy._NoRedirect(), RedirectingTransport())
    with pytest.raises(HTTPError) as caught:
        opener.open("http://agent.invalid/monitors/films?name=x")
    caught.value.close()
    assert calls == ["http://agent.invalid/monitors/films?name=x"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"name":"\\ud800"}',
        b'{"\\udfff":"value"}',
        b'{"nested":' + b"[" * 15000 + b"0" + b"]" * 15000 + b"}",
        b'{"percent":1e999}',
    ],
    ids=["surrogate-value", "surrogate-key", "deep-nesting", "overflow-float"],
)
def test_210_invalid_json_unicode_and_depth_are_sanitized(hub, body):
    client, _, _, sid, _, opener = hub
    response = Response(body)
    opener.open.return_value = response
    result = client.get(f"/servers/{sid}/monitors/films?name=x", headers=AUTH)
    assert result.status_code == 502
    assert result.json() == {
        "error": "invalid_agent_response",
        "detail": "Agent returned an invalid film list response.",
    }
    assert response.closed
