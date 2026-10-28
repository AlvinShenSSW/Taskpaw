"""#355: the controller summary carries each GPU worker's live heartbeat."""

# ruff: noqa: F811

from __future__ import annotations

from datetime import timedelta

import pytest

from taskpaw_v3.agent.server import workflow_events as events
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.tests.test_workflow_board_inputs import (  # noqa: F401
    Host,
    online,
    world,
)
from taskpaw_v3.tests.test_workflow_events import host_for
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import claim_remote, enrol_remote, route
from taskpaw_v3.workflow import models as m


@pytest.fixture
def observer(tmp_path):
    return events.WorkflowEvents(EventQueue("blackgoldpig"), tmp_path / "seen.json")


def _revoke(rt, remote):
    worker = rt.store.get_worker(rt.operator, remote.worker_id).worker
    rt.store.revoke_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRevocation(
            worker_id=remote.worker_id, expected_version=worker.version, reason="测试"
        ),
    )


def test_workers_report_heartbeat_gpu_current_and_transfers(world, observer):
    rt = world.rt
    busy = enrol_remote(rt, "Blacksilverpig")
    idle = enrol_remote(rt, "Bushpig")
    lost = enrol_remote(rt, "DarkbluePig")
    gone = enrol_remote(rt, "Retiredpig")
    film, staged = add_film(rt), add_film(rt)
    route(rt, film, busy.worker_id)
    claim_remote(rt, busy, film, "restore")
    online(world, lost)
    world.clock.advance(timedelta(seconds=90))  # DarkbluePig falls silent
    online(
        world,
        busy,
        gpu={
            "name": "NVIDIA GeForce RTX 4090",
            "vram_mib": 24564,
            "used_mib": 20100,
            "util": 98.0,
            "power_w": 351.5,
            "hold": "workflow",
        },
        staged=[{"film": staged, "rev": 1}],
        transfers=[
            {"film": staged, "dir": "down", "done": 25, "total": 100, "rate": 5e7}
        ],
        current={
            "film_id": film,
            "stage": "restore",
            "attempt_id": identity(),
            "phase": "running",
            "progress": "修复 · 阶段 2/4 encode · 21% · 剩余 6:33:54",
            "elapsed_s": 60,
            "ltx": {
                "ltx_progress": {
                    "stage_index": 2,
                    "stage_total": 4,
                    "stage_name": "encode",
                    "percent": 21.0,
                    "mode": "restore",
                }
            },
        },
    )
    online(world, idle, paused=True)
    _revoke(rt, gone)
    observer.observe(Host(rt, world.fleet))
    summary = observer.snapshot()

    assert summary["workers_total"] == 3 and summary["workers_more"] == 0
    rows = {row["name"]: row for row in summary["workers"]}
    assert list(rows) == ["Blacksilverpig", "Bushpig", "DarkbluePig"]
    names = {row["film_id"]: row["name"] for row in summary["films"]}
    worker = rows["Blacksilverpig"]
    assert worker["state"] == "online" and worker["heartbeat_age_s"] == 0
    assert worker["paused"] is False and worker["admission"] == "enabled"
    assert worker["gpu"] == {
        "name": "NVIDIA GeForce RTX 4090",
        "util": 98.0,
        "used_mib": 20100,
        "vram_mib": 24564,
        "power_w": 351.5,
        "hold": "workflow",
    }
    assert worker["current"] == {
        "film": names[film],
        "stage": "restore",
        "phase": "running",
        "progress": "修复 · 阶段 2/4 encode · 21% · 剩余 6:33:54",
        "ltx": {
            "stage_index": 2,
            "stage_total": 4,
            "stage_name": "encode",
            "percent": 21.0,
            "mode": "restore",
        },
    }
    assert worker["staged"] == 1
    assert worker["transfers"] == [
        {"film": names[staged], "dir": "down", "percent": 25, "rate": 5e7}
    ]
    assert rows["Bushpig"]["paused"] is True and rows["Bushpig"]["current"] is None
    assert rows["DarkbluePig"]["state"] == "lost"
    assert rows["DarkbluePig"]["heartbeat_age_s"] == 90
    # no ids, addresses, attempt ids or controls
    text = repr(summary["workers"])
    for secret in (busy.worker_id, "192.0.2.30", "attempt", "address", "port"):
        assert secret not in text


def test_workers_are_bounded_with_untruncated_totals(world, observer):
    rt = world.rt
    for index in range(17):
        online(world, enrol_remote(rt, f"GPU-{index:02}"))
    observer.observe(Host(rt, world.fleet))
    summary = observer.snapshot()
    assert [row["name"] for row in summary["workers"]] == [
        f"GPU-{index:02}" for index in range(16)
    ]
    assert summary["workers_total"] == 17 and summary["workers_more"] == 1


def test_long_worker_text_is_bounded(world, observer):
    rt = world.rt
    worker = enrol_remote(rt, "GPU")
    film = add_film(rt)
    route(rt, film, worker.worker_id)
    claim_remote(rt, worker, film, "restore")
    online(
        world,
        worker,
        current={
            "film_id": film,
            "stage": "restore",
            "attempt_id": identity(),
            "phase": "running",
            "progress": "片" * 400,
            "elapsed_s": 1,
        },
    )
    observer.observe(Host(rt, world.fleet))
    current = observer.snapshot()["workers"][0]["current"]
    assert len(current["progress"]) <= events.MAX_SUMMARY_TEXT


def test_single_machine_mode_has_no_workers(observer, monkeypatch):
    host, _ = host_for(monkeypatch, [])
    observer.observe(host)
    summary = observer.snapshot()
    assert summary is not None
    assert not {"workers", "workers_total", "workers_more"} & set(summary)
