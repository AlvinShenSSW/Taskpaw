"""#286: fixed, authenticated and bounded Hub workflow reads, including images."""

import io
import json
from email.message import Message
from http.client import HTTPException
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import BaseHandler, build_opener

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.hub.server.app import create_hub_app, create_hub_control_app
from taskpaw_v3.hub.server.store import HubStore

AUTH = {"Authorization": "Bearer client-token"}
RESOURCES = ("films", "plans", "films/movie", "films/movie/poster", "films/movie/cover")


class Response(io.BytesIO):
    def __init__(
        self,
        body=b'{"films": [], "additive": {"x": 1}}',
        status=200,
        content_type="application/json",
    ):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from taskpaw_v3.hub.server import workflow_proxy

    store = HubStore(tmp_path / "hub.db")
    sid = store.add_server("agent", "::1", 5680)
    config = HubConfig(
        self_monitor=False, api_token="client-token", polling_token="poll-token"
    )
    app, service = create_hub_app(config, store)
    snapshots = {sid: {"online": True, "snapshot": {"version": "3.9.18"}}}
    monkeypatch.setattr(service.poller, "snapshot_statuses", lambda: snapshots)
    opener = Mock()
    monkeypatch.setattr(workflow_proxy, "_opener", opener)
    yield TestClient(app), store, service, sid, snapshots, opener, config
    store.close()


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize("ip", ["::1", "10.0.0.2"])
def test_workflow_routes_target_current_token_and_no_polling_side_effects(
    hub, resource, ip
):
    client, store, service, sid, snapshots, opener, _ = hub
    store.update_server(sid, ip=ip)
    before = (
        service.poller.snapshot_acks(),
        json.dumps(snapshots),
        store.latest_statuses(),
        store.recent_events(),
    )
    image = resource.endswith(("/poster", "/cover"))
    body = b"image bytes" if image else b'{"films": [], "additive": {"x": 1}}'
    for token in (None, "rotated-token", ""):
        if token is not None:
            store.set_config("polling_token", token)
        upstream = Response(
            body, content_type="image/png" if image else "application/json"
        )
        opener.open.return_value = upstream
        result = client.get(
            f"/servers/{sid}/workflow/{resource}",
            headers=AUTH,
            params={"url": "http://other.invalid/control", "token": "browser-value"},
        )
        assert result.status_code == 200
        assert result.content == body
        assert result.headers["content-type"] == (
            "image/png" if image else "application/json"
        )
        request = opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        assert url.netloc == ("[::1]:5680" if ip == "::1" else "10.0.0.2:5680")
        assert url.path == f"/workflow/{resource}"
        assert not url.query and not url.fragment
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == (
            f"Bearer {token if token is not None else 'poll-token'}"
            if token != ""
            else None
        )
        assert opener.open.call_args.kwargs == {"timeout": service.poller.http_timeout}
        assert upstream.read_sizes == [(8 if image else 1) * 1024 * 1024 + 1]
        assert upstream.closed
    assert before == (
        service.poller.snapshot_acks(),
        json.dumps(snapshots),
        store.latest_statuses(),
        store.recent_events(),
    )


@pytest.mark.parametrize("resource", RESOURCES)
@pytest.mark.parametrize(
    "condition,status,code",
    [
        ("unknown", 404, "unknown_server"),
        ("disabled", 409, "agent_disabled"),
        ("offline", 503, "agent_offline"),
        ("missing", 503, "agent_offline"),
        ("invalid_sid", 400, "invalid_parameters"),
    ],
)
def test_workflow_preflight_and_auth_first(hub, resource, condition, status, code):
    client, store, _, sid, snapshots, opener, _ = hub
    if condition == "unknown":
        sid = 999
    elif condition == "disabled":
        store.set_server_enabled(sid, False)
    elif condition == "offline":
        snapshots[sid]["online"] = False
    elif condition == "missing":
        snapshots.clear()
    else:
        sid = "bad"
    path = f"/servers/{sid}/workflow/{resource}"
    assert client.get(path).status_code == 401
    result = client.get(path, headers=AUTH)
    assert result.status_code == status and result.json()["error"] == code
    opener.open.assert_not_called()


