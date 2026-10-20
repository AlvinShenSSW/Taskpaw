"""T06-P1 媒体入库 control routes (§3, §7 D05–D09, §8), over the F02 e2e World."""

# ruff: noqa: F811

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server import workflow_routes
from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests import test_workflow_scrape_step as scr
from taskpaw_v3.tests.test_workflow_finalize import (  # noqa: F401
    FILM_DIR,
    World,
    probeable_video,
    world,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_fake_filmpaw import FakeFilmpaw
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.filmpaw_client import (
    FilmpawClient,
    FilmpawRefused,
    FilmpawUnavailable,
)
from taskpaw_v3.workflow.local_host import retry_failed_stage

AUTH = {"Authorization": "Bearer t"}
BASE = "/control/workflow"


class Host:
    """The routes' view of a host (D14), wrapping the World's real runtime."""

    def __init__(self, world: World):
        self.state, self.reason = "running", ""
        self.runtime = world.rt
        self.finalizer = world.finalizer()
        self.current: dict[str, str] | None = None
        self.confirm_error: Exception | None = None

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "dispatch": {
                "state": "idle",
                "reason": "idle",
                "current": self.current,
                "waiting": dict(self.finalizer.waiting),
            },
        }

    def confirm(self, film_id: str, performer_id: str) -> str:
        if self.confirm_error is not None:
            raise self.confirm_error
        return self.finalizer.confirm(film_id, performer_id)

    def retry(self, film_id: str, stage: str) -> None:
        retry_failed_stage(self.runtime, film_id, stage)


def _client(provider, settings: WorkflowSettings | None = None) -> TestClient:
    config = AgentConfig(
        server_id="s1", machine="dev", workflow=settings or WorkflowSettings()
    )
    return TestClient(
        create_control_app(
            config,
            control_token="t",
            control_active=lambda: True,
            workflow_provider=provider,
        ),
        headers=AUTH,
    )


@pytest.fixture
def host(world):
    return Host(world)


@pytest.fixture
def client(host):
    return _client(lambda: host)


def _row(client: TestClient, film: str) -> dict[str, Any]:
    films = client.get(f"{BASE}/films").json()["films"]
    return next(f for f in films if f["film_id"] == film)


# ── list + lifecycle gating (D07) ────────────────────────────────────────
def test_list_shows_the_film_its_group_and_the_running_stage(world, host, client):
    host.current = {"film_id": world.film, "film": "Film", "stage": "restore"}
    data = client.get(f"{BASE}/films").json()
    assert (data["state"], data["current"]["stage"]) == ("running", "restore")
    (row,) = data["films"]
    assert (row["film_id"], row["name"], row["group"]) == (world.film, "Film", "active")
    world.scrape()
    row = _row(client, world.film)
    assert (row["name"], row["group"], row["can_confirm"]) == (
        FILM_DIR,
        "confirm",
        True,
    )


@pytest.mark.parametrize(
    ("settings", "state", "reason"),
    [
        (WorkflowSettings(), "off", "未启用"),
        (WorkflowSettings(enabled=True), "off", "未设置收件目录"),
        (
            WorkflowSettings(enabled=True, inbox_root="X:/in"),
            "failed",
            "启动失败（见日志）",
        ),
    ],
)
def test_no_host_explains_why_and_refuses_everything_else(settings, state, reason):
    for provider in (None, lambda: None):
        client = _client(provider, settings)
        assert client.get(f"{BASE}/films").json() == {
            "state": state,
            "reason": reason,
            "current": None,
            "films": [],
        }
        film = identity()
        for response in (
            client.get(f"{BASE}/films/{film}"),
            client.get(f"{BASE}/films/{film}/poster"),
            client.get(f"{BASE}/performers", params={"q": "a"}),
            client.get(f"{BASE}/performers/p/thumb"),
            client.post(f"{BASE}/films/{film}/confirm", json={"performer_id": "a"}),
            client.post(f"{BASE}/films/{film}/retry", json={"stage": "asr"}),
            client.post(f"{BASE}/films/{film}/open", json={"what": "inbox"}),
        ):
            assert response.status_code == 503
            assert response.json() == {"detail": reason}


def test_a_host_that_is_not_running_lists_nothing(world, host, client):
    host.state, host.reason = "failed", "收件目录不存在：X:/in"
    assert client.get(f"{BASE}/films").json() == {
        "state": "failed",
        "reason": "收件目录不存在：X:/in",
        "current": None,
        "films": [],
    }
    response = client.post(
        f"{BASE}/films/{world.film}/confirm", json={"performer_id": "actor"}
    )
    assert response.status_code == 503 and "收件目录" in response.json()["detail"]


def test_routes_live_on_the_control_api_only(host):
    network = TestClient(
        create_network_app(
            AgentConfig(server_id="s1", machine="dev"), EventQueue("dev")
        )
    )
    assert network.get(f"{BASE}/films").status_code == 404
    assert network.post(f"{BASE}/films/x/confirm", json={}).status_code == 404
    bare = TestClient(
        create_control_app(
            AgentConfig(server_id="s1", machine="dev"),
            control_token="t",
            control_active=lambda: True,
            workflow_provider=lambda: host,
        )
    )
    assert bare.get(f"{BASE}/films").status_code == 401


