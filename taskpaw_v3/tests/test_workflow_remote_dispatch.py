"""P2-3 (#294) §3–§5 on the worker side, against a scripted controller:
`WorkerRuntime`, the claim gate (R6), prefetch (R7), staging GC (R10), the
keep-awake seam (R11), 暂停领取 (R12) and the controller's routing thread
(R13). The real controller is exercised end to end in
`test_workflow_remote_e2e.py`."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_films import input_manifest
from taskpaw_v3.tests.test_workflow_remote_worker import (
    FakeController,
    loop_worker,
    until,
)
from taskpaw_v3.tests.worker_api_helpers import VERSION, beat
from taskpaw_v3.workflow.executors import ExecOutcome, SyntheticExecutor
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.keep_awake import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    KeepAwake,
)
from taskpaw_v3.workflow.remote_dispatch import RemoteDispatcher, WorkerRuntime
from taskpaw_v3.workflow.routing import RoutingThread
from taskpaw_v3.workflow.stage_results import StageResultError
from taskpaw_v3.workflow.staging import Staging
from taskpaw_v3.workflow.worker_client import ApiError

GB = 10**9


def ident() -> str:
    return str(uuid.uuid4())


# ── WorkerRuntime (R3) ──────────────────────────────────────────────────
def _ledger(film: str, stage: str, attempt: str, rev: int = 1) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "film_id": film,
            "input_revision": rev,
            "stage": stage,
            "attempt_id": attempt,
            "videos": [],
        },
        sort_keys=True,
    )


def _envelope(film: str, *, ledgers=None, stages=None, rev: int = 1) -> dict:
    return {
        "film": film,
        "rev": rev,
        "stages": stages
        if stages is not None
        else {"restore": {"job_id": ident(), "version": 1, "status": "succeeded"}},
        "ledgers": ledgers or {},
    }


def test_worker_runtime_reads_digest_checked_ledgers(tmp_path):
    rt = WorkerRuntime(tmp_path)
    film, attempt = ident(), ident()
    text = _ledger(film, "restore", attempt)
    digest = hashlib.sha256(text.encode()).hexdigest()
    rt.use(
        _envelope(
            film,
            ledgers={
                "restore": {
                    "attempt_id": attempt,
                    "payload_digest": digest,
                    "text": text,
                }
            },
        )
    )
    assert rt.work_dir == tmp_path / "work"
    result = rt.stage_result(film, "restore")
    assert result is not None and result.attempt_id == attempt
    assert rt.stage_result(film, "asr") is None  # the film has no ASR stage


@pytest.mark.parametrize("bad", ["digest", "attempt", "film", "pending"])
def test_worker_runtime_refuses_an_unverified_ledger(tmp_path, bad):
    rt = WorkerRuntime(tmp_path)
    film, attempt = ident(), ident()
    text = _ledger(ident() if bad == "film" else film, "restore", attempt)
    digest = hashlib.sha256(text.encode()).hexdigest()
    entry = {
        "attempt_id": ident() if bad == "attempt" else attempt,
        "payload_digest": "0" * 64 if bad == "digest" else digest,
        "text": text,
    }
    rt.use(_envelope(film, ledgers={} if bad == "pending" else {"restore": entry}))
    with pytest.raises(StageResultError):
        rt.stage_result(film, "restore")


# ── a scripted controller for the dispatcher ────────────────────────────
class Scripted:
    """Envelopes, ranged downloads and claim/renew/finish, in memory."""

    def __init__(self, staging: Staging) -> None:
        self.staging = staging
        self.films: dict[tuple[str, int], dict[str, Any]] = {}
        self.claims: list[dict] = []
        self.finishes: list[tuple[str, dict]] = []
        self.downloads: list[str] = []
        self.started = threading.Event()
        self.lock = threading.Lock()

    def add(self, size: int = 10, stage_status: str = "pending") -> str:
        film = ident()
        rel = f"copies/{film}/movie.mkv"
        self.films[film, 1] = {
            "film": film,
            "rev": 1,
            "name": f"Film {film[:4]}",
            "ltx_model": "standard",
            "manifest": input_manifest(film).model_dump(mode="json"),
            "workcopy": [
                {
                    "item_id": ident(),
                    "relative_path": rel,
                    "size": size,
                    "sha256": "0" * 64,
                }
            ],
            "stages": {
                "restore": {"job_id": ident(), "version": 1, "status": stage_status},
                "asr": {"job_id": ident(), "version": 1, "status": "pending"},
            },
            "ledgers": {},
        }
        return film

    def __call__(self) -> Scripted:
        return self

    def envelope(self, film: str, rev: int) -> dict:
        return json.loads(json.dumps(self.films[film, rev]))

    def download_film(self, envelope, staging, *, cancel=None, on_progress=None):
        total = sum(c["size"] for c in envelope["workcopy"])
        staging.admit(total, 0)
        with self.lock:
            self.downloads.append(envelope["film"])
        for copy in envelope["workcopy"]:
            path = staging.work_dir / copy["relative_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * min(copy["size"], 64))
        return []

    def claim(self, body):
        self.claims.append(body)
        return {"assignment_id": ident(), "attempt_id": body["attempt_id"]}

    def renew(self, attempt_id, body):
        return {"version": 2}

    def finish(self, attempt_id, body):
        self.finishes.append((attempt_id, body))
        return {"state": "resolved", "outcome": body["outcome"]}

    def attempt(self, attempt_id):
        raise ApiError(404, "not_found")


class Blocking:
    """A restore whose body waits for `release` (no child: nothing to kill)."""

    stage = "restore"
    uses_gpu = True

    def __init__(self) -> None:
        self.release, self.running = threading.Event(), threading.Event()
        self.runs = 0

    def tools(self, job=None):
        return SyntheticExecutor("restore").tools()

    def max_resolution(self, vram):
        return (1920, 1080)

    def run(self, context):
        self.runs += 1
        self.running.set()
        while not self.release.wait(0.01):
            if context.cancel.is_set():
                return ExecOutcome.failed("cancelled", "已停止")
        return ExecOutcome.failed("transient", "synthetic")


def _dispatcher(tmp_path, client, executor, *, free=2_000 * GB, cap_gb=500):
    staging = Staging(tmp_path / "staging", cap_gb, free_bytes=lambda p: free)
    rt = WorkerRuntime(staging.root)
    d = RemoteDispatcher(
        staging,
        client,
        {"restore": executor},
        rt,
        boot_id=ident(),
        lease=GpuLease(),
        power=lambda: None,
        poll=0.02,
        stop_settle=5.0,
        backoff_base=0.01,
    )
    return d, staging


def _observe(d, routes, **kw):
    d.observe(
        {
            "routes": routes,
            "cancel": kw.pop("cancel", []),
            "release": kw.pop("release", []),
        },
        reconciled=kw.pop("reconciled", True),
        tiers=("standard",),
        asr_ready=True,
        hold=kw.pop("hold", "none"),
        paused=kw.pop("paused", False),
    )


def _route(film, position):
    return {"route_id": ident(), "film": film, "rev": 1, "position": position}


# ── R7 prefetch, R6 gate ────────────────────────────────────────────────
def test_position_one_is_fetched_while_position_zero_runs(tmp_path):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    first, second = client.add(), client.add()
    d.start()
    try:
        _observe(d, [_route(first, 0), _route(second, 1)])
        assert executor.running.wait(10)  # position 0 claimed and running
        until(lambda: d.index.complete(second, 1))  # prefetched meanwhile
        assert client.downloads == [first, second]
        assert [c["job_id"] for c in client.claims] == [
            client.films[first, 1]["stages"]["restore"]["job_id"]
        ]  # only the position-0 route is claimed
        assert d.current()["film_id"] == first
        assert d.claimed()
    finally:
        executor.release.set()
        assert d.stop(10)


def test_the_disk_budget_refuses_a_prefetch_and_no_attempt_is_made(tmp_path):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor, free=60 * GB)
    client.__init__(staging)
    film = client.add(size=20 * GB)  # need 46 GB; free − need < 50 GB
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        until(lambda: d.reason == "空间不足")
        assert not d.index.complete(film, 1) and client.claims == []
        assert list((staging.root / "attempts").glob("*.json")) == []
    finally:
        assert d.stop(10)


@pytest.mark.parametrize(
    "gate,reason",
    [
        ({"reconciled": False}, "等待对账"),
        ({"paused": True}, "暂停领取"),
        ({"hold": "unknown"}, "GPU 有未知占用"),
    ],
)
def test_the_claim_gate_holds_every_rule(tmp_path, gate, reason):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)], **gate)
        until(lambda: d.index.complete(film, 1))  # prefetch goes on
        until(lambda: d.reason == reason)
        assert client.claims == [] and executor.runs == 0
        _observe(d, [_route(film, 0)])  # every rule holds again
        assert executor.running.wait(10)
    finally:
        executor.release.set()
        assert d.stop(10)


def test_a_manual_gpu_task_makes_the_claim_wait(tmp_path):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    film = client.add()
    manual = ("jasna-1", 7)
    assert d.lease.try_acquire(manual, 0.01, "Jasna：手动")
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        until(lambda: d.reason.startswith("等待 GPU"))
        assert client.claims == []
        d.lease.release(manual)
        assert executor.running.wait(10)
    finally:
        executor.release.set()
        assert d.stop(10)


# ── R9 cancel, R10 GC ───────────────────────────────────────────────────
def test_a_cancel_stops_the_job_and_finishes_cancelled(tmp_path):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        assert executor.running.wait(10)
        attempt = client.claims[0]["attempt_id"]
        _observe(d, [_route(film, 0)], cancel=[attempt])
        until(lambda: client.finishes)
        assert client.finishes[0] == (
            attempt,
            {"outcome": "failed", "error_class": "cancelled", "message": "已停止"},
        )
        until(lambda: not (staging.root / "attempts" / f"{attempt}.json").exists())
    finally:
        assert d.stop(10)


def test_gc_deletes_only_released_or_unkept_films(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    kept, released, unkept = client.add(), client.add(), client.add()
    for film in (kept, released, unkept):
        envelope = client.envelope(film, 1)
        client.download_film(envelope, staging)
        d.index.mark(film, 1, [c["relative_path"] for c in envelope["workcopy"]], True)

    def present(film):
        rel = client.films[film, 1]["workcopy"][0]["relative_path"]
        return (staging.work_dir / rel).exists()

    _observe(d, [], release=[{"film": released, "rev": 1}])
    assert not present(released) and present(kept) and present(unkept)
    d.booted({"reconciled": True, "attempts": {}, "keep": [{"film": kept, "rev": 1}]})
    assert present(kept) and not present(unkept)
    assert {(s["film"], s["rev"]) for s in d.staged()} == {(kept, 1)}


# ── R11 keep-awake: one thread, on with work, off after it and at stop ──
class Calls:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def __call__(self, flags: int) -> int:
        self.calls.append((threading.get_ident(), flags))
        return 1


def test_keep_awake_follows_the_routes_from_the_heartbeat_thread(tmp_path):
    fake = FakeController()
    film = ident()
    routed = {"routes": [_route(film, 0)], "reconcile_needed": False}
    idle = {"routes": [], "reconcile_needed": False}
    gate = threading.Event()
    fake.script["heartbeat"] = [routed, routed, routed]

    def heartbeat(beat_body):
        fake.beats.append(beat_body)
        queue = fake.script["heartbeat"]
        if not queue:
            gate.set()
            return idle
        return queue.pop(0)

    fake.heartbeat = heartbeat  # type: ignore[method-assign]
    fake.envelope = lambda f, r: (_ for _ in ()).throw(ApiError(0, "unreachable"))  # type: ignore[attr-defined]
    fake.attempt = lambda a: (_ for _ in ()).throw(ApiError(404, "not_found"))  # type: ignore[attr-defined]
    calls = Calls()
    worker = loop_worker(tmp_path, fake, keep_awake=KeepAwake(calls), lease=GpuLease())
    try:
        worker.start()
        assert gate.wait(10)
        until(lambda: len(calls.calls) >= 2)
    finally:
        assert worker.stop()
    flags = [f for _, f in calls.calls]
    assert flags[0] == ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    assert flags[-1] == ES_CONTINUOUS and not worker.keep_awake.held
    threads = {t for t, _ in calls.calls}
    assert len(threads) == 1 and threads != {threading.get_ident()}


def test_keep_awake_is_cleared_at_stop_while_a_route_is_held(tmp_path):
    fake = FakeController()
    routed = {"routes": [_route(ident(), 0)], "reconcile_needed": False}
    fake.script["heartbeat"] = [routed] * 1000
    fake.envelope = lambda f, r: (_ for _ in ()).throw(ApiError(0, "unreachable"))  # type: ignore[attr-defined]
    fake.attempt = lambda a: (_ for _ in ()).throw(ApiError(404, "not_found"))  # type: ignore[attr-defined]
    calls = Calls()
    worker = loop_worker(tmp_path, fake, keep_awake=KeepAwake(calls), lease=GpuLease())
    try:
        worker.start()
        until(lambda: worker.keep_awake.held)
    finally:
        assert worker.stop()
    assert [f for _, f in calls.calls][-1] == ES_CONTINUOUS
    assert len({t for t, _ in calls.calls}) == 1


# ── R12 暂停领取 ─────────────────────────────────────────────────────────
def test_pause_is_reported_by_the_heartbeat_and_survives_beats(tmp_path):
    fake = FakeController()
    fake.attempt = lambda a: (_ for _ in ()).throw(ApiError(404, "not_found"))  # type: ignore[attr-defined]
    worker = loop_worker(tmp_path, fake, lease=GpuLease())
    try:
        worker.start()
        until(lambda: worker.reason == "在线")
        worker.set_paused(True)
        count = len(fake.beats)
        until(lambda: len(fake.beats) > count + 1)
        assert fake.beats[-1]["paused"] is True and worker.status()["paused"] is True
        worker.set_paused(False)
        count = len(fake.beats)
        until(lambda: len(fake.beats) > count + 1)
        assert fake.beats[-1]["paused"] is False
    finally:
        assert worker.stop()


def test_a_paused_worker_loses_its_unclaimed_auto_route(tmp_path):
    """S8: 暂停领取 uses the 停领 rules — the pass withdraws the worker's
    unclaimed auto route; a claimed one would stay."""
    from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
    from taskpaw_v3.tests.workflow_route_helpers import enrol_remote, route
    from taskpaw_v3.workflow.routing import run_pass

    rt = runtime(tmp_path / "wf")
    try:
        remote = enrol_remote(rt, "5060 Ti-1")
        film = add_film(rt)
        route(rt, film, remote.worker_id)
        now = rt.clock.now()
        fleet = FleetLive(now - timedelta(minutes=5), VERSION)
        fleet.observe(
            remote.worker_id,
            Heartbeat.model_validate(beat(remote.boot_id, paused=True)),
            now,
        )
        outcome = run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live=fleet.snapshot(),
            now=now,
            executors=rt.executors,
            remote_enabled=True,
        )
        assert [w.film_id for w in outcome.withdrawn] == [film]
        assert outcome.withdrawn[0].reason == "暂停领取"
        assert rt.store.film_route(rt.operator, film, 1) is None
    finally:
        rt.close()


# ── R13 the routing thread ──────────────────────────────────────────────
class Ticks:
    """The thread's timer: each `tick()` ends one wait as a timeout."""

    def __init__(self) -> None:
        self._ticks = threading.Semaphore(0)

    def __call__(self, event: threading.Event, timeout: float) -> bool:
        while not event.is_set():
            if self._ticks.acquire(timeout=0.01):
                return False
        return True

    def tick(self) -> None:
        self._ticks.release()


