"""U1 P3' / L / §7 control routes: 待处理, 重新查询, pause and the target preflight."""

# ruff: noqa: F811

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests.test_workflow_control_routes import BASE, Host, _client
from taskpaw_v3.tests.test_workflow_finalize import (  # noqa: F401
    FILM_DIR,
    World,
    probeable_video,
)
from taskpaw_v3.tests.test_workflow_lookup import FAKE, Profile
from taskpaw_v3.tests.test_workflow_plans import new_revision
from taskpaw_v3.workflow import lookup as lk


class FakeDispatcher:
    def __init__(self) -> None:
        self.wakes = 0
        self.paused = False

    def wake(self) -> None:
        self.wakes += 1

    def set_paused(self, paused: bool) -> None:
        self.paused = paused


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path, plan=False)
    try:
        yield w
    finally:
        w.rt.close()


@pytest.fixture
def host(world):
    value = Host(world)
    value.dispatcher = FakeDispatcher()
    return value


@pytest.fixture
def client(host):
    return _client(lambda: host)


def plans(client) -> list[dict[str, Any]]:
    response = client.get(f"{BASE}/plans")
    assert response.status_code == 200
    return response.json()["plans"]


def row(client, film):
    return next(
        f for f in client.get(f"{BASE}/films").json()["films"] if f["film_id"] == film
    )


def item(world, **kw):
    return {
        "film_id": world.film,
        "input_revision": 1,
        "ltx_model": "standard",
        "machine": world.rt.identity.worker_id,
        **kw,
    }


# ── GET /plans ───────────────────────────────────────────────────────────
def test_the_pending_list_shows_the_row_data_and_the_recommendation(world, client):
    data = client.get(f"{BASE}/plans").json()
    (entry,) = data["plans"]
    assert entry["film_id"] == world.film and entry["input_revision"] == 1
    assert entry["name"] == "movie"
    assert (entry["resolution"], entry["duration_ms"]) == ("1080p", 120000)
    assert entry["size_bytes"] == len(ret.VIDEO)
    assert (entry["video_count"], entry["item_count"]) == (1, 2)
    assert entry["needs_gpu"] is True and entry["revision_changed"] is False
    assert entry["lookup"] is None  # Miko not configured
    assert entry["recommendation"] == {
        "ltx_model": "standard",
        "ltx_reason": "默认",
        "machine": world.rt.identity.worker_id,
        "machine_reason": "NVIDIA GeForce RTX 5060 8 GB",
    }
    assert data["default_tier"] == "standard"
    assert [t["value"] for t in data["choices"]["tiers"]] == [
        "standard",
        "good",
        "high",
    ]
    (machine,) = data["choices"]["machines"]
    assert (machine["id"], machine["name"]) == (world.rt.identity.worker_id, "本机")
    assert row(client, world.film)["group"] == "planning"


def test_counts_dispatch_and_gpu_are_on_the_film_list(world, host, client):
    data = client.get(f"{BASE}/films").json()
    assert data["counts"]["planning"] == 1 and data["counts"]["waiting_for_you"] == 1
    assert data["dispatch"] == {"state": "idle", "reason": "idle", "paused": False}
    assert data["gpu"] == {
        "name": "NVIDIA GeForce RTX 5060",
        "vram_mib": 8151,
        "max_output": "1080p",
    }


