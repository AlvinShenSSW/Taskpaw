"""Each machine's online / 停领 / offline spans on the controller (EPIC #387 C1).

`analytics.db` sits beside `workflow.db`, which keeps schema v4 (design §6.1):
one connection behind one lock, the file checked read-only before anything is
written, a foreign, newer or altered file refused untouched with a stable
reason. One daemon thread ticks every 30 s on a monotonic schedule (§6.2): one
`list_workers` read, then one transaction that extends, closes or opens a span
per lane. Losing the last tick on power loss is acceptable for this log.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .schema import BUSY_TIMEOUT_MS, SchemaError, _connect, _normalized_sql
from .schema import _path as local_database_path

if TYPE_CHECKING:
    from .fleet_live import FleetLive
    from .local_dispatch import LocalDispatcher
    from .local_runtime import LocalRuntime
    from .models import WorkerRecord

log = logging.getLogger("taskpaw.workflow.device_history")

APPLICATION_ID = 0x54504153  # TPAS
SCHEMA_VERSION = 1
TICK_SECONDS = 30.0
# a tick later than this by either clock: the process was suspended, the
# machine slept or the wall clock jumped; the stall reads as a gap (§6.2)
GAP_SECONDS = 3 * 30.0
STOP_SECONDS = 5.0
LOG_EVERY_SECONDS = 3600.0  # one log line per exception type per hour

# The stable reasons a refused or unusable file leaves the recorder off with.
REFUSED_PATH = "设备记录文件必须在本机磁盘上"
REFUSED_FOREIGN = "设备记录文件 analytics.db 不是本程序的文件"
REFUSED_VERSION = "设备记录文件 analytics.db 的版本比本程序新或无法识别"
REFUSED_SCHEMA = "设备记录文件 analytics.db 的表结构与本程序不符"
REFUSED_DAMAGED = "设备记录文件 analytics.db 已损坏或无法读取"
UNAVAILABLE = "设备记录文件 analytics.db 无法打开"

STATES = ("online", "paused", "offline")
REASONS = ("", "admission", "worker", "global", "version")
Lane = tuple[str, str]  # (state, reason)
UNKNOWN: Lane = ("unknown", "")  # 等待心跳: nothing is written
OFF: Lane = ("off", "")  # not recorded; an open span is closed

# v1, exactly as `sqlite_master` holds it (compared whitespace-normalized).
# Times are UTC `YYYY-MM-DDTHH:MM:SSZ` on the controller's clock; `last_seen_at`
# is the latest tick that confirmed the span's state.
_DDL = (
    """CREATE TABLE device_spans (
        span_id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('online','paused','offline')),
        reason TEXT NOT NULL DEFAULT ''
            CHECK(reason IN ('','admission','worker','global','version')),
        started_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0,1))
    )""",
    "CREATE UNIQUE INDEX one_open_span_per_worker"
    " ON device_spans(worker_id) WHERE closed=0",
    "CREATE INDEX device_spans_seen ON device_spans(last_seen_at)",
)


class Refused(Exception):
    """The file is not one this build may write; `reason` is stable."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Span:
    worker_id: str
    state: str
    reason: str
    start: str
    end: str  # the last tick that confirmed it
    open: bool


def stamp(value: datetime) -> str:
    """Fixed-width UTC seconds, so stored times compare as text."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lane(
    worker: WorkerRecord,
    *,
    controller_id: str,
    dispatcher_paused: bool,
    fleet: FleetLive | None,
    now: datetime,
) -> Lane:
    """One machine's state now, first match wins (§6.2); shared with the
    analytics route's 「当前状态」. `fleet` is None while remote workers are off."""
    if worker.worker_id == controller_id:
        return ("paused", "global") if dispatcher_paused else ("online", "")
    if (
        fleet is None
        or worker.enrollment_state != "verified"
        or worker.admission_state == "revoked"
    ):
        return OFF
    live = fleet.state(worker.worker_id, now)
    if live == "waiting":
        return UNKNOWN
    if live == "lost":
        return ("offline", "")
    if worker.admission_state in ("draining", "disabled"):
        return ("paused", "admission")
    entry = fleet.entry(worker.worker_id)
    if entry is None:  # forgotten between the two reads (a revocation)
        return UNKNOWN
    if entry.beat.paused:
        return ("paused", "worker")
    if dispatcher_paused:
        return ("paused", "global")
    if not fleet.version_ok(entry.beat):
        return ("paused", "version")
    return ("online", "")


def _layout(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    rows = conn.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return {(row[0], row[1]): _normalized_sql(row[2] or "") for row in rows}


def _expected() -> dict[tuple[str, str], str]:
    """`_layout` of a file holding exactly `_DDL`."""
    mem = sqlite3.connect(":memory:")
    try:
        for sql in _DDL:
            mem.execute(sql)
        return _layout(mem)
    finally:
        mem.close()


def _check(conn: sqlite3.Connection) -> bool:
    """True for an empty file to create, False for v1; Refused otherwise."""
    try:
        app = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        layout = _layout(conn)
    except sqlite3.DatabaseError as exc:
        raise Refused(REFUSED_DAMAGED) from exc
    if app == 0 and version == 0 and not layout:
        return True
    if app != APPLICATION_ID:
        raise Refused(REFUSED_FOREIGN)
    if version != SCHEMA_VERSION:
        raise Refused(REFUSED_VERSION)
    if layout != _expected():
        raise Refused(REFUSED_SCHEMA)
    return False


def open_database(path: Path) -> sqlite3.Connection:
    """Create v1 in one transaction, or open v1; Refused leaves the file as
    it was (the check runs read-only, before any write or PRAGMA change)."""
    try:
        path = local_database_path(path)
    except SchemaError as exc:
        raise Refused(REFUSED_PATH) from exc
    if path.is_file() and path.stat().st_size > 0:
        preflight = _connect(path, "ro")
        try:
            _check(preflight)
        finally:
            preflight.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path, "rwc")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _check(conn):  # again under the write lock
                for sql in _DDL:
                    conn.execute(sql)
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