def test_the_routing_thread_runs_on_start_wake_and_its_timer():
    ticks = Ticks()
    runs: list[int] = []
    thread = RoutingThread(lambda: runs.append(1), interval=3600, wait=ticks)
    thread.start()
    try:
        assert thread.wait_passes(1)  # at start
        thread.wake()
        assert thread.wait_passes(2)  # a trigger
        ticks.tick()
        assert thread.wait_passes(3)  # the 30 s timer
    finally:
        assert thread.stop(10)
    assert len(runs) == thread.passes


def test_wakes_during_a_pass_coalesce_and_a_failed_pass_is_retried():
    ticks = Ticks()
    inside, go = threading.Event(), threading.Event()
    seen: list[int] = []

    def run():
        seen.append(len(seen))
        if len(seen) == 2:
            inside.set()
            assert go.wait(10)
        if len(seen) == 3:
            raise RuntimeError("store busy")

    thread = RoutingThread(run, interval=3600, wait=ticks)
    thread.start()
    try:
        assert thread.wait_passes(1)
        thread.wake()
        assert inside.wait(10)
        for _ in range(5):
            thread.wake()  # five triggers during the pass → one more pass
        go.set()
        assert thread.wait_passes(3)
        thread.wake()
        assert thread.wait_passes(4)  # after the failed one
    finally:
        assert thread.stop(10)
    assert thread.passes == 4


