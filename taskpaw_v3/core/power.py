"""Per-machine power estimate: model, sampler, storage (PWR-1, #287).

Design: docs/specs/2026-10-22-power-estimation.md, v2 PW01-PW11. Every figure
is an estimate (估算); there is no billing claim and no electricity price.

- **Model.** Machine W = GPU + CPU + other. GPU is nvidia-smi `power.draw`
  (0 without a reading); CPU is `idle + (tdp − idle) × utilisation`, or 0 for
  `cpu_source: none`; other is a per-machine baseline. With a PSU efficiency
  the wall figure is `(GPU + CPU + other) / efficiency` and the loss is shown
  as part of "other" (the calibration helper's `其他基线 = 插座读数 × 效率 −
  GPU − CPU` inverts exactly this).
- **Sampler (PW01, PW09).** One agent-level thread samples every 5 s. GPU
  energy uses the trapezoid rule; CPU energy a rectangle over the window
  average (`psutil.cpu_percent(interval=None)` on this thread, which psutil
  tracks per thread). Time deltas are monotonic; a delta over 15 s (or a
  backwards one) is a gap and is never integrated — nor is the first window,
  whose CPU reading is dropped.
- **Attribution (PW03, PW04).** Each window belongs to exactly one activity
  (restore, asr, translate, idle, other) and one owner: a film id, `manual`
  or `none`. The GPU lease holder decides first; otherwise the workflow's
  running non-GPU stage; otherwise idle. No heuristics. Every owner has a
  cumulative in-memory counter: the dispatcher (PWR-2) takes `reading(film)`
  at claim and at resolve and keeps `energy_between(a, b)`.
- **Storage (PW07, PW11).** 15-minute buckets (Wh per activity plus GPU / CPU
  / other Wh and the covered seconds), keyed by UTC epoch, kept 35 days in
  `power.db` under the data dir (in memory without one). Only the sampler
  writes: one additive upsert per closed bucket, plus the open one at stop,
  so a restart keeps the counters and loses at most the open bucket on a
  crash. Reads use the in-memory window; today / week (from Monday) / month
  and history points are computed on read in local time.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from datetime import time as dtime
from pathlib import Path
from typing import Any, Callable, Optional

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core.config import PowerSettings

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

log = logging.getLogger("taskpaw.power")

ACTIVITIES: tuple[str, ...] = ("restore", "asr", "translate", "idle", "other")
MANUAL = "manual"  # a manual Jasna / avsubs task holds the GPU
NO_OWNER = "none"  # idle, or nothing attributable
SAMPLE_SECONDS = 5.0
GAP_SECONDS = 15.0
BUCKET_SECONDS = 900
RETENTION_SECONDS = 35 * 86400
# Retention is a count, never a wall-clock comparison: a bad RTC must not
# be able to expire real history.
MAX_BUCKETS = RETENTION_SECONDS // BUCKET_SECONDS
OWNER_IDLE_SECONDS = 48 * 3600  # an owner counter unused this long is dropped
PEAK_SAMPLES = 17280  # per owner: 24 h of 5 s samples for the peak W
# range → (points, seconds per point); at most 168 points per series
RANGES: dict[str, tuple[int, int]] = {
    "24h": (96, 900),
    "7d": (168, 3600),
    "30d": (120, 21600),
}
_DESKTOP = (65.0, 10.0, 35.0)  # tdp, idle, other (W)
_PLATFORM_DEFAULTS = {"darwin": (20.0, 3.0, 5.0)}
_MANUAL_ACTIVITY = {"jasna": "restore", "avsubs": "asr", "lada": "restore"}
BUSY_GPU_PCT = 30.0  # GPU utilisation with no lease holder: an untracked job
_GPU_STAGE_ACTIVITY = {"restore": "restore", "asr": "asr", "translate": "translate"}

RunId = gpu_lease.RunId
_SAMPLER_IDS = itertools.count(1)
Activity = tuple[str, str]  # (activity, owner)


# ── model ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PowerModel:
    tdp_w: float
    idle_w: float
    other_w: float
    psu_efficiency: Optional[float]
    cpu_source: str


@dataclass(frozen=True)
class Estimate:
    w: float
    gpu_w: Optional[float]  # None: no reading (no NVIDIA GPU, or [N/A])
    cpu_w: float
    other_w: float  # the baseline plus any PSU loss


def resolve_model(
    settings: PowerSettings, platform: Optional[str] = None
) -> PowerModel:
    """The settings with this platform's defaults filled in (PW09)."""
    tdp, idle, other = _PLATFORM_DEFAULTS.get(platform or sys.platform, _DESKTOP)
    tdp = settings.tdp_w if settings.tdp_w is not None else tdp
    idle = settings.idle_w if settings.idle_w is not None else idle
    other = settings.other_w if settings.other_w is not None else other
    return PowerModel(
        tdp_w=float(tdp),
        idle_w=float(min(idle, tdp)),  # never an inverted CPU curve
        other_w=float(other),
        psu_efficiency=settings.psu_efficiency,
        cpu_source=settings.cpu_source,
    )


