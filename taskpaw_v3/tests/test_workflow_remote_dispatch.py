"""P2-3 (#294) §3–§5 on the worker side, against a scripted controller:
`WorkerRuntime`, the claim gate (R6), prefetch (R7), staging GC (R10), the
keep-awake seam (R11), 暂停领取 (R12) and the controller's routing thread
(R13). The real controller is exercised end to end in
`test_workflow_remote_e2e.py`."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import ExecOutcome, SyntheticExecutor
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.keep_awake import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    KeepAwake,
)
from taskpaw_v3.workflow.local_dispatch import run_stage
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
        self._status(body["attempt_id"], "running")
        return {"assignment_id": ident(), "attempt_id": body["attempt_id"]}

    def _status(self, attempt_id, status):
        """The stage of a claimed attempt moves on as the store would."""
        job = next(
            (c["job_id"] for c in self.claims if c["attempt_id"] == attempt_id), None
        )
        for film in self.films.values():
            for record in film["stages"].values():
                if record["job_id"] == job:
                    record["status"] = status

    def renew(self, attempt_id, body):
        return {"version": 2}

    def finish(self, attempt_id, body):
        self.finishes.append((attempt_id, body))
        self._status(attempt_id, body["outcome"])
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
            {
                "outcome": "failed",
                "error_class": "cancelled",
                "message": "已停止",
                "energy": None,  # unknown, never 0
            },
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


def test_a_route_overtaken_by_a_race_is_logged_with_its_code(
    tmp_path, monkeypatch, caplog
):
    """§11: a decision the store refuses with a tolerated code is skipped,
    logged at INFO with the code and named in the outcome."""
    import logging

    from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
    from taskpaw_v3.tests.workflow_route_helpers import enrol_remote
    from taskpaw_v3.workflow import models as m
    from taskpaw_v3.workflow.routing import run_pass

    rt = runtime(tmp_path / "wf")
    try:
        remote = enrol_remote(rt, "5060 Ti-1")
        film = add_film(rt)
        now = rt.clock.now()
        fleet = FleetLive(now - timedelta(minutes=5), VERSION)
        fleet.observe(
            remote.worker_id, Heartbeat.model_validate(beat(remote.boot_id)), now
        )

        def refused(*args, **kw):
            raise m.WorkflowError("source_not_admissible")

        monkeypatch.setattr(rt.store, "route_film", refused)
        with caplog.at_level(logging.INFO, logger="taskpaw.workflow"):
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
        assert [d.film_id for d in outcome.planned.routes] == [film]
        assert outcome.routed == ()
        assert outcome.overtaken == ((film, "source_not_admissible"),)
        assert any(
            r.levelno == logging.INFO and "code=source_not_admissible" in r.message
            for r in caplog.records
        )
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


# ── self-review: Windows file locks ─────────────────────────────────────
def test_a_held_attempt_file_is_retried_and_a_lost_request_write_sends_nothing(
    tmp_path, monkeypatch
):
    from taskpaw_v3.workflow import remote_dispatch as rd

    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    film = client.add()
    real, refusals = rd.atomic_json, {"left": 2}

    def flaky(path, value):
        if path.parent.name == "attempts" and refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(32, "sharing violation")
        return real(path, value)

    monkeypatch.setattr(rd, "atomic_json", flaky)
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        assert executor.running.wait(10)  # two refusals, then the claim went out
        assert len(client.claims) == 1
    finally:
        executor.release.set()
        assert d.stop(10)
    refusals["left"] = 10**6  # the request can never be written: no claim
    executor2 = Blocking()
    d2, _ = _dispatcher(tmp_path / "b", lambda: client, executor2)
    d2.staging = staging
    other = client.add()
    d2.index = d.index
    client.download_film(client.envelope(other, 1), staging)
    d.index.mark(
        other,
        1,
        [c["relative_path"] for c in client.envelope(other, 1)["workcopy"]],
        True,
    )
    before = len(client.claims)
    d2.start()
    try:
        _observe(d2, [_route(other, 0)])
        until(lambda: d2.reason.startswith("无法写入"))
        assert len(client.claims) == before and executor2.runs == 0
    finally:
        assert d2.stop(10)


def test_a_staged_file_held_open_keeps_its_release_for_later(tmp_path, monkeypatch):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    film = client.add()
    envelope = client.envelope(film, 1)
    client.download_film(envelope, staging)
    d.index.mark(film, 1, [c["relative_path"] for c in envelope["workcopy"]], True)
    held = staging.work_dir / envelope["workcopy"][0]["relative_path"]
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if self == held:
            raise PermissionError(32, "in use")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    _observe(d, [], release=[{"film": film, "rev": 1}])
    assert held.exists() and d.staged() == [{"film": film, "rev": 1}]  # asked again
    monkeypatch.setattr(Path, "unlink", real)
    _observe(d, [], release=[{"film": film, "rev": 1}])
    assert not held.exists() and d.staged() == []


# ── §11 review dispositions ─────────────────────────────────────────────
OUTPUT = b"restored video"


class Producing:
    """A restore that writes one output and its ledger (with timing)."""

    stage = "restore"
    uses_gpu = True

    def __init__(self, message: str = "ok") -> None:
        self.runs, self.message = 0, message
        self.timing = {"scan": 1.0, "ltx": 4.0, "ltx_runs": 1, "wall": 6.0}

    def tools(self, job=None):
        return SyntheticExecutor("restore").tools()

    def max_resolution(self, vram):
        return (1920, 1080)

    def run(self, context):
        from taskpaw_v3.workflow import models as m
        from taskpaw_v3.workflow.stage_results import RESULT_NAME

        self.runs += 1
        attempt = context.workdir.name
        (context.workdir / "movie.mkv").write_bytes(OUTPUT)
        ledger = {
            "schema_version": 1,
            "film_id": context.job.film_id,
            "input_revision": context.job.input_revision,
            "stage": "restore",
            "attempt_id": attempt,
            "videos": [
                {
                    "video_item_id": ident(),
                    "volume_ordinal": None,
                    "restore": {
                        "decision": "applied",
                        "file": {
                            "path": f"{attempt}/movie.mkv",
                            "sha256": hashlib.sha256(OUTPUT).hexdigest(),
                            "size": len(OUTPUT),
                        },
                        "width": 1920,
                        "height": 1080,
                        "duration_ms": 1000,
                        "video_streams": 1,
                        "audio_streams": 1,
                        "timing": self.timing,
                    },
                }
            ],
        }
        raw = json.dumps(ledger).encode("utf-8")
        (context.workdir / RESULT_NAME).write_bytes(raw)
        return ExecOutcome.succeeded(
            self.message,
            hashlib.sha256(raw).hexdigest(),
            m.RestoreTiming.model_validate(self.timing),
        )


class Uploads(Scripted):
    """Scripted plus an output plan and uploads (`gate`: each upload waits
    for it, or for the dispatcher's stop)."""

    def __init__(self, staging: Staging) -> None:
        super().__init__(staging)
        self.uploads: list[str] = []
        self.gate: threading.Event | None = None
        self.uploading = threading.Event()
        self.cancelled = threading.Event()
        self.upload_error: ApiError | None = None
        self.upload_once: ApiError | None = None
        self.finish_error: ApiError | None = None

    def plan_outputs(self, attempt_id, files):
        return {"files": list(files)}

    def upload_file(self, attempt_id, path, local, *, size, sha256, **kw):
        self.uploads.append(path)
        self.uploading.set()
        cancel = kw.get("cancel")
        if self.gate is not None:
            while not self.gate.wait(0.01):
                if cancel is not None and cancel.is_set():
                    self.cancelled.set()
                    raise ApiError(0, "unreachable")
        once, self.upload_once = self.upload_once, None
        if once is not None:
            raise once
        if self.upload_error is not None:
            raise self.upload_error
        return {"done": True}

    def finish(self, attempt_id, body):
        self.finishes.append((attempt_id, body))
        if self.finish_error is not None:
            raise self.finish_error
        self._status(attempt_id, body["outcome"])
        return {"state": "resolved", "outcome": body["outcome"]}


def _uploads(tmp_path, executor, **kw):
    client = Uploads.__new__(Uploads)
    d, staging = _dispatcher(tmp_path, lambda: client, executor, **kw)
    client.__init__(staging)
    return d, staging, client


class FakeJob:
    """An `OwnedJob` whose processes outlive the stop until `gone` is set."""

    def __init__(self) -> None:
        self.gone = threading.Event()

    def settle(self, timeout: float) -> bool:
        return self.gone.wait(min(timeout, 0.05))

    def terminate(self) -> None:
        pass


def test_cx1_stop_stays_unconfirmed_while_the_children_survive(tmp_path, monkeypatch):
    from taskpaw_v3.workflow import remote_dispatch as rd
    from taskpaw_v3.workflow.local_dispatch import StageRun

    job, ran = FakeJob(), threading.Event()

    def unsettled(executor, job_, manifest, workdir, cancel, **kw):
        ran.set()
        assert cancel.wait(10)
        return StageRun(ExecOutcome.failed("cancelled", "已停止"), job, False)

    monkeypatch.setattr(rd, "run_stage", unsettled)
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    film = client.add()
    d.start()
    _observe(d, [_route(film, 0)])
    assert ran.wait(10)
    assert d.stop(1.0) is False  # the tree still runs: not confirmed
    assert d.lease.holder() == d.run_id  # and it keeps the GPU (C4)
    assert d.stop(0.2) is False  # asked again: settled again, still running
    job.gone.set()
    assert d.stop(1.0) is True  # a later call settles it
    assert d.lease.holder() is None
    (path,) = (staging.root / "attempts").glob("*.json")
    settled = json.loads(path.read_text(encoding="utf-8"))["settled"]
    assert settled["error_class"] == "cancelled"  # persisted, never finished
    assert client.finishes == []


def _held_attempt(staging, film):
    attempt = ident()
    (staging.root / "attempts").mkdir(parents=True, exist_ok=True)
    (staging.root / "attempts" / f"{attempt}.json").write_text(
        json.dumps(
            {
                "attempt_id": attempt,
                "film_id": film,
                "rev": 1,
                "stage": "restore",
                "request": {},
                "assignment_id": ident(),
                "children": ["1:1"],
            }
        ),
        encoding="utf-8",
    )
    return attempt


def test_cx2_a_held_child_of_an_earlier_boot_reserves_the_gpu(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    film = client.add()
    attempt = _held_attempt(staging, film)
    d.start()
    try:
        d.booted({"reconciled": False, "attempts": {attempt: "held"}, "keep": []})
        assert d.lease.holder() == d.run_id
        assert d.lease.blocking_label() == "上次的任务进程仍在运行"
        assert not d.lease.try_acquire(("jasna-1", 7), 0.01, "Jasna：手动")
        d.lease.withdraw(("jasna-1", 7))
        _observe(d, [_route(film, 0)], reconciled=False)
        until(lambda: d.reason == "等待对账")  # the gate failed …
        d.wake()
        until(lambda: d.reason == "等待对账")
        assert d.lease.holder() == d.run_id  # … and kept the reservation
        d.booted({"reconciled": True, "attempts": {attempt: "interrupted"}})
        _observe(d, [])
        until(lambda: d.lease.holder() is None)  # confirmed stopped: free
        assert d.lease.try_acquire(("jasna-1", 7), 0.01, "Jasna：手动")
        d.lease.release(("jasna-1", 7))
    finally:
        assert d.stop(10)


def test_cx5_a_cancel_after_a_successful_settle_keeps_the_success(tmp_path):
    executor = Producing()
    d, staging, client = _uploads(tmp_path, executor)
    client.gate = threading.Event()
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        assert client.uploading.wait(10)
        attempt = client.claims[0]["attempt_id"]
        assert d.current()["phase"] == "uploading"
        _observe(d, [_route(film, 0)], cancel=[attempt])  # too late: settled ok
        client.upload_once = ApiError(0, "unreachable")  # the body is built again
        client.gate.set()
        until(lambda: client.finishes)
        assert client.finishes[0][1]["outcome"] == "succeeded"
        assert executor.runs == 1
    finally:
        assert d.stop(10)


def test_cx6_a_lost_settled_write_is_retried_before_the_delivery(tmp_path, monkeypatch):
    from taskpaw_v3.workflow import remote_dispatch as rd

    executor = Producing()
    d, staging, client = _uploads(tmp_path, executor)
    film = client.add()
    real, refuse = rd.atomic_json, threading.Event()
    refused = {"n": 0}

    def flaky(path, value):
        if path.parent.name == "attempts" and value.get("settled") and refuse.is_set():
            refused["n"] += 1
            raise PermissionError(32, "sharing violation")
        return real(path, value)

    monkeypatch.setattr(rd, "atomic_json", flaky)
    refuse.set()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        until(lambda: refused["n"] >= rd.SAVE_TRIES + 1)  # settle, then a retry
        assert client.finishes == [] and d.claimed()  # never dropped
        refuse.clear()
        until(lambda: client.finishes)
        assert client.finishes[0][1]["outcome"] == "succeeded"
        assert len(client.claims) == 1 and executor.runs == 1  # no re-run
    finally:
        assert d.stop(10)


def test_ir2_a_staged_file_gone_is_fetched_again_before_a_claim(tmp_path):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    film = client.add()
    envelope = client.envelope(film, 1)
    client.download_film(envelope, staging)
    rel = envelope["workcopy"][0]["relative_path"]
    d.index.mark(film, 1, [rel], True, sizes={rel: 10})
    (staging.work_dir / rel).unlink()  # gone behind the index's back
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        assert executor.running.wait(10)
        assert client.downloads == [film, film]  # fetched again first
        assert (staging.work_dir / rel).stat().st_size == 10
    finally:
        executor.release.set()
        assert d.stop(10)


def test_ir2_a_release_in_progress_is_never_claimable(tmp_path, monkeypatch):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    film = client.add()
    envelope = client.envelope(film, 1)
    client.download_film(envelope, staging)
    rel = envelope["workcopy"][0]["relative_path"]
    d.index.mark(film, 1, [rel], True, sizes={rel: 10})
    held = staging.work_dir / rel
    real = Path.unlink

    def unlink(self, missing_ok=False):
        if self == held:
            raise PermissionError(32, "in use")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    _observe(d, [], release=[{"film": film, "rev": 1}])
    assert held.exists() and d.staged() == [{"film": film, "rev": 1}]
    assert not d.index.complete(film, 1)  # releasing: never claimed again


def test_ir3_a_refused_failed_finish_ends_and_frees_the_gpu(tmp_path):
    class Long(Blocking):
        def run(self, context):
            self.runs += 1
            return ExecOutcome.failed("needs_attention", "错" * 5000)

    executor = Long()
    d, staging, client = _uploads(tmp_path, executor)
    client.finish_error = ApiError(422, "invalid_request")
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        until(lambda: client.finishes)
        attempt, body = client.finishes[0]
        assert len(body["message"]) <= 2000  # the model's limit, at the source
        refused = staging.root / "attempts" / "refused" / f"{attempt}.json"
        until(refused.exists)  # kept for inspection
        until(lambda: d.lease.holder() is None)
        assert "invalid_request" in (d.notice or "")
        assert not (staging.root / "attempts" / f"{attempt}.json").exists()
        _observe(d, [])
        until(lambda: d.reason == "idle")
        assert len(client.finishes) == 1  # terminal: never sent again
    finally:
        assert d.stop(10)


def test_ir5_recover_collects_a_dropped_attempt_and_orphan_work(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    gone = _held_attempt(staging, ident())  # the controller answers 404
    (staging.work_dir / gone).mkdir(parents=True)
    (staging.work_dir / gone / "big.mkv").write_bytes(b"x")
    orphan = staging.work_dir / ident()  # no attempt file at all
    orphan.mkdir()
    copies = staging.work_dir / "copies"
    copies.mkdir()
    assert d.recover(client)
    assert not (staging.work_dir / gone).exists() and not orphan.exists()
    assert copies.exists()
    assert list((staging.root / "attempts").glob("*.json")) == []


def test_ir5_nothing_is_swept_while_an_attempt_is_unknown(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    _held_attempt(staging, ident())
    orphan = staging.work_dir / ident()
    orphan.mkdir(parents=True)

    def unreachable(attempt_id):
        raise ApiError(0, "unreachable")

    client.attempt = unreachable  # type: ignore[method-assign]
    with pytest.raises(ApiError):
        d.recover(client)
    assert orphan.exists()


def test_ir6_stop_interrupts_an_upload_and_ir8_keeps_the_started_time(tmp_path):
    now = [100.0]
    executor = Producing()
    client = Uploads.__new__(Uploads)
    staging = Staging(tmp_path / "staging", 500, free_bytes=lambda p: 2_000 * GB)
    d = RemoteDispatcher(
        staging,
        lambda: client,
        {"restore": executor},
        WorkerRuntime(staging.root),
        boot_id=ident(),
        lease=GpuLease(),
        power=lambda: None,
        poll=0.02,
        stop_settle=5.0,
        backoff_base=0.01,
        clock=lambda: now[0],
    )
    client.__init__(staging)
    client.gate = threading.Event()  # never opened: only the stop ends it
    film = client.add()
    d.start()
    stopped = False
    try:
        _observe(d, [_route(film, 0)])
        assert client.uploading.wait(10)
        now[0] += 50.0
        current = d.current()
        assert current["phase"] == "uploading"
        assert current["elapsed_s"] >= 50.0  # IR8: 用时 counts from the claim
        assert d.stop(10) is True  # IR6: the upload saw the stop
        stopped = True
        assert client.cancelled.is_set() and client.finishes == []
        (path,) = (staging.root / "attempts").glob("*.json")
        assert json.loads(path.read_text(encoding="utf-8"))["settled"]["ok"]
    finally:
        client.gate.set()
        if not stopped:
            d.stop(10)


def test_ir7_a_mismatching_output_is_sent_whole_three_times_in_all(tmp_path):
    executor = Producing()
    d, staging, client = _uploads(tmp_path, executor)
    client.upload_error = ApiError(409, "hash_mismatch")
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        until(lambda: client.finishes)
        assert len(client.uploads) == 1  # one call: the client's three sends
        body = client.finishes[-1][1]
        assert body["outcome"] == "failed"
        assert body["error_class"] == "needs_attention"
    finally:
        assert d.stop(10)


def test_ir9_a_ledger_found_at_boot_finishes_with_its_timing(tmp_path):
    executor = Producing()
    d, staging, client = _uploads(tmp_path, executor)
    film = client.add()
    reached = threading.Event()
    d._settle = lambda active, outcome: reached.set()  # type: ignore[method-assign]
    d.start()
    _observe(d, [_route(film, 0)])
    assert reached.wait(10)  # the process 'dies' before the settled record
    d._unsettled = None
    assert d.stop(10)
    (path,) = (staging.root / "attempts").glob("*.json")
    d2, _, client2 = _uploads(tmp_path, executor)
    d2.booted({"reconciled": False, "attempts": {path.stem: "finish"}})
    d2.start()
    try:
        until(lambda: client2.finishes)
        body = client2.finishes[0][1]
        assert body["outcome"] == "succeeded"
        assert body["timing"]["wall"] == 6.0 and body["energy"] is None
    finally:
        assert d2.stop(10)


def test_ir1_a_settled_attempt_is_delivered_whatever_the_boot_says(tmp_path):
    executor = Producing()
    d, staging, client = _uploads(tmp_path, executor)
    film = client.add()
    reached = threading.Event()
    real_settle = d._settle

    def settle_then_die(active, outcome):  # settled on disk, never delivered
        d._stop.set()
        real_settle(active, outcome)
        reached.set()

    d._settle = settle_then_die  # type: ignore[method-assign]
    d.start()
    _observe(d, [_route(film, 0)])
    assert reached.wait(10)
    assert d.stop(10)
    (path,) = (staging.root / "attempts").glob("*.json")
    d2, _, client2 = _uploads(tmp_path, executor)
    d2.booted({"reconciled": True, "attempts": {}})  # no verdict for it
    d2.start()
    try:
        until(lambda: client2.finishes)
        assert client2.finishes[0][0] == path.stem
        until(lambda: not path.exists())
    finally:
        assert d2.stop(10)


def test_ir11_the_typed_current_fits_the_heartbeat(tmp_path):
    class Sampler:
        def live(self):
            return {
                "w": 120.0,
                "gpu_w": 90.0,
                "cpu_w": 20.0,
                "other_w": 10.0,
                "today_wh": 999.0,
                "activity": "workflow",
            }

        def reading(self, owner):
            return None

    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    d.power = lambda: Sampler()
    client.__init__(staging)
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        assert executor.running.wait(10)
        with d._lock:
            active = d._active
        assert active is not None
        active.ltx = {"console": "x" * 200_000, "params": {"p": "y" * 100_000}}
        value = d.current()
        assert len(json.dumps(value).encode("utf-8")) <= 32 << 10
        assert value["power"] == {
            "w": 120.0,
            "gpu_w": 90.0,
            "cpu_w": 20.0,
            "other_w": 10.0,
            "estimated": True,
        }
    finally:
        executor.release.set()
        assert d.stop(10)


# ── §11 Codex r2 (CX7–CX9) ──────────────────────────────────────────────
def _settled_ok_on_disk(tmp_path, executor):
    """A successful stage settled on disk and never delivered (the process
    'dies' after the settled record); returns the attempt file and folder."""
    d, staging, client = _uploads(tmp_path, executor)
    film = client.add()
    reached = threading.Event()
    real_settle = d._settle

    def settle_then_die(active, outcome):
        d._stop.set()
        real_settle(active, outcome)
        reached.set()

    d._settle = settle_then_die  # type: ignore[method-assign]
    d.start()
    _observe(d, [_route(film, 0)])
    assert reached.wait(10)
    assert d.stop(10)
    (path,) = (staging.root / "attempts").glob("*.json")
    return path, staging.work_dir / path.stem


@pytest.mark.parametrize("lost", ["ledger", "output", "changed"])
@pytest.mark.parametrize("verdict", ["finish", None])
def test_cx7_a_recovered_success_that_lost_its_files_fails_once(
    tmp_path, lost, verdict
):
    from taskpaw_v3.workflow.stage_results import RESULT_NAME

    executor = Producing()
    path, folder = _settled_ok_on_disk(tmp_path, executor)
    if lost == "ledger":
        (folder / RESULT_NAME).unlink()
    elif lost == "output":
        (folder / "movie.mkv").unlink()
    else:
        with (folder / RESULT_NAME).open("ab") as fh:
            fh.write(b" ")  # the digest the settled record holds no longer fits
    d2, _, client2 = _uploads(tmp_path, executor)
    attempts = {path.stem: verdict} if verdict else {}
    d2.booted({"reconciled": True, "attempts": attempts})
    d2.start()
    try:
        until(lambda: client2.finishes)
        attempt, body = client2.finishes[0]
        assert attempt == path.stem
        assert body["outcome"] == "failed"
        assert body["error_class"] == "cancelled"  # a free retry (T08-P1a R3)
        assert "本地结果丢失" in body["message"]
        until(lambda: not path.exists() and not folder.exists())
        until(lambda: not d2.claimed() and d2.lease.holder() is None)
        assert len(client2.finishes) == 1 and client2.uploads == []  # once, no loop
    finally:
        assert d2.stop(10)


def test_cx7_an_intact_recovered_success_is_still_delivered(tmp_path):
    executor = Producing()
    path, folder = _settled_ok_on_disk(tmp_path, executor)
    d2, _, client2 = _uploads(tmp_path, executor)
    d2.booted({"reconciled": True, "attempts": {path.stem: "finish"}})
    d2.start()
    try:
        until(lambda: client2.finishes)
        assert client2.finishes[0][1]["outcome"] == "succeeded"
        assert client2.uploads == [f"{path.stem}/movie.mkv"]
    finally:
        assert d2.stop(10)


def test_cx7_the_worker_claims_again_after_a_lost_result(tmp_path):
    from taskpaw_v3.workflow.stage_results import RESULT_NAME

    executor = Producing()
    path, folder = _settled_ok_on_disk(tmp_path, executor)
    (folder / RESULT_NAME).unlink()
    d2, _, client2 = _uploads(tmp_path, executor)
    other = client2.add()
    d2.booted({"reconciled": True, "attempts": {path.stem: "finish"}})
    d2.start()
    try:
        _observe(d2, [_route(other, 0)])
        until(lambda: len(client2.claims) == 1)  # not stuck in the delivery
        until(lambda: len(client2.finishes) == 2)
        assert [b["outcome"] for _, b in client2.finishes] == ["failed", "succeeded"]
    finally:
        assert d2.stop(10)


def test_cx8_an_incomplete_download_is_reported_and_releasable(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    film = client.add()
    envelope = client.envelope(film, 1)
    rel = envelope["workcopy"][0]["relative_path"]
    part = staging.work_dir / f"{rel}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"xx")
    d.index.mark(film, 1, [rel], False, sizes={rel: 10})  # a download cut short
    assert d.staged() == [{"film": film, "rev": 1, "incomplete": True}]
    assert not d.index.complete(film, 1)
    _observe(d, [], release=[{"film": film, "rev": 1}])  # the route moved on
    assert not part.exists() and d.staged() == []
    assert d.index.entries() == {}


def test_cx8_an_active_fetch_is_never_released(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    film = client.add()
    rel = client.envelope(film, 1)["workcopy"][0]["relative_path"]
    part = staging.work_dir / f"{rel}.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"xx")
    d.index.mark(film, 1, [rel], False, sizes={rel: 10})
    d._fetching = f"{film}/1"  # the fetch thread is on it
    _observe(d, [], release=[{"film": film, "rev": 1}])
    assert part.exists()
    assert d.staged() == [{"film": film, "rev": 1, "incomplete": True}]
    d._fetching = None
    _observe(d, [], release=[{"film": film, "rev": 1}])  # asked again, now idle
    assert not part.exists() and d.staged() == []


def test_cx11_a_file_shared_by_two_films_outlives_the_first_release(tmp_path):
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    first, second = client.add(), client.add()
    own = {
        f: client.envelope(f, 1)["workcopy"][0]["relative_path"]
        for f in (first, second)
    }
    shared = "package/cover.jpg"  # one intake package, one folder cover
    root = staging.work_dir
    for rel in (*own.values(), shared):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x")
    d.index.mark(first, 1, [own[first], shared], True)
    d.index.mark(second, 1, [own[second], shared], True)  # staged
    _observe(d, [], release=[{"film": first, "rev": 1}])
    assert not (root / own[first]).exists() and (root / shared).exists()
    # the second film is being downloaded: its `.part` is not another's to take
    part = root / f"{shared}.part"
    (root / shared).unlink()
    part.write_bytes(b"xx")
    d.index.mark(first, 1, [own[first], shared], True)
    d.index.mark(second, 1, [own[second], shared], False)
    d._fetching = f"{second}/1"
    _observe(d, [], release=[{"film": first, "rev": 1}])
    assert part.exists()
    d._fetching = None
    _observe(d, [], release=[{"film": second, "rev": 1}])  # the last holder
    assert not part.exists() and not (root / own[second]).exists()
    assert d.index.entries() == {}


class _Reading:
    def __init__(self, owner: str) -> None:
        self.owner = owner


class _Delta:
    def to_dict(self) -> dict:
        return {
            "wh": 1.5,
            "gpu_wh": 1.0,
            "cpu_wh": 0.3,
            "other_wh": 0.2,
            "seconds": 60.0,
            "avg_w": 90.0,
            "peak_w": 120.0,
        }


class _Sampler:
    def reading(self, owner):
        return _Reading(owner)

    def energy_between(self, start, end):
        return _Delta()

    def live(self):
        return None


def test_cx9_a_cancelled_finish_keeps_the_settled_energy(tmp_path):
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    d.power = lambda: _Sampler()
    client.__init__(staging)
    film = client.add()
    d.start()
    try:
        _observe(d, [_route(film, 0)])
        assert executor.running.wait(10)
        attempt = client.claims[0]["attempt_id"]
        _observe(d, [_route(film, 0)], cancel=[attempt])
        until(lambda: client.finishes)
        body = client.finishes[0][1]
        assert body["error_class"] == "cancelled" and body["message"] == "已停止"
        assert body["energy"]["wh"] == 1.5 and body["energy"]["peak_w"] == 120.0
    finally:
        assert d.stop(10)


def test_cx10_a_pause_during_the_envelope_read_admits_no_claim(tmp_path):
    """§11 CX10: the gate's `paused` was read before the envelope request; a
    pause acknowledged while that request is in flight must still stop the
    claim."""
    executor = Blocking()
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, executor)
    client.__init__(staging)
    film = client.add()
    reading, proceed = threading.Event(), threading.Event()
    envelope = client.envelope

    def slow_envelope(film_id, rev):
        if threading.current_thread().name == "workflow-worker-run":  # the gate
            reading.set()
            assert proceed.wait(10)
        return envelope(film_id, rev)

    client.envelope = slow_envelope  # type: ignore[method-assign]
    routes = [_route(film, 0)]
    d.start()
    try:
        _observe(d, routes)
        assert reading.wait(10)  # the gate passed `paused=False` and waits
        _observe(d, routes, paused=True)
        d.acknowledge_pause()  # what WorkerHost.set_paused(True) ends with
        proceed.set()
        until(lambda: d.reason == "暂停领取")
        assert client.claims == [] and executor.runs == 0
        assert list((staging.root / "attempts").glob("*.json")) == []
        assert not d.claimed()
    finally:
        proceed.set()
        executor.release.set()
        assert d.stop(10)


@pytest.mark.parametrize("error_class", ["needs_attention", "permission", "transient"])
def test_cx11_a_stop_is_a_free_retry_whatever_the_child_reported(tmp_path, error_class):
    """§11 CX11: a stage stopped because its session ended resolves
    `cancelled` (a free retry) even when the executor already failed with
    another class by the time the stop was seen."""
    stopped = threading.Event()

    class ExitsAsStopped:
        stage = "restore"
        uses_gpu = True

        def run(self, context):
            context.cancel.set()  # the stop lands, then the child's own exit
            stopped.set()
            return ExecOutcome.failed(error_class, "child exit 3")

    client = Scripted.__new__(Scripted)
    staging = Staging(tmp_path / "staging", 500, free_bytes=lambda p: 2_000 * GB)
    client.__init__(staging)
    envelope = client.envelope(client.add(), 1)
    job = m.PendingComputeJob(
        job_id=envelope["stages"]["restore"]["job_id"],
        film_id=envelope["film"],
        display_name="x",
        stage="restore",
        input_revision=1,
        version=1,
        film_created_at=datetime.now(timezone.utc),
        ltx_model=None,
    )
    result = run_stage(
        ExitsAsStopped(),  # type: ignore[arg-type]
        job,
        m.InputManifest.model_validate(envelope["manifest"]),
        tmp_path / "work",
        threading.Event(),
        on_launch=lambda identity: None,
        progress=lambda text, ltx=None: None,
        stop_settle=5.0,
    )
    assert stopped.is_set()
    assert result.outcome.error_class == "cancelled" and result.settled


def test_cx13_held_children_keep_the_shutdown_unconfirmed(tmp_path, monkeypatch):
    from taskpaw_v3.workflow import remote_dispatch as rd

    alive = {"yes": True}
    monkeypatch.setattr(rd, "process_alive", lambda identity: alive["yes"])
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    attempt = _held_attempt(staging, client.add())
    d.booted({"reconciled": False, "attempts": {attempt: "held"}, "keep": []})
    assert d.lease.holder() == d.run_id
    assert d.stop(0.2) is False  # a child of an earlier boot still runs
    assert d.reason == rd.HELD and d.reason == "上次的任务进程仍在运行"
    assert d.lease.holder() == d.run_id  # the reservation stays
    assert d.halted
    alive["yes"] = False
    assert d.stop(0.2) is True  # gone: confirmed, the GPU is free
    assert d.lease.holder() is None


def test_cx14_a_deletion_that_failed_is_retried_by_the_periodic_sweep(
    tmp_path, monkeypatch
):
    from taskpaw_v3.workflow import remote_dispatch as rd

    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    gone = _held_attempt(staging, ident())  # the controller answers 404
    (staging.work_dir / gone).mkdir(parents=True)
    (staging.work_dir / gone / "big.mkv").write_bytes(b"x")
    live = _held_attempt(staging, ident())  # a file names it: never swept
    (staging.work_dir / live).mkdir(parents=True)
    held_open = threading.Event()  # Windows: the output is still open
    real = rd.shutil.rmtree

    def rmtree(path, *args, **kw):
        if Path(path).name == gone and not held_open.is_set():
            return  # ignore_errors=True: it stays behind
        return real(path, *args, **kw)

    monkeypatch.setattr(rd.shutil, "rmtree", rmtree)
    monkeypatch.setattr(rd, "SWEEP_SECONDS", 0.0)
    real_get = client.attempt

    def attempt(attempt_id):
        if attempt_id == live:
            return {"state": "active"}
        return real_get(attempt_id)  # 404

    client.attempt = attempt  # type: ignore[method-assign]
    assert d.recover(client)
    assert (staging.work_dir / gone).exists()  # the first deletion failed
    assert not (staging.root / "attempts" / f"{gone}.json").exists()
    d.start()
    try:
        d.wake()
        assert (staging.work_dir / gone).exists()  # still held: kept
        held_open.set()
        d.wake()
        until(lambda: not (staging.work_dir / gone).exists())
        assert (staging.work_dir / live).exists()
    finally:
        assert d.stop(10)


# ── Codex round 6 (P2-4 §9 CX18) ────────────────────────────────────────
def _deny_reads(monkeypatch, *paths):
    """Reading these attempt files raises PermissionError (antivirus, an
    ACL); every other read is real. Returns a function that lifts it."""
    denied = {Path(p) for p in paths}
    real = Path.read_text

    def read_text(self, *args, **kw):
        if self in denied:
            raise PermissionError("denied")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    return denied.clear


def test_cx18_an_unreadable_attempt_record_is_reported_held_and_kept(
    tmp_path, monkeypatch
):
    """§9 CX18 (Codex r6): after a restart an attempt file that raises on
    read is reported to the boot as an explicit unknown entry by its file
    name, never left out (which the controller would settle as interrupted);
    a held verdict keeps the file and its work folder, reserves the GPU and
    keeps the shutdown unconfirmed; the read is retried at every report."""
    from taskpaw_v3.workflow import remote_dispatch as rd

    monkeypatch.setattr(rd, "process_alive", lambda identity: False)
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    readable = _held_attempt(staging, client.add())
    locked = _held_attempt(staging, client.add())
    path = staging.root / "attempts" / f"{locked}.json"
    (staging.work_dir / locked).mkdir(parents=True)
    lift = _deny_reads(monkeypatch, path)

    reports = {r["attempt_id"]: r for r in d.reports()}
    assert set(reports) == {readable, locked}
    assert reports[locked]["unreadable"] is True
    assert reports[locked]["children_alive"] is True  # unknown is never stopped
    assert reports[readable]["unreadable"] is False

    d.booted({"reconciled": False, "attempts": {locked: "held"}, "keep": []})
    assert path.exists() and (staging.work_dir / locked).exists()
    assert d.lease.holder() == d.run_id
    assert d.lease.blocking_label() == rd.UNREADABLE_ATTEMPTS
    # a stale or mistaken verdict never deletes a record it cannot read
    d.booted({"reconciled": False, "attempts": {locked: "interrupted"}, "keep": []})
    assert path.exists() and (staging.work_dir / locked).exists()
    d.booted({"reconciled": False, "attempts": {locked: "held"}, "keep": []})
    assert d.stop(0.2) is False  # its children are unknown: not confirmed
    assert d.lease.holder() == d.run_id

    lift()  # the read works again: an ordinary report next time
    again = {r["attempt_id"]: r for r in d.reports()}
    assert again[locked]["unreadable"] is False
    assert again[locked]["children_alive"] is False


def test_cx18_an_unreadable_entry_without_an_attempt_name_is_still_reported(
    tmp_path,
):
    """§9 CX18: a corrupt file or one whose name is no attempt id is still
    reported (a stable id derived from its name), so the boot holds."""
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    attempts = staging.root / "attempts"
    attempts.mkdir(parents=True, exist_ok=True)
    corrupt = ident()
    (attempts / f"{corrupt}.json").write_text("{not json", encoding="utf-8")
    (attempts / "junk.tmp").write_bytes(b"x")
    reports = d.reports()
    assert all(r["unreadable"] for r in reports) and len(reports) == 2
    ids = {r["attempt_id"] for r in reports}
    assert corrupt in ids
    for value in ids:
        assert str(uuid.UUID(value)) == value  # a valid boot body
    assert ids == {r["attempt_id"] for r in d.reports()}  # stable


def test_cx18_an_unreadable_attempts_folder_sweeps_no_work_folder(
    tmp_path, monkeypatch
):
    """§9 CX18: the orphan sweep names its known attempts from the folder; a
    folder that cannot be listed names nothing, so nothing is swept."""
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    attempt = _held_attempt(staging, ident())
    (staging.work_dir / attempt).mkdir(parents=True)
    target = os.fspath(d.attempt_dir)
    denied = _path_shim(os.scandir, lambda path: path == target)
    real_glob = Path.glob

    def swallowed(self, pattern, *args, **kw):  # `Path.glob` hides the error
        if self == d.attempt_dir:
            return iter(())
        return real_glob(self, pattern, *args, **kw)

    monkeypatch.setattr(os, "scandir", denied)
    monkeypatch.setattr(Path, "glob", swallowed)
    d._sweep_orphans()
    assert (staging.work_dir / attempt).exists()


# ── root cause (P2-4 §9 CX20–CX22): one attempt inventory ────────────────
def _boot_reports(reports):
    from taskpaw_v3.workflow import remote_attempts as ra

    return [
        ra.AttemptReport(
            r["attempt_id"],
            r["children_alive"],
            r["ledger"],
            r["outputs_intact"],
            r["outcome"],
            unreadable=r["unreadable"],
        )
        for r in reports
    ]


def _settle(attempts, reports):
    """`settle_boot` over open assignments of an earlier boot (`attempts`)."""
    from types import SimpleNamespace

    from taskpaw_v3.workflow import remote_attempts as ra

    worker, old_boot, new_boot = (ident() for _ in range(3))
    resolved: list[str] = []
    open_ = [
        SimpleNamespace(
            assignment=SimpleNamespace(worker_id=worker, boot_id=old_boot, attempt_id=a)
        )
        for a in attempts
    ]
    rt = SimpleNamespace(
        operator=None,
        store=SimpleNamespace(list_open_assignments=lambda op: open_),
        resolve_interrupted=lambda a: resolved.append(a.attempt_id),
    )
    return ra.settle_boot(rt, worker, new_boot, _boot_reports(reports)), resolved


def test_cx20_a_read_failing_once_then_working_is_still_reported(tmp_path, monkeypatch):
    """§9 CX20 (Codex): the boot report read each record twice (once for the
    states, once for the unreadable names); a read failing in the first pass
    and working in the second dropped the record from the report, and the
    controller settled its assignment as interrupted. One read per entry:
    it is reported unreadable, nothing is deleted, nothing is interrupted."""
    from taskpaw_v3.workflow import remote_dispatch as rd

    monkeypatch.setattr(rd, "process_alive", lambda identity: False)
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    flaky = _held_attempt(staging, client.add())
    record = staging.root / "attempts" / f"{flaky}.json"
    (staging.work_dir / flaky).mkdir(parents=True)
    reads = {"n": 0}
    real = Path.read_text

    def read_text(self, *args, **kw):
        if self == record:
            reads["n"] += 1
            if reads["n"] == 1:
                raise PermissionError("denied once")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", read_text)
    reports = d.reports()
    assert [r["attempt_id"] for r in reports] == [flaky]
    assert reports[0]["unreadable"] is True
    assert reports[0]["children_alive"] is True  # unknown is never stopped
    verdicts, resolved = _settle([flaky], reports)
    assert verdicts == {flaky: "held"} and resolved == []
    reads["n"] = 0  # the booted pass fails its one read again
    d.booted({"reconciled": False, "attempts": {flaky: "interrupted"}, "keep": []})
    assert record.exists() and (staging.work_dir / flaky).exists()
    assert d.lease.blocking_label() == rd.UNREADABLE_ATTEMPTS
    reads["n"] = 0  # and the stop's one read fails again
    assert d.stop(0.2) is False  # held and unknown: unconfirmed
    assert d.lease.holder() == d.run_id


def test_cx22_an_unreadable_attempts_folder_keeps_a_held_attempt_alive(
    tmp_path, monkeypatch
):
    """§9 CX22 (Codex): a held attempt whose folder can no longer be listed
    is unknown, which is alive — `stop()` is unconfirmed and the GPU lease
    stays reserved (the old check read the record by path and, its children
    gone, released the GPU while the folder said nothing)."""
    import os

    from taskpaw_v3.workflow import remote_dispatch as rd

    monkeypatch.setattr(rd, "process_alive", lambda identity: False)
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    held = _held_attempt(staging, client.add())
    monkeypatch.setattr(rd, "process_alive", lambda identity: True)
    d.booted({"reconciled": False, "attempts": {held: "held"}, "keep": []})
    assert d.lease.holder() == d.run_id
    monkeypatch.setattr(rd, "process_alive", lambda identity: False)
    target = os.fspath(d.attempt_dir)
    monkeypatch.setattr(
        os, "scandir", _path_shim(os.scandir, lambda path: path == target)
    )
    assert d.stop(0.2) is False
    assert d.lease.holder() == d.run_id
    assert (staging.root / "attempts" / f"{held}.json").exists()


def _path_shim(real: Any, deny: Any) -> Any:
    """A filesystem shim that refuses (PermissionError) only a path argument
    `deny(os.fspath(path))` names and hands everything else to `real`
    unchanged — on POSIX `shutil.rmtree` walks by file descriptor and calls
    `os.scandir(fd)` with an int (`_rmtree_safe_fd`), which must reach the
    real function."""

    def shim(path: Any = ".") -> Any:
        if isinstance(path, (str, bytes, os.PathLike)) and deny(os.fspath(path)):
            raise PermissionError("denied")
        return real(path)

    return shim


def test_the_path_shim_forwards_a_file_descriptor_to_the_real_function(tmp_path):
    seen: list[Any] = []

    def real(path: Any = ".") -> str:
        seen.append(path)
        return "real"

    shim = _path_shim(real, lambda path: True)
    assert shim(7) == "real" and seen == [7]  # an fd is never denied
    with pytest.raises(PermissionError):
        shim(tmp_path)
    assert seen == [7]
    other = _path_shim(real, lambda path: path == os.fspath(tmp_path / "x"))
    assert other(tmp_path) == "real" and seen == [7, tmp_path]


@pytest.mark.skipif(
    os.scandir not in os.supports_fd, reason="os.scandir takes no fd here"
)
def test_rmtree_by_file_descriptor_passes_through_the_shim(tmp_path, monkeypatch):
    """The POSIX path CI hit: rmtree's fd walk through the patched scandir."""
    import shutil

    denied = tmp_path / "attempts"
    denied.mkdir()
    victim = tmp_path / "work" / "a" / "b"
    victim.mkdir(parents=True)
    (victim / "f.bin").write_bytes(b"x")
    target = os.fspath(denied)
    monkeypatch.setattr(
        os, "scandir", _path_shim(os.scandir, lambda path: path == target)
    )
    shutil.rmtree(tmp_path / "work")
    assert not (tmp_path / "work").exists()
    with pytest.raises(PermissionError):
        os.scandir(denied)


_MODES = ("readable", "unreadable-file", "unreadable-dir", "transient")
_OPS = ("boot", "busy", "stop", "sweep", "recover", "deliver")


@pytest.mark.parametrize("seed", range(40))
def test_no_operation_deletes_or_idles_what_its_inventory_could_not_read(
    tmp_path, monkeypatch, seed
):
    """§9 CX20–CX22, property style: random entries — readable, a file that
    never reads, a folder that cannot be listed, a read failing every other
    time — under random boot / busy / stop / GC / recovery / delivery steps.
    No step deletes a record or a work folder an inventory of that step
    could not read, and busy is never false while any entry exists."""
    import random

    from taskpaw_v3.workflow import remote_dispatch as rd
    from taskpaw_v3.workflow.remote_dispatch import worker_busy

    rng = random.Random(seed)
    monkeypatch.setattr(rd, "process_alive", lambda identity: False)
    monkeypatch.setattr(rd, "SWEEP_SECONDS", 0.0)
    client = Scripted.__new__(Scripted)
    d, staging = _dispatcher(tmp_path, lambda: client, Blocking())
    client.__init__(staging)
    attempts = staging.root / "attempts"
    modes: dict[str, str] = {}
    for _ in range(rng.randint(1, 4)):
        attempt = _held_attempt(staging, client.add())
        (staging.work_dir / attempt).mkdir(parents=True)
        modes[attempt] = rng.choice(_MODES)
    paths = {attempts / f"{a}.json": a for a in modes}
    failed: set[str] = set()
    counts: dict[str, int] = {}
    real_read, real_scandir = Path.read_text, os.scandir

    def read_text(self, *args, **kw):
        attempt = paths.get(self)
        if attempt is not None:
            counts[attempt] = counts.get(attempt, 0) + 1
            mode = modes[attempt]
            if mode == "unreadable-file" or (
                mode == "transient" and counts[attempt] % 2 == 1
            ):
                failed.add(attempt)
                raise PermissionError("denied")
        return real_read(self, *args, **kw)

    def unlistable(path: Any) -> bool:
        if path != os.fspath(attempts) or not any(
            modes[a] == "unreadable-dir" and (attempts / f"{a}.json").exists()
            for a in modes
        ):
            return False
        failed.update(a for a in modes if (attempts / f"{a}.json").exists())
        return True

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(os, "scandir", _path_shim(real_scandir, unlistable))

    class Answers:
        def attempt(self, attempt_id):
            if rng.random() < 0.5:
                raise ApiError(404, "not_found")
            return {"state": rng.choice(("resolved", "active"))}

    for _ in range(12):
        op = rng.choice(_OPS)
        failed.clear()
        before = {a for a in modes if (attempts / f"{a}.json").exists()}
        if op == "boot":
            reports = d.reports()
            verdicts, _resolved = _settle(sorted(modes), reports)
            for attempt in modes:  # a stale or mistaken verdict, now and then
                if rng.random() < 0.3:
                    verdicts[attempt] = rng.choice(("held", "interrupted", "finish"))
            failed.clear()  # the reply is a decision on its own inventory
            d.booted({"reconciled": False, "attempts": verdicts, "keep": []})
        elif op == "busy":
            assert worker_busy(staging.root, None) is bool(before)
            assert worker_busy(staging.root, d) is True or not before
        elif op == "stop":
            d.stop(0.2)
        elif op == "sweep":
            d._sweep_orphans()
        elif op == "recover":
            d.recover(Answers())
        else:
            with d._lock:
                pending = list(d._deliver)
            if pending:
                d._deliver_one(pending[0])
        for attempt in failed & before:
            assert (attempts / f"{attempt}.json").exists(), (op, modes[attempt])
            assert (staging.work_dir / attempt).exists(), (op, modes[attempt])
        left = any((attempts / f"{a}.json").exists() for a in modes)
        if left:
            assert worker_busy(staging.root, None) is True
            assert d.claimed() is True
