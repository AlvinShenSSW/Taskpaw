"""P2-2 (#293): the workers' live state on the controller, in memory only.

A heartbeat goes into `FleetLive` and nothing else: no store write, no renew
(C5). Every age is measured on the controller's own clock, so a worker's clock
never matters. A worker is 失联 after 60 s without a heartbeat (OD2); one not
heard from since the controller started is 等待心跳 for the first 60 s (IR12,
C14), and either state clears by itself when heartbeats return (R8).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from . import models as m
from .scheduler import OFFLINE_SECONDS, WorkerLive

PROTOCOL = 1  # S2: a mismatch is the eligibility reason 版本不一致, never a 426
LiveState = Literal["online", "lost", "waiting"]

_Short = Annotated[str, Field(strict=True, min_length=1, max_length=128)]
_Bytes = Annotated[int, Field(strict=True, ge=0, le=10**16)]
_Number = Annotated[float, Field(ge=0, le=10**6, allow_inf_nan=False)]


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


class TransferLive(m.Model):
    film: m.Id
    dir: Literal["down", "up"]
    done: _Bytes
    total: _Bytes
    rate: _Number


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
    current: None = None  # the running stage arrives with remote execution (P2-3)
    transfers: tuple[TransferLive, ...] = Field(default=(), max_length=8)


@dataclass(frozen=True)
class LiveEntry:
    beat: Heartbeat
    last_seen: datetime  # the controller's clock


class FleetLive:
    def __init__(self, started_at: datetime, version: str) -> None:
        self.started_at, self.version = started_at, version
        self._lock = threading.Lock()
        self._entries: dict[str, LiveEntry] = {}

    def observe(self, worker_id: str, beat: Heartbeat, now: datetime) -> None:
        with self._lock:
            self._entries[worker_id] = LiveEntry(beat, now)

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
