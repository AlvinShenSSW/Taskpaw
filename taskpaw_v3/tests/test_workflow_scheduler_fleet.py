"""P2-1 (#292): random fleet sequences over the real store never double-run.

Five fake remote workers (3 × 5060 Ti 16 GB, 1 × 5070 Ti 16 GB, 1 × 5060 8 GB)
are routed by `routing.run_pass` on a fake clock while they claim, finish,
fail, crash, go offline, pause and drain, and the operator withdraws, re-pins
(改派) and attests stopped work (判定已停止). The controller's own dispatcher
runs between the passes (its local routes, reuse-only restores that need no
GPU, translate), films get new input revisions mid-run, and some passes race a
claim, a dispatch, a finish or a 改派 between their snapshot and their writes.
After every step the ledger holds the invariants of §4.4 and OD5.
"""

from __future__ import annotations

import os
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, dispatcher, runtime
from taskpaw_v3.tests.test_workflow_plans import new_revision
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import (
    GPU_8,
    GPU_16,
    GPU_16_FAST,
    QUIET,
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
from taskpaw_v3.workflow.executors import ExecOutcome, JobContext, SyntheticExecutor
from taskpaw_v3.workflow.local_dispatch import should_retry
from taskpaw_v3.workflow.local_gpu import FULL_HD, UHD

START = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
FINISHED = ("succeeded", "skipped")
# A restore-only worker's ASR claim is refused by the first guard it meets: an
# unreconciled boot (owner_unresolved), its restore-only approval or draining
# (precondition_failed), or OD7 R5's route check (route_required). `check`
# asserts no worker ever holds an ASR attempt, whichever guard refused it.
ASR_REFUSED = ("owner_unresolved", "precondition_failed", "route_required")


def seed_ids(monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    """Every uuid4 of this thread (store ids and the helpers' alike) comes from
    the seed. Under the fake clock most rows tie on their timestamps and are
    ordered by id, so random ids would make each run a different sequence."""
    rnd = random.Random(f"fleet-ids-{seed}")
    owner = threading.get_ident()

    class SeededOs:
        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

        @staticmethod
        def urandom(size: int) -> bytes:
            if threading.get_ident() != owner:
                return os.urandom(size)  # a stray thread never shifts the stream
            return rnd.randbytes(size)

    monkeypatch.setattr(uuid, "os", SeededOs())  # uuid4 reads `os.urandom`
    state = rnd.getstate()
    first = uuid.uuid4()
    rnd.setstate(state)
    assert uuid.uuid4() == first, "uuid4 no longer draws from os.urandom"


class FakeClock:
    def __init__(self) -> None:
        self.value = START

    def now(self) -> datetime:
        return self.value


class InstantExecutor(SyntheticExecutor):
    """Succeeds at once; a restore of a `reuse` film needs no GPU (all reused)."""

    def __init__(self, stage, reuse: set[str]) -> None:
        super().__init__(stage, uses_gpu=stage != "translate")
        self.reuse = reuse

    def needs_gpu(self, manifest: m.InputManifest) -> bool:
        if self.stage == "restore" and manifest.film_id in self.reuse:
            return False
        return self.uses_gpu

    def max_resolution(self, vram_mib: int) -> tuple[int, int]:
        # OD7 R8: like WhisperJAV, ASR reads audio only (4K on the 8 GB card)
        return UHD if self.stage == "asr" else super().max_resolution(vram_mib)

    def run(self, context: JobContext) -> ExecOutcome:
        return ExecOutcome.succeeded()


def instant(reuse: set[str]) -> list[InstantExecutor]:
    return [InstantExecutor(stage, reuse) for stage in ("restore", "asr", "translate")]


class Fleet:
    def __init__(self, tmp_path, seed: int) -> None:
        self.rnd = random.Random(seed)
        self.clock = FakeClock()
        self.reuse: set[str] = set()
        self.rt = runtime(
            tmp_path / "wf", clock=self.clock.now, execs=instant(self.reuse)
        )
        rt = self.rt
        self.dispatcher = dispatcher(rt)
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
        self.films: list[str] = []
        self.revision: dict[str, int] = {}
        for index in range(8):
            uhd = self.rnd.random() < 0.3
            film = add_film(
                rt,
                width=3840 if uhd else 1920,
                height=2160 if uhd else 1080,
                plan=False,
            )
            if not uhd and self.rnd.random() < 0.4:
                # its restore is the controller's (C12); the 8 GB controller's
                # approved profiles stop at 1080p, so only 1080p films here
                self.reuse.add(film)
            self.films.append(film)
            self.revision[film] = 1
            self.plan(film)

    # ── helpers ─────────────────────────────────────────────────────────
    def plan(self, film: str) -> None:
        rt = self.rt
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
                input_revision=self.revision[film],
                ltx_model=self.rnd.choice(("standard", "good", "high")),
                machine=pin,
            ),
        )

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
            executors=rt.executors,
            remote_enabled=True,
        )
        for decision in outcome.routed:
            pin = rt.store.get_pin(
                rt.operator, decision.film_id, decision.input_revision
            )
            if pin is not None and pin.worker_id is not None:
                assert (decision.kind, decision.worker_id) == ("pinned", pin.worker_id)
            else:
                assert decision.kind == "auto"
        # IR1: the controller's route is handed over only to a film routed now
        routed = {decision.film_id for decision in outcome.routed}
        for item in outcome.withdrawn:
            if item.reason == s.HAND_OVER:
                assert item.film_id in routed
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
            # an older build would try ASR too: it is refused (ASR_REFUSED)
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
            except m.WorkflowError as exc:
                assert name == "restore" or exc.code in ASR_REFUSED, exc.code
                continue
            return

    def finish(self, *, fail_share: float = 0.2) -> None:
        rt = self.rt
        candidates = [
            a
            for a in self.open_assignments()
            if a.status == "running"
            and a.worker_id in self.online
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
        stage = rt.store.get_stage(rt.operator, job_id)
        if stage.input_revision != self.revision[stage.film_id]:
            return  # an older revision's work is never retried
        if should_retry(rt.store.list_attempts(rt.operator, job_id)):
            if stage.status == "failed":
                rt.store.retry_stage(
                    rt.operator,
                    rt.meta(),
                    m.StageRetry(
                        job_id=job_id, expected_version=stage.version, reason="重试"
                    ),
                )

    def dispatch(self) -> None:
        """The controller's own dispatcher: one admission round."""
        self.dispatcher.run_once()

    def revise(self) -> None:
        """A new input revision (regroup / new files), planned at once."""
        film = self.rnd.choice(self.films)
        self.revision[film] += 1
        new_revision(self.rt, film, self.revision[film])
        self.plan(film)

    def crash(self) -> None:
        remote = self.rnd.choice(self.remotes)
        self.set_remote(boot(self.rt, remote, identity()))

    def settle(self) -> None:
        """判定已停止 for one unknown assignment, then the worker re-posts its boot."""
        unknown = [
            a
            for a in self.open_assignments()
            if a.status == "unknown" and a.worker_id in self.online
        ]
        if not unknown:
            for remote in self.remotes:  # C6: re-post until reconciled
                self.set_remote(boot(self.rt, remote, remote.boot_id))
            return
        current = self.rnd.choice(unknown)
        self.clock.value += QUIET  # IR4: no renewal for five minutes
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
        revision = self.revision[film]
        target = self.rnd.choice([None, *(r.worker_id for r in self.remotes)])
        try:
            rt.store.set_pin(
                rt.operator,
                rt.meta(),
                m.PinSet(
                    film_id=film,
                    input_revision=revision,
                    expected_version=pin_version(rt, film, revision),
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

    def race(self) -> None:
        """A pass whose snapshot goes stale before its writes (CAS loses)."""
        store = self.rt.store
        real = store.routing_snapshot
        meanwhile = self.rnd.choice(
            (
                lambda: self.claim(self.rnd.choice(self.remotes)),
                self.dispatch,
                self.finish,
                self.repin,
            )
        )

        def racing(auth, *hold):
            snapshot = real(auth, *hold)
            meanwhile()
            return snapshot

        store.routing_snapshot = racing  # type: ignore[method-assign]
        try:
            self.run_pass()
        finally:
            del store.routing_snapshot

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
            # every unresolved compute claim, of any revision, sits on its
            # worker's open route (IR6: a new revision keeps a claimed route)
            assert not conn.execute(
                "SELECT a.assignment_id FROM assignments a "
                "JOIN stages s ON s.job_id=a.job_id "
                "WHERE a.resolved_at IS NULL AND a.worker_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM film_routes r WHERE r.state='open' "
                "AND r.film_id=s.film_id AND r.input_revision=s.input_revision "
                "AND r.worker_id=a.worker_id)"
            ).fetchall()
            # S4: a worker queues at most two routes, old revisions included
            assert not conn.execute(
                "SELECT worker_id FROM film_routes WHERE state='open' "
                "AND worker_id!=? GROUP BY worker_id HAVING count(*)>?",
                (rt.identity.worker_id, s.MAX_OPEN_ROUTES),
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
            # OD7: ASR is the controller's alone, on its own local route
            assert not conn.execute(
                "SELECT 1 FROM attempts a JOIN stages s ON s.job_id=a.job_id "
                "WHERE s.stage='asr' AND a.worker_id!=?",
                (rt.identity.worker_id,),
            ).fetchall()
            # IR3: a restore that needs no GPU is the controller's, never routed
            reuse = sorted(self.reuse)
            marks = ",".join("?" * len(reuse))
            assert not conn.execute(
                "SELECT 1 FROM attempts a JOIN stages s ON s.job_id=a.job_id "
                f"WHERE s.stage='restore' AND a.worker_id!=? AND s.film_id IN ({marks})",
                (rt.identity.worker_id, *reuse),
            ).fetchall()
            assert not conn.execute(
                "SELECT 1 FROM film_routes r JOIN stages s ON s.film_id=r.film_id "
                "AND s.input_revision=r.input_revision AND s.stage='restore' "
                "WHERE r.state='open' AND r.worker_id!=? AND s.status='pending' "
                f"AND r.film_id IN ({marks})",
                (rt.identity.worker_id, *reuse),
            ).fetchall()
            # a film pinned to a remote worker is only ever routed there
            assert not conn.execute(
                "SELECT 1 FROM film_routes r JOIN film_pins p ON p.film_id=r.film_id "
                "AND p.input_revision=r.input_revision WHERE r.state='open' "
                "AND p.worker_id IS NOT NULL AND p.worker_id!=r.worker_id "
                "AND r.worker_id!=?",
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
                "dispatch",
                "revise",
                "race",
            ),
            weights=(6, 6, 6, 2, 2, 1, 1, 1, 1, 1, 3, 3, 1, 2),
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
        elif action == "dispatch":
            self.dispatch()
        elif action == "revise":
            self.revise()
        elif action == "race":
            self.race()
        else:
            self.clock.value += timedelta(seconds=self.rnd.randint(10, 900))

    def heal(self) -> None:
        """Everyone back, enabled and reconciled; unknown work attested."""
        rt = self.rt
        for remote in self.remotes:
            self.online[remote.worker_id] = True
            self.paused[remote.worker_id] = False
        while any(
            rt.store.get_worker(rt.operator, r.worker_id).worker.admission_state
            != "enabled"
            for r in self.remotes
        ):
            self.drain()  # flips a random worker; repeat until all enabled
        while any(
            a.status == "unknown" and a.worker_id in self.online
            for a in self.open_assignments()
        ):
            self.settle()
        self.settle()


@pytest.mark.parametrize("seed", range(10))
def test_random_fleet_sequences_never_double_run(tmp_path, monkeypatch, seed):
    seed_ids(monkeypatch, seed)
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
            fleet.dispatch()
            fleet.settle()  # C6: a worker re-posts its boot until reconciled
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
            pin = rt.store.get_pin(rt.operator, film, fleet.revision[film])
            assert pin.worker_id == fleet.small
            assert outcome.planned.waiting[film].startswith("等待 5060：")
    finally:
        fleet.rt.close()


def pass_once(rt, clock, live):
    return routing.run_pass(
        rt.store,
        rt.operator,
        rt.meta,
        controller_id=rt.identity.worker_id,
        live=live,
        now=clock.now(),
        executors=rt.executors,
        remote_enabled=True,
    )


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
        live = {remote.worker_id: s.WorkerLive(last_seen=clock.now())}
        outcome = pass_once(rt, clock, live)
        (decision,) = outcome.routed
        value = rt.store.film_route(rt.operator, film, 1)
        assert (value.worker_id, value.kind, value.reason) == (
            remote.worker_id,
            "auto",
            decision.reason,
        )
        assert "无历史，按排队先后" in value.reason
        # a lost CAS (the film was routed meanwhile) is skipped, not raised
        assert pass_once(rt, clock, live).routed == ()
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

        def racing(auth, *hold):
            snapshot = real(auth, *hold)
            route(rt, film, remote.worker_id, reason="并发")  # someone was faster
            return snapshot

        monkeypatch.setattr(rt.store, "routing_snapshot", racing)
        live = {remote.worker_id: s.WorkerLive(last_seen=clock.now())}
        outcome = pass_once(rt, clock, live)
        assert outcome.routed == () and len(outcome.planned.routes) == 1
        assert rt.store.film_route(rt.operator, film, 1).reason == "并发"
    finally:
        rt.close()


def test_a_withdrawal_decided_before_a_repin_loses_its_cas(tmp_path, monkeypatch):
    """CX2: the operator pins the film to its (draining) worker after the
    pass's snapshot; the pass's withdrawal of that route must not land."""
    clock = FakeClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        drained = enrol_remote(rt, "5060 Ti-1", gpu=GPU_16)
        other = enrol_remote(rt, "5060 Ti-2", gpu=GPU_16)
        film = add_film(rt)  # 本机 → 自动
        route(rt, film, drained.worker_id)
        worker = rt.store.get_worker(rt.operator, drained.worker_id).worker
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
                admission_state="draining",
            ),
        )
        real = rt.store.routing_snapshot

        def racing(auth, *hold):
            snapshot = real(auth, *hold)
            rt.store.set_pin(
                rt.operator,
                rt.meta(),
                m.PinSet(
                    film_id=film,
                    input_revision=1,
                    expected_version=pin_version(rt, film),
                    worker_id=drained.worker_id,
                    reason="改派",
                ),
            )
            return snapshot

        monkeypatch.setattr(rt.store, "routing_snapshot", racing)
        live = {
            r.worker_id: s.WorkerLive(last_seen=clock.now()) for r in (drained, other)
        }
        outcome = pass_once(rt, clock, live)
        # the lost withdrawal sends the pass to a fresh snapshot (CX4), where
        # the pin keeps the route
        assert outcome.planned.withdrawals == ()
        assert outcome.withdrawn == () and outcome.routed == ()
        assert rt.store.film_route(rt.operator, film, 1).worker_id == drained.worker_id
    finally:
        rt.close()


def test_a_lost_withdrawal_frees_no_slot_for_the_pass(tmp_path, monkeypatch):
    """CX4: the pass withdraws a route the worker cannot run and gives its slot
    to a waiting film; the operator pins the film to that worker after the
    snapshot, so the withdrawal loses. The waiting film must not become the
    worker's third open route: the pass decides again from a fresh snapshot."""
    clock = FakeClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        worker = enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST)
        films = []
        for tier in ("high", "standard", "standard"):
            film = add_film(rt, plan=False)
            rt.store.confirm_plan(
                rt.operator,
                rt.meta(),
                m.FilmPlanConfirm(
                    film_id=film, input_revision=1, ltx_model=tier, machine=None
                ),
            )
            films.append(film)
        lacking, queued, waiting = films
        route(rt, lacking, worker.worker_id)
        route(rt, queued, worker.worker_id)
        real = rt.store.routing_snapshot
        raced = []

        def racing(auth, *hold):
            snapshot = real(auth, *hold)
            if not raced:
                raced.append(True)
                rt.store.set_pin(
                    rt.operator,
                    rt.meta(),
                    m.PinSet(
                        film_id=lacking,
                        input_revision=1,
                        expected_version=pin_version(rt, lacking),
                        worker_id=worker.worker_id,
                        reason="改派",
                    ),
                )
            return snapshot

        monkeypatch.setattr(rt.store, "routing_snapshot", racing)
        live = {
            worker.worker_id: s.WorkerLive(
                last_seen=clock.now(), tiers_ready=frozenset({"standard"})
            )
        }
        outcome = pass_once(rt, clock, live)
        assert outcome.withdrawn == () and outcome.routed == ()
        assert rt.store.film_route(rt.operator, waiting, 1) is None
        mine = [
            r
            for r in rt.store.list_routes(rt.operator)
            if r.worker_id == worker.worker_id
        ]
        assert len(mine) == s.MAX_OPEN_ROUTES
        assert outcome.planned.waiting[waiting] == f"等待设备：{s.QUEUE_FULL}"
    finally:
        rt.close()


def test_a_plan_on_the_controller_routes_auto(tmp_path):
    """OD1/IR9: the phase-1 待处理 confirms with 本机, which is stored as 自动."""
    clock = FakeClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        remote = enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST)
        film = add_film(rt)  # planned with machine = the controller
        pin = rt.store.get_pin(rt.operator, film, 1)
        assert (pin.worker_id, pin.version) == (None, 1)
        live = {remote.worker_id: s.WorkerLive(last_seen=clock.now())}
        (decision,) = pass_once(rt, clock, live).routed
        assert decision.kind == "auto"
        (plan,) = rt.store.list_plans(rt.operator)
        assert plan.machine == rt.identity.worker_id  # the plan never changes
        assert rt.store.film_route(rt.operator, film, 1).worker_id == remote.worker_id
    finally:
        rt.close()
