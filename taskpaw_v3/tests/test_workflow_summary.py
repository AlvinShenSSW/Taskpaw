"""#351: the existing observer exposes the controller queue without new reads."""

# ruff: noqa: F811

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from taskpaw_v3.agent.server import workflow_events as events
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.tests.test_workflow_board_inputs import (  # noqa: F401
    Host,
    online,
    world,
)
from taskpaw_v3.tests.test_workflow_events import host_for
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, plan_film
from taskpaw_v3.tests.test_workflow_plans import new_revision
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import claim_remote, enrol_remote, route
from taskpaw_v3.workflow import board


@pytest.fixture
def observer(tmp_path):
    return events.WorkflowEvents(EventQueue("blackgoldpig"), tmp_path / "seen.json")


def test_queue_retains_pending_status_machine_order_and_paused_controller(
    world, observer
):  # noqa: F811
    rt = world.rt
    worker = enrol_remote(rt, "Jasna 5070")
    first, second = add_film(rt), add_film(rt)
    route(rt, first, worker.worker_id)
    world.clock.advance(timedelta(seconds=1))
    route(rt, second, worker.worker_id)
    claim_remote(rt, worker, first, "restore")
    online(
        world,
        worker,
        current={
            "film_id": first,
            "stage": "restore",
            "attempt_id": identity(),
            "phase": "running",
            "progress": "LTX 2/4 encode 52%",
            "elapsed_s": 60,
        },
    )
    host = Host(rt, world.fleet)
    status = host.status

    def paused():
        value = status()
        value["dispatch"].update(state="idle", paused=True, reason="已暂停派发")
        return value

    host.status = paused
    observer.observe(host)
    summary = observer.snapshot()
    assert summary["dispatch"] == {
        "state": "idle",
        "paused": True,
        "reason": "已暂停派发",
    }
    assert summary["films_total"] == 2 and summary["films_more"] == 0
    films = {row["film_id"]: row for row in summary["films"]}
    assert films[first]["status"] == "running"
    assert films[second]["status"] == "pending"
    assert films[second]["group"] == "active"  # active is not a running count
    assert films[first]["machine"] == films[second]["machine"] == worker.name
    assert [films[key]["route_position"] for key in (first, second)] == [0, 1]
    assert films[second]["route_live"] == "online"
    assert "下载到 Jasna 5070" in films[second]["note"]
    assert summary["currents_total"] == 1 and summary["currents_more"] == 0
    assert summary["currents"][0]["machine"] == worker.name
    assert datetime.fromisoformat(summary["sampled_at"]).utcoffset() == timedelta(0)
    assert 0 <= summary["sample_age_seconds"] < 15
    assert set(films[first]) == {
        "film_id",
        "name",
        "group",
        "stage",
        "status",
        "note",
        "attention_kind",
        "machine",
        "route_position",
        "route_live",
    }


def test_lost_old_revision_has_worker_name_without_live_route(world, observer):  # noqa: F811
    rt = world.rt
    worker = enrol_remote(rt, "Jasna lost")
    film_id = add_film(rt)
    route(rt, film_id, worker.worker_id)
    claim_remote(rt, worker, film_id, "restore")
    online(world, worker)
    new_revision(rt, film_id)
    plan_film(rt, film_id, revision=2)
    world.clock.advance(timedelta(seconds=61))
    observer.observe(Host(rt, world.fleet))
    summary = observer.snapshot()
    (row,) = summary["films"]
    assert row["group"] == "attention" and row["attention_kind"] == "device_lost"
    assert row["machine"] == worker.name and row["route_position"] is None
    assert row["route_live"] is None and "设备失联" in row["note"]
    assert summary["currents"] == [] and summary["currents_total"] == 0


def test_unassigned_local_film_does_not_invent_a_machine(world, observer):
    """S351-01: deployment mode is not a per-film assignment or current."""
    film_id = add_film(world.rt)
    observer.observe(Host(world.rt, None))
    (row,) = observer.snapshot()["films"]
    assert row["film_id"] == film_id and row["status"] == "pending"
    assert row["machine"] is None
    assert row["route_position"] is None and row["route_live"] is None


