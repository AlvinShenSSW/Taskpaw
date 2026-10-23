"""P2-3 (#294) R15/R16: remote currents on the board and the drawer, and the
worker's `/status` summary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from taskpaw_v3.tests.test_workflow_board import done_through, film
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.worker_api_helpers import VERSION, beat
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def remote(film_id: str, **kw) -> dict:
    value = {
        "film_id": film_id,
        "stage": "restore",
        "attempt_id": identity(),
        "phase": "running",
        "progress": "LTX 2/4 encode",
        "elapsed_s": 600.0,
        "ltx": {
            "stage_index": 2,
            "stage_total": 4,
            "stage_name": "encode",
            "console": "LTX 2/4 encode: 52%",
            "params": {"preset": "5070Ti-16"},
            "timing": {"ltx": 300.0},
        },
        "energy": {"wh": 12.0, "gpu_wh": 10.0, "seconds": 600.0, "avg_w": 72.0},
        "power": {"w": 210.0},
        "uses_gpu": True,
    }
    value.update(kw)
    return value


def fleet_with(worker_id: str, current: dict | None, transfers=()) -> FleetLive:
    fleet = FleetLive(NOW - timedelta(hours=1), VERSION)
    fleet.observe(
        worker_id,
        Heartbeat.model_validate(
            beat(identity(), current=current, transfers=list(transfers))
        ),
        NOW,
    )
    return fleet


def test_a_remote_restore_shows_its_ltx_params_timing_machine_and_start():
    row_film = film({**done_through("intake"), "restore": "running"})
    worker = identity()
    fleet = fleet_with(worker, remote(row_film.film_id))
    currents = fleet.currents(NOW, {worker: "5070 Ti"})
    (row,) = board.film_rows((row_film,), {}, currents)
    step = next(s for s in row["steps"] if s["key"] == "restore")
    assert step["state"] == "running" and step["progress"] == "LTX 2/4 encode"
    assert step["ltx"]["stage_name"] == "encode"
    assert step["params"] == {"preset": "5070Ti-16"}
    assert step["timing"] == {"ltx": 300.0}
    assert step["console"] == "LTX 2/4 encode: 52%"
    assert step["machine"] == {"id": worker, "name": "5070 Ti"}
    assert step["started_at"] == (NOW - timedelta(seconds=600)).isoformat()  # C14
    assert step["attempt_id"] == currents[row_film.film_id]["attempt_id"]
    assert row["live_power"] == {"w": 210.0}
    assert row["energy"]["running"] is True


def test_an_upload_and_a_download_are_the_step_note():
    uploading = film({**done_through("intake"), "restore": "running"})
    fetching = film({**done_through("intake")})
    worker = identity()
    transfers = [
        {
            "film": uploading.film_id,
            "dir": "up",
            "done": 12_400_000_000,
            "total": 38_000_000_000,
            "rate": 610e6,
        },
        {
            "film": fetching.film_id,
            "dir": "down",
            "done": 1_000_000_000,
            "total": 4_000_000_000,
            "rate": 0.0,
        },
    ]
    fleet = fleet_with(
        worker, remote(uploading.film_id, phase="uploading"), transfers=transfers
    )
    rows = board.film_rows(
        (uploading, fetching), {}, fleet.currents(NOW, {worker: "w1"})
    )
    up = next(s for s in rows[0]["steps"] if s["key"] == "restore")
    assert up["note"] == "回传 12.4/38.0 GB · 610 MB/s"
    down = next(s for s in rows[1]["steps"] if s["key"] == "restore")
    assert down["state"] == "waiting" and down["note"] == "下载源文件 1.0/4.0 GB"
    assert rows[1]["note"] == "下载源文件 1.0/4.0 GB"


def test_a_lost_workers_current_is_not_shown():
    row_film = film({**done_through("intake"), "restore": "running"})
    worker = identity()
    fleet = fleet_with(worker, remote(row_film.film_id))
    assert fleet.currents(NOW + timedelta(seconds=61), {}) == {}


def test_the_local_current_still_drives_its_film():
    row_film = film({**done_through("restore"), "asr": "running"})
    local = {"film_id": row_film.film_id, "stage": "asr", "progress": "50%"}
    (row,) = board.film_rows((row_film,), {}, {row_film.film_id: local})
    step = next(s for s in row["steps"] if s["key"] == "asr")
    assert step["progress"] == "50%" and "machine" not in step


# ── R16: /status.workflow_worker ────────────────────────────────────────
def test_the_worker_summary_has_no_secret(tmp_path):
    from taskpaw_v3.tests.test_workflow_remote_worker import (
        FakeController,
        loop_worker,
        until,
    )

    fake = FakeController()
    worker = loop_worker(tmp_path, fake)
    try:
        assert worker.summary()["connected"] is False
        worker.start()
        until(lambda: worker.reason == "在线")
        summary = worker.summary()
        assert set(summary) == {
            "state",
            "reason",
            "connected",
            "routes",
            "current",
            "paused",
            "busy",  # P2-4 R9
        }
        assert summary["busy"] is False
        assert summary["connected"] is True and summary["routes"] == 0
        assert summary["current"] is None and summary["paused"] is False
        assert "s" * 43 not in str(summary)
    finally:
        assert worker.stop()


def test_a_restarted_worker_with_attempt_files_is_busy_before_any_controller(
    tmp_path,
):
    """§9 CX1: no controller reachable after a restart, so no dispatcher was
    built; an attempt file recovery has not settled (its children may live)
    keeps `busy` true — 判定已停止 must not read the machine as quiet."""
    from taskpaw_v3.tests.test_workflow_remote_worker import loop_worker, until
    from taskpaw_v3.workflow.remote_dispatch import ATTEMPTS_DIR
    from taskpaw_v3.workflow.worker_client import ApiError

    class Unreachable:
        def __getattr__(self, _name):
            def call(*_a, **_k):
                raise ApiError(0, "unreachable")

            return call

    worker = loop_worker(tmp_path, lambda *_a, **_k: Unreachable())
    try:
        worker.start()
        until(lambda: "重试" in worker.reason)
        assert worker.dispatcher is None
        assert worker.summary()["busy"] is False  # nothing on disk
        attempts = tmp_path / "staging" / ATTEMPTS_DIR
        attempts.mkdir(parents=True)
        (attempts / f"{identity()}.json").write_text("{}", encoding="utf-8")
        assert worker.summary()["busy"] is True
    finally:
        assert worker.stop()


def test_an_unreadable_attempts_dir_is_busy_and_a_missing_one_is_idle(
    tmp_path, monkeypatch
):
    """§9 CX10: `Path.glob` swallows PermissionError; only a missing folder
    means no attempts, any other read error means unknown, which is busy."""
    import os

    from taskpaw_v3.workflow import remote_dispatch
    from taskpaw_v3.workflow.remote_dispatch import ATTEMPTS_DIR, attempts_on_disk

    assert attempts_on_disk(tmp_path) is False  # no folder yet
    attempts = tmp_path / ATTEMPTS_DIR
    attempts.mkdir()
    assert attempts_on_disk(tmp_path) is False  # empty
    (attempts / "a.json").write_text("{}", encoding="utf-8")
    assert attempts_on_disk(tmp_path) is True

    def denied(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "scandir", denied)
    assert remote_dispatch.attempts_on_disk(tmp_path) is True


def test_status_carries_the_worker_summary(monkeypatch, tmp_path):
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.tests.test_worker_api import _agent_config
    from taskpaw_v3.workflow import local_host, remote_worker

    captured = {}

    class Stub:
        dispatcher = None

        def summary(self):
            return {
                "state": "running",
                "reason": "在线",
                "connected": True,
                "routes": 1,
                "current": None,
                "paused": False,
            }

        def stop(self, timeout=30.0):
            return True

    monkeypatch.setattr(remote_worker, "start_worker_host", lambda s, d: Stub())
    monkeypatch.setattr(local_host, "start_local_workflow", lambda *a, **k: None)
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    from taskpaw_v3.agent.server import app

    real = app.create_control_app

    def spy(config, **kw):
        captured["status"] = kw["status_provider"]
        return real(config, **kw)

    monkeypatch.setattr(app, "create_control_app", spy)
    config = _agent_config(enabled=True, role="worker", remote_workers=True)
    shutdown = launcher.run_agent(
        config, config_path=tmp_path / "agent.yaml", block=False
    )
    try:
        from taskpaw_v3.tests.test_workflow_remote_worker import until

        until(lambda: "workflow_worker" in captured["status"]())
        assert captured["status"]()["workflow_worker"]["routes"] == 1
    finally:
        shutdown.shutdown()
        assert shutdown.stopped.wait(30)


def test_cx12_the_start_time_is_fixed_at_ingest_not_at_each_read():
    row_film = film({**done_through("intake"), "restore": "running"})
    worker = identity()
    fleet = fleet_with(worker, remote(row_film.film_id))  # beat at NOW
    first = fleet.currents(NOW, {})[row_film.film_id]["started_at"]
    later = fleet.currents(NOW + timedelta(seconds=30), {})[row_film.film_id]
    assert later["started_at"] == first
    assert first == (NOW - timedelta(seconds=600)).isoformat()
