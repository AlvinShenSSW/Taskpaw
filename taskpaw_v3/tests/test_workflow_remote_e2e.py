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

import json
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

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
from taskpaw_v3.workflow import board, devices, local_host
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.keep_awake import KeepAwake
from taskpaw_v3.workflow.local_gpu import LocalGpu
from taskpaw_v3.workflow.local_runtime import LocalRuntime
from taskpaw_v3.workflow.remote_dispatch import AttemptState
from taskpaw_v3.workflow.remote_worker import Probe, WorkerHost, probe_body
from taskpaw_v3.workflow.return_step import LocalReturnStep
from taskpaw_v3.workflow.worker_client import ApiError, WorkerClient

WORKER_GPU = LocalGpu("NVIDIA GeForce RTX 5060 Ti", "GPU-fake-16gb", 16311)
VERSION = __version__  # the controller's own: no 「版本不一致」
LONG = 60.0  # a condition wait's ceiling; never a timing budget


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
        self.settings = WorkflowSettings(
            enabled=True, role="controller", remote_workers=True
        )
        self.host: local_host.LocalWorkflowHost | None = None
        self.down = False
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
                SyntheticExecutor("asr"),
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
        self.down = False

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
        return next(
            s
            for s in self.rt.store.get_film(self.rt.operator, film).stages
            if s.stage == name
        )

    def status(self, film: str, name: str) -> str:
        return self.stage(film, name).status

    def attempts(self, film: str, name: str) -> list[m.AttemptRecord]:
        job = self.stage(film, name).job_id
        return list(self.rt.store.list_attempts(self.rt.operator, job))

    def jasna_runs(self) -> int:
        return len(self.tools.runs(self.tools.jasna_exe))

    def invariants(self) -> None:
        """≤ 1 unresolved assignment per job, ≤ 1 unresolved GPU owner."""
        open_ = self.rt.store.list_open_assignments(self.rt.operator)
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
            f"route {self.rt.store.film_route(self.rt.operator, film, 1)}",
        ]
        assert self.host is not None
        lines.append(f"controller {self.host.status().get('dispatch')}")
        from taskpaw_v3.workflow.routing import gpu_stages
        from taskpaw_v3.workflow.scheduler import schedule

        rt, fleet = self.rt, self.host.fleet
        assert fleet is not None
        snap = rt.store.routing_snapshot(rt.operator)
        planned = schedule(
            snap,
            live=fleet.snapshot(),
            now=rt.clock.now(),
            controller_id=rt.identity.worker_id,
            gpu_stages=gpu_stages(rt.store, rt.operator, snap, rt.executors),
            remote_enabled=True,
        )
        lines.append(
            f"pass {planned.waiting} {planned.routes} passes={self.host.scheduler.passes}"
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
    film = ret.add_real_film(world.rt)
    world.wake()
    return film


def wait_status(world: World, film: str, name: str, *states: str) -> None:
    try:
        until(lambda: world.status(film, name) in states, LONG)
    except AssertionError:
        raise AssertionError(world.explain(film)) from None


# ── §7: the fake-worker end to end ──────────────────────────────────────
def test_a_film_restores_and_transcribes_remotely_and_reaches_confirm(world):
    worker = world.join()
    film = routed_film(world)
    wait_status(world, film, "scrape", "succeeded")
    row = world.row(film)
    assert row["can_confirm"] is True  # 待确认
    # restore and ASR ran on the worker, translate/return/scrape here
    (restore,) = world.attempts(film, "restore")
    (asr,) = world.attempts(film, "asr")
    worker_id = world.issued["w1"]["worker_id"]
    assert restore.worker_id == asr.worker_id == worker_id
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
    world.tools.set_jasna(linger=1.0)
    worker = world.join()
    film = routed_film(world)
    until(lambda: (worker.dispatcher.current() or {}).get("phase") == "running", LONG)
    world.restart_controller()
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
    world.invariants()


def test_case_6_a_controller_restart_mid_upload_resumes_the_part(world, monkeypatch):
    cut = threading.Event()
    real = WorkerClient.upload_file

    def upload(self, attempt_id, path, local, *, size, sha256, **kw):
        if not cut.is_set():
            cut.set()
            half = size // 2
            with local.open("rb") as stream:
                body = stream.read(half)
            try:  # half the body, then the controller goes away
                self.open(
                    "PUT",
                    f"/attempts/{attempt_id}/files/{path}",
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
    world.invariants()


def test_case_7_a_lost_finish_reply_then_a_worker_restart_is_collected(
    world, monkeypatch
):
    gone, real = threading.Event(), WorkerClient.finish

    def finish(self, attempt_id, body):
        result = real(self, attempt_id, body)
        if not gone.is_set() and body.get("outcome") == "succeeded":
            gone.set()
            raise ApiError(0, "unreachable")  # the reply is lost …
        return result

    monkeypatch.setattr(WorkerClient, "finish", finish)
    worker = world.join()
    film = routed_film(world)
    assert gone.wait(LONG)
    wait_status(world, film, "restore", "succeeded")
    worker = world.restart_worker(worker)  # … and the worker restarts
    wait_status(world, film, "asr", "succeeded")
    staging = world.tmp / "w1" / "staging"
    until(lambda: not list((staging / "attempts").glob("*.json")), LONG)
    restore = world.attempts(film, "restore")[0].attempt_id
    assert not (staging / "work" / restore).exists()
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
    world.tools.set_jasna(linger=2.0)
    net = Partition()
    worker = world.join(client_factory=net)
    film = routed_film(world)
    until(lambda: (worker.dispatcher.current() or {}).get("phase") == "running", LONG)
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
    net.cut.clear()
    staging = world.tmp / "w1" / "staging"
    (first,) = world.attempts(film, "restore")
    if attested:  # the finish is stale (409): killed and discarded
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
    devices.revoke(world.rt, worker_id, record.version)
    until(lambda: worker.state == "revoked", LONG)
    dispatcher = worker.dispatcher
    assert dispatcher is not None and dispatcher.current() is None  # settled
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
    world.tools.set_jasna(linger=1.0)
    worker = world.join()
    film = routed_film(world)
    until(lambda: world.status(film, "restore") == "running", LONG)
    worker.set_paused(True)
    wait_status(world, film, "restore", "succeeded")  # the run goes on
    assert world.attempts(film, "asr") == []  # but nothing new is claimed
    worker.set_paused(False)
    world.wake()
    wait_status(world, film, "asr", "succeeded")
