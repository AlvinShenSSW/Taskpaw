"""Issue #286: the LAN workflow surface only reads the existing controller."""

# ruff: noqa: F811

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_network_app
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.tests.test_workflow_control_routes import AUTH, Host, _client
from taskpaw_v3.tests.test_workflow_finalize import (  # noqa: F401
    World,
    probeable_video,
    world,
)
from taskpaw_v3.tests.test_workflow_image_size import jpeg


def network(provider=None, *, enabled=True, token="t"):
    config = AgentConfig(
        server_id="test",
        machine="test",
        api_token=token,
        workflow=WorkflowSettings(enabled=enabled),
    )
    return create_network_app(
        config,
        EventQueue("test"),
        workflow_provider=provider,
    )


@pytest.fixture
def host(world):
    return Host(world)


@pytest.fixture
def client(host):
    return TestClient(network(lambda: host), headers=AUTH)


def test_every_network_route_is_get_only():
    app = network()
    assert app.routes
    assert all(route.methods == {"GET"} for route in app.routes)
    # the PWR-1 power route and the #286 workflow reads share the one network app
    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/power/history", "/workflow/films", "/workflow/plans"} <= paths


@pytest.mark.parametrize(
    "resource", ["films", "plans", "films/x", "films/x/poster", "films/x/cover"]
)
def test_workflow_auth_runs_before_host_or_resource_resolution(resource):
    def forbidden():
        pytest.fail("unauthorized request reached the workflow")

    client = TestClient(network(forbidden))
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.get(f"/workflow/{resource}", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize(
    "resource", ["films", "plans", "films/x", "films/x/poster", "films/x/cover"]
)
def test_disabled_workflow_is_404_even_with_a_provider(host, resource):
    client = TestClient(network(lambda: host, enabled=False), headers=AUTH)
    assert client.get(f"/workflow/{resource}").status_code == 404


def test_enabled_missing_host_keeps_control_snapshot_shape():
    settings = WorkflowSettings(enabled=True)
    control = _client(None, settings)
    client = TestClient(network(), headers=AUTH)
    assert (
        client.get("/workflow/films").json()
        == control.get("/control/workflow/films").json()
    )
    assert client.get("/workflow/plans").status_code == 503


def test_enabled_snapshot_detail_and_plans_match_control(world, host, client):
    host.current = {
        "film_id": world.film,
        "film": "ABF-394",
        "stage": "restore",
        "ltx": {"stage_index": 2, "stage_name": "encode", "percent": 52},
        "params": {"ltx_model": "good"},
    }
    control = _client(lambda: host, WorkflowSettings(enabled=True))
    for resource in ("films", "plans", f"films/{world.film}"):
        response = client.get(f"/workflow/{resource}")
        expected = control.get(f"/control/workflow/{resource}")
        assert response.status_code == expected.status_code == 200
        assert response.json() == expected.json()
    assert client.get("/workflow/films").json()["current"]["ltx"]["percent"] == 52


def test_snapshot_carries_the_power_fields_like_control(
    monkeypatch, world, host, client
):
    from taskpaw_v3.core import power_fleet

    cluster = {"w": 120.0, "today_wh": 300.0, "machines": 1, "estimated": True}
    monkeypatch.setattr(power_fleet, "cluster_power", lambda: dict(cluster))
    control = _client(lambda: host, WorkflowSettings(enabled=True))
    data = client.get("/workflow/films").json()
    assert data == control.get("/control/workflow/films").json()
    assert data["power"] == cluster
    assert isinstance(data["energy_rates"], list)
    assert all("energy" in row and "live_power" in row for row in data["films"])


def test_pending_recommendations_match_the_control_read(tmp_path):
    w = World(tmp_path, plan=False)
    try:
        host = Host(w)
        client = TestClient(network(lambda: host), headers=AUTH)
        expected = _client(lambda: host).get("/control/workflow/plans").json()
        data = client.get("/workflow/plans").json()
        assert data == expected
        assert data["plans"][0]["recommendation"]
    finally:
        w.rt.close()


@pytest.mark.parametrize("kind", ["poster", "cover"])
def test_images_use_the_same_id_lookup_and_content(world, host, client, kind):
    world.scrape()
    if kind == "cover":
        from taskpaw_v3.workflow import board

        final = board.final_manifest(world.rt, world.film)
        assert final is not None
        (board.output_dir(world.rt, final) / "fanart.jpg").write_bytes(jpeg(2184, 1477))
    resource = f"films/{world.film}/{kind}"
    response = client.get(f"/workflow/{resource}")
    expected = _client(lambda: host).get(f"/control/workflow/{resource}")
    assert response.status_code == expected.status_code == 200
    assert response.content == expected.content
    assert response.headers["content-type"] == expected.headers["content-type"]
    assert client.get(f"/workflow/films/unknown/{kind}").status_code == 404


@pytest.mark.parametrize("resource", ["films", "plans", "detail"])
def test_snapshot_size_is_bounded(monkeypatch, client, world, resource):
    from taskpaw_v3.agent.server import workflow_network
    from taskpaw_v3.workflow import board, planning

    monkeypatch.setattr(workflow_network, "MAX_JSON_BYTES", 1024)
    large = {"note": "x" * 2048}
    if resource == "films":
        monkeypatch.setattr(
            board, "film_rows", lambda *args, **kw: [dict(large, group="active")]
        )
    elif resource == "plans":
        monkeypatch.setattr(planning, "plan_list", lambda *args: large)
    else:
        monkeypatch.setattr(board, "film_detail", lambda *args, **kw: large)
    path = f"films/{world.film}" if resource == "detail" else resource
    response = client.get(f"/workflow/{path}")
    assert response.status_code == 413
    assert len(response.content) < 1024


@pytest.mark.parametrize("kind", ["poster", "cover"])
def test_image_read_is_bounded(monkeypatch, client, world, kind, tmp_path):
    from taskpaw_v3.agent.server import workflow_network
    from taskpaw_v3.workflow import board

    image = tmp_path / "poster.jpg"
    image.write_bytes(b"x" * 1025)
    monkeypatch.setattr(workflow_network, "MAX_IMAGE_BYTES", 1024)
    monkeypatch.setattr(board, f"{kind}_path", lambda *args: image)
    assert client.get(f"/workflow/films/{world.film}/{kind}").status_code == 413


@pytest.mark.parametrize("kind", ["poster", "cover"])
def test_client_cannot_select_an_arbitrary_image_path(world, client, kind, tmp_path):
    private = tmp_path / "private.jpg"
    private.write_bytes(b"not a workflow image")
    for film_id in ("..%2Fprivate.jpg", "%2Fetc%2Fpasswd", "private.jpg"):
        response = client.get(f"/workflow/films/{film_id}/{kind}")
        assert response.status_code == 404
        assert private.read_bytes() not in response.content
    response = client.get(
        f"/workflow/films/{world.film}/{kind}", params={"path": str(private)}
    )
    assert private.read_bytes() not in response.content


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_network_app_never_accepts_workflow_commands(client, method):
    for path in (
        "/workflow/plans",
        "/workflow/pause",
        "/workflow/films/x/confirm",
        "/control/workflow/plans",
    ):
        assert client.request(method, path, json={}).status_code in (404, 405)


@pytest.mark.parametrize(
    "resource", ["films", "plans", "films/x", "films/x/poster", "films/x/cover"]
)
def test_a_worker_never_serves_the_workflow_reads(host, resource):
    """P2-2 S-5: on role=worker `/workflow/*` is 404, so the Mac Hub never
    takes a worker as its workflow source."""
    config = AgentConfig(
        server_id="test",
        machine="test",
        api_token="t",
        workflow=WorkflowSettings(enabled=True, role="worker"),
    )
    app = create_network_app(config, EventQueue("test"), workflow_provider=lambda: host)
    assert all(route.methods == {"GET"} for route in app.routes)
    assert TestClient(app, headers=AUTH).get(f"/workflow/{resource}").status_code == 404
