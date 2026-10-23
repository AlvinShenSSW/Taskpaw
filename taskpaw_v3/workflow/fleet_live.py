"""P2-2 (#293): the workers' live state on the controller, in memory only.

A heartbeat goes into `FleetLive` and nothing else: no store write, no renew
(C5). Every age is measured on the controller's own clock, so a worker's clock
never matters. A worker is 失联 after 60 s without a heartbeat (OD2); one not
heard from since the controller started is 等待心跳 for the first 60 s (IR12,
C14), and either state clears by itself when heartbeats return (R8).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from . import models as m
from .scheduler import OFFLINE_SECONDS, WorkerLive

PROTOCOL = 1  # S2: a mismatch is the eligibility reason 版本不一致, never a 426
LiveState = Literal["online", "lost", "waiting"]

_Short = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
_Bytes = Annotated[int, Field(strict=True, ge=0, le=10**16)]
_Number = Annotated[float, Field(ge=0, le=10**6, allow_inf_nan=False)]
# bytes per second: 10G is 1.25e9 (a bound of 1e6 refused every real rate)
_Rate = Annotated[float, Field(ge=0, le=10**12, allow_inf_nan=False)]


class GpuLive(m.Model):
    name: _Short | None = None
    vram_mib: m.Count | None = None
    used_mib: m.Count | None = None
    util: _Number | None = None
    power_w: _Number | None = None
    hold: Literal["none", "workflow", "manual", "unknown"] = "none"


class DiskLive(m.Model):
    free: _Bytes
    staging_used: _Bytes
    cap: _Bytes


class StagedFilm(m.Model):
    film: m.Id
    rev: m.Revision
    incomplete: bool = False  # §11 CX8: a download cut short, still releasable


class TransferLive(m.Model):
    film: m.Id
    dir: Literal["down", "up"]
    done: _Bytes
    total: _Bytes
    rate: _Rate  # bytes per second


class CurrentLive(m.Model):
    """P2-3 §2: the worker's running stage, the dispatcher's `current` typed.
    `elapsed_s` becomes `started_at` on the controller's clock (C14); `ltx`,
    `energy` and `power` are the shapes the board shows unchanged."""

    film_id: m.Id
    stage: Literal["restore", "asr"]
    attempt_id: m.Id
    phase: Literal["running", "settling", "uploading", "finishing"]
    progress: Annotated[str, Field(strict=True, max_length=400)] = ""
    elapsed_s: _Number = 0.0
    ltx: dict[str, Any] | None = None
    energy: dict[str, Any] | None = None
    power: dict[str, Any] | None = None
    uses_gpu: bool = True


class Heartbeat(m.Model):
    """What a worker reports every 5 s with a route, every 15 s idle (R7)."""

    boot_id: m.Id
    version: _Short
    protocol: Annotated[int, Field(strict=True, ge=0, le=1000)]
    paused: bool
    gpu: GpuLive
    disk: DiskLive
    tiers_ready: tuple[m.LtxTier, ...] = Field(max_length=3)
    asr_ready: bool
    staged: tuple[StagedFilm, ...] = Field(default=(), max_length=64)
    current: CurrentLive | None = None  # P2-3: the running stage, typed
    transfers: tuple[TransferLive, ...] = Field(default=(), max_length=8)


@dataclass(frozen=True)
class LiveEntry:
    beat: Heartbeat
    last_seen: datetime  # the controller's clock


class FleetLive:
    def __init__(
        self,
        started_at: datetime,
        version: str,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        """`on_change`: P2-3 R13 — called (outside the lock) when a beat
        changes what the routing pass decides on."""
        self.started_at, self.version = started_at, version
        self.on_change = on_change
        self._lock = threading.Lock()
        self._entries: dict[str, LiveEntry] = {}

    def observe(self, worker_id: str, beat: Heartbeat, now: datetime) -> None:
        with self._lock:
            old = self._entries.get(worker_id)
            self._entries[worker_id] = LiveEntry(beat, now)
        if self.on_change is not None and _changed(old, beat, now):
            try:
                self.on_change()
            except Exception:  # a trigger never refuses a heartbeat
                pass

    def forget(self, worker_id: str) -> None:
        """A revoked worker leaves the live state at once (IR14)."""
        with self._lock:
            self._entries.pop(worker_id, None)

    def entry(self, worker_id: str) -> LiveEntry | None:
        with self._lock:
            return self._entries.get(worker_id)

    def version_ok(self, beat: Heartbeat) -> bool:
        return beat.version == self.version and beat.protocol == PROTOCOL

    def state(self, worker_id: str, now: datetime) -> LiveState:
        entry = self.entry(worker_id)
        if entry is None:
            grace = self.started_at + timedelta(seconds=OFFLINE_SECONDS)
            return "waiting" if now < grace else "lost"
        age = (now - entry.last_seen).total_seconds()
        return "online" if age < OFFLINE_SECONDS else "lost"

    def currents(
        self, now: datetime, names: Mapping[str, str]
    ) -> dict[str, dict[str, Any]]:
        """R15: each online worker's running stage as the board's `current`
        (`started_at` = last heartbeat − `elapsed_s`, C14), with its machine and the
        transfer of that film as the step note."""
        with self._lock:
            entries = dict(self._entries)
        found: dict[str, dict[str, Any]] = {}
        for worker_id, entry in entries.items():
            beat = entry.beat
            if (now - entry.last_seen).total_seconds() >= OFFLINE_SECONDS:
                continue
            for item in beat.transfers:
                found.setdefault(
                    item.film, _transfer_current(worker_id, names, item, beat, now)
                )
            current = beat.current
            if current is None:
                continue
            value = current.model_dump(mode="json", exclude={"elapsed_s", "phase"})
            started = entry.last_seen - timedelta(
                seconds=current.elapsed_s
            )  # at ingest
            transfer = next(
                (t for t in beat.transfers if t.film == current.film_id), None
            )
            value.update(
                started_at=started.isoformat(),
                phase=current.phase,
                machine={"id": worker_id, "name": names.get(worker_id, "")},
                transfer=transfer_note(transfer) if transfer else None,
            )
            if transfer is not None:
                value["progress"] = transfer_note(transfer)
            found[current.film_id] = value
        return found

    def snapshot(self) -> dict[str, WorkerLive]:
        """The scheduler's `live` input: every worker heard from."""
        with self._lock:
            entries = dict(self._entries)
        return {
            worker_id: WorkerLive(
                last_seen=entry.last_seen,
                paused=entry.beat.paused,
                tiers_ready=frozenset(entry.beat.tiers_ready),
                asr_ready=entry.beat.asr_ready,
                free_bytes=entry.beat.disk.free,
                staging_cap_bytes=entry.beat.disk.cap,
                version_ok=self.version_ok(entry.beat),
            )
            for worker_id, entry in entries.items()
        }