# ── detail + poster (U2, D11) ────────────────────────────────────────────
def test_detail_shows_the_package_nfo_inputs_and_state(world, client):
    world.scrape()
    data = client.get(f"{BASE}/films/{world.film}").json()
    assert set(data) == {
        "film_id",
        "name",
        "number",
        "title",
        "actors",
        "marks",
        "files",
        "inputs",
        "workcopy_count",
        "has_poster",
        "group",
        "can_confirm",
        "can_retry",
        "retry_stage",
        "note",
        "stages",
        "archive",
        "cleanup",
    }
    assert (data["name"], data["number"]) == (FILM_DIR, FILM_DIR)
    assert (data["title"], data["actors"]) == ("假的标题", ["Actor"])
    assert data["marks"] == {"restored": True, "chinese_subtitles": True, "uhd": False}
    roles = {f["path"]: f["role"] for f in data["files"]}
    assert roles["poster.jpg"] == "poster" and roles[f"{FILM_DIR}.nfo"] == "nfo"
    assert all(set(f) == {"role", "path", "size", "volume"} for f in data["files"])
    video = next(f for f in data["files"] if f["role"] == "main_video")
    assert video["size"] > 0
    assert sorted(i["path"] for i in data["inputs"]) == ["movie.ja.srt", "movie.mkv"]
    assert data["workcopy_count"] == len(data["files"])
    assert data["has_poster"] is True
    assert (data["group"], data["can_confirm"]) == ("confirm", True)
    stages = {s["stage"]: s["status"] for s in data["stages"]}
    assert stages["scrape"] == "succeeded" and stages["archive"] == "pending"
    assert data["archive"] == {"status": "pending", "error": None, "target_path": None}
    assert data["cleanup"] == {"input": "pending", "workcopy": "pending", "kept": []}


def test_unknown_film_is_404(client):
    film = identity()
    assert client.get(f"{BASE}/films/{film}").status_code == 404
    assert client.get(f"{BASE}/films/{film}/poster").status_code == 404
    assert client.get(f"{BASE}/films/not-an-id").status_code == 404


def test_poster_is_served_by_film_id(world, client):
    assert client.get(f"{BASE}/films/{world.film}/poster").status_code == 404
    world.scrape()
    response = client.get(f"{BASE}/films/{world.film}/poster")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"image poster.jpg"


def test_after_cleanup_detail_keeps_status_and_the_nas_target(world, host, client):
    world.scrape()
    with world.fake:
        assert (
            client.post(
                f"{BASE}/films/{world.film}/confirm", json={"performer_id": "actor"}
            ).status_code
            == 200
        )
        world.ticks(host.finalizer)
    data = client.get(f"{BASE}/films/{world.film}").json()
    assert data["group"] == "done" and data["has_poster"] is False
    assert data["archive"] == {
        "status": "succeeded",
        "error": None,
        "target_path": str(world.nas / FILM_DIR),
    }
    assert data["cleanup"] == {
        "input": "succeeded",
        "workcopy": "succeeded",
        "kept": [],
    }
    assert data["title"] is None and data["actors"] == []
    assert client.get(f"{BASE}/films/{world.film}/poster").status_code == 404
    assert (
        client.post(
            f"{BASE}/films/{world.film}/open", json={"what": "output"}
        ).status_code
        == 404
    )


# ── Filmpaw proxy (U3, D08) ──────────────────────────────────────────────
def test_performer_search_is_proxied_with_aliases(world, client):
    world.fake.aliases["actor"] = ["别名一"]
    world.fake.thumbs["actor"] = b"jpeg bytes"
    with world.fake:
        response = client.get(f"{BASE}/performers", params={"q": "别名"})
        assert response.status_code == 200
        assert response.json() == {
            "items": [
                {
                    "id": "actor",
                    "name": "Actor",
                    "source_label": "NAS",
                    "unc_path": str(world.nas),
                    "has_thumb": True,
                    "aliases": ["别名一"],
                }
            ]
        }
        assert world.fake.searches[-1] == {
            "q": "别名",
            "include_missing": "false",
            "page_size": "50",
        }
        thumb = client.get(f"{BASE}/performers/actor/thumb")
        assert (thumb.status_code, thumb.content) == (200, b"jpeg bytes")
        assert thumb.headers["content-type"] == "image/jpeg"
        assert client.get(f"{BASE}/performers/nobody/thumb").status_code == 404
    down = client.get(f"{BASE}/performers", params={"q": "a"})
    assert down.status_code == 503 and "Filmpaw 未运行" in down.json()["detail"]
    assert client.get(f"{BASE}/performers/actor/thumb").status_code == 503