def _insert(conn: sqlite3.Connection, worker_id: str, value: Lane, now: str) -> None:
    conn.execute(
        "INSERT INTO device_spans(worker_id,state,reason,started_at,last_seen_at)"
        " VALUES (?,?,?,?,?)",
        (worker_id, value[0], value[1], now, now),
    )


def _write(
    conn: sqlite3.Connection, lanes: dict[str, Lane], now: str, gap: bool
) -> None:
    """One tick (§6.2), inside the caller's transaction."""
    if gap:  # the time since the last confirmed tick is a gap, not a state
        conn.execute("UPDATE device_spans SET closed=1 WHERE closed=0")
    open_: dict[str, tuple[int, Lane]] = {
        row[1]: (row[0], (row[2], row[3]))
        for row in conn.execute(
            "SELECT span_id,worker_id,state,reason FROM device_spans WHERE closed=0"
        )
    }
    for worker_id in sorted(lanes.keys() | open_.keys()):
        value = lanes.get(worker_id, OFF)
        if value == UNKNOWN:
            continue
        current = open_.get(worker_id)
        if current is not None and current[1] == value:
            conn.execute(
                "UPDATE device_spans SET last_seen_at=? WHERE span_id=?",
                (now, current[0]),
            )
            continue
        if current is not None:
            conn.execute(
                "UPDATE device_spans SET closed=1,last_seen_at=? WHERE span_id=?",
                (now, current[0]),
            )
        if value[0] in STATES:
            _insert(conn, worker_id, value, now)


class DeviceHistory:
    """The recorder and its reads; every method is safe from any thread."""

    def __init__(
        self,
        path: Path,
        rt: LocalRuntime,
        dispatcher: LocalDispatcher,
        fleet: FleetLive | None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`fleet`: None while remote workers are off."""
        self.path = path
        self._rt, self._dispatcher, self._fleet = rt, dispatcher, fleet
        self._monotonic = monotonic
        self.reason = ""
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # the last tick that wrote: (monotonic, wall); None closes open spans
        self._last: tuple[float, datetime] | None = None
        self._logged: dict[str, float] = {}

    @property
    def available(self) -> bool:
        return self._conn is not None

    def open(self) -> bool:
        """False, with `reason`, when the file is refused or unusable."""
        try:
            self._conn = open_database(self.path)
        except Refused as exc:
            log.warning("device history off: %s", exc.reason)
            self.reason = exc.reason
        except Exception:
            log.exception("device history off: analytics.db unusable")
            self.reason = UNAVAILABLE
        return self._conn is not None

    def start(self) -> None:
        if not self.open():
            return
        self._thread = threading.Thread(
            target=self._run, name="workflow-device-history", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_SECONDS) -> bool:
        """Join the thread (at most 5 s) and close; False while a tick still
        runs, which keeps the connection (and the runtime) in use."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(min(timeout, STOP_SECONDS))
            if thread.is_alive():
                return False
            self._thread = None
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        return True

    def _run(self) -> None:
        due = self._monotonic()
        while not self._stop.is_set():
            self.tick()
            due += TICK_SECONDS
            now = self._monotonic()
            if due <= now:  # fell behind (a stall): restart the schedule
                due = now + TICK_SECONDS
            self._stop.wait(due - now)

    def tick(self) -> bool:
        """One tick; any Exception is logged (once per type per hour) and the
        next tick retries."""
        if self._conn is None:
            return False
        try:
            self._tick()
        except Exception as exc:
            name = type(exc).__name__
            mono = self._monotonic()
            last = self._logged.get(name)
            if last is None or mono - last >= LOG_EVERY_SECONDS:
                self._logged[name] = mono
                log.warning("device history tick failed: %s", name, exc_info=True)
            return False
        return True

    def _tick(self) -> None:
        rt = self._rt
        mono, now = self._monotonic(), rt.clock.now()
        workers = rt.store.list_workers(rt.operator)
        paused = bool(self._dispatcher.paused)
        lanes = {
            w.worker_id: lane(
                w,
                controller_id=rt.identity.worker_id,
                dispatcher_paused=paused,
                fleet=self._fleet,
                now=now,
            )
            for w in workers
        }
        last = self._last
        gap = (
            last is None
            or mono - last[0] > GAP_SECONDS
            or (now - last[1]).total_seconds() > GAP_SECONDS
        )
        with self._lock:
            conn = self._conn
            if conn is None:  # stopped meanwhile
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                _write(conn, lanes, stamp(now), gap)
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        self._last = (mono, now)

    # ── reads (the analytics route, C2) ────────────────────────────────
    def _read(self, sql: str, args: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            if self._conn is None:
                return []
            return self._conn.execute(sql, tuple(args)).fetchall()

    def spans(self, since: datetime | None) -> list[Span]:
        """Spans overlapping [since, now], oldest first; all without `since`.
        An open span ends at its last confirmed tick."""
        rows = self._read(
            "SELECT worker_id,state,reason,started_at,last_seen_at,closed"
            " FROM device_spans WHERE last_seen_at>=? ORDER BY started_at,span_id",
            ("" if since is None else stamp(since),),
        )
        return [Span(r[0], r[1], r[2], r[3], r[4], not r[5]) for r in rows]

    def coverage_start(self) -> str | None:
        """The earliest span's start (数据记录起点), None before any."""
        rows = self._read("SELECT MIN(started_at) FROM device_spans")
        return rows[0][0] if rows else None
