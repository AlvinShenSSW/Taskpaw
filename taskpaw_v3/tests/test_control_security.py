"""R01: actual ASGI rejection precedes parsing and every handler side effect."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from taskpaw_v3.core.control import add_control_guard
from taskpaw_v3.core.cors import UI_ORIGINS

TOKEN = "control-test-key-only"


@pytest.fixture
def guarded():
    state = {"writes": 0, "reads": 0, "active": True}
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    @app.get("/view")
    def view():
        state["reads"] += 1
        return {"ok": True}

    @app.api_route("/mutate", methods=["POST", "PATCH", "DELETE", "PUT"])
    def mutate():
        state["writes"] += 1
        return {"ok": True}

    @app.post("/body")
    def body(body: dict):
        state["writes"] += 1
        return body

    add_control_guard(
        app,
        control_token=TOKEN,
        is_active=lambda: state["active"],
        ping_path="/ping",
    )
    return TestClient(app), state


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
@pytest.mark.parametrize("auth", [None, "", "Bearer wrong", "Bearer network-test-key"])
def test_missing_wrong_or_read_token_cannot_mutate(guarded, method, auth):
    client, state = guarded
    response = client.request(
        method, "/mutate", headers={} if auth is None else {"Authorization": auth}
    )
    assert response.status_code == 401
    assert response.json()["error"] == "control_unauthorized"
    assert "TaskPaw Control" in response.headers["www-authenticate"]
    assert state["writes"] == 0
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "",
        "https://evil.invalid",
        "http://localhost:5174",
        "http://localhost:5173/",
        "http://localhost:5173/x",
        "http://localhost:5173?x=1",
        "http://localhost:5173#fragment",
        "http://localhost.evil.invalid:5173",
        "http://user@localhost:5173",
        "tauri://evil",
        "tauri://localhost/",
        "http://tauri.localhost:80",
        "HTTP://localhost:5173",
        "http://127.1:5173",
        "http://localhost:5173 https://evil.invalid",
        " http://localhost:5173",
    ],
)
def test_untrusted_origin_with_correct_token_rejected_before_effect(guarded, origin):
    client, state = guarded
    response = client.post(
        "/mutate", headers={"Origin": origin, "Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 403
    assert response.json()["error"] == "control_origin_forbidden"
    assert state["writes"] == 0


@pytest.mark.parametrize(
    "content_type,data",
    [
        ("application/x-www-form-urlencoded", "name=fixture"),
        ("text/plain", "name=fixture"),
        ("multipart/form-data; boundary=fixture", "--fixture--"),
    ],
)
def test_simple_forms_cannot_execute_bodyless_mutator(guarded, content_type, data):
    client, state = guarded
    response = client.post(
        "/mutate",
        content=data,
        headers={"Origin": "https://evil.invalid", "Content-Type": content_type},
    )
    assert response.status_code == 403
    assert state["writes"] == 0


@pytest.mark.parametrize("origin", UI_ORIGINS + ["http://[::1]:5173", None])
def test_ui_and_originless_cli_authorized(guarded, origin):
    client, state = guarded
    headers = {"Authorization": f"Bearer {TOKEN}"}
    if origin is not None:
        headers["Origin"] = origin
    assert client.post("/mutate", headers=headers).status_code == 200
    assert client.get("/view", headers=headers).status_code == 200
    assert state["writes"] == state["reads"] == 1


def test_guard_precedes_json_validation_and_reads(guarded):
    client, state = guarded
    assert client.post("/body", content="invalid-json").status_code == 401
    assert client.get("/view").status_code == 401
    assert state["writes"] == state["reads"] == 0
    valid = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/body", headers=valid, json={"key": "value"}).status_code == 200
    assert state["writes"] == 1


@pytest.mark.parametrize("header", ["Authorization", "Origin"])
def test_duplicate_raw_headers_fail_closed(guarded, header):
    client, state = guarded
    headers = [
        ("Authorization", f"Bearer {TOKEN}"),
        ("Origin", "http://localhost:5173"),
    ]
    headers.append((header, headers[0 if header == "Authorization" else 1][1]))
    response = client.post("/mutate", headers=headers)
    assert response.status_code == (401 if header == "Authorization" else 403)
    assert state["writes"] == 0


def test_ping_is_only_open_probe_and_inactive_rejects_even_valid_key(guarded):
    client, state = guarded
    assert client.get("/ping").status_code == 200
    assert client.get("/ping", headers={"Origin": "null"}).status_code == 403
    state["active"] = False
    assert (
        client.post("/mutate", headers={"Authorization": f"Bearer {TOKEN}"}).status_code
        == 401
    )
    assert state["writes"] == 0


def test_valid_preflight_needs_no_token_but_does_not_authorize_post(guarded):
    client, state = guarded
    origin = "http://localhost:5173"
    response = client.options(
        "/mutate",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert client.post("/mutate", headers={"Origin": origin}).status_code == 401
    assert state["writes"] == 0


def test_invalid_or_duplicate_origin_preflight_never_bypasses_guard(guarded):
    client, state = guarded
    response = client.options(
        "/mutate",
        headers=[
            ("Origin", "http://localhost:5173"),
            ("Origin", "https://evil.invalid"),
            ("Access-Control-Request-Method", "POST"),
        ],
    )
    assert response.status_code == 403
    assert state["writes"] == 0


@pytest.mark.parametrize("token", ["", " ", "new\nkey", "☃", "x" * 1025])
def test_invalid_factory_token_never_disables_guard(token):
    with pytest.raises(ValueError):
        add_control_guard(
            FastAPI(), control_token=token, is_active=lambda: True, ping_path="/ping"
        )
