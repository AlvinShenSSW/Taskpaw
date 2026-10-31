"""Device availability history in `analytics.db` (EPIC #387 C1, design §6.1, §6.2).

Every test uses its own temp data dir and a fake runtime (clock, store,
dispatcher); `FleetLive` and `Heartbeat` are the real ones.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.tests.test_workflow_fleet_live import beat
from taskpaw_v3.workflow import device_history as dh
from taskpaw_v3.workflow.device_history import OFF, UNKNOWN, DeviceHistory, lane
from taskpaw_v3.workflow.fleet_live import PROTOCOL, FleetLive

START = datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)
CTRL = "00000000-0000-4000-8000-0000000000a0"  # sorts first
GPU = "00000000-0000-4000-8000-0000000000a1"
GPU2 = "00000000-0000-4000-8000-0000000000a2"


def worker(worker_id: str, enrollment: str = "verified", admission: str = "enabled"):
    return SimpleNamespace(
        worker_id=worker_id, enrollment_state=enrollment, admission_state=admission
    )


class World:
    """A fake controller: its clock, store, dispatcher, fleet and monotonic time."""

    def __init__(self, tmp_path: Path, *, remote: bool = True) -> None:
        self.wall = START
        self.mono = 1000.0
        self.workers = [worker(CTRL), worker(GPU)]
        self.fail: BaseException | None = None
        self.block: threading.Event | None = None
        self.calls = 0
        self.rt = SimpleNamespace(
            clock=SimpleNamespace(now=lambda: self.wall),
            store=SimpleNamespace(list_workers=self._list),
            operator=object(),
            identity=SimpleNamespace(worker_id=CTRL),
        )
        self.dispatcher = SimpleNamespace(paused=False)
        # started a minute before START: no 等待心跳 grace unless a test asks
        self.fleet = (
            FleetLive(START - timedelta(minutes=5), "9.9.9") if remote else None
        )
        self.path = tmp_path / "analytics.db"

    def _list(self, auth):
        self.calls += 1
        if self.block is not None:
            self.block.wait(30)
        if self.fail is not None:
            raise self.fail
        return tuple(self.workers)

    def history(self) -> DeviceHistory:
        return DeviceHistory(
            self.path, self.rt, self.dispatcher, self.fleet, monotonic=lambda: self.mono
        )

    def beat(self, worker_id: str = GPU, **kw) -> None:
        assert self.fleet is not None
        self.fleet.observe(worker_id, beat(**kw), self.wall)

    def advance(self, seconds: float, wall: float | None = None) -> None:
        self.mono += seconds
        self.wall += timedelta(seconds=seconds if wall is None else wall)


def rows(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT worker_id,state,reason,started_at,last_seen_at,closed"
            " FROM device_spans ORDER BY span_id"
        ).fetchall()
    finally:
        conn.close()


def at(seconds: float = 0) -> str:
    return dh.stamp(START + timedelta(seconds=seconds))


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def opened(world):
    history = world.history()
    assert history.open(), history.reason
    try:
        yield world, history
    finally:
        history.stop()


# ── §6.1 open rules (AC1, AC2) ────────────────────────────────────────────
EXPECTED = {
    ("table", "device_spans"),
    ("index", "one_open_span_per_worker"),
    ("index", "device_spans_seen"),
}


def test_a_new_file_gets_the_exact_schema_and_both_pragmas(world):
    history = world.history()
    assert history.open() and history.available and history.reason == ""
    history.stop()
    conn = sqlite3.connect(world.path)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0x54504153
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        found = conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    assert {(t, n) for t, n, _ in found} == EXPECTED
    assert {" ".join(sql.split()) for *_, sql in found} == {
        " ".join(sql.split()) for sql in dh._DDL
    }
    again = world.history()  # the same file opens again unchanged
    assert again.open(), again.reason
    again.stop()


def test_creation_is_one_transaction(world, monkeypatch):
    """A failure inside the creation leaves no table and no version marker."""
    monkeypatch.setattr(dh, "_DDL", (*dh._DDL, "CREATE TABLE broken ("))
    history = world.history()
    assert not history.open()
    assert history.reason == dh.UNAVAILABLE
    conn = sqlite3.connect(world.path)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    finally:
        conn.close()


def _foreign(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "PRAGMA application_id=42; PRAGMA user_version=1;"
        " CREATE TABLE device_spans (x INTEGER);"
    )
    conn.close()


def _version(value: int):
    def make(path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            f"PRAGMA application_id={0x54504153}; PRAGMA user_version={value};"
        )
        for sql in dh._DDL:
            conn.execute(sql)
        conn.commit()
        conn.close()

    return make


def _mismatched(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        f"PRAGMA application_id={0x54504153}; PRAGMA user_version=1;"
        " CREATE TABLE device_spans (span_id INTEGER PRIMARY KEY, worker_id TEXT);"
    )
    conn.close()


def _other_app(path: Path) -> None:
    """No application id or version, but somebody else's tables."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE notes (body TEXT); INSERT INTO notes VALUES ('x');"
    )
    conn.close()


