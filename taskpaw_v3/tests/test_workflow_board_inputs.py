"""P2-4 (#295) R1–R3, R10: what the board shows in remote mode.

`reads.board_inputs` feeds the list, the detail and the #286 observer. A
film's note: the transfer, then the last routing pass's waiting reason, then
the download / staged state of an open unclaimed route,
then the dispatcher's. A film on a 失联 worker is an attention state. Each
row carries its route and pin; each stage's energy names its machines."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from taskpaw_v3.agent.server import workflow_reads as reads
from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.tests.test_workflow_board import done_through, film
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.worker_api_helpers import VERSION, beat
from taskpaw_v3.tests.workflow_route_helpers import (
    StepClock,
    claim_remote,
    enrol_remote,
    resolve_remote,
    route,
)
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import scheduler as s
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat

SETTINGS = WorkflowSettings(enabled=True, inbox_root="X:/inbox", remote_workers=True)
GPU_WAIT = "等待 GPU 设备：5060 Ti-1、5060 Ti-2"


class Host:
    """A running controller host: its store, fleet, dispatcher and last pass."""

    def __init__(self, rt, fleet, waiting=None, last=None):
        self.state, self.reason = "running", ""
        self.runtime, self.fleet = rt, fleet
        self.finalizer = None
        self.dispatcher = None
        self.waiting = waiting or {}
        self.last_pass = last

    def status(self):
        return {
            "state": "running",
            "reason": "",
            "intake": None,
            "ltx_download": None,
            "dispatch": {
                "state": "idle",
                "reason": "",
                "current": None,
                "waiting": dict(self.waiting),
                "paused": False,
            },
        }


@pytest.fixture
def world(tmp_path):
    clock = StepClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    fleet = FleetLive(rt.clock.now() - timedelta(hours=1), VERSION)
    try:
        yield SimpleNamespace(rt=rt, clock=clock, fleet=fleet)
    finally:
        rt.close()


def online(world, remote, **kw):
    world.fleet.observe(
        remote.worker_id,
        Heartbeat.model_validate(beat(identity(), **kw)),
        world.rt.clock.now(),
    )


def rows(host):
    payload = reads.films(SETTINGS, lambda: host)
    return {row["film_id"]: row for row in payload["films"]}


def test_note_precedence_route_prefetch_pass_and_dispatcher(world):
    rt = world.rt
    w1, w2 = enrol_remote(rt, "5060 Ti-1"), enrol_remote(rt, "5060 Ti-2")
    first, second, held, plain = (add_film(rt) for _ in range(4))
    route(rt, first, w1.worker_id)
    world.clock.advance(timedelta(seconds=1))
    route(rt, second, w1.worker_id)
    online(world, w1)
    online(world, w2)
    last = s.SchedulePass((), (), {held: s.CONTROLLER_DISK}, {})
    waiting = {held: GPU_WAIT, plain: GPU_WAIT, first: GPU_WAIT}
    found = rows(Host(rt, world.fleet, waiting, last))
    assert found[first]["note"] == "下载到 5060 Ti-1 · 0%"
    assert found[second]["note"] == "下载到 5060 Ti-1 · 0%"
    # the controller-disk gate never shows 「等待 GPU 设备」
    assert found[held]["note"] == s.CONTROLLER_DISK
    assert found[plain]["note"] == GPU_WAIT
    step = next(x for x in found[held]["steps"] if x["key"] == "restore")
    assert step["note"] == s.CONTROLLER_DISK
    route_row = found[second]["route"]
    assert route_row["machine"] == {"id": w1.worker_id, "name": "5060 Ti-1"}
    assert (route_row["kind"], route_row["position"], route_row["live"]) == (
        "auto",
        1,
        "online",
    )
    assert route_row["silent_s"] == 0 and route_row["reason"] == "测试"
    assert found[plain]["route"] is None
    assert found[first]["pin"] == {"worker_id": None, "name": "自动", "version": 1}
    assert found[first]["attention_kind"] is None


def test_a_transfer_beats_the_pass_and_a_claimed_film_shows_no_route_note(world):
    rt = world.rt
    w1 = enrol_remote(rt, "5060 Ti-1")
    fetched, claimed = add_film(rt), add_film(rt)
    route(rt, fetched, w1.worker_id)
    route(rt, claimed, w1.worker_id)
    claim_remote(rt, w1, claimed, "restore")
    transfer = {
        "film": fetched,
        "dir": "down",
        "done": 10**9,
        "total": 4 * 10**9,
        "rate": 0.0,
    }
    online(world, w1, transfers=[transfer])
    last = s.SchedulePass((), (), {fetched: "第 1 位 · 哪台先空给哪台"}, {})
    found = rows(Host(rt, world.fleet, {}, last))
    assert found[fetched]["note"] == "下载到 5060 Ti-1 · 25%"
    assert found[claimed]["note"] is None  # claimed: its step runs remotely


def test_a_film_on_a_lost_worker_is_attention_and_clears_by_itself(world):
    rt = world.rt
    w1 = enrol_remote(rt, "5070 Ti")
    lost = add_film(rt)
    route(rt, lost, w1.worker_id)
    claim_remote(rt, w1, lost, "restore")
    online(world, w1)
    world.clock.advance(timedelta(seconds=61))
    row = rows(Host(rt, world.fleet))[lost]
    assert row["group"] == "attention"
    assert row["attention_kind"] == "device_lost"
    assert row["note"] == "设备失联：5070 Ti · 后台重连中"
    assert row["route"]["live"] == "lost" and row["route"]["silent_s"] == 61
    counts = reads.films(SETTINGS, lambda: Host(rt, world.fleet))["counts"]
    assert counts["attention"] == 1
    detail = reads.film(SETTINGS, lambda: Host(rt, world.fleet), lost)
    assert detail["group"] == "attention"
    assert detail["attention_kind"] == "device_lost"
    assert detail["route"]["machine"]["name"] == "5070 Ti"
    online(world, w1)  # heartbeats return: nothing moved, the state clears
    row = rows(Host(rt, world.fleet))[lost]
    assert row["group"] == "active" and row["attention_kind"] is None


def test_the_restart_grace_raises_no_lost_state(world):
    rt = world.rt
    w1 = enrol_remote(rt, "5070 Ti")
    film_id = add_film(rt)
    route(rt, film_id, w1.worker_id)
    fresh = FleetLive(rt.clock.now(), VERSION)  # the controller just started
    row = rows(Host(rt, fresh))[film_id]
    assert row["attention_kind"] is None and row["route"]["live"] == "waiting"


def test_single_machine_rows_have_null_route_and_pin(tmp_path):
    rt = runtime(tmp_path / "wf")
    try:
        film_id = add_film(rt)
        host = Host(rt, None, {film_id: "等待 GPU"})
        row = rows(host)[film_id]
        assert row["note"] == "等待 GPU"
        assert (row["route"], row["pin"], row["attention_kind"]) == (None, None, None)
    finally:
        rt.close()


# ── R10: energy per stage names its machines ────────────────────────────
def test_stage_worker_ids_follow_the_attempts(world):
    rt = world.rt
    w1 = enrol_remote(rt, "5070 Ti")
    film_id = add_film(rt)
    route(rt, film_id, w1.worker_id)
    resolve_remote(rt, w1, claim_remote(rt, w1, film_id, "restore"))
    (row,) = rt.store.film_board(rt.operator, film_id=film_id)
    restore = next(x for x in row.stages if x.stage == "restore")
    assert restore.worker_ids == (w1.worker_id,)
    assert len(restore.attempt_ids) == 1


def test_energy_devices_name_each_attempts_machine_and_the_live_one():
    energy = m.AttemptEnergy(wh=10.0, seconds=60.0)
    row_film = film({**done_through("intake"), "restore": "running"})
    stages = tuple(
        stage.model_copy(
            update={
                "attempt_energy": (energy, energy),
                "attempt_ids": ("a1", "a2"),
                "worker_ids": ("w1", "w1"),
            }
        )
        if stage.stage == "restore"
        else stage
        for stage in row_film.stages
    )
    row_film = row_film.model_copy(update={"stages": stages})
    current = {
        "film_id": row_film.film_id,
        "stage": "restore",
        "attempt_id": "a3",
        "energy": {"wh": 1.0, "seconds": 5.0},
        "machine": {"id": "w2", "name": "5060 Ti-2"},
    }
    names = {"w1": "5070 Ti", "w2": "5060 Ti-2"}
    value = board.film_row(row_film, {}, current=current, names=names)
    devices = value["energy"]["stages"]["restore"]["devices"]
    assert devices == [
        {"id": "w1", "name": "5070 Ti"},
        {"id": "w2", "name": "5060 Ti-2"},
    ]


@pytest.mark.parametrize("downloaded", [False, True])
def test_dispatch_download_copy_reaches_list_and_drawer(world, downloaded):
    rt = world.rt
    w = enrol_remote(rt, "GPU")
    f = add_film(rt)
    route(rt, f, w.worker_id)
    online(
        world,
        w,
        staged=[{"film": f, "rev": 1}] if downloaded else [],
        transfers=[]
        if downloaded
        else [{"film": f, "dir": "down", "done": 25, "total": 100, "rate": 0.0}],
    )
    host = Host(rt, world.fleet)
    expected = "已到 GPU · 等开始" if downloaded else "下载到 GPU · 25%"
    assert rows(host)[f]["note"] == expected
    assert reads.film(SETTINGS, lambda: host, f)["note"] == expected
