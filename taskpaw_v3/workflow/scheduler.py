"""P2-1 (#292): the controller's scheduler, pure over a snapshot and a clock value.

Phase-2 design §4 as revised by §13 (S3–S6, C8, C9, C12, V1) and §14 (OD1,
OD2, OD5); the frozen contract is
docs/specs/2026-10-22-media-workflow-p2-1-store-scheduler.md §5.

`schedule()` reads the store's `RoutingSnapshot`, the workers' live state
(heartbeats, P2-2) and `now`, and returns which unclaimed routes to withdraw,
which films to route where (with the persisted human-readable reason), why the
others wait, and which films need the operator's attention. It performs no
I/O and never reads a clock; `routing.run_pass` applies the result through the
store's CAS commands.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Literal

from . import models as m
from .plan import TIER_LABELS

GB = 10**9
OFFLINE_SECONDS = 60.0  # S3/OD2: 失联; no new routes, nothing moves
MAX_OPEN_ROUTES = 2  # S4: one active route and one prefetch per worker
TRANSFER_BYTES_PER_SECOND = 1e9  # S6: fixed 1 GB/s
TIE_SECONDS = 600.0  # D10: finishes within max(10 min, 5 %) tie
TIE_SHARE = 0.05
RESTORE_MIN_VRAM_MIB = 15360  # OD5: 16 GB class (real cards report ~16300)
STAGING_CAP_BYTES = 500 * GB
STAGING_RESERVE_BYTES = 50 * GB
STAGING_EXTRA_BYTES = 2 * GB
OUTPUT_FACTOR = 1.2  # a restored video against its source
CONTROLLER_RESERVE_BYTES = 100 * GB  # C8: Miko and the archive stage there too
HISTORY_SAMPLES = 10
REMAINING_FLOOR_SECONDS = 120.0
UNKNOWN_DURATION_SECONDS = 3600.0
FULL_HD = (1920, 1080)

LOST = "设备失联"
NO_HISTORY = "无历史，按排队先后"
CONTROLLER_DISK = "主控磁盘空间不足"
QUEUE_FULL = "设备队列已满"
NO_DEVICE = "没有可用的设备"
PIN_GONE = "指定设备不可用"
HAND_OVER = "转派 GPU 设备"
REVOKED = "已撤销"
SOURCE_CHANGED = "片源变化，等待重新扫描"  # CX7: the claim would refuse the source

_FINISHED = ("succeeded", "skipped")
# (stage, GPU model, resolution class); ASR is not keyed on resolution
_Key = tuple[str, str | None, str | None]
_ROUTED: tuple[m.RoutedStage, ...] = ("restore", "asr")


@dataclass(frozen=True)
class WorkerLive:
    """A worker's heartbeat state on the controller's clock (`fleet_live`, P2-2)."""

    last_seen: datetime
    paused: bool = False
    tiers_ready: frozenset[str] = frozenset(TIER_LABELS)
    asr_ready: bool = True
    free_bytes: int = 2_000 * GB
    staging_cap_bytes: int = STAGING_CAP_BYTES
    version_ok: bool = True


@dataclass(frozen=True)
class RouteDecision:
    film_id: str
    input_revision: int
    worker_id: str
    kind: Literal["auto", "pinned"]
    pin_version: int
    reason: str  # V1: persisted on the route
    finish_at: datetime
    energy_wh: float | None


@dataclass(frozen=True)
class Withdrawal:
    route_id: str
    version: int
    film_id: str
    reason: str


@dataclass(frozen=True)
class SchedulePass:
    routes: tuple[RouteDecision, ...]
    withdrawals: tuple[Withdrawal, ...]
    waiting: dict[str, str]
    attention: dict[str, str]


# ── film facts ──────────────────────────────────────────────────────────
def resolution_class(width: int, height: int) -> str:
    return "4K" if width > FULL_HD[0] or height > FULL_HD[1] else "1080p"


def _class(film: m.RoutingFilm) -> str:
    return resolution_class(film.max_width, film.max_height)


def film_seconds(film: m.RoutingFilm) -> float:
    return film.duration_ms / 1000 if film.duration_ms else UNKNOWN_DURATION_SECONDS


def staging_need(film: m.RoutingFilm) -> float:
    """§3.6: the source, its restored output and a margin on the worker."""
    return film.source_bytes * (1 + OUTPUT_FACTOR) + STAGING_EXTRA_BYTES


# per (film id, input revision): its restore and ASR that need a GPU worker
GpuStages = Mapping[tuple[str, int], frozenset[str]]


def routed_stages(
    film: m.RoutingFilm, gpu_stages: GpuStages | None
) -> tuple[m.RoutedStage, ...]:
    """The film revision's unfinished restore and ASR that need a GPU worker."""
    needs = (
        gpu_stages.get((film.film_id, film.input_revision), frozenset(_ROUTED))
        if gpu_stages is not None
        else frozenset(_ROUTED)
    )
    return tuple(
        stage
        for stage in _ROUTED
        if stage in needs and getattr(film, stage) not in _FINISHED
    )


