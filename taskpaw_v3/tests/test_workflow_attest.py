"""P2-4 (#295) R5–R9: 待处理 自动/pin, 改派, 判定已停止 and the C4 check.

判定已停止 is allowed only when both channels have been dead for 300 s: no
heartbeat on 5682 (a revoked worker's tombstone keeps its last one), and a
5680 `/status` read that kept failing (unreachable/timeout) or answered
`workflow_worker.busy == false`, checked within 30 s. Any other answer means
the machine is up. The status channel is faked here; `PowerFleet.quiet` is
tested with the power peers."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.worker_api_helpers import VERSION, beat
from taskpaw_v3.tests.workflow_route_helpers import (
    StepClock,
    claim_remote,
    enrol_remote,
    job,
    pin_version,
    route,
)
from taskpaw_v3.workflow import devices, planning, routing
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import operator_actions as ops
from taskpaw_v3.workflow import scheduler as s
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.local_dispatch import should_retry

TOKEN = "control-token"
DEAD = timedelta(seconds=301)


class Channel:
    """The 5680 `/status` channel: per worker address, its quiet state."""

    def __init__(self) -> None:
        self.states: dict[str, ops.StatusQuiet] = {}

    def quiet(self, address: str) -> ops.StatusQuiet | None:
        return self.states.get(address)


class Waker:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


class World:
    def __init__(self, tmp_path, *, remote: bool = True) -> None:
        self.clock = StepClock()
        self.rt = runtime(tmp_path / "wf", clock=self.clock.now)
        self.fleet = FleetLive(self.rt.clock.now() - timedelta(hours=1), VERSION)
        self.channel = Channel()
        self.dispatcher, self.scheduler = Waker(), Waker()
        self.host = SimpleNamespace(
            state="running",
            reason="",
            runtime=self.rt,
            fleet=self.fleet if remote else None,
            dispatcher=self.dispatcher,
            scheduler=self.scheduler,
            finalizer=None,
            last_pass=None,
            cancels=set(),
            status_channel=lambda: self.channel,
            status=lambda: {"state": "running", "dispatch": None},
        )
        config = AgentConfig(
            server_id="s",
            machine="m",
            host_metrics=False,
            workflow={
                "enabled": True,
                "inbox_root": str(tmp_path),
                "remote_workers": remote,
            },
        )
        app = create_control_app(
            config,
            control_token=TOKEN,
            control_active=lambda: True,
            workflow_provider=lambda: self.host,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def post(self, path, body):
        return self.client.post(
            path, json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )

    def get(self, path):
        return self.client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})

    def beat(self, remote, **kw) -> None:
        self.fleet.observe(
            remote.worker_id,
            Heartbeat.model_validate(beat(identity(), **kw)),
            self.rt.clock.now(),
        )

    def address(self, remote) -> str:
        worker = self.rt.store.get_worker(self.rt.operator, remote.worker_id).worker
        return ops.worker_address(worker)

    def status(self, remote, quiet_s, checked_s=5.0, state=None) -> None:
        self.channel.states[self.address(remote)] = ops.StatusQuiet(
            quiet_s=quiet_s, checked_s=checked_s, state=state
        )

    def assignment(self, film_id) -> m.AssignmentRecord:
        return next(
            o.assignment
            for o in self.rt.store.list_open_assignments(self.rt.operator)
            if self.rt.store.get_stage(self.rt.operator, o.assignment.job_id).film_id
            == film_id
        )

    def attest(self, film_id, assignment=None):
        current = assignment or self.assignment(film_id)
        return self.post(
            f"/control/workflow/films/{film_id}/attest-stopped",
            {
                "assignment_id": current.assignment_id,
                "expected_version": current.version,
            },
        )

    def close(self) -> None:
        self.rt.close()


@pytest.fixture
def world(tmp_path):
    value = World(tmp_path)
    try:
        yield value
    finally:
        value.close()


def claimed_film(world, name="5060 Ti-1", **kw):
    """A remote restore claimed by a worker that then went quiet."""
    remote = enrol_remote(world.rt, name, **kw)
    film_id = add_film(world.rt)
    route(world.rt, film_id, remote.worker_id)
    claim_remote(world.rt, remote, film_id, "restore")
    world.beat(remote)
    return remote, film_id


# ── C4 matrix (R8) ──────────────────────────────────────────────────────
HEARTBEAT = {
    "online": (timedelta(seconds=10), False),
    "lost_under_5": (timedelta(seconds=120), False),
    "lost_5": (DEAD, True),
    "revoked_tombstone": (DEAD, True),
}
STATUS = {
    "busy": (None, 5.0, "busy", False),
    "idle": (301.0, 5.0, None, True),
    "unreachable": (301.0, 5.0, None, True),
    "timeout": (305.0, 2.0, None, True),
    "401": (None, 5.0, "unauthorized", False),
    "invalid": (None, 5.0, "invalid", False),
    "stale_cache": (400.0, 31.0, None, False),
    "no_busy": (None, 5.0, "no_busy", False),
    "short": (120.0, 5.0, None, False),
    "never": None,
}


@pytest.mark.parametrize("heartbeat", sorted(HEARTBEAT))
@pytest.mark.parametrize("status", sorted(STATUS))
def test_c4_both_channels_must_be_dead_for_five_minutes(tmp_path, heartbeat, status):
    world = World(tmp_path)
    try:
        remote, film_id = claimed_film(world)
        silence, beat_dead = HEARTBEAT[heartbeat]
        world.clock.advance(silence)
        if heartbeat == "revoked_tombstone":
            worker = world.rt.store.get_worker(world.rt.operator, remote.worker_id)
            devices.revoke(
                world.rt, remote.worker_id, worker.worker.version, world.fleet
            )
        if STATUS[status] is not None:
            quiet, checked, state, _ = STATUS[status]
            world.status(remote, quiet, checked, state)
        status_dead = STATUS[status] is not None and STATUS[status][3]
        world.clock.advance(timedelta(seconds=1))  # IR4: no renewal for 5 min
        if not beat_dead:
            world.clock.advance(timedelta(minutes=5))
            world.beat(remote)  # it is still beating
            world.clock.advance(silence)
        response = world.attest(film_id)
        if beat_dead and status_dead:
            assert response.status_code == 200, response.text
            assert job(world.rt, film_id, "restore").status == "pending"  # retried
        else:
            assert response.status_code == 409, response.text
            body = response.json()
            assert body["code"] == "still_alive"
            channel = "心跳通道" if not beat_dead else "状态通道"
            assert body["detail"].startswith(channel)
            assert job(world.rt, film_id, "restore").status in ("running", "unknown")
    finally:
        world.close()


def test_a_status_401_names_the_token(world):
    remote, film_id = claimed_film(world)
    world.clock.advance(DEAD)
    world.status(remote, None, 3.0, "unauthorized")
    body = world.attest(film_id).json()
    assert "401" in body["detail"] and "令牌" in body["detail"]


def test_ir4_a_recent_renewal_refuses_even_with_dead_channels(world):
    remote, film_id = claimed_film(world)
    world.clock.advance(timedelta(seconds=200))
    current = world.assignment(film_id)
    world.rt.store.renew(
        remote.auth,
        world.rt.meta(),
        m.AssignmentRenewal(
            assignment_id=current.assignment_id,
            expected_version=current.version,
            worker_id=remote.worker_id,
            boot_id=remote.boot_id,
            attempt_id=current.attempt_id,
            authority_epoch=current.authority_epoch,
            fencing_token=current.fencing_token,
            local_owner=m.LocalOwnerEvidence(
                owner_id=identity(),
                generation=1,
                boot_id=remote.boot_id,
                process_identity="remote",
                state="running",
                evidence=world.rt.evidence("native_owner"),
            ),
        ),
    )
    world.clock.advance(timedelta(seconds=150))  # heartbeat silent 350 s
    world.status(remote, 350.0)
    response = world.attest(film_id)
    assert response.status_code == 409
    assert response.json()["code"] == "recently_renewed"


def test_a_stale_version_or_attempt_is_refused(world):
    remote, film_id = claimed_film(world)
    world.clock.advance(DEAD)
    world.status(remote, 400.0)
    current = world.assignment(film_id)
    stale = current.model_copy(update={"version": current.version + 7})
    assert world.attest(film_id, stale).json()["code"] == "cas_conflict"
    other = add_film(world.rt)
    response = world.post(
        f"/control/workflow/films/{other}/attest-stopped",
        {"assignment_id": current.assignment_id, "expected_version": current.version},
    )
    assert response.json()["code"] == "stale_attempt"
    gone = world.post(
        f"/control/workflow/films/{film_id}/attest-stopped",
        {"assignment_id": identity(), "expected_version": 1},
    )
    assert gone.json()["code"] == "stale_attempt"


def test_a_successful_attest_retries_wakes_and_re_routes(world):
    remote, film_id = claimed_film(world)
    other = enrol_remote(world.rt, "5070 Ti")
    world.clock.advance(DEAD)
    world.beat(other)
    world.status(remote, 320.0)
    response = world.attest(film_id)
    assert response.status_code == 200, response.text
    restore = job(world.rt, film_id, "restore")
    assert restore.status == "pending"
    attempts = world.rt.store.list_attempts(world.rt.operator, restore.job_id)
    assert attempts[0].status == "failed" and should_retry(attempts[:1])
    assert world.rt.store.film_route(world.rt.operator, film_id, 1) is None
    assert world.dispatcher.wakes == 1 and world.scheduler.wakes == 1
    outcome = routing.run_pass(
        world.rt.store,
        world.rt.operator,
        world.rt.meta,
        controller_id=world.rt.identity.worker_id,
        live=world.fleet.snapshot(),
        now=world.rt.clock.now(),
        executors=world.rt.executors,
        remote_enabled=True,
    )
    assert {d.film_id: d.worker_id for d in outcome.routed} == {
        film_id: other.worker_id
    }


def test_a_film_pinned_to_the_dead_worker_waits_until_reassigned(world):
    remote = enrol_remote(world.rt, "5060 Ti-1")
    other = enrol_remote(world.rt, "5070 Ti")
    film_id = add_film(world.rt, plan=False)
    world.rt.store.confirm_plan(
        world.rt.operator,
        world.rt.meta(),
        m.FilmPlanConfirm(
            film_id=film_id,
            input_revision=1,
            ltx_model="standard",
            machine=remote.worker_id,
        ),
    )
    route(world.rt, film_id, remote.worker_id, kind="pinned")
    claim_remote(world.rt, remote, film_id, "restore")
    world.beat(remote)
    world.clock.advance(DEAD)
    world.beat(other)
    world.status(remote, 330.0)
    assert world.attest(film_id).status_code == 200

    def run():
        return routing.run_pass(
            world.rt.store,
            world.rt.operator,
            world.rt.meta,
            controller_id=world.rt.identity.worker_id,
            live=world.fleet.snapshot(),
            now=world.rt.clock.now(),
            executors=world.rt.executors,
            remote_enabled=True,
        )

    first = run()
    assert first.routed == ()
    assert first.planned.waiting[film_id] == "等待 5060 Ti-1：离线"
    response = world.post(
        f"/control/workflow/films/{film_id}/pin",
        {
            "input_revision": 1,
            "expected_version": pin_version(world.rt, film_id),
            "worker_id": None,
        },
    )
    assert response.status_code == 200, response.text
    assert {d.film_id: d.worker_id for d in run().routed} == {film_id: other.worker_id}


def test_attest_and_pin_are_refused_with_remote_workers_off(tmp_path):
    world = World(tmp_path, remote=False)
    try:
        film_id = add_film(world.rt)
        for path, body in (
            ("attest-stopped", {"assignment_id": identity(), "expected_version": 1}),
            ("pin", {"input_revision": 1, "expected_version": 1, "worker_id": None}),
        ):
            response = world.post(f"/control/workflow/films/{film_id}/{path}", body)
            assert response.status_code == 409
    finally:
        world.close()


# ── 改派 (R7) ───────────────────────────────────────────────────────────
def pin(world, film_id, worker_id, version=None):
    return world.post(
        f"/control/workflow/films/{film_id}/pin",
        {
            "input_revision": 1,
            "expected_version": version or pin_version(world.rt, film_id),
            "worker_id": worker_id,
        },
    )


def test_reassign_to_a_device_moves_an_unclaimed_route(world):
    w1, w2 = enrol_remote(world.rt, "5060 Ti-1"), enrol_remote(world.rt, "5070 Ti")
    film_id = add_film(world.rt)
    route(world.rt, film_id, w1.worker_id)
    world.beat(w1)
    assert pin(world, film_id, w2.worker_id).status_code == 200
    assert world.rt.store.film_route(world.rt.operator, film_id, 1) is None
    assert world.rt.store.get_pin(world.rt.operator, film_id, 1).worker_id == (
        w2.worker_id
    )
    assert world.scheduler.wakes == 1


def test_reassign_to_auto_withdraws_only_a_route_on_a_silent_worker(world):
    w1, w2 = enrol_remote(world.rt, "5060 Ti-1"), enrol_remote(world.rt, "5070 Ti")
    healthy, stranded = add_film(world.rt), add_film(world.rt)
    route(world.rt, healthy, w1.worker_id)
    route(world.rt, stranded, w2.worker_id)
    world.beat(w2)
    world.clock.advance(timedelta(seconds=90))
    world.beat(w1)  # w1 beats on; w2 is 失联
    assert pin(world, healthy, None).status_code == 200
    assert world.rt.store.film_route(world.rt.operator, healthy, 1) is not None
    assert pin(world, stranded, None).status_code == 200
    assert world.rt.store.film_route(world.rt.operator, stranded, 1) is None
    (closed,) = [
        r
        for r in world.rt.store.list_routes(world.rt.operator, open_only=False)
        if r.film_id == stranded
    ]
    assert closed.close_reason == ops.WITHDRAWN_LOST


def test_reassign_is_refused_once_claimed_and_never_to_the_controller(world):
    remote, film_id = claimed_film(world)
    other = enrol_remote(world.rt, "5070 Ti")
    response = pin(world, film_id, other.worker_id)
    assert response.status_code == 409
    assert response.json() == {"detail": "已开始处理", "code": "route_claimed"}
    free = add_film(world.rt)
    refused = pin(world, free, world.rt.identity.worker_id)
    assert refused.status_code == 409 and refused.json()["code"] == (
        "precondition_failed"
    )
    stale = pin(world, free, None, version=99)
    assert stale.json()["code"] == "cas_conflict"


def test_a_claim_racing_the_withdrawal_answers_route_claimed(world, monkeypatch):
    w1 = enrol_remote(world.rt, "5060 Ti-1")
    film_id = add_film(world.rt)
    route(world.rt, film_id, w1.worker_id)  # w1 never beat: not online
    real = world.rt.store.set_pin

    def racing(*args, **kw):
        result = real(*args, **kw)
        claim_remote(world.rt, w1, film_id, "restore")  # the worker claims now
        return result

    monkeypatch.setattr(world.rt.store, "set_pin", racing)
    response = pin(world, film_id, None)
    assert response.status_code == 409
    assert response.json()["code"] == "route_claimed"
    assert response.json()["detail"] == "已开始处理"


# ── row actions, reassign options and 设备 (R2, §2) ──────────────────────
def test_rows_offer_reassign_and_stop_with_both_channel_ages(world):
    from taskpaw_v3.agent.server import workflow_reads as reads
    from taskpaw_v3.core.config import WorkflowSettings

    remote, claimed = claimed_film(world)
    free = add_film(world.rt)
    world.clock.advance(timedelta(seconds=100))
    world.status(remote, 80.0)
    settings = WorkflowSettings(enabled=True, inbox_root="X:/", remote_workers=True)
    world.host.status = lambda: {"dispatch": None}
    payload = reads.films(settings, lambda: world.host)
    rows = {row["film_id"]: row for row in payload["films"]}
    assert rows[free]["actions"] == {"can_reassign": True, "stop": None}
    stop = rows[claimed]["actions"]["stop"]
    assert rows[claimed]["actions"]["can_reassign"] is False
    current = world.assignment(claimed)
    assert stop["assignment_id"] == current.assignment_id
    assert stop["expected_version"] == current.version
    assert stop["stage"] == "restore"
    assert stop["worker"] == {"id": remote.worker_id, "name": "5060 Ti-1"}
    assert stop["allowed"] is False and stop["reason"].startswith("心跳通道")
    assert stop["heartbeat_silent_s"] == 100 and stop["status_quiet_s"] == 80
    detail = reads.film(settings, lambda: world.host, free)
    options = {o["name"]: o for o in detail["reassign_options"]}
    assert options["自动"]["worker_id"] is None and options["自动"]["current"]
    assert options["5060 Ti-1"]["reason"] == "离线"  # 100 s without a heartbeat
    assert detail["actions"]["can_reassign"] is True


def test_devices_list_unresolved_assignments_and_route_depth(world):
    remote, film_id = claimed_film(world)
    second = add_film(world.rt)
    route(world.rt, second, remote.worker_id)
    view = devices.device_view(
        world.rt, world.fleet, remote_workers=True, channel=world.channel
    )
    (row,) = view["workers"]
    assert row["route_depth"] == 2
    (item,) = row["assignments"]
    current = world.assignment(film_id)
    assert item["assignment_id"] == current.assignment_id
    assert item["version"] == current.version
    assert (item["film_id"], item["stage"], item["state"]) == (
        film_id,
        "restore",
        "running",
    )
    assert item["film_name"]
    assert item["stop"]["allowed"] is False


# ── plans: 自动/pin and the null confirm (R5, R6) ───────────────────────
def remote_inputs(world, **kw):
    return {
        "live": world.fleet.snapshot(),
        "now": world.rt.clock.now(),
        "controller_id": world.rt.identity.worker_id,
        "controller_free_bytes": kw.get("free", 10**15),
        "live_since": world.fleet.started_at,
    }


def test_remote_plans_give_auto_and_each_device_per_tier(world):
    fast = enrol_remote(world.rt, "5070 Ti")
    small = enrol_remote(world.rt, "5060", gpu=("NVIDIA GeForce RTX 5060", 8151))
    world.beat(fast)
    world.beat(small)
    film_id = add_film(world.rt, plan=False)
    payload = planning.plan_list(world.rt, remote=remote_inputs(world))
    assert payload["remote"] is True
    (entry,) = payload["plans"]
    assert entry["film_id"] == film_id
    assert entry["recommendation"]["machine"] is None
    assert set(entry["routing"]) == {"standard", "good", "high"}
    standard = entry["routing"]["standard"]
    assert standard["auto"]["machine"] == {"id": fast.worker_id, "name": "5070 Ti"}
    assert standard["auto"]["text"].startswith("预计 ")
    assert "在 5070 Ti 完成" in standard["auto"]["text"]
    by_name = {d["name"]: d for d in standard["devices"]}
    assert by_name["5060"]["reason"] == "显存不足，不参与修复"
    assert by_name["5070 Ti"]["finish_at"] is not None
    assert by_name["5070 Ti"]["vram_mib"] == 16311
    names = [machine["name"] for machine in payload["choices"]["machines"]]
    assert names == ["5060", "5070 Ti"]  # 本机 is never a choice (OD1)


def test_remote_plans_wait_on_the_disk_gate(world):
    fast = enrol_remote(world.rt, "5070 Ti")
    world.beat(fast)
    add_film(world.rt, plan=False)
    payload = planning.plan_list(world.rt, remote=remote_inputs(world, free=10**9))
    routing_ = payload["plans"][0]["routing"]["standard"]
    assert routing_["auto"] == {"waiting": s.CONTROLLER_DISK}


def test_single_machine_plans_are_unchanged(world):
    add_film(world.rt, plan=False)
    payload = planning.plan_list(world.rt)
    assert payload["remote"] is False
    entry = payload["plans"][0]
    assert entry["routing"] is None
    assert entry["recommendation"]["machine"] == world.rt.identity.worker_id


def test_a_null_machine_is_automatic_and_remote_refuses_the_controller(world):
    remote = enrol_remote(world.rt, "5070 Ti")
    auto, pinned, local, gone = (add_film(world.rt, plan=False) for _ in range(4))
    revoked = enrol_remote(world.rt, "old")
    worker = world.rt.store.get_worker(world.rt.operator, revoked.worker_id).worker
    devices.revoke(world.rt, revoked.worker_id, worker.version, world.fleet)

    def item(film_id, machine):
        return {
            "film_id": film_id,
            "input_revision": 1,
            "ltx_model": "standard",
            "machine": machine,
        }

    response = world.post(
        "/control/workflow/plans",
        {
            "plans": [
                item(auto, None),
                item(pinned, remote.worker_id),
                item(local, world.rt.identity.worker_id),
                item(gone, revoked.worker_id),
            ]
        },
    )
    results = {r["film_id"]: r for r in response.json()["results"]}
    assert results[auto]["ok"] and results[pinned]["ok"]
    assert results[local]["error"] == "invalid_machine"
    assert results[gone]["error"] == "invalid_machine"
    assert world.rt.store.get_pin(world.rt.operator, auto, 1).worker_id is None
    assert world.rt.store.get_pin(world.rt.operator, pinned, 1).worker_id == (
        remote.worker_id
    )
    assert world.scheduler.wakes == 1


def test_single_machine_confirm_takes_null_or_the_controller(tmp_path):
    world = World(tmp_path, remote=False)
    try:
        a, b = add_film(world.rt, plan=False), add_film(world.rt, plan=False)
        response = world.post(
            "/control/workflow/plans",
            {
                "plans": [
                    {
                        "film_id": a,
                        "input_revision": 1,
                        "ltx_model": "standard",
                        "machine": None,
                    },
                    {
                        "film_id": b,
                        "input_revision": 1,
                        "ltx_model": "standard",
                        "machine": world.rt.identity.worker_id,
                    },
                ]
            },
        )
        assert [r["ok"] for r in response.json()["results"]] == [True, True]
    finally:
        world.close()


# ── FleetLive tombstone and the worker's busy (R8, R9) ──────────────────
def test_forget_keeps_the_last_heartbeat_as_a_tombstone(world):
    remote = enrol_remote(world.rt, "5070 Ti")
    world.beat(remote)
    world.clock.advance(timedelta(seconds=40))
    world.fleet.forget(remote.worker_id)
    assert world.fleet.entry(remote.worker_id) is None
    assert world.fleet.silent_for(remote.worker_id, world.rt.clock.now()) == (
        pytest.approx(40, abs=1)
    )
    unknown = identity()  # never heard from: the controller's start counts
    assert world.fleet.silent_for(unknown, world.rt.clock.now()) == pytest.approx(
        3640, abs=2
    )
    world.beat(remote)  # back: the tombstone goes
    assert world.fleet.silent_for(remote.worker_id, world.rt.clock.now()) < 1


def test_the_worker_dispatcher_is_busy_while_a_run_or_a_held_child_lives(tmp_path):
    from taskpaw_v3.tests.test_workflow_remote_dispatch import _dispatcher

    d, _ = _dispatcher(tmp_path, lambda: None, None)
    assert d.busy() is False
    d._active = object()
    assert d.busy() is True
    d._active = None
    d._unsettled = (object(), None, None)
    assert d.busy() is True
    d._unsettled = None
    d._held = {"a1"}
    assert d.busy() is False  # no attempt file: no child recorded
