"""P2-3 (#294) §7: remote execution end to end, and the crash matrix (§5).

A real controller (`LocalWorkflowHost`: store, dispatcher, routing thread,
fleet, transfers, the 5682 app on a loopback port) and a real worker
(`WorkerHost` → `RemoteDispatcher` → `WorkerClient`) with fake Jasna and
WhisperJAV (`launch_prefix`), a fake GPU, its own GPU lease and a fake power
sampler. The controller translates, returns and scrapes with fakes. Nothing
touches a real GPU, the operator's folders or `%APPDATA%`.

Each crash case asserts its end state, at most one unresolved assignment per
job, at most one unresolved owner per GPU, and how often the fake Jasna ran.
"""

from __future__ import annotations

import functools
import json
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, cast
from urllib.parse import quote

import pytest

from taskpaw_v3 import __version__
from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_8GB
from taskpaw_v3.tests.test_workflow_remote_worker import Server, until
from taskpaw_v3.tests.test_workflow_scrape_step import scrape_step
from taskpaw_v3.tests.workflow_exec_helpers import Tools, video
from taskpaw_v3.tests.workflow_route_helpers import StepClock, attest
from taskpaw_v3.workflow import board, devices, local_host, routing, transfer
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.keep_awake import KeepAwake
from taskpaw_v3.workflow.local_dispatch import NO_GPU_DEVICE, REMOTE_GPU
from taskpaw_v3.workflow.local_gpu import LocalGpu
from taskpaw_v3.workflow.local_runtime import LocalRuntime
from taskpaw_v3.workflow.remote_dispatch import AttemptState
from taskpaw_v3.workflow.remote_worker import Probe, WorkerHost, probe_body
from taskpaw_v3.workflow.return_step import LocalReturnStep
from taskpaw_v3.workflow.scheduler import CONTROLLER_DISK
from taskpaw_v3.workflow.worker_client import ApiError, WorkerClient

WORKER_GPU = LocalGpu("NVIDIA GeForce RTX 5060 Ti", "GPU-fake-16gb", 16311)
VERSION = __version__  # the controller's own: no 「版本不一致」
LONG = 60.0  # a condition wait's ceiling; never a timing budget
# §11 Linux CI: the controller's free disk is injected, never the runner's
# (a GitHub Linux runner has far less than the C8 reserve of 100 GB free)
CONTROLLER_FREE = 2_000 * 10**9


@pytest.fixture(autouse=True)
def probeable_video(monkeypatch):
    # the fake ffprobe reads a video's media facts from its JSON content
    monkeypatch.setattr(ret, "VIDEO", video(1920, 1080, 120.0))


# ── fakes ───────────────────────────────────────────────────────────────
class Reading:
    def __init__(self, owner: str, sampler: int) -> None:
        self.owner, self.sampler = owner, sampler


class Delta:
    def to_dict(self) -> dict[str, float]:
        return {
            "wh": 2.5,
            "gpu_wh": 2.0,
            "cpu_wh": 0.3,
            "other_wh": 0.2,
            "seconds": 3.0,
            "avg_w": 120.0,
            "peak_w": 150.0,
        }


class FakeSampler:
    """PW04 per-owner counters; `restart()` is a new sampler process."""

    def __init__(self) -> None:
        self.generation = 1
        self.readings: list[str] = []

    def reading(self, owner: str) -> Reading:
        self.readings.append(owner)
        return Reading(owner, self.generation)

    def energy_between(self, start: Reading, end: Reading) -> Delta | None:
        return Delta() if start.sampler == end.sampler else None

    def live(self) -> dict[str, Any]:
        return {"w": 120.0}

    def restart(self) -> None:
        self.generation += 1