def _ready_to_route(film: m.RoutingFilm, stages: tuple[str, ...]) -> bool:
    """The first routed stage can be claimed next (its predecessor finished)."""
    if not stages or not film.planned or film.claimed_by is not None:
        return False
    if stages[0] == "restore":
        return film.restore == "pending"
    return film.restore in _FINISHED and film.asr == "pending"


# ── prediction (S6) ─────────────────────────────────────────────────────
class Rates:
    """Seconds per film-second keyed on (GPU model, resolution class) × tier."""

    def __init__(self, samples: Iterable[m.RateSample]) -> None:
        ordered = sorted(
            (x for x in samples if x.wall_seconds > 0 and x.duration_ms > 0),
            key=lambda x: (x.ended_at, x.worker_id),
            reverse=True,
        )
        by_tier: dict[str, list[float]] = {}
        for sample in ordered:
            if sample.stage == "restore":
                by_tier.setdefault(sample.tier or "standard", []).append(
                    self._ratio(sample)
                )
        standard = by_tier.get("standard")
        self.factors = {
            tier: median(values) / median(standard)
            for tier, values in by_tier.items()
            if standard and median(standard) > 0 and tier != "standard"
        }
        bases: dict[_Key, list[float]] = {}
        watts: dict[tuple[str, str], list[float]] = {}
        for sample in ordered:
            if sample.gpu_model is None:
                continue
            ratio = self._ratio(sample)
            key: _Key
            if sample.stage == "restore":
                key = (
                    "restore",
                    sample.gpu_model,
                    resolution_class(sample.max_width, sample.max_height),
                )
                ratio /= self.factor(sample.tier)
            else:
                key = ("asr", sample.gpu_model, None)
            values = bases.setdefault(key, [])
            if len(values) < HISTORY_SAMPLES:
                values.append(ratio)
            if sample.avg_w is not None:
                power = watts.setdefault((sample.stage, sample.gpu_model), [])
                if len(power) < HISTORY_SAMPLES:
                    power.append(sample.avg_w)
        self.bases = {key: median(values) for key, values in bases.items()}
        self.watts = {key: median(values) for key, values in watts.items()}

    @staticmethod
    def _ratio(sample: m.RateSample) -> float:
        return sample.wall_seconds / (sample.duration_ms / 1000)

    def factor(self, tier: str | None) -> float:
        return self.factors.get(tier or "standard", 1.0)

    def estimate(
        self, stage: str, worker: m.RoutingWorker, film: m.RoutingFilm
    ) -> tuple[float, bool]:
        """(seconds, from history); no sample: the film's duration (D10)."""
        key: _Key
        if stage == "restore":
            key = ("restore", worker.gpu_model, _class(film))
            factor = self.factor(film.ltx_model)
        else:
            key, factor = ("asr", worker.gpu_model, None), 1.0
        base = self.bases.get(key) if worker.gpu_model else None
        ratio = 1.0 if base is None else base
        return film_seconds(film) * ratio * factor, base is not None

    def energy_wh(
        self, stages: tuple[str, ...], worker: m.RoutingWorker, film: m.RoutingFilm
    ) -> float | None:
        """§4.2: the first routed stage's estimate × its median W, else unknown."""
        stage = stages[0]
        power = self.watts.get((stage, worker.gpu_model or ""))
        if power is None:
            return None
        return self.estimate(stage, worker, film)[0] * power / 3600