def test_workflow_control_surface_keeps_its_guard_and_only_five_get_routes(hub):
    client, store, service, sid, _, opener, config = hub
    active = [True]
    control = TestClient(
        create_hub_control_app(
            config,
            store,
            service,
            control_token="local-token",
            control_active=lambda: active[0],
        )
    )
    for surface, headers in (
        (client, AUTH),
        (control, {"Authorization": "Bearer local-token"}),
    ):
        routes = [r for r in surface.app.routes if "/workflow/" in r.path]
        assert len(routes) == 5 and all(r.methods == {"GET"} for r in routes)
        for resource in RESOURCES:
            opener.open.return_value = Response(
                content_type="image/png"
                if resource.endswith(("/poster", "/cover"))
                else "application/json"
            )
            path = f"/servers/{sid}/workflow/{resource}"
            assert surface.get(path).status_code == 401
            assert surface.get(path, headers=headers).status_code == 200
            for method in ("post", "put", "patch", "delete"):
                assert (
                    getattr(surface, method)(path, headers=headers).status_code == 405
                )
    active[0] = False
    assert (
        control.get(
            f"/servers/{sid}/workflow/films",
            headers={"Authorization": "Bearer local-token"},
        ).status_code
        == 401
    )


@pytest.mark.parametrize("film_id", ["ABF 中文 #?%&", "https:other.invalid"])
def test_workflow_film_id_is_a_quoted_path_segment(hub, film_id):
    client, _, _, sid, _, opener, _ = hub
    opener.open.return_value = Response()
    result = client.get(
        f"/servers/{sid}/workflow/films/{quote(film_id, safe='')}", headers=AUTH
    )
    assert result.status_code == 200
    url = urlsplit(opener.open.call_args.args[0].full_url)
    assert url.path == f"/workflow/films/{quote(film_id, safe='')}"
    assert not url.query and not url.fragment


@pytest.mark.parametrize(
    "resource,film_id",
    [
        ("../control", None),
        ("https://other.invalid", None),
        ("film", None),
        ("film", ""),
        ("poster", "."),
        ("cover", ".."),
        ("film", "../../control"),
        ("poster", "..\\control"),
        ("cover", "name\nheader"),
    ],
)
def test_workflow_transport_rejects_nonresource_and_path_traversal(
    hub, resource, film_id
):
    from taskpaw_v3.hub.server.workflow_proxy import (
        WorkflowProxyError,
        fetch_agent_workflow,
    )

    _, store, _, sid, _, opener, _ = hub
    with pytest.raises(WorkflowProxyError) as caught:
        fetch_agent_workflow(store.get_server(sid), resource, {}, film_id=film_id)
    assert caught.value.code == "invalid_parameters"
    opener.open.assert_not_called()


def test_workflow_json_exactly_at_the_limit_is_preserved(hub):
    client, _, _, sid, _, opener, _ = hub
    body = b'{"s":"' + b"x" * (1024 * 1024 - 8) + b'"}'
    assert len(body) == 1024 * 1024
    upstream = Response(body)
    opener.open.return_value = upstream
    result = client.get(f"/servers/{sid}/workflow/films", headers=AUTH)
    assert result.status_code == 200 and result.content == body
    assert upstream.closed and upstream.read_sizes == [1024 * 1024 + 1]