class World:
    """One controller and the workers that join it."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp, self.clock = tmp_path, StepClock()
        self.tools = Tools(tmp_path / "tools")
        self.sampler = FakeSampler()
        clock = self.clock

        def make_runtime(data_dir, executors):
            return LocalRuntime(
                data_dir,
                executors,
                gpu=lambda: GPU_8GB,
                machine=lambda: "controller-machine",
                clock_source=clock.now,
            )

        monkeypatch.setattr(local_host, "LocalRuntime", make_runtime)
        # the C8 gates of the routing pass and of the output plans read this,
        # not the host's disk; `free` lets a test shrink it
        self.free = CONTROLLER_FREE
        monkeypatch.setattr(transfer, "disk_free", lambda path: self.free)
        monkeypatch.setattr(
            local_host,
            "Transfers",
            functools.partial(transfer.Transfers, free_bytes=lambda p: self.free),
        )
        # every real pass, so a decision that never routes is named (§11)
        self.passes: list[Any] = []
        real_pass = routing.run_pass

        def recorded(*args: Any, **kw: Any) -> Any:
            outcome = real_pass(*args, **kw)
            self.passes.append(outcome)
            return outcome

        monkeypatch.setattr(routing, "run_pass", recorded)
        self.settings = WorkflowSettings(
            enabled=True, role="controller", remote_workers=True
        )
        self.host: local_host.LocalWorkflowHost | None = None
        self.down = False
        self.restarts = 0  # completed restarts: a read waits one out
        self.start_controller()
        self.server = Server(self)  # the app reads `state`, `runtime`, … below
        self.workers: list[WorkerHost] = []
        self.issued: dict[str, Any] = {}

    # the worker API's late-bound provider sees this World as the host
    def __getattr__(self, name: str) -> Any:
        if name in {"state", "runtime", "fleet", "transfers", "dispatcher"} | {
            "cancels",
            "remote_resolved",
            "scheduler",
        }:
            host = self.__dict__.get("host")
            if self.__dict__.get("down") or host is None:
                return "starting" if name == "state" else None
            return getattr(host, name)
        raise AttributeError(name)

    # ── the controller ──
    def start_controller(self) -> None:
        tools_rt: dict[str, Any] = {}

        def executors(rt):
            tools_rt["rt"] = rt
            return [
                SyntheticExecutor("restore"),  # needs a GPU: routed to a worker
                self.tools.asr(rt),  # OD7: the controller transcribes (fake WhisperJAV)
                ret.FakeTranslate(rt),
            ]

        host = local_host.LocalWorkflowHost(
            self.settings,
            self.tmp / "controller",
            executor_factory=executors,
            return_step=LocalReturnStep(),
            scrape_step=scrape_step(self.tmp),
        )
        host.start()
        until(lambda: host.state != "starting", LONG)
        assert host.state == "running", host.reason
        self.host = host

    def restart_controller(self) -> None:
        """The controller process stops and starts again on the same data
        dir; meanwhile the worker API answers 503 (transient)."""
        self.down = True
        old, self.host = self.host, None
        assert old is not None
        old.stop()
        self.start_controller()
        self.restarts += 1
        self.down = False

    def read(self, fn: Callable[[LocalRuntime], Any]) -> Any:
        """Read the running controller's store. A restart that begins after
        `rt` was taken closes that store (`authority_unavailable`): the read
        then waits for the restart to finish, by its counter, and runs on the
        new runtime. Any other failure, or no restart, is the test's failure."""
        while True:
            seen = self.restarts
            try:
                return fn(self.rt)
            except (m.WorkflowError, RuntimeError) as exc:
                # a store closed under the reader, or a stopped runtime
                # that has none any more
                if str(exc) not in {
                    "authority_unavailable",
                    "workflow runtime has no store yet",
                }:
                    raise
                until(lambda: self.restarts != seen and not self.down, LONG)

    @property
    def rt(self) -> LocalRuntime:
        """The running controller's runtime (waits out a restart)."""
        until(lambda: not self.down and self.host is not None, LONG)
        host = self.host
        assert host is not None and host.runtime is not None
        return host.runtime

    def wake(self) -> None:
        """The 30 s routing tick, now."""
        assert self.host is not None and self.host.scheduler is not None
        self.host.scheduler.wake()

    # ── workers ──
    def executors(self, runtime):
        return [self.tools.restore(runtime), self.tools.asr(runtime)]

    def probe(self, settings, staging):
        unbound = cast(Any, None)
        body = probe_body(self.executors(unbound), WORKER_GPU, staging)
        return Probe(body, ("standard", "good", "high"), True)

    def worker(self, name: str = "w1", **kw: Any) -> WorkerHost:
        data = self.tmp / name
        worker = WorkerHost(
            WorkflowSettings(enabled=True, role="worker", staging_cap_gb=500),
            data,
            probe=self.probe,
            gpu_live=lambda: {"used_mib": 512, "util": 1.0, "power_w": 18.0},
            version=VERSION,
            active_seconds=0.05,
            idle_seconds=0.05,
            backoff_base=0.02,
            staging_free=lambda path: 2_000 * 10**9,
            executor_factory=self.executors,
            lease=kw.pop("lease", GpuLease()),
            power=lambda: self.sampler,
            keep_awake=KeepAwake(lambda flags: 1),
            dispatch_poll=0.02,
            stop_settle=10.0,
            **kw,
        )
        self.workers.append(worker)
        return worker

    def join(self, name: str = "w1", **kw: Any) -> WorkerHost:
        """Pair, probe, 核验并启用 (OD4) and boot one worker."""
        issued = devices.add_device(
            self.rt, name, "192.168.1.31", host="127.0.0.1", port=self.server.port
        )
        worker = self.worker(name, **kw)
        worker.join(issued["code"], str(self.tmp / name / "staging"))
        until(lambda: worker.state == "running", LONG)
        version = self.rt.store.get_worker(self.rt.operator, issued["worker_id"]).worker
        devices.approve(
            self.rt, issued["worker_id"], version.version, restore=True, asr=True
        )
        self.issued[name] = issued
        until(lambda: worker.reconciled, LONG)
        return worker

    def restart_worker(self, old: WorkerHost, name: str = "w1", **kw) -> WorkerHost:
        """A new worker process on the same data and staging (a new boot)."""
        assert old.stop()
        worker = self.worker(name, **kw)
        worker.start()
        return worker

    def close(self) -> None:
        for worker in self.workers:
            worker.stop()
        self.server.stop()
        if self.host is not None:
            self.host.stop()

    # ── reads ──
    def stage(self, film: str, name: str) -> m.StageRecord:
        stages = self.read(lambda rt: rt.store.get_film(rt.operator, film).stages)
        return next(s for s in stages if s.stage == name)

    def status(self, film: str, name: str) -> str:
        return self.stage(film, name).status

    def attempts(self, film: str, name: str) -> list[m.AttemptRecord]:
        job = self.stage(film, name).job_id
        return list(
            self.read(lambda rt: list(rt.store.list_attempts(rt.operator, job)))
        )

    def jasna_runs(self) -> int:
        return len(self.tools.runs(self.tools.jasna_exe))

    def hold_jasna(self) -> Path:
        """§11 IR13: the next Jasna runs until the returned file exists, so a
        test acts while the restore is still running (an event, never a
        wall-clock linger)."""
        release = self.tmp / f"release-{uuid.uuid4()}"
        self.tools.set_jasna(mode="sleep", release=str(release))
        return release

    def invariants(self) -> None:
        """≤ 1 unresolved assignment per job, ≤ 1 unresolved GPU owner."""
        open_ = self.read(lambda rt: rt.store.list_open_assignments(rt.operator))
        jobs = [o.assignment.job_id for o in open_]
        assert len(jobs) == len(set(jobs))
        gpus = [o.assignment.resource_id for o in open_ if o.assignment.resource_id]
        assert len(gpus) == len(set(gpus))

    def explain(self, film: str) -> str:
        """What every party says, for a failed wait."""
        stages = {
            s.stage: (s.status, s.reason)
            for s in self.rt.store.get_film(self.rt.operator, film).stages
        }
        lines = [
            f"stages {stages}",
            "attempts "
            + str(
                {
                    s.stage: [
                        (a.status, sorted((e.kind, e.summary.note) for e in a.evidence))
                        for a in self.rt.store.list_attempts(self.rt.operator, s.job_id)
                    ]
                    for s in self.rt.store.get_film(self.rt.operator, film).stages
                    if s.job_id
                }
            ),
            f"route {self.rt.store.film_route(self.rt.operator, film, 1)}",
        ]
        assert self.host is not None
        lines.append(f"controller {self.host.status().get('dispatch')}")
        from taskpaw_v3.workflow.routing import gpu_stages
        from taskpaw_v3.workflow.scheduler import schedule

        rt, fleet = self.rt, self.host.fleet
        assert fleet is not None
        snap = rt.store.routing_snapshot(rt.operator)
        planned = schedule(  # with the real pass's inputs (§11)
            snap,
            live=fleet.snapshot(),
            now=rt.clock.now(),
            controller_id=rt.identity.worker_id,
            controller_free_bytes=self.free,
            gpu_stages=gpu_stages(rt.store, rt.operator, snap, rt.executors),
            remote_enabled=True,
            live_since=fleet.started_at,
        )
        lines.append(
            f"pass {planned.waiting} {planned.routes} passes={self.host.scheduler.passes}"
        )
        if self.passes:
            last = self.passes[-1]
            lines.append(
                f"last pass waiting={last.planned.waiting} "
                f"routed={[d.film_id for d in last.routed]} "
                f"overtaken={list(last.overtaken)}"
            )
        lines.append(f"live {fleet.snapshot()}")
        for worker in self.workers:
            d = worker.dispatcher
            lines.append(
                f"worker {worker.state} {worker.reason} "
                f"dispatch={d.reason if d else None} routes={worker.routes}"
            )
        return " | ".join(lines)

    def row(self, film: str) -> dict[str, Any]:
        (value,) = self.rt.store.film_board(self.rt.operator, film_id=film)
        return board.film_row(value, {})