def _cost(
    rates: Rates,
    worker: m.RoutingWorker,
    film: m.RoutingFilm,
    stages: tuple[str, ...],
    now: datetime,
) -> tuple[float, bool]:
    """Fetch + Σ stage estimates + upload, a claimed stage less its elapsed time."""
    total, known = 0.0, False
    if "restore" in stages and film.restore == "pending" and film.claimed_by is None:
        total += film.source_bytes / TRANSFER_BYTES_PER_SECOND
    for stage in stages:
        seconds, from_history = rates.estimate(stage, worker, film)
        known |= from_history
        if film.claimed_stage == stage and film.claimed_at is not None:
            elapsed = (now - film.claimed_at).total_seconds()
            seconds = max(REMAINING_FLOOR_SECONDS, seconds - elapsed)
        total += seconds
    if "restore" in stages:
        total += film.source_bytes * OUTPUT_FACTOR / TRANSFER_BYTES_PER_SECOND
    return total, known


# ── eligibility ─────────────────────────────────────────────────────────
def _gone(worker: m.RoutingWorker) -> bool:
    return worker.enrollment_state == "retired" or worker.admission_state == "revoked"


def remote_mode(
    workers: Iterable[m.RoutingWorker | m.WorkerRecord],
    controller_id: str,
    *,
    enabled: bool,
) -> tuple[str, ...] | None:
    """OD1 (correction): None is single-machine mode (`workflow.remote_workers`
    off, P2-2 IR1: every stage runs on the controller, phase 1). Otherwise
    remote mode, whatever the workers' state: the controller's GPU takes no
    stage, and the result names the workers that can run one — every worker
    but the controller that is verified and not revoked (IR5; a retired one
    is never verified), possibly none. The single predicate of the pass, the
    controller's dispatcher and 设备."""
    if not enabled:
        return None
    return tuple(
        worker.name
        for worker in workers
        if worker.worker_id != controller_id
        and worker.enrollment_state == "verified"
        and worker.admission_state != "revoked"
    )


def _online(live: WorkerLive | None, now: datetime) -> bool:
    return live is not None and (now - live.last_seen).total_seconds() < OFFLINE_SECONDS


def _fits(worker: m.RoutingWorker, stage: str, film: m.RoutingFilm) -> bool:
    return any(
        profile.stage == stage
        and film.max_width <= profile.max_width
        and film.max_height <= profile.max_height
        for profile in worker.profiles
    )


def eligibility(
    worker: m.RoutingWorker,
    film: m.RoutingFilm,
    stages: tuple[str, ...],
    live: WorkerLive | None,
    now: datetime,
    *,
    queued_need: float | None = 0.0,
) -> str | None:
    """None when `worker` may take `stages` of `film`, else the first failing
    rule as the reason shown to the operator. `queued_need`: the staging the
    worker's open routes already need; None skips the disk rule."""
    if _gone(worker):
        return REVOKED
    if worker.enrollment_state != "verified":
        return "未核验"
    if not worker.participation or worker.admission_state == "disabled":
        return "未启用"
    if worker.admission_state == "draining":
        return "停领"
    if live is None or not _online(live, now):
        return "离线"
    if live.paused:
        return "暂停领取"
    if not live.version_ok:
        return "版本不一致"
    if not worker.boot_reconciled:
        return "等待对账"
    too_big = "显存不足 4K" if _class(film) == "4K" else "分辨率超出设备上限"
    if "restore" in stages:
        if worker.vram_mib < RESTORE_MIN_VRAM_MIB:
            return "显存不足，不参与修复"
        if not _fits(worker, "restore", film):
            return too_big
        tier = film.ltx_model
        if tier is not None and tier not in live.tiers_ready:
            return f"缺少{TIER_LABELS.get(tier, tier)}画质模型"
    if "asr" in stages:
        if not _fits(worker, "asr", film):
            return too_big
        if not live.asr_ready:
            return "识别未就绪"
    if queued_need is not None:
        need = queued_need + staging_need(film)  # IR11/CX1: the queue's staging too
        if (
            need > live.staging_cap_bytes
            or live.free_bytes - need < STAGING_RESERVE_BYTES
        ):
            return "空间不足"
    return None


