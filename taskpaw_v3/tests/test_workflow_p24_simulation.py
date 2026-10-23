"""P2-4 (#295) §5: the five-worker simulation on a fake clock.

The operator's fleet (3 × 5060 Ti 16 GB, 1 × 5070 Ti 16 GB) plus one 8 GB
card OD5 guards against. Random steps: confirms (自动 or pinned), claims and
finishes, heartbeat loss and return, 改派, 判定已停止, a shrinking (and
recovering) controller disk, routing passes. After every step: each row's
note is the last pass's reason, the 失联 routes are exactly the
`device_lost` films, an attest succeeds only under C4, and the P2-1
invariants hold."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from taskpaw_v3.agent.server import workflow_reads as reads
from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.power_fleet import StatusQuiet
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.worker_api_helpers import VERSION, beat
from taskpaw_v3.tests.workflow_route_helpers import (
    GPU_8,
    GPU_16,
    GPU_16_FAST,
    claim_remote,
    enrol_remote,
    job,
    pin_version,
    resolve_open,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import operator_actions as ops
from taskpaw_v3.workflow import planning, routing
from taskpaw_v3.workflow import scheduler as s
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.local_gpu import FULL_HD

START = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
GB = 10**9
FINISHED = ("succeeded", "skipped")
SETTINGS = WorkflowSettings(enabled=True, inbox_root="X:/inbox", remote_workers=True)
RACED = {"route_claimed", "cas_conflict", "precondition_failed", "not_found"}


class FakeClock:
    def __init__(self) -> None:
        self.value = START

    def now(self) -> datetime:
        return self.value


class Channel:
    """/status as each simulated machine would answer it."""

    def __init__(self, sim: Sim) -> None:
        self.sim = sim

    def quiet(self, address: str) -> StatusQuiet | None:
        worker = self.sim.by_address.get(address)
        if worker is None:
            return None
        since = self.sim.dead_since[worker]
        if since is None:  # up: busy or idle, it answers
            return StatusQuiet(quiet_s=None, checked_s=2.0, state="busy")
        dead = (self.sim.clock.now() - since).total_seconds()
        return StatusQuiet(quiet_s=dead, checked_s=2.0)


class Sim:
    def __init__(self, tmp_path, seed: int) -> None:
        self.rnd = random.Random(seed)
        self.clock = FakeClock()
        self.rt = rt = runtime(tmp_path / "wf", clock=self.clock.now)
        self.remotes = [  # one machine per address (the status channel's key)
            enrol_remote(rt, "5060 Ti-1", gpu=GPU_16, address="192.0.2.41"),
            enrol_remote(rt, "5060 Ti-2", gpu=GPU_16, address="192.0.2.42"),
            enrol_remote(rt, "5060 Ti-3", gpu=GPU_16, address="192.0.2.43"),
            enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST, address="192.0.2.44"),
            enrol_remote(rt, "5060", gpu=GPU_8, limit=FULL_HD, address="192.0.2.45"),
        ]
        self.small = self.remotes[4].worker_id
        self.fleet = FleetLive(self.clock.now(), VERSION)
        self.dead_since: dict[str, datetime | None] = {
            r.worker_id: None for r in self.remotes
        }
        self.by_address = {
            ops.worker_address(
                rt.store.get_worker(rt.operator, r.worker_id).worker
            ): r.worker_id
            for r in self.remotes
        }
        self.free = 2_000 * GB
        self.host = SimpleNamespace(
            state="running",
            reason="",
            runtime=rt,
            fleet=self.fleet,
            dispatcher=None,
            scheduler=None,
            finalizer=None,
            last_pass=None,
            status_channel=lambda: Channel(self),
            status=lambda: {"dispatch": {}},
        )
        self.films = []
        for _ in range(10):
            uhd = self.rnd.random() < 0.3
            self.films.append(
                add_film(
                    rt,
                    width=3840 if uhd else 1920,
                    height=2160 if uhd else 1080,
                    plan=False,
                )
            )
        self.attests = 0
        self.tick(1)

    # ── the world ───────────────────────────────────────────────────────
    def now(self) -> datetime:
        return self.clock.now()

    def tick(self, seconds: float) -> None:
        self.clock.value += timedelta(seconds=seconds)
        for remote in self.remotes:
            if self.dead_since[remote.worker_id] is None:
                self.fleet.observe(
                    remote.worker_id,
                    Heartbeat.model_validate(beat(remote.boot_id or identity())),
                    self.now(),
                )

    def open_assignments(self) -> list[m.OpenAssignment]:
        return list(self.rt.store.list_open_assignments(self.rt.operator))

    # ── steps ───────────────────────────────────────────────────────────
    def confirm(self) -> None:
        unplanned = list(self.rt.store.list_unplanned(self.rt.operator))
        if not unplanned:
            return
        item = self.rnd.choice(unplanned)
        machine = (
            self.rnd.choice(self.remotes).worker_id if self.rnd.random() < 0.3 else None
        )
        (result,) = planning.confirm_plans(
            self.rt,
            [
                {
                    "film_id": item.film_id,
                    "input_revision": item.input_revision,
                    "ltx_model": "standard",
                    "machine": machine,
                }
            ],
            remote=True,
        )
        assert result["ok"], result

    def run_pass(self) -> None:
        outcome = routing.run_pass(
            self.rt.store,
            self.rt.operator,
            self.rt.meta,
            controller_id=self.rt.identity.worker_id,
            live=self.fleet.snapshot(),
            now=self.now(),
            executors=self.rt.executors,
            controller_free_bytes=self.free,
            remote_enabled=True,
            live_since=self.fleet.started_at,
        )
        self.host.last_pass = outcome.planned

    def claim(self) -> None:
        rt = self.rt
        online = [r for r in self.remotes if self.dead_since[r.worker_id] is None]
        if not online:
            return
        remote = self.rnd.choice(online)
        mine = [
            r
            for r in rt.store.list_routes(rt.operator)
            if r.worker_id == remote.worker_id
        ]
        if not mine:
            return
        film_id = mine[0].film_id
        restore, asr = job(rt, film_id, "restore"), job(rt, film_id, "asr")
        name = (
            "restore"
            if restore.status == "pending"
            else "asr"
            if restore.status in FINISHED and asr.status == "pending"
            else None
        )
        if name is None:
            return
        try:
            claim_remote(rt, remote, film_id, name)
        except m.WorkflowError:
            return

    def finish(self) -> None:
        running = [
            o.assignment
            for o in self.open_assignments()
            if o.assignment.status == "running"
            and self.dead_since.get(o.assignment.worker_id or "", "x") is None
        ]
        if running:
            resolve_open(self.rt, self.rnd.choice(running).assignment_id)

    def toggle(self) -> None:
        remote = self.rnd.choice(self.remotes)
        if self.dead_since[remote.worker_id] is None:
            self.dead_since[remote.worker_id] = self.now()  # 失联
        else:
            self.dead_since[remote.worker_id] = None  # back: it beats again

    def reassign(self) -> None:
        rt = self.rt
        film_id = self.rnd.choice(self.films)
        target = self.rnd.choice([None, *(r.worker_id for r in self.remotes)])
        claimed = film_id in reads.claimed_films(rt)
        try:
            ops.reassign(
                rt,
                fleet=self.fleet,
                film_id=film_id,
                input_revision=1,
                expected_version=pin_version(rt, film_id) or 1,
                worker_id=target,
            )
        except m.WorkflowError as exc:
            assert exc.code in RACED, exc.code
            return
        assert not claimed  # 改派 never moves a claimed film

    def attest(self) -> None:
        remote_ids = {r.worker_id for r in self.remotes}
        candidates = [
            o for o in self.open_assignments() if o.assignment.worker_id in remote_ids
        ]
        if not candidates:
            return
        item = self.rnd.choice(candidates)
        assignment = item.assignment
        film_id = self.rt.store.get_stage(self.rt.operator, assignment.job_id).film_id
        worker_id = assignment.worker_id
        assert worker_id is not None
        since = self.dead_since[worker_id]
        now = self.now()
        expected = (
            since is not None
            and (now - since).total_seconds() >= 300
            and (now - assignment.renewed_at).total_seconds() >= 300
        )
        try:
            ops.attest(
                self.rt,
                fleet=self.fleet,
                channel=Channel(self),
                film_id=film_id,
                assignment_id=assignment.assignment_id,
                expected_version=assignment.version,
            )
        except ops.ActionRefused as exc:
            assert not expected, exc.detail  # C4 held: it must be allowed
            return
        assert expected  # an attest only under C4
        self.attests += 1

    def disk(self) -> None:
        self.free = (
            max(0, self.free - 400 * GB)
            if self.rnd.random() < 0.6
            else self.free + 500 * GB
        )

    # ── invariants ──────────────────────────────────────────────────────
    def check(self) -> None:
        rt = self.rt
        routes = rt.store.list_routes(rt.operator)
        claims = reads.claimed_films(rt)
        rows = {
            row["film_id"]: row
            for row in reads.films(SETTINGS, lambda: self.host)["films"]
        }
        # row notes are the last pass's reasons
        routed = {r.film_id for r in routes}
        last = self.host.last_pass
        for film_id, why in (last.waiting if last else {}).items():
            row = rows.get(film_id)
            if (
                row is not None
                and film_id not in routed
                and film_id not in claims
                and row["group"] == "active"
            ):
                assert row["note"] == why, (row["note"], why)
        # the 失联 routes are exactly the device_lost films
        lost = s.lost_films(
            routes,
            live=self.fleet.snapshot(),
            now=self.now(),
            controller_id=rt.identity.worker_id,
            live_since=self.fleet.started_at,
        )
        flagged = {
            f for f, row in rows.items() if row["attention_kind"] == "device_lost"
        }
        assert flagged == {
            f for f in lost if rows.get(f, {}).get("group") in ("active", "attention")
        }
        for film_id in flagged:
            assert rows[film_id]["note"].startswith("设备失联：")
        # P2-1 invariants
        per_worker: dict[str, int] = {}
        for value in routes:
            per_worker[value.worker_id] = per_worker.get(value.worker_id, 0) + 1
            pin = rt.store.get_pin(rt.operator, value.film_id, value.input_revision)
            # a pin to a device holds its route there (改派 to 自动 keeps one)
            if value.kind != "local" and pin is not None and pin.worker_id:
                assert pin.worker_id == value.worker_id
            if value.worker_id == self.small:  # OD5: 8 GB never restores
                assert job(rt, value.film_id, "restore").status in FINISHED
        assert all(count <= s.MAX_OPEN_ROUTES for count in per_worker.values())
        jobs: dict[str, int] = {}
        for item in self.open_assignments():
            jobs[item.assignment.job_id] = jobs.get(item.assignment.job_id, 0) + 1
            if item.stage in ("restore", "asr"):  # OD1: never the controller
                assert item.assignment.worker_id != rt.identity.worker_id
            if item.assignment.worker_id == self.small:
                assert item.stage != "restore"
        assert all(count == 1 for count in jobs.values())

    def step(self) -> None:
        action = self.rnd.choices(
            [
                self.confirm,
                self.run_pass,
                self.claim,
                self.finish,
                self.toggle,
                self.reassign,
                self.attest,
                self.disk,
            ],
            weights=[3, 5, 4, 3, 2, 2, 2, 1],
        )[0]
        action()
        self.tick(self.rnd.choice((5, 30, 90, 200)))


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 8])
def test_five_workers_random_steps_keep_the_board_and_c4_honest(tmp_path, seed):
    sim = Sim(tmp_path, seed)
    try:
        for _ in range(120):
            sim.step()
            sim.check()
        sim.run_pass()
        sim.check()
    finally:
        sim.rt.close()


def test_the_simulation_does_reach_an_allowed_attest(tmp_path):
    """The C4 branch is exercised: a claimed worker dies for 5 min."""
    sim = Sim(tmp_path, 7)
    try:
        sim.confirm()
        sim.run_pass()
        sim.claim()
        for _ in range(20):
            if sim.open_assignments():
                break
            sim.confirm()
            sim.run_pass()
            sim.claim()
        (item,) = sim.open_assignments()[:1]
        sim.dead_since[item.assignment.worker_id] = sim.now()
        sim.tick(120)
        sim.check()
        sim.attest()
        assert sim.attests == 0  # not yet 5 min
        sim.tick(200)
        sim.check()
        sim.attest()
        assert sim.attests == 1
        sim.check()
    finally:
        sim.rt.close()