def _damaged(path: Path) -> None:
    path.write_bytes(b"this is not a database file at all" * 200)


def _damaged_page(path: Path) -> None:
    """A valid header and schema over a damaged data page (Codex r1)."""
    conn = dh.open_database(path)
    conn.executemany(
        "INSERT INTO device_spans(worker_id,state,reason,started_at,last_seen_at,closed)"
        " VALUES (?,'online','','2026-10-31T00:00:00Z','2026-10-31T00:00:30Z',1)",
        [(f"worker-{i:04d}-" + "x" * 40,) for i in range(400)],
    )
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    data = bytearray(path.read_bytes())
    size = int.from_bytes(data[16:18], "big")
    pages = len(data) // size
    data[(pages - 1) * size : pages * size] = bytes([0xFF]) * size
    path.write_bytes(bytes(data))
    probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:  # the header and schema still read fine: only a full check sees it
        assert probe.execute("PRAGMA application_id").fetchone()[0] == dh.APPLICATION_ID
        assert dh._layout(probe) == dh._expected()
    finally:
        probe.close()


@pytest.mark.parametrize(
    "make, reason",
    [
        (_foreign, dh.REFUSED_FOREIGN),
        (_other_app, dh.REFUSED_FOREIGN),
        (_version(2), dh.REFUSED_VERSION),
        (_version(0), dh.REFUSED_VERSION),
        (_mismatched, dh.REFUSED_SCHEMA),
        (_damaged, dh.REFUSED_DAMAGED),
        (_damaged_page, dh.REFUSED_DAMAGED),
    ],
    ids=["foreign-id", "other-app", "newer", "unknown", "ddl", "damaged", "page"],
)
def test_a_foreign_newer_or_mismatched_file_is_refused_untouched(world, make, reason):
    make(world.path)
    before = world.path.read_bytes()
    history = world.history()
    history.start()
    try:
        assert not history.available
        assert history.reason == reason
        assert history._thread is None  # the recorder stays off
        assert history.spans(None) == [] and history.coverage_start() is None
        assert history.tick() is False and world.calls == 0
    finally:
        assert history.stop() is True  # a stop with the recorder off: no-op
    assert world.path.read_bytes() == before
    assert not world.path.with_name("analytics.db-wal").exists()


def test_reasons_are_stable_strings():
    reasons = (
        dh.REFUSED_FOREIGN,
        dh.REFUSED_VERSION,
        dh.REFUSED_SCHEMA,
        dh.REFUSED_DAMAGED,
        dh.REFUSED_PATH,
        dh.UNAVAILABLE,
    )
    assert len(set(reasons)) == len(reasons)
    assert all(isinstance(r, str) and r for r in reasons)


def test_a_network_path_is_refused(world):
    history = DeviceHistory(
        Path("//nas/share/analytics.db"), world.rt, world.dispatcher, world.fleet
    )
    assert not history.open()
    assert history.reason == dh.REFUSED_PATH


# ── §6.2 lane derivation, row by row (AC4) ────────────────────────────────
def _lane(world, w, **kw):
    return lane(
        w,
        controller_id=CTRL,
        dispatcher_paused=kw.pop("paused", False),
        fleet=kw.pop("fleet", world.fleet),
        now=world.wall,
    )