def cpu_watts(model: PowerModel, cpu_pct: Optional[float]) -> float:
    if model.cpu_source == "none":
        return 0.0
    util = min(100.0, max(0.0, cpu_pct or 0.0)) / 100.0
    return model.idle_w + (model.tdp_w - model.idle_w) * util


def _wall_share(model: PowerModel, gpu: float, cpu: float, base: float) -> float:
    """The non-GPU/CPU share: the baseline plus the PSU loss of the whole."""
    eff = model.psu_efficiency
    total = (gpu + cpu + base) / eff if eff else gpu + cpu + base
    return total - gpu - cpu


def estimate(
    model: PowerModel, gpu_w: Optional[float], cpu_pct: Optional[float]
) -> Estimate:
    gpu = max(0.0, gpu_w) if gpu_w is not None else 0.0
    cpu = cpu_watts(model, cpu_pct)
    other = _wall_share(model, gpu, cpu, model.other_w)
    return Estimate(gpu + cpu + other, gpu_w, cpu, other)


# ── attribution (PW03) ──────────────────────────────────────────────────────
def classify(
    holder: Optional[RunId],
    workflow_run: Optional[RunId],
    current: Optional[tuple[str, str]],
    manual_type: Callable[[str], Optional[str]],
    gpu_util: Optional[float] = None,
) -> Activity:
    """(activity, owner) of this moment. The GPU lease holder first: the
    workflow's run maps its running stage, a manual task maps its monitor type
    (Jasna / Lada → restore, avsubs → asr). Otherwise the workflow's running
    non-GPU stage: translate, or other (return, scrape). Otherwise, a busy GPU
    (>= BUSY_GPU_PCT) nobody holds is an untracked job (e.g. Jasna started
    outside TaskPaw): other, never idle. Otherwise idle."""
    if holder is not None:
        if workflow_run is not None and holder == workflow_run:
            if current is None:
                return ("other", NO_OWNER)
            film_id, stage = current
            return (_GPU_STAGE_ACTIVITY.get(stage, "other"), film_id)
        try:
            kind = manual_type(holder[0])
        except Exception:
            kind = None
        return (_MANUAL_ACTIVITY.get(kind or "", "other"), MANUAL)
    if current is not None:
        film_id, stage = current
        return ("translate" if stage == "translate" else "other", film_id)
    if gpu_util is not None and gpu_util >= BUSY_GPU_PCT:
        return ("other", NO_OWNER)
    return ("idle", NO_OWNER)


def activity_source(
    manual_type: Callable[[str], Optional[str]],
    dispatcher: Callable[[], Any],
    gpu_util: Optional[Callable[[], Optional[float]]] = None,
) -> Callable[[], Activity]:
    """A reader for the sampler: `gpu_lease.holder()` plus the workflow
    dispatcher's `run_id` and `current_stage()` (None: no workflow) and the
    GPU utilisation (the shared GPU sample unless `gpu_util` is given)."""

    def read() -> Activity:
        holder = gpu_lease.holder()
        d = dispatcher()
        run = getattr(d, "run_id", None) if d is not None else None
        current = d.current_stage() if d is not None else None
        try:
            util = (gpu_util or _gpu_util_reader)()
        except Exception:
            util = None
        return classify(holder, run, current, manual_type, gpu_util=util)

    return read


# ── default readers ─────────────────────────────────────────────────────────
def read_gpu_power() -> Optional[float]:
    """Summed nvidia-smi `power.draw` (W) from the shared GPU sample."""
    from taskpaw_v3.core.gpu_sample import gpu_sample

    sample = gpu_sample()
    return None if sample is None else sample.get("power_w")