# ── text ────────────────────────────────────────────────────────────────
def duration_text(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if hours else f"{minutes} 分"


def _energy_text(wh: float | None) -> str | None:
    return None if wh is None else f"约 {wh / 1000:.1f} 度"


@dataclass(frozen=True)
class _Option:
    worker: m.RoutingWorker
    finish: float  # seconds from now
    energy: float | None
    routes: int
    known: bool


def lost_films(
    routes: Iterable[m.FilmRoute],
    *,
    live: Mapping[str, WorkerLive],
    now: datetime,
    controller_id: str,
    live_since: datetime | None = None,
) -> dict[str, str]:
    """P2-4 R3 (OD2/V2): film id → the worker of its open route that is 失联.
    A worker not heard from since the controller started is still 等待心跳
    inside the 60 s grace (IR12, C14). The one rule of the pass's attention,
    the board's 设备失联 and the #286 observer."""
    grace = (
        live_since is not None and (now - live_since).total_seconds() < OFFLINE_SECONDS
    )
    return {
        route.film_id: route.worker_id
        for route in routes
        if route.worker_id != controller_id
        and not _online(live.get(route.worker_id), now)
        and not (grace and route.worker_id not in live)
    }


@dataclass
class _PassState:
    """What the pass decides on: steps 1–3 of `schedule` (P2-4 `preview`
    reads the same state, so its 自动 is the pass's own choice)."""

    rates: Rates
    films: dict[str, m.RoutingFilm]
    workers: dict[str, m.RoutingWorker]
    targets: list[m.RoutingWorker]
    withdrawals: list[Withdrawal]
    hand_over: dict[str, m.FilmRoute]
    remaining: list[m.FilmRoute]
    attention: dict[str, str]
    counts: dict[str, int]
    busy: dict[str, float]
    needs: dict[str, float]
    outputs: float


def _pass_state(
    snapshot: m.RoutingSnapshot,
    *,
    live: Mapping[str, WorkerLive],
    now: datetime,
    controller_id: str,
    gpu_stages: GpuStages | None,
    live_since: datetime | None,
) -> _PassState:
    rates = Rates(snapshot.samples)
    films = {film.film_id: film for film in snapshot.films}
    facts = {  # CX5: every open route is accounted with its own revision
        (film.film_id, film.input_revision): film
        for film in (*snapshot.older_revisions, *snapshot.films)
    }
    workers = {worker.worker_id: worker for worker in snapshot.workers}
    targets = sorted(
        (w for w in snapshot.workers if w.worker_id != controller_id),  # OD1
        key=lambda w: (w.name, w.worker_id),
    )
    routes = sorted(snapshot.routes, key=lambda r: (r.routed_at, r.route_id))

    # 1. withdraw unclaimed routes that can no longer run (§4.4, OD2); the
    # controller's unclaimed local route of a film whose next stage needs a GPU
    # worker is handed over only when the film is routed in this pass (C12, IR1)
    withdrawals: list[Withdrawal] = []
    hand_over: dict[str, m.FilmRoute] = {}
    for route in routes:
        film = films.get(route.film_id)
        if film is None or film.input_revision != route.input_revision:
            continue  # an older revision's route closes when its work resolves
        if film.claimed_by is not None:
            continue  # claimed: only its worker or 判定已停止 settles it
        stages = routed_stages(film, gpu_stages)
        reason = None
        if not film.source_admissible:  # CX7: it could never be claimed
            reason = SOURCE_CHANGED
        elif route.worker_id == controller_id:
            if route.kind == "local" and _ready_to_route(film, stages):
                hand_over[film.film_id] = route
        elif stages and film.pin_worker_id != route.worker_id:
            # an auto route; a film pinned to this worker never moves (§4.3)
            worker = workers.get(route.worker_id)
            worker_live = live.get(route.worker_id)
            if worker is not None and (_gone(worker) or _online(worker_live, now)):
                # 失联 never moves a film (OD2); disk only gates new routes
                reason = eligibility(
                    worker, film, stages, worker_live, now, queued_need=None
                )
        if reason is not None:
            withdrawals.append(
                Withdrawal(route.route_id, route.version, route.film_id, reason)
            )
    gone = {w.route_id for w in withdrawals} | {r.route_id for r in hand_over.values()}
    remaining = [route for route in routes if route.route_id not in gone]

    # 2. attention: a film on a lost worker (OD2/V2, `lost_films`)
    attention = {
        film_id: LOST
        for film_id in lost_films(
            remaining,
            live=live,
            now=now,
            controller_id=controller_id,
            live_since=live_since,
        )
    }

    # 3. the pass state: queue length, busy time and staging per worker; an
    # older revision's route still running there holds its slot (IR6) and its
    # own revision's busy time, staging and output (CX5)
    counts = {w.worker_id: 0 for w in targets}
    busy = {w.worker_id: 0.0 for w in targets}
    needs = {w.worker_id: 0.0 for w in targets}
    outputs = 0.0
    for route in remaining:
        if route.worker_id not in counts:
            continue
        counts[route.worker_id] += 1
        film = facts.get((route.film_id, route.input_revision))
        if film is None:
            continue
        stages = routed_stages(film, gpu_stages)
        busy[route.worker_id] += _cost(
            rates, workers[route.worker_id], film, stages, now
        )[0]
        needs[route.worker_id] += staging_need(film)
        if "restore" in stages:
            outputs += film.source_bytes * OUTPUT_FACTOR
    return _PassState(
        rates,
        films,
        workers,
        targets,
        withdrawals,
        hand_over,
        remaining,
        attention,
        counts,
        busy,
        needs,
        outputs,
    )


@dataclass(frozen=True)
class DevicePreview:
    """One device of 待处理's select: its finish, or why it cannot take it."""

    worker_id: str
    name: str
    gpu_model: str | None
    vram_mib: int
    finish_at: datetime | None
    energy_wh: float | None
    reason: str | None


@dataclass(frozen=True)
class Preview:
    """P2-4 R5: 自动 for one film and tier — the device the next pass would
    route it to (`machine`) with its finish, energy and reason, or why it
    would wait (`waiting`) — and every device's own option."""

    machine: str | None
    finish_at: datetime | None
    energy_wh: float | None
    reason: str | None
    waiting: str | None
    devices: tuple[DevicePreview, ...]


_Found = tuple["_Option | None", "str | None", "list[_Option]"]


@dataclass
class _Probe:
    """The film `preview` follows through the candidate loop."""

    film_id: str
    found: _Found | None = None
    devices: tuple[DevicePreview, ...] = ()


def _devices(
    state: _PassState,
    film: m.RoutingFilm,
    stages: tuple[str, ...],
    live: Mapping[str, WorkerLive],
    now: datetime,
) -> tuple[DevicePreview, ...]:
    found = []
    for worker in state.targets:
        reason = eligibility(
            worker,
            film,
            stages,
            live.get(worker.worker_id),
            now,
            queued_need=state.needs[worker.worker_id],
        )
        if reason == REVOKED:
            continue
        if reason is None and state.counts[worker.worker_id] >= MAX_OPEN_ROUTES:
            reason = QUEUE_FULL
        option = (
            None
            if reason is not None
            else _measure(
                state.rates, film, stages, worker, now, state.counts, state.busy
            )
        )
        found.append(
            DevicePreview(
                worker_id=worker.worker_id,
                name=worker.name,
                gpu_model=worker.gpu_model,
                vram_mib=worker.vram_mib,
                finish_at=None
                if option is None
                else now + timedelta(seconds=option.finish),
                energy_wh=None if option is None else option.energy,
                reason=reason,
            )
        )
    return tuple(found)


def _route_films(
    state: _PassState,
    snapshot: m.RoutingSnapshot,
    *,
    live: Mapping[str, WorkerLive],
    now: datetime,
    controller_free_bytes: int | None,
    gpu_stages: GpuStages | None,
    probe: _Probe | None = None,
) -> tuple[list[RouteDecision], dict[str, str]]:
    """Step 4: the candidates, 4K first, then confirm time (§4.3)."""
    open_films = {(route.film_id, route.input_revision) for route in state.remaining}
    ready = [
        film
        for film in snapshot.films
        if (film.film_id, film.input_revision) not in open_films
        and _ready_to_route(film, routed_stages(film, gpu_stages))
    ]
    waiting: dict[str, str] = {
        film.film_id: SOURCE_CHANGED for film in ready if not film.source_admissible
    }
    candidates = sorted(
        (film for film in ready if film.source_admissible),
        key=lambda f: (
            _class(f) != "4K",
            f.confirmed_at or f.created_at,
            f.film_id,
        ),
    )
    rates, counts, busy, needs = state.rates, state.counts, state.busy, state.needs
    decisions: list[RouteDecision] = []
    disk_full = False
    outputs = state.outputs
    for film in candidates:
        stages = routed_stages(film, gpu_stages)
        mine = probe if probe is not None and probe.film_id == film.film_id else None
        if mine is not None:
            mine.devices = _devices(state, film, stages, live, now)
        output = film.source_bytes * OUTPUT_FACTOR if "restore" in stages else 0.0
        disk_full = disk_full or (
            controller_free_bytes is not None
            and outputs + output + CONTROLLER_RESERVE_BYTES > controller_free_bytes
        )
        if disk_full:  # C8/IR10: the gate stops every new route of the pass
            waiting[film.film_id] = CONTROLLER_DISK
            if mine is not None:
                mine.found = (None, CONTROLLER_DISK, [])
            continue
        pin = film.pin_worker_id
        if pin is not None:
            choice = _pinned(
                rates,
                film,
                stages,
                state.workers.get(pin),
                live,
                now,
                counts,
                needs,
                busy,
            )
            if isinstance(choice, str):
                waiting[film.film_id] = choice
                if mine is not None:
                    mine.found = (None, choice, [])
                continue
            options = [choice]
        else:
            found = _options(
                rates, film, stages, state.targets, live, now, counts, needs, busy
            )
            if isinstance(found, str):
                waiting[film.film_id] = found
                if mine is not None:
                    mine.found = (None, found, [])
                continue
            options = found
        best = _choose(options)
        if mine is not None:
            mine.found = (best, None, options)
        worker = best.worker
        kind: Literal["auto", "pinned"] = "pinned" if pin is not None else "auto"
        local = state.hand_over.get(film.film_id)
        if local is not None:  # C12: routed now, so the local route goes first
            state.withdrawals.append(
                Withdrawal(local.route_id, local.version, film.film_id, HAND_OVER)
            )
        decisions.append(
            RouteDecision(
                film_id=film.film_id,
                input_revision=film.input_revision,
                worker_id=worker.worker_id,
                kind=kind,
                pin_version=film.pin_version,
                reason=_reason(kind, best, options),
                finish_at=now + timedelta(seconds=best.finish),
                energy_wh=best.energy,
            )
        )
        counts[worker.worker_id] += 1
        busy[worker.worker_id] += _cost(rates, worker, film, stages, now)[0]
        needs[worker.worker_id] += staging_need(film)
        outputs += output
    return decisions, waiting


# ── the pass ────────────────────────────────────────────────────────────
def schedule(
    snapshot: m.RoutingSnapshot,
    *,
    live: Mapping[str, WorkerLive],
    now: datetime,
    controller_id: str,
    controller_free_bytes: int | None = None,
    gpu_stages: GpuStages | None = None,
    live_since: datetime | None = None,
    remote_enabled: bool = False,
) -> SchedulePass:
    """`live_since`: when the live state started (the controller's start); a
    worker not heard from since raises no 设备失联 in the first 60 s (IR12, C14).
    `remote_enabled`: the `workflow.remote_workers` setting (P2-2 IR1)."""
    if remote_mode(snapshot.workers, controller_id, enabled=remote_enabled) is None:
        # IR1: single machine; the controller's dispatcher runs every stage
        return SchedulePass((), (), {}, {})
    state = _pass_state(
        snapshot,
        live=live,
        now=now,
        controller_id=controller_id,
        gpu_stages=gpu_stages,
        live_since=live_since,
    )
    decisions, waiting = _route_films(
        state,
        snapshot,
        live=live,
        now=now,
        controller_free_bytes=controller_free_bytes,
        gpu_stages=gpu_stages,
    )
    return SchedulePass(
        tuple(decisions), tuple(state.withdrawals), waiting, state.attention
    )


def preview(
    snapshot: m.RoutingSnapshot,
    film_id: str,
    tiers: Iterable[str | None],
    *,
    live: Mapping[str, WorkerLive],
    now: datetime,
    controller_id: str,
    controller_free_bytes: int | None = None,
    gpu_stages: GpuStages | None = None,
    live_since: datetime | None = None,
) -> dict[str | None, Preview]:
    """P2-4 R5, pure: per tier, the film as if confirmed now with 自动, run
    through the pass's own state and candidate order. {} for a film the
    snapshot does not hold (no GPU work left). Advisory: 预计."""
    base = next((f for f in snapshot.films if f.film_id == film_id), None)
    if base is None:
        return {}
    result: dict[str | None, Preview] = {}
    for tier in tiers:
        film = base.model_copy(
            update={
                "planned": True,
                "confirmed_at": now,
                "ltx_model": tier,
                "pin_worker_id": None,
                "pin_version": base.pin_version or 1,
            }
        )
        hypothetical = snapshot.model_copy(
            update={
                "films": tuple(
                    film if f.film_id == film_id else f for f in snapshot.films
                )
            }
        )
        state = _pass_state(
            hypothetical,
            live=live,
            now=now,
            controller_id=controller_id,
            gpu_stages=gpu_stages,
            live_since=live_since,
        )
        probe = _Probe(film_id)
        _route_films(
            state,
            hypothetical,
            live=live,
            now=now,
            controller_free_bytes=controller_free_bytes,
            gpu_stages=gpu_stages,
            probe=probe,
        )
        if probe.found is None:  # not routable yet (a C12 local restore first)
            stages = routed_stages(film, gpu_stages)
            probe.devices = _devices(state, film, stages, live, now)
            found = _options(
                state.rates,
                film,
                stages,
                state.targets,
                live,
                now,
                state.counts,
                state.needs,
                state.busy,
            )
            probe.found = (
                (None, found, [])
                if isinstance(found, str)
                else (_choose(found), None, found)
            )
        best, why, options = probe.found
        result[tier] = Preview(
            machine=best.worker.worker_id if best else None,
            finish_at=now + timedelta(seconds=best.finish) if best else None,
            energy_wh=best.energy if best else None,
            reason=_reason("auto", best, options) if best else None,
            waiting=why,
            devices=probe.devices,
        )
    return result


def _measure(
    rates: Rates,
    film: m.RoutingFilm,
    stages: tuple[str, ...],
    worker: m.RoutingWorker,
    now: datetime,
    counts: Mapping[str, int],
    busy: Mapping[str, float],
) -> _Option:
    cost, known = _cost(rates, worker, film, stages, now)
    return _Option(
        worker,
        busy.get(worker.worker_id, 0.0) + cost,
        rates.energy_wh(stages, worker, film),
        counts.get(worker.worker_id, 0),
        known,
    )


def _pinned(
    rates: Rates,
    film: m.RoutingFilm,
    stages: tuple[str, ...],
    worker: m.RoutingWorker | None,
    live: Mapping[str, WorkerLive],
    now: datetime,
    counts: Mapping[str, int],
    needs: Mapping[str, float],
    busy: Mapping[str, float],
) -> _Option | str:
    """The pinned worker, or why the film waits; a pin never moves (§4.3)."""
    if worker is None or worker.worker_id not in counts:
        return PIN_GONE
    reason = eligibility(
        worker,
        film,
        stages,
        live.get(worker.worker_id),
        now,
        queued_need=needs[worker.worker_id],
    )
    if reason is None and counts[worker.worker_id] >= MAX_OPEN_ROUTES:
        reason = QUEUE_FULL
    if reason is not None:
        return f"等待 {worker.name}：{reason}"
    return _measure(rates, film, stages, worker, now, counts, busy)


def _options(
    rates: Rates,
    film: m.RoutingFilm,
    stages: tuple[str, ...],
    targets: list[m.RoutingWorker],
    live: Mapping[str, WorkerLive],
    now: datetime,
    counts: Mapping[str, int],
    needs: Mapping[str, float],
    busy: Mapping[str, float],
) -> list[_Option] | str:
    """Every eligible worker with a free slot, or why the film waits."""
    options: list[_Option] = []
    reasons: list[str] = []
    full = False
    for worker in targets:
        reason = eligibility(
            worker,
            film,
            stages,
            live.get(worker.worker_id),
            now,
            queued_need=needs[worker.worker_id],
        )
        if reason == REVOKED:
            continue
        if reason is not None:
            if reason not in reasons:
                reasons.append(reason)
            continue
        if counts[worker.worker_id] >= MAX_OPEN_ROUTES:
            full = True
            continue
        options.append(_measure(rates, film, stages, worker, now, counts, busy))
    if options:
        return options
    if full:
        return f"等待设备：{QUEUE_FULL}"
    return f"等待设备：{'、'.join(reasons[:3]) if reasons else NO_DEVICE}"


def _choose(options: list[_Option]) -> _Option:
    """Earliest finish; inside the tie window lower energy wins (§4.3, D10)."""
    ordered = sorted(
        options, key=lambda o: (o.finish, o.routes, o.worker.name, o.worker.worker_id)
    )
    best = ordered[0]
    window = max(TIE_SECONDS, TIE_SHARE * best.finish)
    tied = [o for o in ordered if o.finish - best.finish <= window]
    if len(tied) > 1 and all(o.energy is not None for o in tied):
        return min(
            tied,
            key=lambda o: (o.energy, o.routes, o.worker.name, o.worker.worker_id),
        )
    return best


def _reason(kind: str, best: _Option, options: list[_Option]) -> str:
    """V1: the chosen device, its predicted finish and the finishes it beat."""
    name = best.worker.name
    if kind == "pinned":
        parts = [f"指定 {name} · 约 {duration_text(best.finish)}后完成"]
    else:
        parts = [f"自动 · 约 {duration_text(best.finish)}后在 {name} 完成"]
        beaten = sorted(
            (o for o in options if o.worker.worker_id != best.worker.worker_id),
            key=lambda o: (o.finish, o.worker.name, o.worker.worker_id),
        )[:3]
        if beaten:
            parts.append(
                "胜过 "
                + "、".join(
                    f"{o.worker.name} 约 {duration_text(o.finish)}" for o in beaten
                )
            )
        if not best.known:
            parts.append(NO_HISTORY)
    energy = _energy_text(best.energy)
    if energy:
        parts.append(energy)
    return " · ".join(parts)