# ── POST /plans ──────────────────────────────────────────────────────────
def test_confirming_moves_the_film_into_the_queue_and_wakes_dispatch(
    world, host, client
):
    response = client.post(
        f"{BASE}/plans", json={"plans": [item(world, ltx_model="high")]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "results": [{"film_id": world.film, "ok": True, "error": None, "message": None}]
    }
    assert host.dispatcher.wakes == 1
    assert plans(client) == []
    value = row(client, world.film)
    assert value["group"] == "active"
    assert value["plan"] == {
        "ltx_model": "high",
        "machine": world.rt.identity.worker_id,
    }
    detail = client.get(f"{BASE}/films/{world.film}").json()
    assert detail["plan"] == value["plan"]


def test_each_item_has_its_own_result(world, host, client):
    new_revision(world.rt, world.film)
    body = {
        "plans": [
            item(world),  # revision 1 is stale now
            item(
                world, input_revision=2, machine="00000000-0000-0000-0000-000000000099"
            ),
            item(world, input_revision=2),
        ]
    }
    results = client.post(f"{BASE}/plans", json=body).json()["results"]
    assert [(r["ok"], r["error"]) for r in results] == [
        (False, "cas_conflict"),
        (False, "invalid_machine"),
        (True, None),
    ]
    assert results[0]["message"] == "收件内容已更新，请重新查看后确认"
    again = client.post(
        f"{BASE}/plans", json={"plans": [item(world, input_revision=2)]}
    )
    assert again.json()["results"][0]["error"] == "plan_exists"
    assert host.dispatcher.wakes == 1  # only a confirm that succeeded wakes it


def test_a_revision_newer_than_the_current_one_is_a_cas_conflict(world, client):
    """IR06: the card is behind or ahead of the store, never an unknown film."""
    results = client.post(
        f"{BASE}/plans",
        json={
            "plans": [
                item(world, input_revision=2),
                item(world, film_id="00000000-0000-0000-0000-000000000098"),
            ]
        },
    ).json()["results"]
    assert [(r["ok"], r["error"]) for r in results] == [
        (False, "cas_conflict"),
        (False, "not_found"),
    ]


def test_a_tier_is_required_while_the_film_needs_restoring(world, client):
    results = client.post(
        f"{BASE}/plans", json={"plans": [item(world, ltx_model=None)]}
    ).json()["results"]
    assert results[0]["error"] == "tier_required"


@pytest.mark.parametrize(
    "body",
    [
        {"plans": []},
        {
            "plans": [
                {
                    "film_id": "x",
                    "input_revision": 1,
                    "ltx_model": "ultra",
                    "machine": "m",
                }
            ]
        },
        {
            "plans": [
                {"film_id": "x", "input_revision": 0, "ltx_model": None, "machine": "m"}
            ]
        },
        {"films": []},
    ],
)
def test_bad_bodies_are_refused(client, body):
    assert client.post(f"{BASE}/plans", json=body).status_code == 422


def test_a_changed_revision_is_flagged_on_the_card(world, client):
    new_revision(world.rt, world.film)
    (entry,) = plans(client)
    assert (entry["input_revision"], entry["revision_changed"]) == (2, True)


# ── metadata preview (L) ─────────────────────────────────────────────────
def with_lookup(world, tmp_path, **config) -> Profile:
    profile = Profile(tmp_path / "miko", **config)
    root = tmp_path / "miko" / "root"
    root.mkdir()
    world.rt.lookup = lk.MetadataLookup(
        world.rt,
        python=sys.executable,
        root=str(root),
        profile=str(profile.path),
        launch_prefix=[sys.executable, str(FAKE)],
    )
    return profile


def test_the_lookup_fills_the_card_and_names_the_film(world, client, tmp_path):
    with_lookup(world, tmp_path)
    (entry,) = plans(client)
    assert entry["lookup"]["status"] == "queued"
    assert world.rt.lookup.run_once() is True
    (entry,) = plans(client)
    assert entry["lookup"]["status"] == "succeeded"
    assert (entry["name"], entry["number"], entry["title"]) == (
        "MOVIE",
        "MOVIE",
        "MOVIE 的标题",
    )
    assert entry["actors"] == ["演员甲", "演员乙"]
    assert entry["has_poster"] is True and entry["poster_source"].startswith("lookup-")
    poster = client.get(f"{BASE}/films/{world.film}/poster")
    assert poster.status_code == 200 and poster.content.startswith(b"\xff\xd8fake-jpeg")
    value = row(client, world.film)
    assert (value["name"], value["number"], value["actors"]) == (
        "MOVIE",
        "MOVIE",
        ["演员甲", "演员乙"],
    )
    # the list row carries the same cover as the card (V3 §4.2 queue cards)
    assert value["has_poster"] is True
    assert value["poster_source"] == entry["poster_source"]
    detail = client.get(f"{BASE}/films/{world.film}").json()
    assert (detail["title"], detail["lookup"]["status"]) == (
        "MOVIE 的标题",
        "succeeded",
    )


def test_an_actors_last_tier_is_recommended(world, client, tmp_path):
    with_lookup(world, tmp_path)
    world.rt.lookup.run_once()
    client.post(f"{BASE}/plans", json={"plans": [item(world, ltx_model="high")]})
    new_revision(world.rt, world.film)  # same film again: its own last choice
    world.rt.lookup.run_once()
    (entry,) = plans(client)
    assert entry["recommendation"]["ltx_model"] == "high"
    assert entry["recommendation"]["ltx_reason"] == "上次选择 高"


def test_confirm_snapshots_the_films_actors(world, client, tmp_path):
    """The plan keeps the actors the operator saw, NFKC-normalised and trimmed."""
    with_lookup(world, tmp_path, fake_actors=[" Ａctor ", "演员乙", "Actor"])
    world.rt.lookup.run_once()
    response = client.post(f"{BASE}/plans", json={"plans": [item(world)]})
    assert response.json()["results"][0]["ok"] is True
    (plan,) = world.rt.store.list_plans(world.rt.operator)
    assert plan.actors == ("Actor", "演员乙")  # deduplicated after normalising


def test_an_archived_films_actor_still_counts_after_its_cache_is_reclaimed(
    world, client, tmp_path
):
    """Cleaning a film removes its lookup cache; the plan's own snapshot remains."""
    import shutil

    from taskpaw_v3.tests.test_workflow_local_dispatch import add_film
    from taskpaw_v3.workflow.planning import plan_list

    with_lookup(world, tmp_path, fake_actors=["Actor"])
    world.rt.lookup.run_once()
    client.post(f"{BASE}/plans", json={"plans": [item(world, ltx_model="high")]})
    shutil.rmtree(world.rt.work_dir / "lookup")  # what _reclaim_film does
    world.rt.lookup._records.clear()  # and the cache is not in memory either
    newer = add_film(world.rt, plan=False)
    assert world.rt.lookup.run_once() is True
    (entry,) = plan_list(world.rt)["plans"]
    assert entry["film_id"] == newer and entry["actors"] == ["Actor"]
    assert entry["recommendation"]["ltx_model"] == "high"
    assert entry["recommendation"]["ltx_reason"] == "Actor 上次选择 高"


def test_an_empty_snapshot_falls_back_to_the_films_current_actors(
    world, client, tmp_path
):
    """A plan confirmed before any lookup has no snapshot; a lookup that finished
    later (the film is not yet cleaned) still feeds the history."""
    from taskpaw_v3.tests.test_workflow_local_dispatch import add_film
    from taskpaw_v3.workflow.planning import plan_list

    with_lookup(world, tmp_path, fake_actors=["Actor"])
    client.post(f"{BASE}/plans", json={"plans": [item(world, ltx_model="good")]})
    (plan,) = world.rt.store.list_plans(world.rt.operator)
    assert plan.actors == ()
    world.rt.lookup.run(world.film, 1, "pkg.mkv")
    newer = add_film(world.rt, plan=False)
    assert world.rt.lookup.run_once() is True
    (entry,) = plan_list(world.rt)["plans"]
    assert entry["film_id"] == newer
    assert entry["recommendation"]["ltx_reason"] == "Actor 上次选择 良好"


def test_a_past_films_final_nfo_actor_counts_when_its_lookup_failed(scraped, tmp_path):
    """IR04: the history reads a past film's actors as its card does (final NFO >
    lookup > input NFO), so a failed lookup does not hide the archived actor."""
    from taskpaw_v3.tests.test_workflow_local_dispatch import add_film
    from taskpaw_v3.workflow.planning import plan_list

    profile = with_lookup(scraped, tmp_path, fake_lookup="fail")
    rt = scraped.rt
    rt.lookup.run(scraped.film, 1, "pkg.mkv")
    assert not rt.lookup.result(scraped.film, 1).ok
    profile.set(fake_actors=["Actor"])
    newer = add_film(rt, plan=False)
    assert rt.lookup.run_once() is True
    (entry,) = plan_list(rt)["plans"]
    assert entry["film_id"] == newer and entry["actors"] == ["Actor"]
    assert entry["recommendation"]["ltx_reason"] == "Actor 上次选择 标准"


def test_requery_runs_the_lookup_again(world, client, tmp_path):
    profile = with_lookup(world, tmp_path, fake_lookup="fail")
    world.rt.lookup.run_once()
    (entry,) = plans(client)
    assert (entry["lookup"]["status"], entry["lookup"]["message"]) == (
        "failed",
        "未查到资料",
    )
    assert entry["lookup"]["can_requery"] is True
    profile.set()
    response = client.post(f"{BASE}/films/{world.film}/lookup")
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert plans(client)[0]["lookup"]["status"] == "queued"
    world.rt.lookup.run_once()
    assert plans(client)[0]["lookup"]["status"] == "succeeded"


def test_requery_is_refused_once_the_film_has_a_final(scraped, tmp_path):
    """IR07: the final NFO names the film from then on; a lookup adds nothing."""
    host = Host(scraped)
    client = _client(lambda: host)
    with_lookup(scraped, tmp_path)
    response = client.post(f"{BASE}/films/{scraped.film}/lookup")
    assert response.status_code == 409


def test_requery_is_refused_without_a_number_or_without_miko(world, client, tmp_path):
    assert client.post(f"{BASE}/films/{world.film}/lookup").status_code == 503
    with_lookup(world, tmp_path, fake_lookup="no_number")
    world.rt.lookup.run_once()
    assert client.post(f"{BASE}/films/{world.film}/lookup").status_code == 409
    assert client.post(f"{BASE}/films/nobody/lookup").status_code == 404


# ── pause (§7.5) ─────────────────────────────────────────────────────────
def test_pause_sets_the_dispatcher_flag(world, host, client):
    assert client.post(f"{BASE}/pause", json={"paused": True}).json() == {
        "paused": True
    }
    assert host.dispatcher.paused is True
    assert client.post(f"{BASE}/pause", json={"paused": False}).json() == {
        "paused": False
    }
    assert client.post(f"{BASE}/pause", json={"paused": "yes"}).status_code == 422
    host.dispatcher = None
    assert client.post(f"{BASE}/pause", json={"paused": True}).status_code == 503


# ── target preflight (§7.3) ──────────────────────────────────────────────
@pytest.fixture
def scraped(tmp_path, probeable_video):
    w = World(tmp_path)
    try:
        w.scrape()
        yield w
    finally:
        w.rt.close()


def test_target_preflight_reports_the_folder_and_the_candidates(scraped):
    host = Host(scraped)
    host.dispatcher = FakeDispatcher()
    client = _client(lambda: host)
    url = f"{BASE}/films/{scraped.film}/target"
    with scraped.fake:
        data = client.get(url, params={"performer_id": "actor"}).json()
        assert data == {
            "film_dir": FILM_DIR,
            "performer_id": "actor",
            "target_path": str(Path(scraped.nas) / FILM_DIR),
            "absent": True,
            "error_code": None,
            "message": None,
            "candidates": {
                "query": ["Actor"],
                "items": [
                    {
                        "id": "actor",
                        "name": "Actor",
                        "source_label": "NAS",
                        "unc_path": str(scraped.nas),
                        "has_thumb": False,
                        "aliases": [],
                    }
                ],
            },
        }
        (scraped.nas / FILM_DIR).mkdir()
        duplicate = client.get(url, params={"performer_id": "actor"}).json()
        assert (duplicate["absent"], duplicate["error_code"]) == (False, "duplicate")
        # U1 Codex r2 F4: exactly one candidate is checked in the same call,
        # so the list shows a duplicate (offline, permission, missing) too
        only = client.get(url).json()
        assert {k: only[k] for k in ("performer_id", "absent", "error_code")} == {
            "performer_id": "actor",
            "absent": False,
            "error_code": "duplicate",
        }
        assert only["target_path"] == str(Path(scraped.nas) / FILM_DIR)
        assert len(only["candidates"]["items"]) == 1
        (scraped.nas / FILM_DIR).rmdir()
        free = client.get(url).json()
        assert (free["performer_id"], free["absent"], free["error_code"]) == (
            "actor",
            True,
            None,
        )
        (scraped.nas / FILM_DIR).mkdir()
        gone = client.get(url, params={"performer_id": "nobody"}).json()
        assert gone["error_code"] == "missing"
    offline = client.get(url, params={"performer_id": "actor"}).json()
    assert (offline["error_code"], offline["candidates"]) == ("offline", None)


def test_target_preflight_needs_a_final(world, host, client):
    response = client.get(f"{BASE}/films/{world.film}/target")
    assert response.status_code == 409
