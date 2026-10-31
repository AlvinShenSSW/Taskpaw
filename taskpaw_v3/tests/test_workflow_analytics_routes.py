"""EPIC #387 C2: `GET /control/workflow/analytics` (design §7), controller only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.tests.test_workflow_analytics import (
    Synth,
    standard,
    z,
)
from taskpaw_v3.tests.test_workflow_return_step import make_runtime
from taskpaw_v3.workflow import analytics as an
from taskpaw_v3.workflow.models import WorkflowError

PATH = "/control/workflow/analytics"


def client_for(provider, role="controller", token="Bearer t") -> TestClient:
    config = AgentConfig(
        server_id="s1", machine="dev", workflow=WorkflowSettings(role=role)
    )
    headers = {"Authorization": token} if token else {}
    return TestClient(
        create_control_app(
            config,
            control_token="t",
            control_active=lambda: True,
            workflow_provider=provider,
        ),
        headers=headers,
    )


@pytest.fixture
def host(tmp_path):
    rt = make_runtime(tmp_path / "wf")
    synth = Synth(rt)
    value = SimpleNamespace(
        state="running",
        reason="",
        runtime=rt,
        finalizer=None,
        dispatcher=SimpleNamespace(paused=False),
        analytics=an.AnalyticsFacts(),
        library=None,
        device_history=None,
        fleet=None,
        synth=synth,
    )
    try:
        yield value
    finally:
        synth.close()
        rt.close()


def test_the_route_returns_the_body_as_json(host):
    host.synth.film(code="FAKE-1", accepted=50 * 3600, tries=standard(None))
    response = client_for(lambda: host).get(
        PATH, params={"since": "2026-10-24T09:00:00+09:00"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["since"] == z(0)  # echoed in UTC
    assert [f["code"] for f in body["films"]] == ["FAKE-1"]
    assert body["devices"]["available"] is False
    assert body["inbox"] is None  # no inbox root configured
    assert body["machines"][0]["kind"] == "controller"
    assert client_for(lambda: host).get(PATH).json()["since"] is None


def test_a_worker_install_is_refused_with_409(host):
    response = client_for(lambda: host, role="worker").get(PATH)
    assert response.status_code == 409
    assert response.json() == {"detail": "数据分析只在主控上"}


def test_the_host_off_or_not_running_is_503_with_its_reason(host):
    off = client_for(lambda: None).get(PATH)
    assert off.status_code == 503 and off.json()["detail"] == "未启用"
    host.state, host.reason = "failed", "启动失败（见日志）"
    failed = client_for(lambda: host).get(PATH)
    assert failed.status_code == 503
    assert failed.json() == {"detail": "启动失败（见日志）"}


def test_a_host_without_the_fact_cache_is_503(host):
    host.analytics = None
    response = client_for(lambda: host).get(PATH)
    assert response.status_code == 503
    assert response.json() == {"detail": "数据分析尚未启动"}


def test_a_store_error_is_503(host, monkeypatch):
    def broken(auth, after, limit):
        raise WorkflowError("authority_unavailable")

    monkeypatch.setattr(host.runtime.store, "analytics_receipts", broken)
    response = client_for(lambda: host).get(PATH)
    assert response.status_code == 503
    assert response.json() == {"detail": "媒体流水线暂不可用：authority_unavailable"}


@pytest.mark.parametrize("since", ["yesterday", "2026-10-24T09:00:00", "2026-13-01"])
def test_a_bad_or_naive_since_is_422(host, since):
    response = client_for(lambda: host).get(PATH, params={"since": since})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "since"]


def test_the_route_needs_the_control_token(host):
    assert client_for(lambda: host, token=None).get(PATH).status_code == 401


def test_a_non_finite_number_fails_loudly(host, monkeypatch):
    def nan_body(*args, **kwargs):
        return {"films": [{"restore_s": float("nan")}]}

    monkeypatch.setattr(an, "build", nan_body)
    with pytest.raises(ValueError, match="JSON compliant"):
        client_for(lambda: host).get(PATH)


def test_the_route_does_not_encode_through_fastapi(host, monkeypatch):
    """`JSONResponse(body)`: FastAPI's `jsonable_encoder` never walks it."""
    import fastapi.routing

    def refuse(*args, **kwargs):
        raise AssertionError("jsonable_encoder walked the analytics body")

    monkeypatch.setattr(fastapi.routing, "jsonable_encoder", refuse)
    host.synth.film(tries=standard(None))
    response = client_for(lambda: host).get(PATH)
    assert response.status_code == 200
    assert response.json()["coverage"]["archives"] == z(40 * 3600)