def test_a_fleet_change_wakes_the_pass_and_a_same_beat_does_not():
    now = datetime.now(timezone.utc)
    woken: list[int] = []
    fleet = FleetLive(now, VERSION, on_change=lambda: woken.append(1))
    boot = ident()
    plain = Heartbeat.model_validate(beat(boot))
    fleet.observe("w", plain, now)  # a new entry
    fleet.observe("w", plain, now + timedelta(seconds=5))  # nothing changed
    assert len(woken) == 1
    for change in (
        {"paused": True},
        {"tiers_ready": ["standard"]},
        {"asr_ready": False},
        {"version": "0.0.1"},
        {"staged": [{"film": ident(), "rev": 1}]},
    ):
        fleet.observe("w", Heartbeat.model_validate(beat(boot, **change)), now)
        fleet.observe("w", plain, now)
    assert len(woken) == 1 + 10
    fleet.observe("w", plain, now + timedelta(seconds=120))  # back from 失联
    assert len(woken) == 12


def test_the_controller_host_stops_its_routing_thread(tmp_path):
    from taskpaw_v3.workflow.local_host import LocalWorkflowHost

    host = LocalWorkflowHost.__new__(LocalWorkflowHost)
    thread = RoutingThread(lambda: None, interval=3600, wait=Ticks())
    thread.start()
    assert thread.wait_passes(1)
    host.scheduler = thread
    host._wake_scheduler()
    assert thread.wait_passes(2)
    assert thread.stop(10)
