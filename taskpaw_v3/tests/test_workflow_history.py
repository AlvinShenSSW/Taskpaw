"""#302: retained covers/facts, recorded history and all completed pages (fakes only)."""

# ruff: noqa: F811

import json
import shutil
from pathlib import Path

import pytest

from taskpaw_v3.agent.server import workflow_reads
from taskpaw_v3.tests.test_workflow_board import Seed, done_through
from taskpaw_v3.tests.test_workflow_control_routes import (  # noqa: F401
    BASE,
    client,
    host,
    probeable_video,
    world,
)
from taskpaw_v3.workflow import board, file_identity


def finish(world):
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert world.status("cleanup_workcopy") == "succeeded"
    assert world.status("cleanup_input") == "succeeded"


def test_list_completion_time_matches_detail(world, client):
    world.scrape()
    rows = client.get(f"{BASE}/films").json()["films"]
    assert rows[0]["group"] != "done"
    assert rows[0]["completed_at"] is None

    finish(world)
    rows = client.get(f"{BASE}/films").json()["films"]
    detail = client.get(f"{BASE}/films/{world.film}").json()
    assert rows[0]["group"] == "done"
    assert rows[0]["completed_at"] is not None
    assert rows[0]["completed_at"] == detail["history"]["completed_at"]


def test_history_retained_before_delete_and_survives_reclaim(
    world, monkeypatch, client
):
    world.scrape()
    rt = world.rt
    before = board.film_detail(rt, world.film, {})
    final = board.final_manifest(rt, world.film)
    package = rt.final_dir(final)
    retained = rt.data_dir / "retained" / world.film
    poster = board.poster_path(rt, world.film)
    poster_bytes = poster.read_bytes()
    thumb = next(i for i in final.items if i.role == "thumb")
    thumb_bytes = (package / thumb.relative_path).read_bytes()
    real_delete = file_identity.delete_exact
    deleted = []

    def delete(path, *args):
        if path.is_relative_to(package):
            assert (retained / poster.name).read_bytes() == poster_bytes
            assert (retained / thumb.relative_path).read_bytes() == thumb_bytes
            summary = json.loads((retained / "summary.json").read_text("utf-8"))
            assert summary["title"] == before["title"]
            assert summary["actors"] == before["actors"]
            assert summary["marks"] == list(final.postprocess.actual_marks)
            assert summary["final_name"] == package.name
            assert summary["archive_path"] == str(world.nas / package.name)
            deleted.append(path)
        return real_delete(path, *args)

    monkeypatch.setattr(file_identity, "delete_exact", delete)
    finish(world)
    assert deleted and not package.exists()
    assert not list((rt.work_dir / "scrape").glob(f"{world.film}-r*"))
    assert (retained / poster.name).read_bytes() == poster_bytes
    after = client.get(f"{BASE}/films/{world.film}").json()
    assert after["group"] == "done"
    for key in ("title", "actors", "number", "marks"):
        assert after[key] == before[key]
    assert after["has_poster"]
    assert board.poster_path(rt, world.film) == retained / poster.name
    assert client.get(f"{BASE}/films/{world.film}/poster").content == poster_bytes
    rows = client.get(f"{BASE}/films").json()["films"]
    assert rows[0]["title"] == before["title"] and rows[0]["has_poster"]
    history = after["history"]
    assert history["completed_at"] and history["total_seconds"] >= 0
    assert [s["key"] for s in history["steps"]] == [
        "intake",
        "restore",
        "return",
        "asr",
        "translate",
        "scrape",
        "confirm",
        "archive",
        "cleanup",
    ]
    by_step = {s["key"]: s for s in history["steps"]}
    assert by_step["restore"]["device"] == "本机"
    assert by_step["restore"]["seconds"] == after["timing"]["stages"]["restore"]
    assert by_step["archive"]["result"] == "已校验入库"
    assert by_step["archive"]["seconds"] is not None
    assert by_step["cleanup"]["result"] == "来源与工作副本已清理"
    assert by_step["cleanup"]["seconds"] is not None
    # A retained landscape thumb remains the lightbox source after reclamation.
    monkeypatch.setattr(
        board,
        "is_landscape",
        lambda p: (200, 100) if p.name == thumb.relative_path else None,
    )
    assert board.cover_path(rt, world.film) == retained / thumb.relative_path
    rt.close()
    world.rt = world.runtime()
    restarted = board.film_detail(world.rt, world.film, {})
    assert restarted["title"] == before["title"] and restarted["has_poster"]


def test_retained_copy_failure_is_logged_and_does_not_block_cleanup(
    world, monkeypatch, caplog
):
    world.scrape()
    real_copy = shutil.copyfile
    attempted = []

    def copy(source, target, *args, **kwargs):
        if "retained" in Path(target).parts:  # copytree passes str
            attempted.append(target)
            raise OSError("fake disk full")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", copy)
    finish(world)
    assert attempted
    assert "retained" in caplog.text and "fake disk full" in caplog.text
    assert (world.rt.data_dir / "retained" / world.film / "summary.json").is_file()
    assert not rt_package(world).exists()


def rt_package(world):
    return world.rt.final_dir(board.final_manifest(world.rt, world.film))


@pytest.mark.parametrize("total", [0, 41])
def test_completed_api_pages_total_clamp_and_every_film(
    world, client, monkeypatch, total
):
    rt = world.rt
    seed = Seed(rt)
    with seed:
        for n in range(total):
            seed.film(f"done-{n:03}", done_through("cleanup_workcopy"), final=False)
        removed = seed.film("removed", done_through("cleanup_workcopy"), final=False)
        seed.conn.execute(
            "UPDATE films SET body_json=? WHERE film_id=?",
            ('{"removed_at":"t"}', removed),
        )
    # Seed creates minimal store rows; no filesystem or runtime reads for these fakes.
    monkeypatch.setattr(board, "row_facts", lambda *_: {})
    monkeypatch.setattr(
        workflow_reads,
        "board_inputs",
        lambda *_: workflow_reads.BoardInputs({}, {}, {}),
    )
    seen = []
    pages = max(1, (total + 19) // 20)
    for page in range(1, pages + 1):
        response = client.get(f"{BASE}/films", params={"done_page": page})
        assert response.status_code == 200
        value = response.json()
        assert value["done_paging"] == {
            "total": total,
            "size": 20,
            "page": page,
            "pages": pages,
        }
        assert value["counts"]["done"] == total
        done = [f["film_id"] for f in value["films"] if f["group"] == "done"]
        assert len(done) == min(20, total - (page - 1) * 20)
        seen.extend(done)
        assert world.film in {f["film_id"] for f in value["films"]}
    assert len(seen) == len(set(seen)) == total
    assert set(seen) == set(seed.ids.values()) - {removed}
    last = client.get(f"{BASE}/films?done_page=999").json()
    assert last["done_paging"]["page"] == pages
    assert client.get(f"{BASE}/films?done_page=0").status_code == 422