def test_bounded_rows_and_machines_report_untruncated_totals(world, observer):  # noqa: F811
    rt = world.rt
    films = []
    for _ in range(34):
        films.append(add_film(rt))
        world.clock.advance(timedelta(seconds=1))
    for index, film_id in enumerate(films[:17]):
        worker = enrol_remote(rt, f"GPU-{index:02}")
        online(
            world,
            worker,
            current={
                "film_id": film_id,
                "stage": "restore",
                "attempt_id": identity(),
                "phase": "running",
                "progress": "50%",
                "elapsed_s": 1,
            },
        )
    observer.observe(Host(rt, world.fleet))
    summary = observer.snapshot()
    assert len(summary["films"]) == 32
    assert summary["films_total"] == 34 and summary["films_more"] == 2
    assert [row["film_id"] for row in summary["films"]] == films[:32]
    assert len(summary["currents"]) == 16
    assert summary["currents_total"] == 17 and summary["currents_more"] == 1


def test_projection_excludes_done_rows_limits_text_and_does_not_leak_controls(
    observer, monkeypatch
):
    host, _ = host_for(monkeypatch, [])
    long = "片" * 500
    rows = [
        {"film_id": "finished", "name": "Finished", "group": "done"},
        {
            "film_id": "unfinished",
            "name": long,
            "group": "active",
            "stage": "restore",
            "status": "pending",
            "note": long,
            "attention_kind": None,
            "route": {"machine": {"name": long}, "position": 2, "live": "online"},
            "actions": {"can_reassign": True},
            "secret": "not-for-readers",
            "steps": [],
        },
    ]
    monkeypatch.setattr(board, "film_rows", lambda *_a, **_kw: rows)
    host.status = lambda: {"dispatch": {"state": long, "reason": long, "paused": False}}
    observer.observe(host)
    summary = observer.snapshot()
    assert summary["films_total"] == 1 and summary["films_more"] == 0
    (row,) = summary["films"]
    assert row["name"] == row["note"] == row["machine"] == long[:400]
    assert summary["dispatch"]["state"] == summary["dispatch"]["reason"] == long[:400]
    assert "actions" not in row and "steps" not in row and "secret" not in row
    row["name"] = "edited"
    summary["dispatch"]["paused"] = True
    assert observer.snapshot()["films"][0]["name"] == long[:400]
    assert observer.snapshot()["dispatch"]["paused"] is False


@pytest.mark.parametrize(
    "monotonic_elapsed,wall_elapsed,expected",
    [(2, 3, 3), (7, -300, 7), (0, 16, None), (16, 0, None)],
)
def test_snapshot_age_uses_both_clocks_including_sleep_and_clock_rollback(
    observer, monkeypatch, monotonic_elapsed, wall_elapsed, expected
):
    now = {
        "mono": 100.0,
        "wall": datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp(),
    }
    monkeypatch.setattr(
        events,
        "time",
        SimpleNamespace(monotonic=lambda: now["mono"], time=lambda: now["wall"]),
    )
    host, _ = host_for(monkeypatch, [])
    observer.observe(host)
    now["mono"] += monotonic_elapsed
    now["wall"] += wall_elapsed
    summary = observer.snapshot()
    if expected is None:
        assert summary is None
    else:
        assert summary["sample_age_seconds"] == expected


def test_sampling_delay_does_not_publish_an_old_board_as_fresh(observer, monkeypatch):
    now = {"mono": 100.0, "wall": 1_800_000_000.0}
    monkeypatch.setattr(
        events,
        "time",
        SimpleNamespace(monotonic=lambda: now["mono"], time=lambda: now["wall"]),
    )
    host, _ = host_for(monkeypatch, [])

    def slow_rows(*_args, **_kwargs):
        now["wall"] += 16
        return []

    monkeypatch.setattr(board, "film_rows", slow_rows)
    observer.observe(host)
    assert observer.snapshot() is None


def test_error_stop_and_restart_never_reuse_a_previous_summary(observer, monkeypatch):
    host, _ = host_for(monkeypatch, [])
    observer.observe(host)
    assert observer.snapshot()["films_total"] == 0
    host.runtime.store.film_board = lambda *_a, **_kw: (_ for _ in ()).throw(
        RuntimeError()
    )
    observer.observe(host)
    assert observer.snapshot() is None
    host, _ = host_for(monkeypatch, [])
    observer.observe(host)
    assert observer.stop()
    assert observer.snapshot() is None
    restarted = events.WorkflowEvents(observer.queue, observer.path)
    assert restarted.snapshot() is None