def _gb(value: int) -> str:
    return f"{value / 10**9:.1f}"


def transfer_note(item: TransferLive) -> str:
    """「下载源文件 12.4/38.0 GB · 610 MB/s」 / 「回传 …」 (§6.3)."""
    label = "下载源文件" if item.dir == "down" else "回传"
    rate = f" · {item.rate / 10**6:.0f} MB/s" if item.rate else ""
    return f"{label} {_gb(item.done)}/{_gb(item.total)} GB{rate}"


def _transfer_current(
    worker_id: str,
    names: Mapping[str, str],
    item: TransferLive,
    beat: Heartbeat,
    now: datetime,
) -> dict[str, Any]:
    """A film being fetched (no stage claimed yet): the restore step shows
    the download with its machine."""
    note = transfer_note(item)
    return {
        "film_id": item.film,
        "stage": None,  # nothing claimed yet
        "attempt_id": None,
        "progress": note,
        "transfer": note,
        "phase": "fetching",
        "started_at": None,
        "ltx": None,
        "energy": None,
        "power": None,
        "uses_gpu": False,
        "machine": {"id": worker_id, "name": names.get(worker_id, "")},
        "fetching": True,
    }


def _routing_facts(beat: Heartbeat) -> tuple[object, ...]:
    """What the pass reads of a beat: pause, readiness, version, staged set."""
    return (
        beat.paused,
        frozenset(beat.tiers_ready),
        beat.asr_ready,
        beat.version,
        beat.protocol,
        frozenset((s.film, s.rev, s.incomplete) for s in beat.staged),
        beat.boot_id,
    )


def _changed(old: LiveEntry | None, beat: Heartbeat, now: datetime) -> bool:
    """R13: a new entry, one back from 失联, or changed routing facts."""
    if old is None:
        return True
    if (now - old.last_seen).total_seconds() >= OFFLINE_SECONDS:
        return True
    return _routing_facts(old.beat) != _routing_facts(beat)
