"""P2-1 (#292): random fleet sequences over the real store never double-run.

Five fake remote workers (3 × 5060 Ti 16 GB, 1 × 5070 Ti 16 GB, 1 × 5060 8 GB)
are routed by `routing.run_pass` on a fake clock while they claim, finish,
fail, crash, go offline, pause and drain, and the operator withdraws, re-pins
(改派) and attests stopped work (判定已停止). After every step the ledger
holds the invariants of §4.4 and OD5.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import (
    GPU_8,
    GPU_16,
    GPU_16_FAST,
    Remote,
    attest,
    boot,
    claim_remote,
    enrol_remote,
    job,
    pin_version,
    resolve_open,
    route,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import routing
from taskpaw_v3.workflow import scheduler as s
from taskpaw_v3.workflow.local_dispatch import should_retry
from taskpaw_v3.workflow.local_gpu import FULL_HD

START = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
FINISHED = ("succeeded", "skipped")


class FakeClock:
    def __init__(self) -> None:
        self.value = START

    def now(self) -> datetime:
        return self.value


class Fleet:
    def __init__(self, tmp_path, seed: int) -> None:
        self.rnd = random.Random(seed)
        self.clock = FakeClock()
        self.rt = runtime(tmp_path / "wf", clock=self.clock.now)
        rt = self.rt
        self.remotes = [
            enrol_remote(rt, "5060 Ti-1", gpu=GPU_16),
            enrol_remote(rt, "5060 Ti-2", gpu=GPU_16),
            enrol_remote(rt, "5060 Ti-3", gpu=GPU_16),
            enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST),
            enrol_remote(rt, "5060", gpu=GPU_8, limit=FULL_HD),
        ]
        self.small = self.remotes[4].worker_id
        self.online = {r.worker_id: True for r in self.remotes}
        self.paused = {r.worker_id: False for r in self.remotes}
        self.films = []
        for index in range(8):
            uhd = self.rnd.random() < 0.3
            film = add_film(
                rt,
                width=3840 if uhd else 1920,
                height=2160 if uhd else 1080,
                plan=False,
            )
            pin = (
                self.rnd.choice(self.remotes).worker_id
                if self.rnd.random() < 0.25
                else None
            )
            rt.store.confirm_plan(
                rt.operator,
                rt.meta(),
                m.FilmPlanConfirm(
                    film_id=film,
                    input_revision=1,
                    ltx_model=self.rnd.choice(("standard", "good", "high")),
                    machine=pin,
                ),
            )
            self.films.append(film)

    # ── helpers ─────────────────────────────────────────────────────────
    def remote(self, worker_id: str) -> Remote:
        return next(r for r in self.remotes if r.worker_id == worker_id)

    def set_remote(self, value: Remote) -> None:
        self.remotes = [
            value if r.worker_id == value.worker_id else r for r in self.remotes
        ]

    def live(self) -> dict[str, s.WorkerLive]:
        now = self.clock.now()
        return {
            r.worker_id: s.WorkerLive(
                last_seen=now
                - timedelta(seconds=5 if self.online[r.worker_id] else 300),
                paused=self.paused[r.worker_id],
            )
            for r in self.remotes
        }

    def run_pass(self) -> routing.PassOutcome:
        rt = self.rt
        outcome = routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live=self.live(),
            now=self.clock.now(),
        )
        for decision in outcome.routed:
            pin = rt.store.get_pin(rt.operator, decision.film_id, 1)
            if pin is not None and pin.worker_id not in (None, rt.identity.worker_id):
                assert (decision.kind, decision.worker_id) == ("pinned", pin.worker_id)
            else:
                assert decision.kind == "auto"
        return outcome

    def open_assignments(self) -> list[m.AssignmentRecord]:
        return [
            o.assignment for o in self.rt.store.list_open_assignments(self.rt.operator)
        ]

    def claim(self, remote: Remote) -> None:
        rt = self.rt
        if not self.online[remote.worker_id]:
            return
        for value in rt.store.list_routes(rt.operator):
            if value.worker_id != remote.worker_id:
                continue
            restore, asr = (
                job(rt, value.film_id, "restore"),
                job(rt, value.film_id, "asr"),
            )
            name = (
                "restore"
                if restore.status == "pending"
                else "asr"
                if restore.status in FINISHED and asr.status == "pending"
                else None
            )
            if name is None:
                continue
            try:
                claim_remote(rt, remote, value.film_id, name)
            except m.WorkflowError:
                continue
            return

    def finish(self, *, fail_share: float = 0.2) -> None:
        rt = self.rt
        candidates = [
            a
            for a in self.open_assignments()
            if a.status == "running"
            and a.worker_id is not None
            and a.boot_id == self.remote(a.worker_id).boot_id
        ]
        if not candidates:
            return
        current = self.rnd.choice(candidates)
        outcome = "failed" if self.rnd.random() < fail_share else "succeeded"
        resolve_open(rt, current.assignment_id, outcome)
        if outcome == "failed":
            self.retry(current.job_id)

    def retry(self, job_id: str) -> None:
        rt = self.rt
        if should_retry(rt.store.list_attempts(rt.operator, job_id)):
            stage = rt.store.get_stage(rt.operator, job_id)
            if stage.status == "failed":
                rt.store.retry_stage(
                    rt.operator,
                    rt.meta(),
                    m.StageRetry(
                        job_id=job_id, expected_version=stage.version, reason="重试"
                    ),
                )

    def crash(self) -> None:
        remote = self.rnd.choice(self.remotes)
        self.set_remote(boot(self.rt, remote, identity()))

    def settle(self) -> None:
        """判定已停止 for one unknown assignment, then the worker re-posts its boot."""
        unknown = [a for a in self.open_assignments() if a.status == "unknown"]
        if not unknown:
            for remote in self.remotes:  # C6: re-post until reconciled
                self.set_remote(boot(self.rt, remote, remote.boot_id))
            return
        current = self.rnd.choice(unknown)
        attest(self.rt, current.assignment_id)
        self.retry(current.job_id)
        remote = self.remote(current.worker_id)
        self.set_remote(boot(self.rt, remote, remote.boot_id))

    def withdraw(self) -> None:
        rt = self.rt
        routes = rt.store.list_routes(rt.operator)
        if not routes:
            return
        value = self.rnd.choice(routes)
        try:
            rt.store.withdraw_route(
                rt.operator,
                rt.meta(),
                m.RouteWithdraw(
                    route_id=value.route_id,
                    expected_version=value.version,
                    reason="操作者撤回",
                ),
            )
        except m.WorkflowError as exc:
            assert exc.code == "route_claimed"

    def repin(self) -> None:
        rt = self.rt
        film = self.rnd.choice(self.films)
        target = self.rnd.choice([None, *(r.worker_id for r in self.remotes)])
        try:
            rt.store.set_pin(
                rt.operator,
                rt.meta(),
                m.PinSet(
                    film_id=film,
                    input_revision=1,
                    expected_version=pin_version(rt, film),
                    worker_id=target,
                    reason="改派",
                ),
            )
        except m.WorkflowError as exc:
            assert exc.code == "route_claimed"

    def drain(self) -> None:
        rt = self.rt
        remote = self.rnd.choice(self.remotes)
        worker = rt.store.get_worker(rt.operator, remote.worker_id).worker
        rt.store.update_worker(
            rt.operator,
            rt.meta(),
            m.WorkerUpdate(
                worker_id=worker.worker_id,
                expected_version=worker.version,
                address=worker.address,
                port=worker.port,
                network_revision=worker.network_revision,
                workflow_participation=True,
                admission_state="draining"
                if worker.admission_state == "enabled"
                else "enabled",
            ),
        )

    # ── invariants ──────────────────────────────────────────────────────
    def check(self) -> None:
        rt = self.rt
        conn = rt.store._conn
        with rt.store._lock:
            assert not conn.execute(
                "SELECT job_id FROM assignments WHERE resolved_at IS NULL "
                "GROUP BY job_id HAVING count(*)>1"
            ).fetchall()
            assert not conn.execute(
                "SELECT physical_resource_id FROM resource_owners "
                "WHERE resolved_at IS NULL GROUP BY physical_resource_id "
                "HAVING count(*)>1"
            ).fetchall()
            assert not conn.execute(
                "SELECT film_id FROM film_routes WHERE state='open' "
                "GROUP BY film_id,input_revision HAVING count(*)>1"
            ).fetchall()
            # every unresolved compute claim sits on its worker's open route
            assert not conn.execute(
                "SELECT a.assignment_id FROM assignments a "
                "JOIN stages s ON s.job_id=a.job_id "
                "WHERE a.resolved_at IS NULL AND a.worker_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM film_routes r WHERE r.state='open' "
                "AND r.film_id=s.film_id AND r.input_revision=s.input_revision "
                "AND r.worker_id=a.worker_id)"
            ).fetchall()
            # OD5: the 8 GB card never restores, nor holds a film before its restore
            assert not conn.execute(
                "SELECT 1 FROM attempts a JOIN stages s ON s.job_id=a.job_id "
                "WHERE s.stage='restore' AND a.worker_id=?",
                (self.small,),
            ).fetchall()
            assert not conn.execute(
                "SELECT 1 FROM film_routes r JOIN stages s ON s.film_id=r.film_id "
                "AND s.input_revision=r.input_revision AND s.stage='restore' "
                "WHERE r.state='open' AND r.worker_id=? "
                "AND s.status NOT IN ('succeeded','skipped')",
                (self.small,),
            ).fetchall()
            # a film pinned to a remote worker is only ever routed there
            assert not conn.execute(
                "SELECT 1 FROM film_routes r JOIN film_pins p ON p.film_id=r.film_id "
                "AND p.input_revision=r.input_revision WHERE r.state='open' "
                "AND p.worker_id IS NOT NULL AND p.worker_id!=? AND p.worker_id!=r.worker_id",
                (rt.identity.worker_id,),
            ).fetchall()

    def step(self) -> None:
        action = self.rnd.choices(
            (
                "pass",
                "claim",
                "finish",
                "crash",
                "settle",
                "withdraw",
                "repin",
                "online",
                "pause",
                "drain",
                "tick",
            ),
            weights=(6, 6, 6, 2, 2, 1, 1, 1, 1, 1, 3),
        )[0]
        if action == "pass":
            self.run_pass()
        elif action == "claim":
            self.claim(self.rnd.choice(self.remotes))
        elif action == "finish":
            self.finish()
        elif action == "crash":
            self.crash()
        elif action == "settle":
            self.settle()
        elif action == "withdraw":
            self.withdraw()
        elif action == "repin":
            self.repin()
        elif action == "online":
            worker = self.rnd.choice(self.remotes).worker_id
            self.online[worker] = not self.online[worker]
        elif action == "pause":
            worker = self.rnd.choice(self.remotes).worker_id
            self.paused[worker] = not self.paused[worker]
        elif action == "drain":
            self.drain()
        else:
            self.clock.value += timedelta(seconds=self.rnd.randint(10, 900))

    def heal(self) -> None:
        """Everyone back, enabled and reconciled; unknown work attested."""
        rt = self.rt
        for remote in self.remotes:
            self.online[remote.worker_id] = True
            self.paused[remote.worker_id] = False
            worker = rt.store.get_worker(rt.operator, remote.worker_id).worker
            if worker.admission_state != "enabled":
                self.drain()  # flips a random worker; repeat until all enabled
        while any(
            rt.store.get_worker(rt.operator, r.worker_id).worker.admission_state
            != "enabled"
            for r in self.remotes
        ):
            self.drain()
        while any(a.status == "unknown" for a in self.open_assignments()):
            self.settle()
        self.settle()


@pytest.mark.parametrize("seed", range(10))
def test_random_fleet_sequences_never_double_run(tmp_path, seed):
    fleet = Fleet(tmp_path, seed)
    try:
        for _ in range(200):
            fleet.step()
            fleet.check()
        fleet.heal()
        fleet.check()
        for _ in range(200):  # healthy fleet: everything routable finishes
            fleet.run_pass()
            for remote in fleet.remotes:
                fleet.claim(remote)
            fleet.finish(fail_share=0.0)
            fleet.check()
        rt = fleet.rt
        outcome = fleet.run_pass()
        for film in fleet.films:
            restore, asr = job(rt, film, "restore"), job(rt, film, "asr")
            if "failed" in (restore.status, asr.status):
                continue  # transient failures ran out of retries: the operator's
            if restore.status in FINISHED and asr.status in FINISHED:
                continue
            # the only film left is one pinned to the 8 GB card, waiting visibly
            pin = rt.store.get_pin(rt.operator, film, 1)
            assert pin.worker_id == fleet.small
            assert outcome.planned.waiting[film].startswith("等待 5060：")
    finally:
        fleet.rt.close()


def test_the_pass_persists_its_reason_on_the_route(tmp_path):
    clock = FakeClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        remote = enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST)
        film = add_film(rt, plan=False)
        rt.store.confirm_plan(
            rt.operator,
            rt.meta(),
            m.FilmPlanConfirm(
                film_id=film, input_revision=1, ltx_model="standard", machine=None
            ),
        )
        outcome = routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live={remote.worker_id: s.WorkerLive(last_seen=clock.now())},
            now=clock.now(),
        )
        (decision,) = outcome.routed
        value = rt.store.film_route(rt.operator, film, 1)
        assert (value.worker_id, value.kind, value.reason) == (
            remote.worker_id,
            "auto",
            decision.reason,
        )
        assert "无历史，按排队先后" in value.reason
        # a lost CAS (the film was routed meanwhile) is skipped, not raised
        again = routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live={remote.worker_id: s.WorkerLive(last_seen=clock.now())},
            now=clock.now(),
        )
        assert again.routed == ()
    finally:
        rt.close()


def test_a_stale_snapshot_decision_loses_its_cas(tmp_path, monkeypatch):
    clock = FakeClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        remote = enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST)
        film = add_film(rt, plan=False)
        rt.store.confirm_plan(
            rt.operator,
            rt.meta(),
            m.FilmPlanConfirm(
                film_id=film, input_revision=1, ltx_model="standard", machine=None
            ),
        )
        real = rt.store.routing_snapshot

        def racing(auth):
            snapshot = real(auth)
            route(rt, film, remote.worker_id, reason="并发")  # someone was faster
            return snapshot

        monkeypatch.setattr(rt.store, "routing_snapshot", racing)
        outcome = routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live={remote.worker_id: s.WorkerLive(last_seen=clock.now())},
            now=clock.now(),
        )
        assert outcome.routed == () and len(outcome.planned.routes) == 1
        assert rt.store.film_route(rt.operator, film, 1).reason == "并发"
    finally:
        rt.close()


def test_a_phase1_pin_to_the_controller_becomes_auto(tmp_path):
    """OD1: the phase-1 待处理 confirms with 本机; the pass clears that pin."""
    clock = FakeClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        remote = enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST)
        film = add_film(rt)  # planned with machine = the controller
        assert rt.store.get_pin(rt.operator, film, 1).worker_id == rt.identity.worker_id
        outcome = routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live={remote.worker_id: s.WorkerLive(last_seen=clock.now())},
            now=clock.now(),
        )
        (decision,) = outcome.routed
        assert (decision.kind, decision.unpin) == ("auto", True)
        pin = rt.store.get_pin(rt.operator, film, 1)
        assert (pin.worker_id, pin.version) == (None, 2)
        (plan,) = rt.store.list_plans(rt.operator)
        assert plan.machine == rt.identity.worker_id  # the plan never changes
        assert rt.store.film_route(rt.operator, film, 1).worker_id == remote.worker_id
    finally:
        rt.close()