def test_filmpaw_client_maps_performers_and_raw_thumbs(tmp_path):
    nas = tmp_path / "nas" / "Actor"
    nas.mkdir(parents=True)
    with FakeFilmpaw(tmp_path / "Filmpaw", {"actor": nas}) as fake:
        fake.thumbs["actor"] = b"\xff\xd8 raw"
        client = FilmpawClient(tmp_path / "Filmpaw")
        (item,) = client.performers("act")
        assert item["aliases"] == [] and item["has_thumb"] is True
        assert client.performers("zzz") == []
        assert client.thumb("actor") == (b"\xff\xd8 raw", "image/jpeg")
        with pytest.raises(FilmpawRefused) as e:
            client.thumb("nobody")
        assert e.value.status == 404
    with pytest.raises(FilmpawUnavailable):
        client.performers("act")


# ── confirm (U4, D05) ────────────────────────────────────────────────────
def test_confirm_once_then_refusals_are_409_with_the_reason(world, host, client):
    early = client.post(
        f"{BASE}/films/{world.film}/confirm", json={"performer_id": "actor"}
    )
    assert early.status_code == 409 and "刮削" in early.json()["detail"]
    world.scrape()
    with world.fake:
        ok = client.post(
            f"{BASE}/films/{world.film}/confirm", json={"performer_id": "actor"}
        )
        assert ok.status_code == 200 and set(ok.json()) == {"approval_id"}
    row = _row(client, world.film)
    assert (row["group"], row["can_confirm"]) == ("archiving", False)
    host.finalizer.run_once()  # admitted while Filmpaw is down
    again = client.post(
        f"{BASE}/films/{world.film}/confirm", json={"performer_id": "actor"}
    )
    assert again.status_code == 409 and "已在归档" in again.json()["detail"]
    assert _row(client, world.film)["note"] == "等待 Filmpaw 运行"
    assert client.post(f"{BASE}/films/{world.film}/confirm", json={}).status_code == 422


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RuntimeError("媒体流水线尚未启动"), 503),
        (m.WorkflowError("authority_unavailable"), 503),
        (m.WorkflowError("cas_conflict"), 409),
        (m.WorkflowError("not_found"), 404),
    ],
)
def test_confirm_failures_map_to_status_codes(world, host, client, error, status):
    host.confirm_error = error
    response = client.post(
        f"{BASE}/films/{world.film}/confirm", json={"performer_id": "actor"}
    )
    assert response.status_code == status and response.json()["detail"]


# ── retry (U5, D05) ──────────────────────────────────────────────────────
def test_retry_only_failed_local_stages(world, client):
    ret.run_all(scr.dispatcher(world.rt, scr.scrape_step(world.tmp, "extra_file")))
    row = _row(client, world.film)
    assert (row["group"], row["retry_stage"]) == ("attention", "scrape")
    for stage in ("restore", "archive", "bogus"):
        refused = client.post(f"{BASE}/films/{world.film}/retry", json={"stage": stage})
        assert refused.status_code == 409 and refused.json()["detail"]
    ok = client.post(f"{BASE}/films/{world.film}/retry", json={"stage": "scrape"})
    assert (ok.status_code, ok.json()) == (200, {"ok": True})
    assert world.status("scrape") == "pending"
    again = client.post(f"{BASE}/films/{world.film}/retry", json={"stage": "scrape"})
    assert again.status_code == 409
    unknown = client.post(f"{BASE}/films/{identity()}/retry", json={"stage": "scrape"})
    assert unknown.status_code == 404


# ── open folder (U5, D09) ────────────────────────────────────────────────
def test_open_resolves_folders_by_film_id(world, client, monkeypatch):
    opened: list[Path] = []
    monkeypatch.setattr(workflow_routes, "open_folder", opened.append)
    url = f"{BASE}/films/{world.film}/open"
    assert client.post(url, json={"what": "inbox"}).status_code == 204
    assert client.post(url, json={"what": "output"}).status_code == 404  # no final yet
    world.scrape()
    assert client.post(url, json={"what": "output"}).status_code == 204
    final = world.rt.store.get_final_manifest(world.rt.operator, world.film, 1)
    assert opened == [
        world.inbox / "pkg",
        world.rt.work_dir / final.output_relative_path,
    ]
    assert client.post(url, json={"what": "elsewhere"}).status_code == 422
    assert (
        client.post(
            f"{BASE}/films/{identity()}/open", json={"what": "inbox"}
        ).status_code
        == 404
    )


def test_open_is_501_off_windows(world, client, monkeypatch):
    def refuse(path: Path) -> None:
        raise NotImplementedError("只支持 Windows")

    monkeypatch.setattr(workflow_routes, "open_folder", refuse)
    response = client.post(f"{BASE}/films/{world.film}/open", json={"what": "inbox"})
    assert response.status_code == 501


def test_open_folder_refuses_other_platforms(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_routes.sys, "platform", "linux")
    with pytest.raises(NotImplementedError):
        workflow_routes.open_folder(tmp_path)