@pytest.mark.parametrize(
    "status,expected,code",
    [
        (404, 404, "workflow_unavailable"),
        (401, 502, "agent_auth_failed"),
        (403, 502, "agent_auth_failed"),
        (302, 502, "agent_request_failed"),
        (413, 502, "agent_request_failed"),
        (500, 502, "agent_request_failed"),
    ],
)
def test_workflow_http_errors_closed_and_sanitized(hub, caplog, status, expected, code):
    client, _, _, sid, _, opener, _ = hub
    body = io.BytesIO(b"SECRET-UPSTREAM-BODY")
    opener.open.side_effect = HTTPError(
        "http://secret-url", status, "secret-error", {}, body
    )
    result = client.get(f"/servers/{sid}/workflow/films", headers=AUTH)
    assert result.status_code == expected and result.json()["error"] == code
    assert body.closed and opener.open.call_count == 1
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
        (HTTPException("secret"), 502, "agent_request_failed"),
    ],
)
def test_workflow_transport_errors(hub, error, status, code):
    client, _, _, sid, _, opener, _ = hub
    opener.open.side_effect = error
    result = client.get(f"/servers/{sid}/workflow/films", headers=AUTH)
    assert result.status_code == status and result.json()["error"] == code
    assert "secret" not in result.text and opener.open.call_count == 1


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"{",
        b"[]",
        b"null",
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":1e999}',
        b'{"name":"\\ud800"}',
        b'{"\\udfff":"value"}',
        b" " * (1024 * 1024 + 1),
        b'{"nested":' + b"[" * 15000 + b"0" + b"]" * 15000 + b"}",
    ],
    ids=[
        "utf8",
        "json",
        "array",
        "null",
        "nan",
        "infinity",
        "overflow",
        "surrogate-value",
        "surrogate-key",
        "oversize",
        "deep-nesting",
    ],
)
def test_workflow_invalid_json_is_bounded_and_sanitized(hub, body):
    client, _, _, sid, _, opener, _ = hub
    upstream = Response(body)
    opener.open.return_value = upstream
    result = client.get(f"/servers/{sid}/workflow/films", headers=AUTH)
    assert (
        result.status_code == 502 and result.json()["error"] == "invalid_agent_response"
    )
    assert upstream.closed and upstream.read_sizes == [1024 * 1024 + 1]


@pytest.mark.parametrize("resource", ["poster", "cover"])
@pytest.mark.parametrize(
    "content_type,body,expected",
    [
        ("image/jpeg", b"jpeg", 200),
        ("image/png", b"png", 200),
        ("image/webp", b"webp", 200),
        ("image/gif", b"gif", 200),
        ("image/bmp", b"bmp", 200),
        ("image/svg+xml", b"svg", 502),
        ("text/html", b"html", 502),
        ("", b"missing", 502),
        ("image/png", b"x" * (8 * 1024 * 1024), 200),
        ("image/png", b"x" * (8 * 1024 * 1024 + 1), 502),
    ],
    ids=[
        "jpeg",
        "png",
        "webp",
        "gif",
        "bmp",
        "svg",
        "html",
        "missing",
        "at-cap",
        "over-cap",
    ],
)
def test_workflow_image_limits_and_media_types(
    hub, resource, content_type, body, expected
):
    client, _, _, sid, _, opener, _ = hub
    upstream = Response(body, content_type=content_type)
    opener.open.return_value = upstream
    result = client.get(f"/servers/{sid}/workflow/films/movie/{resource}", headers=AUTH)
    assert result.status_code == expected
    assert upstream.closed
    assert all(size == 8 * 1024 * 1024 + 1 for size in upstream.read_sizes)
    if expected == 200:
        assert result.content == body and result.headers["content-type"] == content_type
        assert result.headers["x-content-type-options"] == "nosniff"
    else:
        assert result.json()["error"] == "invalid_agent_response"


@pytest.mark.parametrize("status", [204, 302])
def test_workflow_unexpected_response_status_is_rejected(hub, status):
    client, _, _, sid, _, opener, _ = hub
    upstream = Response(status=status)
    opener.open.return_value = upstream
    result = client.get(f"/servers/{sid}/workflow/films", headers=AUTH)
    assert (
        result.status_code == 502 and result.json()["error"] == "agent_request_failed"
    )
    assert upstream.closed


def test_workflow_redirect_never_issues_second_request(hub, monkeypatch):
    from taskpaw_v3.hub.server import workflow_proxy

    client, _, _, sid, _, _, _ = hub
    calls = []

    class RedirectingTransport(BaseHandler):
        handler_order = 100

        def http_open(self, request):
            calls.append(request.full_url)
            headers = Message()
            headers["Location"] = "http://other.invalid/control"
            result = Response(b"secret", 302)
            result.code, result.msg = 302, "Found"
            result.info = lambda: headers
            result.geturl = lambda: request.full_url
            return result

    monkeypatch.setattr(
        workflow_proxy,
        "_opener",
        build_opener(NoRedirectHandler(), RedirectingTransport()),
    )
    result = client.get(f"/servers/{sid}/workflow/films", headers=AUTH)
    assert result.status_code == 502
    assert calls == ["http://[::1]:5680/workflow/films"]