def read_gpu_util() -> Optional[float]:
    """GPU utilisation (%) from the shared GPU sample (None: no reading)."""
    from taskpaw_v3.core.gpu_sample import gpu_sample

    sample = gpu_sample()
    return None if sample is None else sample.get("util_pct")


def read_cpu_percent() -> Optional[float]:
    """CPU utilisation since this thread's previous call (never blocks)."""
    if psutil is None:
        return None
    return float(psutil.cpu_percent(interval=None))


# Indirection so the tests' autouse fixture can keep every sampler off the real
# GPU and CPU without hiding the readers themselves.
_gpu_reader: Callable[[], Optional[float]] = read_gpu_power
_cpu_reader: Callable[[], Optional[float]] = read_cpu_percent
_gpu_util_reader: Callable[[], Optional[float]] = read_gpu_util


# ── energy records ──────────────────────────────────────────────────────────
@dataclass
class _Bucket:
    start: int
    seconds: float = 0.0
    gpu: float = 0.0
    cpu: float = 0.0
    other: float = 0.0
    act: dict[str, float] = field(
        default_factory=lambda: dict.fromkeys(ACTIVITIES, 0.0)
    )

    @property
    def wh(self) -> float:
        return self.gpu + self.cpu + self.other

    def merge(self, b: "_Bucket") -> None:
        self.seconds += b.seconds
        self.gpu += b.gpu
        self.cpu += b.cpu
        self.other += b.other
        for k in ACTIVITIES:
            self.act[k] += b.act.get(k, 0.0)


@dataclass(frozen=True)
class OwnerReading:
    """A cumulative per-owner counter at one moment (in memory, this process)."""

    owner: str
    sampler: int  # the sampler (process lifetime) the reading came from
    epoch: int  # identifies the counter; readings compare only within one
    seq: int  # sample sequence number at the reading
    wh: float
    gpu_wh: float
    cpu_wh: float
    other_wh: float
    seconds: float


@dataclass(frozen=True)
class EnergyDelta:
    wh: float
    gpu_wh: float
    cpu_wh: float
    other_wh: float
    seconds: float
    avg_w: Optional[float]
    peak_w: Optional[float]

    def to_dict(self) -> dict[str, Optional[float]]:
        return {
            "wh": round(self.wh, 4),
            "gpu_wh": round(self.gpu_wh, 4),
            "cpu_wh": round(self.cpu_wh, 4),
            "other_wh": round(self.other_wh, 4),
            "seconds": round(self.seconds, 1),
            "avg_w": None if self.avg_w is None else round(self.avg_w, 1),
            "peak_w": None if self.peak_w is None else round(self.peak_w, 1),
        }


@dataclass
class _Counter:
    epoch: int
    last: float  # monotonic time of the last update
    wh: float = 0.0
    gpu: float = 0.0
    cpu: float = 0.0
    other: float = 0.0
    seconds: float = 0.0
    peaks: deque = field(default_factory=lambda: deque(maxlen=PEAK_SAMPLES))
    dropped_seq: int = -1  # the newest sample the full peak buffer evicted


# ── storage ─────────────────────────────────────────────────────────────────
_COLUMNS = "start, seconds, c_gpu, c_cpu, c_other, " + ", ".join(
    f"a_{k}" for k in ACTIVITIES
)
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS power_buckets ("
    "start INTEGER PRIMARY KEY, seconds REAL NOT NULL, "
    "c_gpu REAL NOT NULL, c_cpu REAL NOT NULL, c_other REAL NOT NULL, "
    + ", ".join(f"a_{k} REAL NOT NULL" for k in ACTIVITIES)
    + ")"
)
_UPSERT = (
    f"INSERT INTO power_buckets ({_COLUMNS}) VALUES ({', '.join('?' * 10)}) "
    "ON CONFLICT(start) DO UPDATE SET "
    + ", ".join(
        f"{c} = {c} + excluded.{c}"
        for c in ("seconds", "c_gpu", "c_cpu", "c_other")
        + tuple(f"a_{k}" for k in ACTIVITIES)
    )
)