@pytest.fixture
def world(tmp_path, monkeypatch):
    value = World(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.close()


def routed_film(world: World) -> str:
    """A planned film and a routing pass; it must become a route. §11: a
    decision the store refuses (a tolerated race) or a pass holding it for
    the controller's disk fails at once, naming the code or the reason."""
    film = ret.add_real_film(world.rt)
    seen = len(world.passes)
    world.wake()

    def routed() -> bool:
        for outcome in world.passes[seen:]:
            codes = [code for f, code in outcome.overtaken if f == film]
            if codes:
                raise AssertionError(f"route_film refused {film}: {codes}")
            if outcome.planned.waiting.get(film) == CONTROLLER_DISK:
                raise AssertionError(f"pass held {film}: {CONTROLLER_DISK}")
            if any(d.film_id == film for d in outcome.routed):
                return True
        return world.rt.store.film_route(world.rt.operator, film, 1) is not None

    try:
        until(routed, LONG)
    except AssertionError as exc:
        if str(exc) != "condition not reached":
            raise
        raise AssertionError(world.explain(film)) from None
    return film


def wait_status(world: World, film: str, name: str, *states: str) -> None:
    try:
        until(lambda: world.status(film, name) in states, LONG)
    except AssertionError:
        raise AssertionError(world.explain(film)) from None


# ── §7: the fake-worker end to end ──────────────────────────────────────
def test_a_film_restores_remotely_transcribes_here_and_reaches_confirm(world):
    """OD7 §5: restore on the worker → upload → ASR on the controller →
    translate → return → scrape → 待确认."""
    worker = world.join()
    film = routed_film(world)
    wait_status(world, film, "scrape", "succeeded")
    row = world.row(film)
    assert row["can_confirm"] is True  # 待确认
    # restore ran on the worker; ASR, translate, return and scrape here
    (restore,) = world.attempts(film, "restore")
    (asr,) = world.attempts(film, "asr")
    worker_id = world.issued["w1"]["worker_id"]
    assert restore.worker_id == worker_id
    assert asr.worker_id == world.rt.identity.worker_id
    assert len(world.tools.runs(world.tools.whisper_exe)) == 1
    (translate,) = world.attempts(film, "translate")
    assert translate.worker_id == world.rt.identity.worker_id
    assert world.jasna_runs() == 1
    # digest-checked ledgers on the controller, with timing and energy
    from taskpaw_v3.workflow.stage_results import load_stage_result

    assert load_stage_result(world.rt, film, "restore").attempt_id == restore.attempt_id
    assert load_stage_result(world.rt, film, "asr").attempt_id == asr.attempt_id
    summary = restore.evidence[0].summary
    assert summary.timing is not None and summary.timing.wall > 0
    assert summary.energy is not None and summary.energy.wh == 2.5
    assert film in world.sampler.readings  # R14: the film owned the energy
    # the route closed, the worker released its staged source and its work
    until(lambda: not worker.dispatcher.staged(), LONG)
    until(lambda: not list((world.tmp / "w1" / "staging" / "attempts").glob("*")))
    assert world.rt.store.film_route(world.rt.operator, film, 1) is None
    world.invariants()


def test_the_board_shows_the_remote_running_stage_with_its_machine(world):
    world.tools.set_jasna(linger=1.5)  # long enough to be seen running
    world.join()
    film = routed_film(world)
    until(
        lambda: (
            (current := world.host.fleet.currents(world.rt.clock.now(), {}))
            and film in current
            and current[film].get("stage") == "restore"
            and current[film].get("attempt_id")
        ),
        LONG,
    )
    names = {world.issued["w1"]["worker_id"]: "w1"}
    current = world.host.fleet.currents(world.rt.clock.now(), names)[film]
    assert current["machine"] == {"id": world.issued["w1"]["worker_id"], "name": "w1"}
    assert current["started_at"] and current["uses_gpu"] is True
    wait_status(world, film, "restore", "succeeded")


def test_a_decision_that_never_routes_fails_fast_and_names_why(world):
    """§11 (Linux CI): the pass held every remote route because the test
    controller read the runner's own disk (< the 100 GB C8 reserve). The
    helper now names that at the first pass instead of a 60 s timeout."""
    world.join()
    world.free = 20 * 10**9  # a GitHub Linux runner's free disk
    with pytest.raises(AssertionError, match=CONTROLLER_DISK):
        routed_film(world)


# ── the crash matrix (§5) ───────────────────────────────────────────────
def _block(world: World, worker: WorkerHost) -> threading.Event:
    """The worker's next settle does nothing (the process 'dies' after its
    Job and before the settled record); the returned event is set then."""
    reached = threading.Event()

    def crash(active, outcome):
        reached.set()

    worker.dispatcher._settle = crash  # type: ignore[method-assign]
    return reached


def test_case_1_a_request_never_sent_is_dropped_and_claimed_anew(world):
    worker = world.join()
    assert worker.stop()
    staging = world.tmp / "w1" / "staging"
    ghost = AttemptState(
        attempt_id=str(uuid.uuid4()),
        film_id=str(uuid.uuid4()),
        rev=1,
        stage="restore",
        request={"attempt_id": "x"},
    )
    (staging / "attempts").mkdir(parents=True, exist_ok=True)
    (staging / "attempts" / f"{ghost.attempt_id}.json").write_text(
        json.dumps(ghost.__dict__), encoding="utf-8"
    )
    worker = world.worker()
    worker.start()
    until(lambda: not (staging / "attempts" / f"{ghost.attempt_id}.json").exists())
    film = routed_film(world)
    wait_status(world, film, "restore", "succeeded")
    assert len(world.attempts(film, "restore")) == 1 and world.jasna_runs() == 1
    world.invariants()


def test_case_2_a_lost_claim_reply_replays_to_one_assignment(world, monkeypatch):
    lost = threading.Event()
    real = WorkerClient.claim

    def claim(self, body):
        result = real(self, body)
        if not lost.is_set():
            lost.set()
            world.restart_controller()  # the reply never arrives
            raise ApiError(0, "unreachable")
        return result

    monkeypatch.setattr(WorkerClient, "claim", claim)
    world.join()
    film = routed_film(world)
    wait_status(world, film, "restore", "succeeded")
    assert lost.is_set()
    assert len(world.attempts(film, "restore")) == 1 and world.jasna_runs() == 1
    world.invariants()


@pytest.mark.parametrize("held_store", [False, True])
def test_a_read_that_meets_a_restart_waits_for_the_new_runtime(world, held_store):
    """§9 CX25 (CI run 39911578055): a reader that took `rt` just before a
    restart found its store closed (`authority_unavailable`) — a test race,
    not a product fault: the old host closing its store is the restart."""
    seen: list[LocalRuntime] = []

    def once(rt: LocalRuntime) -> Any:
        seen.append(rt)
        store = rt.store if held_store else None  # CI: the store already in hand
        if len(seen) == 1:
            world.restart_controller()  # the restart lands after `rt` was taken
        return (store or rt.store).list_open_assignments(rt.operator)

    assert not world.read(once)
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert world.restarts == 1


def test_case_3_a_worker_restart_without_a_ledger_is_a_free_retry(world):
    world.tools.set_jasna(mode="fail")  # no ledger
    worker = world.join()
    reached = _block(world, worker)
    film = routed_film(world)
    assert reached.wait(LONG)
    world.tools.set_jasna()  # the retry succeeds
    worker = world.restart_worker(worker)
    wait_status(world, film, "restore", "succeeded")
    first, second = world.attempts(film, "restore")
    assert {e.kind for e in first.evidence} == {"exec_interrupted"}  # free retry
    assert second.status == "succeeded" and world.jasna_runs() == 2
    world.invariants()


def test_case_4_the_worker_runs_on_while_the_controller_restarts(world):
    release = world.hold_jasna()
    worker = world.join()
    film = routed_film(world)
    until(lambda: world.jasna_runs() == 1, LONG)  # Jasna is running
    world.restart_controller()
    # the restart came while the restore was still the worker's, unresolved;
    # a slow runner may already have it settling or finishing (CI 39939505823)
    assert world.status(film, "restore") == "running"
    assert (worker.dispatcher.current() or {}).get("phase") in {
        "running",
        "settling",
        "uploading",
        "finishing",
    }
    release.touch()
    wait_status(world, film, "restore", "succeeded")
    assert len(world.attempts(film, "restore")) == 1 and world.jasna_runs() == 1
    world.invariants()


def test_case_5_a_ledger_with_outputs_is_finished_without_a_re_run(world):
    worker = world.join()
    reached = _block(world, worker)
    film = routed_film(world)
    assert reached.wait(LONG)  # the ledger is written, nothing more
    world.sampler.restart()
    worker = world.restart_worker(worker)
    wait_status(world, film, "restore", "succeeded")
    (restore,) = world.attempts(film, "restore")
    assert world.jasna_runs() == 1  # no GPU re-run
    assert restore.evidence[0].summary.energy is None  # cut: never 0 (R14)
    timing = restore.evidence[0].summary.timing  # §11 IR9: from the ledger
    assert timing is not None and timing.wall > 0
    world.invariants()


def test_case_6_a_controller_restart_mid_upload_resumes_the_part(world, monkeypatch):
    cut = threading.Event()
    real = WorkerClient.upload_file
    resumed: list[int] = []

    def upload(self, attempt_id, path, local, *, size, sha256, **kw):
        if cut.is_set() and not resumed:  # §11 IR12: where the resend starts
            entry = next(
                f for f in self.outputs(attempt_id)["files"] if f["path"] == path
            )
            resumed.append(int(entry["received"]))
        if not cut.is_set():
            cut.set()
            half = size // 2
            with local.open("rb") as stream:
                body = stream.read(half)
            try:  # half the body, then the controller goes away
                self.open(
                    "PUT",
                    f"/attempts/{attempt_id}/files/{quote(path, safe='/')}",
                    data=body,
                    headers={
                        "Content-Range": f"bytes 0-{size - 1}/{size}",
                        "Content-Length": str(half),
                    },
                ).close()
            except ApiError:
                pass
            world.restart_controller()
            raise ApiError(0, "unreachable")
        return real(self, attempt_id, path, local, size=size, sha256=sha256, **kw)

    monkeypatch.setattr(WorkerClient, "upload_file", upload)
    world.join()
    film = routed_film(world)
    wait_status(world, film, "restore", "succeeded")
    assert cut.is_set() and world.jasna_runs() == 1
    assert resumed and resumed[0] > 0  # the prefix survived the restart
    world.invariants()


def test_case_7_a_lost_finish_reply_then_a_worker_restart_is_collected(
    world, monkeypatch
):
    """§11 IR12: the reply is lost and the worker process ends before it
    sends the finish again; the next process's `recover()` reads `resolved`
    and collects the attempt (no second finish)."""
    gone, real = threading.Event(), WorkerClient.finish
    sent: list[str] = []

    def finish(self, attempt_id, body):
        result = real(self, attempt_id, body)
        sent.append(attempt_id)
        if not gone.is_set() and body.get("outcome") == "succeeded":
            gone.set()
            # the process dies before the reply arrives: nothing is resent
            dispatcher = world.workers[-1].dispatcher
            assert dispatcher is not None and dispatcher._stop.wait(LONG)
            raise ApiError(0, "unreachable")
        return result

    monkeypatch.setattr(WorkerClient, "finish", finish)
    worker = world.join()
    film = routed_film(world)
    assert gone.wait(LONG)
    wait_status(world, film, "restore", "succeeded")
    staging = world.tmp / "w1" / "staging"
    (restore,) = world.attempts(film, "restore")
    assert (staging / "attempts" / f"{restore.attempt_id}.json").exists()
    worker = world.restart_worker(worker)  # … and the worker restarts
    wait_status(world, film, "asr", "succeeded")
    until(lambda: not list((staging / "attempts").glob("*.json")), LONG)
    assert not (staging / "work" / restore.attempt_id).exists()
    assert sent.count(restore.attempt_id) == 1  # collected, never resent
    assert world.jasna_runs() == 1
    world.invariants()


def test_case_8_a_lost_finish_reply_across_a_controller_restart(world, monkeypatch):
    gone, real = threading.Event(), WorkerClient.finish

    def finish(self, attempt_id, body):
        result = real(self, attempt_id, body)
        if not gone.is_set():
            gone.set()
            world.restart_controller()
            raise ApiError(0, "unreachable")
        return result

    monkeypatch.setattr(WorkerClient, "finish", finish)
    world.join()
    film = routed_film(world)
    wait_status(world, film, "asr", "succeeded")
    assert gone.is_set() and len(world.attempts(film, "restore")) == 1
    world.invariants()


class Partition:
    """The worker's controller is unreachable while `cut` is set."""

    def __init__(self) -> None:
        self.cut = threading.Event()

    def __call__(self, host, port, credential=None, **kw):
        outer = self

        class Cut(WorkerClient):
            def open(self, *args, **kwargs):
                if outer.cut.is_set():
                    raise ApiError(0, "unreachable")
                return super().open(*args, **kwargs)

        return Cut(host, port, credential, **kw)


@pytest.mark.parametrize("attested", [False, True])
def test_case_9_a_partition_moves_nothing_until_attested(world, attested):
    release = world.hold_jasna()
    net = Partition()
    worker = world.join(client_factory=net)
    film = routed_film(world)
    until(lambda: world.jasna_runs() == 1, LONG)  # Jasna is running
    net.cut.set()
    until(lambda: worker.state == "connecting", LONG)  # a beat failed after the cut
    world.clock.advance(timedelta(seconds=61))  # 失联 on the controller's clock
    world.wake()
    world.host.scheduler.wait_passes(world.host.scheduler.passes + 1)
    worker_id = world.issued["w1"]["worker_id"]
    route = world.rt.store.film_route(world.rt.operator, film, 1)
    assert route is not None and route.worker_id == worker_id  # nothing moved
    assert world.host.fleet.state(worker_id, world.rt.clock.now()) == "lost"
    if attested:
        world.clock.advance(timedelta(minutes=6))
        (open_,) = [
            o
            for o in world.rt.store.list_open_assignments(world.rt.operator)
            if o.assignment.worker_id == worker_id
        ]
        attest(world.rt, open_.assignment.assignment_id)  # 判定已停止
    assert worker.dispatcher.claimed()  # the worker keeps its claim meanwhile
    assert (worker.dispatcher.current() or {}).get("phase") == "running"
    net.cut.clear()
    staging = world.tmp / "w1" / "staging"
    (first,) = world.attempts(film, "restore")
    if not attested:
        release.touch()  # the restore ends only after the partition
    if attested:  # the stale current is cancelled (killed), then discarded
        until(lambda: not list((staging / "attempts").glob("*.json")), LONG)
        assert not (staging / "work" / first.attempt_id).exists()
        assert world.status(film, "restore") == "failed"
        (first,) = world.attempts(film, "restore")
        assert "operator_attested_stopped" in {e.kind for e in first.evidence}
    else:  # back in time: the claimed stage finishes where it ran
        wait_status(world, film, "restore", "succeeded")
        assert len(world.attempts(film, "restore")) == 1
    assert world.jasna_runs() == 1
    world.invariants()


def test_case_10_a_revoke_mid_run_cancels_and_settles_the_job(world):
    world.tools.set_jasna(mode="sleep")
    worker = world.join()
    film = routed_film(world)
    until(lambda: worker.dispatcher.current() is not None, LONG)
    until(lambda: world.status(film, "restore") == "running", LONG)
    worker_id = world.issued["w1"]["worker_id"]
    record = world.rt.store.get_worker(world.rt.operator, worker_id).worker
    dispatcher = worker.dispatcher
    assert dispatcher is not None
    devices.revoke(world.rt, worker_id, record.version)
    until(lambda: worker.state == "revoked", LONG)
    assert dispatcher.current() is None and worker.dispatcher is None  # settled
    assert not any(t.is_alive() for t in dispatcher._threads)
    (open_,) = [
        o
        for o in world.rt.store.list_open_assignments(world.rt.operator)
        if o.assignment.worker_id == worker_id
    ]
    assert open_.assignment.status == "unknown"  # until 判定已停止
    world.invariants()


def test_a_live_child_of_an_earlier_boot_holds_the_worker(world):
    worker = world.join()
    staging = world.tmp / "w1" / "staging"
    reached = _block(world, worker)
    world.tools.set_jasna(mode="fail")
    film = routed_film(world)
    assert reached.wait(LONG)
    assert worker.stop()
    (path,) = (staging / "attempts").glob("*.json")
    state = json.loads(path.read_text(encoding="utf-8"))
    from taskpaw_v3.workflow.owned_process import own_identity

    state["children"] = [own_identity()]  # this test process: alive
    path.write_text(json.dumps(state), encoding="utf-8")
    worker = world.worker()
    worker.start()
    until(lambda: worker.status()["reason"] == "等待对账", LONG)
    assert not worker.reconciled and worker.dispatcher.reason in ("等待对账", "idle")
    state["children"] = ["1:1"]  # the child ended
    path.write_text(json.dumps(state), encoding="utf-8")
    world.tools.set_jasna()
    until(lambda: worker.reconciled, LONG)
    wait_status(world, film, "restore", "succeeded")
    world.invariants()


# ── cancel (R9) and pause (R12) end to end ──────────────────────────────
def test_requeue_is_a_free_retry_routed_again(world):
    world.tools.set_jasna(mode="sleep")
    world.join()
    film = routed_film(world)
    until(lambda: world.status(film, "restore") == "running", LONG)
    # the fake reads its config when the process starts, which on a loaded
    # runner is long after the stage is `running`: wait until the first Jasna
    # has recorded its run (it read `sleep`) before the retry's config is set
    until(lambda: world.jasna_runs() == 1, LONG)
    attempt = world.stage(film, "restore").attempt_id
    world.tools.set_jasna()  # the retry's launch reads this; the first sleeps on
    world.host.requeue(film, attempt)  # 停止并重新排队
    until(lambda: len(world.attempts(film, "restore")) == 2, LONG)
    first = world.attempts(film, "restore")[0]
    assert {e.kind for e in first.evidence} == {"exec_failed:cancelled"}
    wait_status(world, film, "restore", "succeeded")
    assert world.attempts(film, "restore")[1].worker_id == first.worker_id
    assert attempt not in world.host.cancels  # cleared at resolve
    world.invariants()


def test_pause_stops_claims_and_withdraws_the_unclaimed_route(world):
    from taskpaw_v3.tests.workflow_route_helpers import route

    worker = world.join()
    worker.set_paused(True)
    worker_id = world.issued["w1"]["worker_id"]
    until(lambda: world.host.fleet.entry(worker_id).beat.paused, LONG)  # type: ignore[union-attr]
    film = ret.add_real_film(world.rt)
    route(world.rt, film, worker_id)  # an auto route the pass made earlier
    world.wake()
    until(lambda: world.rt.store.film_route(world.rt.operator, film, 1) is None, LONG)
    assert world.attempts(film, "restore") == []  # never claimed
    worker.set_paused(False)
    world.wake()
    wait_status(world, film, "restore", "succeeded")


def test_in_flight_work_completes_while_paused(world):
    release = world.hold_jasna()
    worker = world.join()
    film = routed_film(world)
    until(lambda: world.jasna_runs() == 1, LONG)  # Jasna is running
    worker.set_paused(True)
    worker_id = world.issued["w1"]["worker_id"]
    until(lambda: world.host.fleet.entry(worker_id).beat.paused, LONG)  # type: ignore[union-attr]
    assert world.status(film, "restore") == "running"  # paused mid-run
    release.touch()
    wait_status(world, film, "restore", "succeeded")  # the run goes on
    assert world.attempts(film, "asr") == []  # but nothing new is claimed
    worker.set_paused(False)
    world.wake()
    wait_status(world, film, "asr", "succeeded")


def test_a_worker_re_paired_mid_run_stops_its_job_and_retries(world):
    """§11 CX3 + IR1: 重新配对 while a stage runs. The old session's
    dispatcher is quiesced (its Job stopped and settled) before the new
    identity runs; a new dispatcher with a new boot id finishes the attempt
    `cancelled`, a free retry that runs again on the same worker.

    OD1 correction: while the re-paired worker is not yet 核验 (the verified
    set is empty) the controller's own dispatcher stays running and still
    never claims the retried restore: the film waits for a GPU device
    and completes on the worker after the re-approval, which is retried on a
    version conflict with the new session's own writes."""
    release = world.hold_jasna()
    worker = world.join()
    film = routed_film(world)
    until(lambda: world.jasna_runs() == 1, LONG)  # Jasna is running
    old, boot = worker.dispatcher, worker.boot_id
    assert old is not None
    worker_id = world.issued["w1"]["worker_id"]
    controller = world.dispatcher
    record = world.rt.store.get_worker(world.rt.operator, worker_id).worker
    code = devices.repair(
        world.rt, worker_id, record.version, host="127.0.0.1", port=world.server.port
    )["code"]
    worker.join(code, str(world.tmp / "w1" / "staging"))
    until(lambda: worker.dispatcher is not None and worker.dispatcher is not old, LONG)
    assert old.halted and not any(t.is_alive() for t in old._threads)
    assert worker.boot_id != boot
    release.touch()  # the retry's Jasna runs straight through
    # the worker is not 核验 yet: the controller's GPU still takes nothing
    until(
        lambda: (
            controller.status().waiting.get(film) == f"{REMOTE_GPU}：{NO_GPU_DEVICE}"
        ),
        LONG,
    )
    assert world.status(film, "restore") == "pending"

    def approved() -> bool:
        record = world.rt.store.get_worker(world.rt.operator, worker_id).worker
        if record.enrollment_state == "verified":
            return True
        try:
            devices.approve(world.rt, worker_id, record.version, restore=True, asr=True)
        except m.WorkflowError as exc:  # the session's own write / not probed yet
            if exc.code in {"cas_conflict", "precondition_failed"}:
                return False
            raise
        return True

    until(approved, LONG)
    world.wake()
    wait_status(world, film, "asr", "succeeded")
    first, second = world.attempts(film, "restore")
    assert {e.kind for e in first.evidence} == {"exec_failed:cancelled"}
    assert second.status == "succeeded" and world.jasna_runs() == 2
    # never a restore on the controller's GPU; OD7: ASR is the controller's
    assert {a.worker_id for a in world.attempts(film, "restore")} == {worker_id}
    assert {a.worker_id for a in world.attempts(film, "asr")} == {
        world.rt.identity.worker_id
    }
    world.invariants()


def test_a_revoked_worker_joined_again_in_the_same_process(world):
    """§11 IR1/IR5: after 撤销 the same process joins as a new device. Its
    dispatcher has a new boot id; the old credential's attempt file is
    unknown to the new identity (404) and collected with its work folder."""
    release = world.hold_jasna()
    worker = world.join()
    film = routed_film(world)
    until(lambda: world.jasna_runs() == 1, LONG)  # Jasna is running
    attempt = world.stage(film, "restore").attempt_id
    worker_id = world.issued["w1"]["worker_id"]
    record = world.rt.store.get_worker(world.rt.operator, worker_id).worker
    devices.revoke(world.rt, worker_id, record.version)
    until(lambda: worker.state == "revoked", LONG)
    staging = world.tmp / "w1" / "staging"
    assert (staging / "attempts" / f"{attempt}.json").exists()  # settled, kept
    boot = worker.boot_id
    release.touch()
    issued = devices.add_device(
        world.rt, "w1-b", "192.168.1.31", host="127.0.0.1", port=world.server.port
    )
    worker.join(issued["code"], str(staging))
    until(lambda: worker.reconciled and worker.state == "running", LONG)
    assert worker.boot_id != boot
    until(lambda: not (staging / "attempts" / f"{attempt}.json").exists(), LONG)
    assert not (staging / "work" / attempt).exists()
    world.invariants()


# ── Codex round 6 (P2-4 §9 CX18) ────────────────────────────────────────
def test_cx18_settle_boot_never_interrupts_while_a_record_is_unreadable():
    """§9 CX18 (Codex r6): the controller side. An assignment the worker did
    not report is interrupted only when every record was read; a report of
    any unreadable record holds it (the record may be that assignment's)."""
    from types import SimpleNamespace

    from taskpaw_v3.workflow import remote_attempts as ra

    worker, old_boot, new_boot = (str(uuid.uuid4()) for _ in range(3))
    first, second, other = (str(uuid.uuid4()) for _ in range(3))
    resolved: list[str] = []

    def rt_of(*attempts: str) -> Any:
        open_ = [
            SimpleNamespace(
                assignment=SimpleNamespace(
                    worker_id=worker, boot_id=old_boot, attempt_id=a
                )
            )
            for a in attempts
        ]
        return SimpleNamespace(
            operator=None,
            store=SimpleNamespace(list_open_assignments=lambda op: open_),
            resolve_interrupted=lambda a: resolved.append(a.attempt_id),
        )

    stopped = ra.AttemptReport(second, False, False, False, None)
    unreadable = ra.AttemptReport(other, True, False, False, None, unreadable=True)
    verdicts = ra.settle_boot(
        rt_of(first, second), worker, new_boot, [stopped, unreadable]
    )
    assert verdicts == {first: "held", second: "held"} and resolved == []
    # its own record unreadable: held as well
    own = ra.AttemptReport(first, True, False, False, None, unreadable=True)
    assert ra.settle_boot(rt_of(first), worker, new_boot, [own]) == {first: "held"}
    assert resolved == []
    # every record read: the unreported and the stopped are interrupted
    verdicts = ra.settle_boot(rt_of(first, second), worker, new_boot, [stopped])
    assert verdicts == {first: "interrupted", second: "interrupted"}
    assert resolved == [first, second]


def test_cx18_an_unreadable_record_after_a_restart_is_held_not_interrupted(
    world, monkeypatch
):
    """§9 CX18 end to end: the worker restarts while its attempt file raises
    PermissionError. The assignment stays unresolved (no free retry), the
    boot stays unreconciled and nothing is deleted while the read fails; once
    it reads again the boot settles it as before (a free retry)."""
    world.tools.set_jasna(mode="fail")  # no ledger
    worker = world.join()
    reached = _block(world, worker)
    film = routed_film(world)
    assert reached.wait(LONG)
    (open_,) = world.rt.store.list_open_assignments(world.rt.operator)
    attempt = open_.assignment.attempt_id
    staging = world.tmp / "w1" / "staging"
    record = staging / "attempts" / f"{attempt}.json"
    assert record.exists()
    real = Path.read_text
    blocked = {"on": True}

    def read_text(self: Path, *args: Any, **kw: Any) -> str:
        if blocked["on"] and self == record:
            raise PermissionError("denied")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    boots: list[list[dict[str, Any]]] = []
    real_boot = WorkerClient.boot

    def boot(self, boot_id, attempts=None):
        boots.append(list(attempts or []))
        return real_boot(self, boot_id, attempts)

    monkeypatch.setattr(WorkerClient, "boot", boot)
    world.tools.set_jasna()  # a retry would succeed
    worker = world.restart_worker(worker)
    until(lambda: len(boots) >= 3, LONG)  # re-posted while unreconciled
    assert any(r.get("unreadable") for r in boots[-1])
    open_now = world.rt.store.list_open_assignments(world.rt.operator)
    assert [o.assignment.attempt_id for o in open_now] == [attempt]
    assert record.exists() and not worker.reconciled
    assert len(world.attempts(film, "restore")) == 1 and world.jasna_runs() == 1
    blocked["on"] = False  # readable again: settled as a free retry
    wait_status(world, film, "restore", "succeeded")
    first, second = world.attempts(film, "restore")
    assert {e.kind for e in first.evidence} == {"exec_interrupted"}
    assert second.status == "succeeded"
    world.invariants()