def test_the_controller_lane(world):
    assert _lane(world, worker(CTRL)) == ("online", "")
    assert _lane(world, worker(CTRL), paused=True) == ("paused", "global")
    # the controller lane is decided first: no fleet, no beat needed
    assert _lane(world, worker(CTRL), fleet=None) == ("online", "")


@pytest.mark.parametrize(
    "enrollment, admission",
    [
        ("verified", "revoked"),
        ("retired", "revoked"),
        ("retired", "draining"),
        ("pending_validation", "disabled"),
        ("pending_pairing", "disabled"),
    ],
)
def test_revoked_retired_or_unverified_workers_are_not_recorded(
    world, enrollment, admission
):
    world.beat()  # even with a fresh heartbeat
    assert _lane(world, worker(GPU, enrollment, admission)) == OFF


def test_remote_workers_off_records_no_gpu_lane(world):
    world.beat()
    assert _lane(world, worker(GPU), fleet=None) == OFF


def test_waiting_is_unknown_and_lost_is_offline(world):
    fresh = FleetLive(world.wall, "9.9.9")  # the controller just started
    assert _lane(world, worker(GPU), fleet=fresh) == UNKNOWN
    world.advance(60)
    assert _lane(world, worker(GPU), fleet=fresh) == ("offline", "")
    world.beat()
    world.advance(60)  # 60 s without a heartbeat: 失联
    assert _lane(world, worker(GPU)) == ("offline", "")


@pytest.mark.parametrize(
    "setup, expected",
    [
        (dict(admission="draining"), ("paused", "admission")),
        (dict(admission="disabled"), ("paused", "admission")),
        (dict(beat_paused=True), ("paused", "worker")),
        (dict(paused=True), ("paused", "global")),
        (dict(protocol=PROTOCOL + 1), ("paused", "version")),
        ({}, ("online", "")),
        # first match wins
        (
            dict(admission="draining", beat_paused=True, paused=True),
            ("paused", "admission"),
        ),
        (dict(beat_paused=True, paused=True, protocol=0), ("paused", "worker")),
        (dict(paused=True, protocol=0), ("paused", "global")),
    ],
)
def test_gpu_lane_rows_in_order(world, setup, expected):
    world.beat(
        paused=setup.get("beat_paused", False), protocol=setup.get("protocol", PROTOCOL)
    )
    w = worker(GPU, admission=setup.get("admission", "enabled"))
    assert _lane(world, w, paused=setup.get("paused", False)) == expected


def test_offline_wins_over_admission_and_pauses(world):
    world.beat(paused=True)
    world.advance(61)
    w = worker(GPU, admission="draining")
    assert _lane(world, w, paused=True) == ("offline", "")


def test_recorded_reasons_are_the_closed_set(world):
    assert set(dh.REASONS) == {"", "admission", "worker", "global", "version"}


# ── tick writes (AC5) ─────────────────────────────────────────────────────
def test_first_tick_opens_one_span_per_lane(opened):
    world, history = opened
    world.beat()
    assert history.tick() is True
    assert rows(world.path) == [
        (CTRL, "online", "", at(), at(), 0),
        (GPU, "online", "", at(), at(), 0),
    ]


def test_an_unchanged_state_extends_and_a_change_closes_and_opens(opened):
    world, history = opened
    world.beat()
    assert history.tick()
    world.advance(30)
    world.beat()
    assert history.tick()
    assert rows(world.path) == [
        (CTRL, "online", "", at(), at(30), 0),
        (GPU, "online", "", at(), at(30), 0),
    ]
    world.advance(30)
    world.beat(paused=True)
    assert history.tick()
    assert rows(world.path) == [
        (CTRL, "online", "", at(), at(60), 0),
        (GPU, "online", "", at(), at(60), 1),
        (GPU, "paused", "worker", at(60), at(60), 0),
    ]


def test_a_reason_change_alone_is_a_new_span(opened):
    world, history = opened
    world.beat(paused=True)
    assert history.tick()
    world.advance(30)
    world.beat()
    world.workers[1] = worker(GPU, admission="draining")
    assert history.tick()
    assert [r[1:3] + (r[5],) for r in rows(world.path) if r[0] == GPU] == [
        ("paused", "worker", 1),
        ("paused", "admission", 0),
    ]