class _Store:
    """power.db (or an in-memory database): written by the sampler only."""

    def __init__(self, path: Optional[Path]) -> None:
        self.conn = self._open(path)

    @staticmethod
    def _open(path: Optional[Path]) -> sqlite3.Connection:
        if path is not None:
            conn = None
            try:
                conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
                conn.execute(_SCHEMA)
                conn.commit()
                return conn
            except (sqlite3.Error, OSError) as exc:
                if conn is not None:
                    conn.close()
                log.warning(
                    "power.db unavailable (%s); keeping power data in memory",
                    type(exc).__name__,
                )
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.execute(_SCHEMA)
        return conn

    def load(self) -> list[_Bucket]:
        """The newest MAX_BUCKETS buckets, oldest first."""
        rows = self.conn.execute(
            f"SELECT {_COLUMNS} FROM (SELECT * FROM power_buckets "
            "ORDER BY start DESC LIMIT ?) ORDER BY start",
            (MAX_BUCKETS,),
        ).fetchall()
        out = []
        for row in rows:
            b = _Bucket(int(row[0]), row[1], row[2], row[3], row[4])
            b.act = dict(zip(ACTIVITIES, row[5:]))
            out.append(b)
        return out

    def upsert(self, b: _Bucket, prune: bool = False) -> None:
        self.conn.execute(
            _UPSERT,
            (
                b.start,
                b.seconds,
                b.gpu,
                b.cpu,
                b.other,
                *(b.act[k] for k in ACTIVITIES),
            ),
        )
        if prune:  # keep the newest MAX_BUCKETS rows by start
            self.conn.execute(
                "DELETE FROM power_buckets WHERE start NOT IN "
                "(SELECT start FROM power_buckets ORDER BY start DESC LIMIT ?)",
                (MAX_BUCKETS,),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ── local calendar ──────────────────────────────────────────────────────────
def _local(now: float, tz: Optional[tzinfo]) -> datetime:
    return datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)


def _midnight(day: date, tz: Optional[tzinfo]) -> float:
    return datetime.combine(day, dtime(), tzinfo=tz).timestamp()


def _utc_offset(now: float, tz: Optional[tzinfo]) -> int:
    aware = (
        datetime.fromtimestamp(now, tz)
        if tz
        else datetime.fromtimestamp(now).astimezone()
    )
    offset = aware.utcoffset()
    return int(offset.total_seconds()) if offset else 0


def calendar_starts(now: float, tz: Optional[tzinfo] = None) -> dict[str, float]:
    """Local today / week (Monday) / month starts as epoch seconds (PW11)."""
    day = _local(now, tz).date()
    return {
        "today": _midnight(day, tz),
        "week": _midnight(day - timedelta(days=day.weekday()), tz),
        "month": _midnight(day.replace(day=1), tz),
    }


def _r(value: float) -> float:
    return round(value, 3)