@pytest.mark.parametrize(
    "enrollment, admission",
    [
        ("verified", "revoked"),
        ("retired", "revoked"),
        ("pending_validation", "enabled"),
    ],
)
def test_revoked_or_retired_closes_the_open_span(opened, enrollment, admission):
    world, history = opened
    world.beat()
    assert history.tick()
    world.advance(30)
    world.beat()
    world.workers[1] = worker(GPU, enrollment, admission)
    assert history.tick()
    assert [r for r in rows(world.path) if r[0] == GPU] == [
        (GPU, "online", "", at(), at(30), 1)
    ]
    world.advance(30)
    assert history.tick()  # stays unrecorded
    assert len([r for r in rows(world.path) if r[0] == GPU]) == 1


def test_a_worker_gone_from_the_store_closes_its_span(opened):
    world, history = opened
    world.beat()
    assert history.tick()
    world.advance(30)
    world.workers.pop()
    assert history.tick()
    assert [r[5] for r in rows(world.path) if r[0] == GPU] == [1]


def test_remote_workers_off_records_only_the_controller(tmp_path):
    world = World(tmp_path, remote=False)
    history = world.history()
    assert history.open()
    try:
        assert history.tick()
        assert [r[0] for r in rows(world.path)] == [CTRL]
    finally:
        history.stop()


def test_gpu_lanes_global_and_version(opened):
    world, history = opened
    world.workers.append(worker(GPU2))
    world.beat(GPU)
    world.beat(GPU2, protocol=PROTOCOL + 1)
    world.dispatcher.paused = True
    assert history.tick()
    assert {r[0]: r[1:3] for r in rows(world.path)} == {
        CTRL: ("paused", "global"),
        GPU: ("paused", "global"),
        GPU2: ("paused", "global"),
    }
    world.advance(30)
    world.beat(GPU)
    world.beat(GPU2, protocol=PROTOCOL + 1)
    world.dispatcher.paused = False
    assert history.tick()
    assert {r[0]: r[1:3] for r in rows(world.path) if not r[5]} == {
        CTRL: ("online", ""),
        GPU: ("online", ""),
        GPU2: ("paused", "version"),
    }


def test_waiting_writes_nothing(opened):
    world, history = opened
    world.beat()
    assert history.tick()
    before = rows(world.path)
    history._fleet = FleetLive(world.wall, "9.9.9")  # back in the 60 s grace
    world.advance(30)
    assert history.tick()
    after = rows(world.path)
    assert [r for r in after if r[0] == GPU] == [r for r in before if r[0] == GPU]
    assert [r for r in after if r[0] == CTRL] == [(CTRL, "online", "", at(), at(30), 0)]


def test_one_open_span_per_worker_is_enforced_by_the_index(opened):
    world, history = opened
    assert history.tick()
    conn = sqlite3.connect(world.path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO device_spans(worker_id,state,started_at,last_seen_at)"
                " VALUES (?,?,?,?)",
                (CTRL, "online", at(), at()),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO device_spans(worker_id,state,reason,started_at,last_seen_at)"
                " VALUES (?,?,?,?,?)",
                (GPU2, "online", "bogus", at(), at()),
            )
    finally:
        conn.close()


def test_a_tick_is_one_transaction(opened, monkeypatch):
    """A failure after the first write of a tick leaves the file as it was."""
    world, history = opened
    world.beat()
    assert history.tick()
    before = rows(world.path)
    world.advance(30)
    world.beat(paused=True)
    real = dh._insert

    def explode(conn, *a):
        real(conn, *a)
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dh, "_insert", explode)
    assert history.tick() is False
    assert rows(world.path) == before


# ── restart and stalls (AC6) ──────────────────────────────────────────────
def test_restart_closes_open_spans_without_moving_last_seen(opened):
    world, history = opened
    world.beat()
    assert history.tick()
    world.advance(30)
    world.beat()
    assert history.tick()
    history.stop()
    world.advance(600)  # the controller was down for ten minutes
    world.beat()
    again = world.history()
    assert again.open()
    try:
        assert again.tick()
    finally:
        again.stop()
    assert rows(world.path) == [
        (CTRL, "online", "", at(), at(30), 1),
        (GPU, "online", "", at(), at(30), 1),
        (CTRL, "online", "", at(630), at(630), 0),
        (GPU, "online", "", at(630), at(630), 0),
    ]


def test_restart_during_the_grace_closes_and_writes_only_the_controller(opened):
    world, history = opened
    world.beat()
    assert history.tick()
    history.stop()
    world.advance(300)
    world.fleet = FleetLive(world.wall, "9.9.9")  # new process: 等待心跳
    again = world.history()
    assert again.open()
    try:
        assert again.tick()
    finally:
        again.stop()
    assert rows(world.path) == [
        (CTRL, "online", "", at(), at(), 1),
        (GPU, "online", "", at(), at(), 1),
        (CTRL, "online", "", at(300), at(300), 0),
    ]


@pytest.mark.parametrize(
    "mono, wall, gap",
    [
        (91, 91, True),
        (91, 30, True),  # the process was suspended: the monotonic delta
        (30, 91, True),  # the wall clock jumped: the rt.clock delta
        (89, 89, False),
        (90, 90, False),
    ],
)
def test_a_late_tick_closes_spans_as_a_gap(opened, mono, wall, gap):
    world, history = opened
    world.beat()
    assert history.tick()
    world.advance(mono, wall=wall)
    world.beat()
    assert history.tick()
    end = at(wall)
    if gap:
        assert rows(world.path) == [
            (CTRL, "online", "", at(), at(), 1),
            (GPU, "online", "", at(), at(), 1),
            (CTRL, "online", "", end, end, 0),
            (GPU, "online", "", end, end, 0),
        ]
    else:
        assert rows(world.path) == [
            (CTRL, "online", "", at(), end, 0),
            (GPU, "online", "", at(), end, 0),
        ]


def test_a_failed_first_tick_still_closes_the_old_spans_later(opened):
    world, history = opened
    world.beat()
    assert history.tick()
    history.stop()
    world.advance(30)
    again = world.history()
    assert again.open()
    try:
        world.fail = RuntimeError("store busy")
        assert again.tick() is False
        world.fail = None
        world.advance(30)
        world.beat()
        assert again.tick()
    finally:
        again.stop()
    assert [r[5] for r in rows(world.path)] == [1, 1, 0, 0]
    assert rows(world.path)[0][4] == at()  # last_seen_at kept


# ── failures (AC7) ────────────────────────────────────────────────────────
def _logged(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "taskpaw.workflow.device_history"
    ]


def test_tick_failures_are_logged_once_per_type_per_hour(opened, caplog):
    world, history = opened
    caplog.set_level(logging.WARNING, logger="taskpaw.workflow.device_history")
    world.fail = RuntimeError("store")
    assert history.tick() is False
    world.advance(30)
    assert history.tick() is False
    assert len(_logged(caplog)) == 1 and "RuntimeError" in _logged(caplog)[0]
    world.fail = ValueError("odd state")
    world.advance(30)
    assert history.tick() is False
    assert len(_logged(caplog)) == 2 and "ValueError" in _logged(caplog)[1]
    world.fail = RuntimeError("store")
    world.advance(3600)
    assert history.tick() is False
    assert len(_logged(caplog)) == 3
    world.fail = None
    world.advance(30)
    world.beat()
    assert history.tick() is True  # the next tick retries and writes
    assert {r[0] for r in rows(world.path) if not r[5]} == {CTRL, GPU}


def test_a_database_error_is_caught_and_the_next_tick_recovers(opened, caplog):
    world, history = opened
    caplog.set_level(logging.WARNING, logger="taskpaw.workflow.device_history")
    assert history.tick()
    other = sqlite3.connect(world.path)
    try:
        other.execute("ALTER TABLE device_spans RENAME TO moved")
        other.commit()
        world.advance(30)
        assert history.tick() is False
        assert "OperationalError" in _logged(caplog)[0]
        other.execute("ALTER TABLE moved RENAME TO device_spans")
        other.commit()
    finally:
        other.close()
    world.advance(30)
    assert history.tick() is True