# ── sampler ─────────────────────────────────────────────────────────────────
class PowerSampler:
    """The agent's power sampler (PW01): one thread, started by the launcher.

    `settings` is read on every tick, so `power:` edits (including `enabled`)
    apply live. The readers and both clocks are injectable for tests."""

    def __init__(
        self,
        settings: Optional[Callable[[], PowerSettings]] = None,
        *,
        activity: Optional[Callable[[], Activity]] = None,
        gpu: Optional[Callable[[], Optional[float]]] = None,
        cpu: Optional[Callable[[], Optional[float]]] = None,
        data_dir: Optional[Path] = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        interval: float = SAMPLE_SECONDS,
        platform: Optional[str] = None,
        tz: Optional[tzinfo] = None,
    ) -> None:
        self._settings = settings or get_power_settings
        self._activity = activity or (lambda: ("idle", NO_OWNER))
        self._gpu, self._cpu = gpu, cpu
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._clock, self._wall = clock, wall
        self._interval, self._platform, self._tz = interval, platform, tz
        self._lock = threading.Lock()  # in-memory state; never held for I/O
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._store: Optional[_Store] = None
        self._buckets: dict[int, _Bucket] = {}  # closed, by start
        self._open: Optional[_Bucket] = None
        self._prev: Optional[tuple[float, Optional[float]]] = None  # (mono, gpu W)
        # latest (estimate, activity, cpu reading had a window behind it)
        self._now: Optional[tuple[Estimate, str, bool]] = None
        self._seq = 0
        self._epochs = 0
        self._counters: dict[str, _Counter] = {}
        self._id = next(_SAMPLER_IDS)  # readings from another sampler never compare
        self.ticks = 0

    # ── lifecycle ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="power-sampler", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        """Stop the thread; True once it has exited and the open bucket is
        flushed. A thread that outlives `timeout` flushes it itself on exit."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                return False
        self._shutdown()
        return True

    def _shutdown(self) -> None:
        """Flush the open bucket and close the store (idempotent)."""
        with self._lock:
            pending, self._open = self._open, None
            if pending is not None:
                self._keep(pending)
            store, self._store = self._store, None
        if store is not None:
            try:
                self._save(store, pending)
            finally:
                store.close()

    @staticmethod
    def _save(store: _Store, bucket: Optional[_Bucket]) -> None:
        if bucket is None:
            return
        try:
            store.upsert(bucket)
        except sqlite3.Error:
            log.warning("power bucket could not be saved")

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    log.exception("power sample failed")
                self._stop.wait(self._interval)
        finally:
            self._shutdown()  # a late exit after a timed-out stop() still writes

    def _ensure_store(self, create: bool = True) -> Optional[_Store]:
        """The store, opened (and its retained buckets loaded) on first use.
        Not `create`: only an existing power.db is opened, else None."""
        if self._store is not None:
            return self._store
        path = self._data_dir / "power.db" if self._data_dir is not None else None
        if not create and (path is None or not path.exists()):
            return None
        store = _Store(path)
        try:
            loaded = store.load()
        except sqlite3.Error as exc:
            store.close()
            self._quarantine(path, exc)
            store, loaded = _Store(None), []
        with self._lock:
            for b in loaded:
                self._keep(b)
        self._store = store
        return store

    def _quarantine(self, path: Optional[Path], exc: Exception) -> None:
        """A power.db with a valid header but damaged pages: move it aside so
        the next start is clean, and keep this run's data in memory."""
        moved = None
        if path is not None:
            dest = path.with_name(f"{path.name}.corrupt-{int(self._wall())}")
            try:
                os.replace(path, dest)
                moved = dest.name
            except OSError:
                pass
        log.warning(
            "power.db is damaged (%s); %s; keeping power data in memory",
            type(exc).__name__,
            f"moved to {moved}" if moved else "it could not be moved aside",
        )

    # ── one sample ──────────────────────────────────────────────────────
    @staticmethod
    def _read(reader: Callable[[], Optional[float]]) -> Optional[float]:
        try:
            value = reader()
        except Exception:
            return None
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    def _read_activity(self) -> Activity:
        try:
            activity, owner = self._activity()
        except Exception:
            return ("other", NO_OWNER)
        if activity not in ACTIVITIES:
            activity = "other"
        return activity, str(owner or NO_OWNER)

    def tick(self) -> None:
        """Take one sample and integrate the window since the previous one."""
        if self._stop.is_set():  # stopped: the store is flushed and closed
            return
        settings = self._settings()
        if not settings.enabled:
            # Not sampling, but the retained history stays readable, and the
            # open bucket is written so a restart does not lose it.
            store = self._ensure_store(create=False)
            with self._lock:
                self._prev, self._now = None, None
                pending, self._open = self._open, None
                if pending is not None:
                    self._keep(pending)
            if store is not None:
                self._save(store, pending)
            return
        store = self._ensure_store()
        assert store is not None
        model = resolve_model(settings, self._platform)
        gpu_w = self._read(self._gpu or _gpu_reader)
        cpu_pct = self._read(self._cpu or _cpu_reader)
        activity, owner = self._read_activity()
        now, wall = self._clock(), self._wall()
        est = estimate(model, gpu_w, cpu_pct)
        closed: Optional[_Bucket] = None
        with self._lock:
            self.ticks += 1
            self._seq += 1
            prev, self._prev = self._prev, (now, gpu_w)
            # the first CPU reading after a (re)start has no window behind it
            self._now = (est, activity, prev is not None)
            dt = now - prev[0] if prev is not None else 0.0
            if prev is not None and 0 < dt <= GAP_SECONDS:
                h = dt / 3600.0
                gpu_wh = ((prev[1] or 0.0) + (est.gpu_w or 0.0)) / 2 * h  # trapezoid
                cpu_wh = est.cpu_w * h  # rectangle over the window average
                other_wh = _wall_share(model, gpu_wh, cpu_wh, model.other_w * h)
                closed = self._add(wall, dt, gpu_wh, cpu_wh, other_wh, activity)
                self._count(owner, now, dt, gpu_wh, cpu_wh, other_wh, est.w)
        if closed is not None:
            try:
                store.upsert(closed, prune=True)
            except sqlite3.Error:
                log.warning("power bucket could not be saved")

    def _add(
        self,
        wall: float,
        dt: float,
        gpu: float,
        cpu: float,
        other: float,
        activity: str,
    ) -> Optional[_Bucket]:
        """Add a window to its bucket (by its end); return a bucket it closed."""
        start = int(wall // BUCKET_SECONDS) * BUCKET_SECONDS
        closed = None
        if self._open is not None and self._open.start != start:
            closed, self._open = self._open, None
            self._keep(closed)
            self._prune()
        if self._open is None:
            self._open = _Bucket(start)
        b = self._open
        b.seconds += dt
        b.gpu += gpu
        b.cpu += cpu
        b.other += other
        b.act[activity] += gpu + cpu + other
        return closed

    def _keep(self, b: _Bucket) -> None:
        kept = self._buckets.get(b.start)
        if kept is None:
            copy = _Bucket(b.start)
            copy.merge(b)
            self._buckets[b.start] = copy
        else:
            kept.merge(b)

    def _prune(self) -> None:
        """Keep the newest MAX_BUCKETS buckets and drop idle counters."""
        for start in sorted(self._buckets)[: max(0, len(self._buckets) - MAX_BUCKETS)]:
            del self._buckets[start]
        idle = self._clock() - OWNER_IDLE_SECONDS
        for owner in [o for o, c in self._counters.items() if c.last < idle]:
            del self._counters[owner]

    def _counter(self, owner: str, now: float) -> _Counter:
        c = self._counters.get(owner)
        if c is None:
            self._epochs += 1
            c = self._counters[owner] = _Counter(self._epochs, now)
        return c

    def _count(
        self,
        owner: str,
        now: float,
        dt: float,
        gpu: float,
        cpu: float,
        other: float,
        watts: float,
    ) -> None:
        c = self._counter(owner, now)
        c.last = now
        c.gpu += gpu
        c.cpu += cpu
        c.other += other
        c.wh += gpu + cpu + other
        c.seconds += dt
        if len(c.peaks) == c.peaks.maxlen:
            c.dropped_seq = c.peaks[0][0]
        c.peaks.append((self._seq, watts))

    # ── per-owner counters (PW04; the dispatcher reads these in PWR-2) ──
    def reading(self, owner: str) -> OwnerReading:
        """The owner's cumulative counter now (a film id, MANUAL or NO_OWNER)."""
        with self._lock:
            c = self._counter(str(owner), self._clock())
            return OwnerReading(
                owner=str(owner),
                sampler=self._id,
                epoch=c.epoch,
                seq=self._seq,
                wh=c.wh,
                gpu_wh=c.gpu,
                cpu_wh=c.cpu,
                other_wh=c.other,
                seconds=c.seconds,
            )

    def energy_between(
        self, start: OwnerReading, end: OwnerReading
    ) -> Optional[EnergyDelta]:
        """The owner's energy between two readings; None when they are not of
        one counter (another owner, a dropped counter, another sampler)."""
        with self._lock:
            c = self._counters.get(start.owner)
            if (
                start.sampler != self._id
                or end.sampler != self._id
                or start.owner != end.owner
                or start.epoch != end.epoch
                or c is None
                or c.epoch != start.epoch
                or end.seq < start.seq
            ):
                return None
            # Codex r3: an attempt longer than the buffer (24 h) lost early
            # samples, so its maximum is unknown rather than understated
            peaks = (
                [w for seq, w in c.peaks if start.seq < seq <= end.seq]
                if c.dropped_seq <= start.seq
                else []
            )
        seconds = max(0.0, end.seconds - start.seconds)
        wh = max(0.0, end.wh - start.wh)
        return EnergyDelta(
            wh=wh,
            gpu_wh=max(0.0, end.gpu_wh - start.gpu_wh),
            cpu_wh=max(0.0, end.cpu_wh - start.cpu_wh),
            other_wh=max(0.0, end.other_wh - start.other_wh),
            seconds=seconds,
            avg_w=wh * 3600 / seconds if seconds > 0 else None,
            peak_w=max(peaks) if peaks else None,
        )

    # ── reads ───────────────────────────────────────────────────────────
    def _all_buckets(self) -> list[_Bucket]:
        """Closed buckets plus the open one (merged when they share a start)."""
        out = dict(self._buckets)
        if self._open is not None:
            merged = _Bucket(self._open.start)
            if self._open.start in out:
                merged.merge(out[self._open.start])
            merged.merge(self._open)
            out[self._open.start] = merged
        return list(out.values())

    def live(self) -> Optional[dict[str, Any]]:
        """`/status.power`: the latest estimate plus today / week / month Wh;
        None while sampling is off or before the first sample."""
        wall = self._wall()
        starts = calendar_starts(wall, self._tz)
        with self._lock:
            if self._now is None:
                return None
            est, activity, cpu_ok = self._now
            buckets = self._all_buckets()
        # a bucket from a wall clock that has since stepped back is not now
        open_end = int(wall // BUCKET_SECONDS) * BUCKET_SECONDS + BUCKET_SECONDS
        totals = {
            key: sum(b.wh for b in buckets if since <= b.start < open_end)
            for key, since in starts.items()
        }
        return {
            "w": round(est.w if cpu_ok else est.w - est.cpu_w, 1),
            "gpu_w": None if est.gpu_w is None else round(est.gpu_w, 1),
            "cpu_w": round(est.cpu_w, 1) if cpu_ok else None,
            "other_w": round(est.other_w, 1),
            "estimated": True,
            "today_wh": _r(totals["today"]),
            "week_wh": _r(totals["week"]),
            "month_wh": _r(totals["month"]),
            "activity": activity,
        }

    def history(self, range_: str = "24h") -> dict[str, Any]:
        """Bounded series for `GET /power/history`: at most 168 points, each
        a local-aligned step. `covered_s` is the sampled time in the point; a
        point with none has `avg_w` None (a gap, never a zero)."""
        if range_ not in RANGES:
            raise ValueError("invalid range")
        points, step = RANGES[range_]
        wall = self._wall()
        offset = _utc_offset(wall, self._tz)
        open_end = int(wall // BUCKET_SECONDS) * BUCKET_SECONDS + BUCKET_SECONDS
        end = math.ceil((open_end + offset) / step) * step - offset
        first = end - points * step
        with self._lock:
            # a bucket later than now (the clock stepped back) is not history;
            # `end` only aligns the chart
            buckets = [
                b for b in self._all_buckets() if first <= b.start < min(end, open_end)
            ]
        covered = [0.0] * points
        gpu, cpu, other = [0.0] * points, [0.0] * points, [0.0] * points
        acts = {k: [0.0] * points for k in ACTIVITIES}
        for b in buckets:
            i = int((b.start - first) // step)
            covered[i] += b.seconds
            gpu[i] += b.gpu
            cpu[i] += b.cpu
            other[i] += b.other
            for k in ACTIVITIES:
                acts[k][i] += b.act[k]
        wh = [g + c + o for g, c, o in zip(gpu, cpu, other)]
        return {
            "range": range_,
            "step_s": step,
            "estimated": True,
            "t": [first + i * step for i in range(points)],
            "covered_s": [round(s, 1) for s in covered],
            "avg_w": [
                round(w * 3600 / s, 1) if s > 0 else None for w, s in zip(wh, covered)
            ],
            "wh": [_r(v) for v in wh],
            "gpu_wh": [_r(v) for v in gpu],
            "cpu_wh": [_r(v) for v in cpu],
            "other_wh": [_r(v) for v in other],
            "activity_wh": {k: [_r(v) for v in acts[k]] for k in ACTIVITIES},
            "totals": {
                "wh": _r(sum(wh)),
                "activity_wh": {k: _r(sum(acts[k])) for k in ACTIVITIES},
            },
        }


def power_view(sampler: PowerSampler, range_: str = "24h") -> dict[str, Any]:
    """The console's `GET /control/power`: live estimate + the same history
    as the network route."""
    history = sampler.history(range_)  # raises ValueError on a bad range first
    return {"power": sampler.live(), "history": history}


# ── process-wide holders (set by the launcher) ──────────────────────────────
_holder_lock = threading.Lock()
_settings: PowerSettings = PowerSettings()
_sampler: Optional[PowerSampler] = None


def set_power_settings(settings: PowerSettings) -> None:
    global _settings
    with _holder_lock:
        _settings = settings


def get_power_settings() -> PowerSettings:
    with _holder_lock:
        return _settings


def set_sampler(sampler: Optional[PowerSampler]) -> None:
    global _sampler
    with _holder_lock:
        _sampler = sampler


def get_sampler() -> Optional[PowerSampler]:
    """The running agent's sampler (PWR-2's dispatcher reads its counters)."""
    with _holder_lock:
        return _sampler


def _reset_for_tests() -> None:
    set_power_settings(PowerSettings())
    set_sampler(None)