def test_the_thread_survives_raising_ticks(world, monkeypatch):
    monkeypatch.setattr(dh, "TICK_SECONDS", 0.01)
    world.fail = RuntimeError("store")
    history = DeviceHistory(world.path, world.rt, world.dispatcher, world.fleet)
    history.start()
    try:
        deadline = time.monotonic() + 20
        while world.calls < 5:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert history._thread is not None and history._thread.is_alive()
        world.fail = None
        while not rows(world.path):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        assert history.stop() is True
    assert history._thread is None or not history._thread.is_alive()


def test_ticks_follow_a_monotonic_schedule(opened, monkeypatch):
    """Every 30 s from the first tick, whatever a tick costs; after a stall
    the schedule restarts from now instead of firing a burst of ticks."""
    world, history = opened
    costs = iter([2.0, 2.0, 100.0, 0.0])
    waits: list[float] = []

    def tick() -> bool:
        world.mono += next(costs)
        return True

    class Stop:
        def is_set(self) -> bool:
            return len(waits) == 4

        def wait(self, seconds: float) -> None:
            waits.append(seconds)
            world.mono += seconds

        def set(self) -> None:
            pass

    monkeypatch.setattr(history, "tick", tick)
    history._stop = Stop()  # type: ignore[assignment]
    history._run()
    assert waits == [28.0, 28.0, 30.0, 30.0]


def test_a_non_exception_base_exception_is_not_swallowed(opened):
    world, history = opened
    world.fail = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        history.tick()


# ── stop (AC8) ────────────────────────────────────────────────────────────
def test_stop_joins_the_thread_and_closes_the_database(world):
    history = world.history()
    history.start()
    thread = history._thread
    assert thread is not None and thread.is_alive()
    deadline = time.monotonic() + 20
    while not rows(world.path):  # the first tick runs at start
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert history.stop() is True
    assert not thread.is_alive() and not history.available
    assert history.spans(None) == [] and history.coverage_start() is None
    assert history.stop() is True  # idempotent


def test_stop_is_bounded_while_a_tick_is_stuck(world):
    world.block = threading.Event()
    history = world.history()
    history.start()
    deadline = time.monotonic() + 20
    while world.calls == 0:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    began = time.monotonic()
    assert history.stop(timeout=0.2) is False
    assert time.monotonic() - began < 5
    assert history.available  # never closed under a running tick
    world.block.set()
    assert history.stop() is True and not history.available


def test_stop_waits_at_most_five_seconds(world):
    joined: list[float] = []
    history = world.history()
    history._thread = SimpleNamespace(  # type: ignore[assignment]
        join=lambda timeout: joined.append(timeout), is_alive=lambda: True
    )
    assert history.stop(timeout=60) is False
    assert joined == [dh.STOP_SECONDS] and dh.STOP_SECONDS == 5.0


# ── reads (AC9) ───────────────────────────────────────────────────────────
def test_spans_since_returns_overlapping_spans_oldest_first(opened):
    world, history = opened
    assert history.coverage_start() is None and history.spans(None) == []
    world.beat()
    assert history.tick()  # CTRL, GPU online from 0
    world.advance(30)
    world.beat(paused=True)
    assert history.tick()  # GPU online closed at 30, paused from 30
    world.advance(600)  # a gap: everything closed, new spans at 630
    world.beat(paused=True)
    assert history.tick()
    world.advance(30)
    world.beat(paused=True)
    assert history.tick()

    every = history.spans(None)
    assert [(s.worker_id, s.state, s.start, s.end, s.open) for s in every] == [
        (CTRL, "online", at(), at(30), False),
        (GPU, "online", at(), at(30), False),
        (GPU, "paused", at(30), at(30), False),
        (CTRL, "online", at(630), at(660), True),
        (GPU, "paused", at(630), at(660), True),
    ]
    assert every[2].reason == "worker"
    assert history.spans(START + timedelta(seconds=30)) == every
    later = history.spans(START + timedelta(seconds=31))
    assert [(s.worker_id, s.start) for s in later] == [(CTRL, at(630)), (GPU, at(630))]
    # an open span ends at its last confirmed tick, not at "now"
    assert history.spans(START + timedelta(hours=1)) == []
    assert history.coverage_start() == at()


def test_spans_since_accepts_any_offset(opened):
    world, history = opened
    assert history.tick()
    local = (START - timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
    assert len(history.spans(local)) == 2
    assert history.spans(START + timedelta(seconds=1)) == []
