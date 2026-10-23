"""P2-3 (#294) §3–§5: a GPU worker's dispatcher.

One fetch thread stages the routed films' work copies (position 0 first, then
position 1 within the disk rule, R7); one run thread claims the position-0
route's next stage only when the claim gate holds (R6), runs it through the
shared `run_stage()`, settles it, uploads its outputs and finishes it. Every
step that a crash could interrupt is persisted first in
`staging/attempts/<a>.json` (`request` → `claimed` → `settled` → `finish
sent`), so a restart reports it to the controller's boot settlement (§5) and
never runs a GPU stage twice: a ledger with intact outputs is uploaded and
finished, not re-run. A lost reply is never taken as proof that anything
stopped; only a settled Job is.

`WorkerRuntime` is the executors' `ExecRuntime` here: the staging work folder,
the local GPU, and the earlier stages' ledgers from the route envelope, each
checked against its digest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core import power as power_estimate
from taskpaw_v3.core.generation import next_generation
from taskpaw_v3.core.state import atomic_json

from . import models as m
from .executors import ExecOutcome, StageExecutor
from .local_dispatch import run_stage
from .local_gpu import LocalGpu
from .owned_process import own_identity, process_alive
from .stage_results import RESULT_NAME, StageResult, StageResultError
from .staging import Staging, StagingError
from .timing import merge as merge_timing
from .transfer import ledger_refs
from .worker_client import ApiError, WorkerClient, backoff

log = logging.getLogger("taskpaw.workflow.remote_dispatch")

ATTEMPTS_DIR = "attempts"
INDEX_NAME = "routes.json"
POLL_SECONDS = 5.0
STOP_SETTLE_SECONDS = 30.0
MAX_MISMATCHES = 3  # §3.5: three whole-file mismatches need the operator
STAGES = ("restore", "asr")
SAVE_TRIES = 5  # a file held by antivirus / the indexer for a moment (H1-2)
SAVE_PAUSE = 0.05
_FINISHED = ("succeeded", "skipped")
UNSETTLED = "停止未确认：任务进程仍在运行"
UNKNOWN_GPU = "GPU 有未知占用"
HELD = "上次的任务进程仍在运行"  # §11 CX2: the GPU lease's holder label
UNREADABLE_ATTEMPTS = "有无法读取的任务记录"  # P2-4 §9 CX13: kept, still busy
SWEEP_SECONDS = 60.0  # §11 CX14: orphan work folders are swept this often
REFUSED_DIR = "refused"  # §11 IR3: attempt files the controller refused
MESSAGE_CAP = 2000  # §11 IR3: the finish model's `message` limit
CURRENT_CAP = 32 << 10  # §11 IR11: `current` within the 64 KiB heartbeat
_PERMANENT = frozenset({400, 404, 413, 422})  # §11 IR3: never accepted later
_WATTS = ("w", "gpu_w", "cpu_w", "other_w")


# ── the executors' runtime on a worker ──────────────────────────────────
class WorkerRuntime:
    """`ExecRuntime` over a staging folder and the route envelopes."""

    def __init__(self, staging_root: Path, gpu: LocalGpu | None = None) -> None:
        self._root, self._gpu = staging_root, gpu
        self._lock = threading.Lock()
        self._envelopes: dict[str, Mapping[str, Any]] = {}

    @property
    def work_dir(self) -> Path:
        return self._root / "work"

    @property
    def gpu(self) -> LocalGpu | None:
        return self._gpu

    @gpu.setter
    def gpu(self, value: LocalGpu | None) -> None:
        self._gpu = value

    def use(self, envelope: Mapping[str, Any]) -> None:
        """The envelope a stage of its film runs with (its earlier ledgers)."""
        with self._lock:
            self._envelopes[str(envelope["film"])] = envelope

    def stage_result(self, film_id: str, stage: str) -> StageResult | None:
        with self._lock:
            envelope = self._envelopes.get(film_id)
        if envelope is None:
            raise StageResultError(f"{stage} 阶段没有可核对的结果")
        ledger = (envelope.get("ledgers") or {}).get(stage)
        if ledger is None:
            if stage not in (envelope.get("stages") or {}):
                return None  # the film has no such stage
            raise StageResultError(f"{stage} 阶段尚未成功")
        raw = str(ledger.get("text", "")).encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != ledger.get("payload_digest"):
            raise StageResultError(f"{stage} 阶段结果与记录不符")
        try:
            result = StageResult.model_validate_json(raw)
        except ValueError:
            raise StageResultError(f"{stage} 阶段结果无法读取") from None
        if (
            result.film_id != film_id
            or result.stage != stage
            or result.attempt_id != ledger.get("attempt_id")
            or result.input_revision != envelope.get("rev")
        ):
            raise StageResultError(f"{stage} 阶段结果属于其他影片或版本")
        return result


# ── persisted state ─────────────────────────────────────────────────────
@dataclass
class AttemptState:
    """`attempts/<a>.json`: the crash points of one attempt (§3)."""

    attempt_id: str
    film_id: str
    rev: int
    stage: str
    request: dict[str, Any]  # the exact claim body, written before sending
    name: str = ""
    assignment_id: str | None = None
    children: list[str] = field(default_factory=list)
    settled: dict[str, Any] | None = None  # outcome, digest, timing, energy
    finish_sent: bool = False

    @classmethod
    def load(cls, path: Path) -> AttemptState:
        """Raises on any read or parse failure. P2-4 §9 CX20: only
        `AttemptInventory.scan` reads a record, and it classifies the error."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an attempt record")
        return cls(**data)


@dataclass(frozen=True)
class AttemptInventory:
    """P2-4 §9 CX20–CX22: THE view of an attempts folder — one listing and
    one read per entry — so each decision (the boot report, busy, the held
    check at stop, GC, recovery, delivery) reads one consistent snapshot and
    nothing else lists or reads the folder. `unreadable`: entries that exist
    but did not read or parse (by their report id); `dir_unreadable`: the
    folder could not be listed, so nothing in it is known. A missing folder
    is empty. Unknown is never stopped: an unknown attempt is held and busy,
    and nothing is deleted on an inventory that is not `complete`."""

    states: dict[str, AttemptState] = field(default_factory=dict)
    unreadable: frozenset[str] = frozenset()
    dir_unreadable: bool = False

    @classmethod
    def scan(cls, attempt_dir: Path) -> AttemptInventory:
        try:
            with os.scandir(attempt_dir) as entries:
                names = sorted(e.name for e in entries if e.name != REFUSED_DIR)
        except FileNotFoundError:
            return cls()
        except OSError:
            return cls(dir_unreadable=True)
        states: dict[str, AttemptState] = {}
        unreadable: set[str] = set()
        for name in names:
            stem = name[: -len(".json")] if name.endswith(".json") else ""
            state: AttemptState | None = None
            if stem:
                try:
                    state = AttemptState.load(attempt_dir / name)
                except FileNotFoundError:
                    continue  # gone since the listing: no record any more
                except (OSError, ValueError, TypeError):
                    state = None
            if state is None or state.attempt_id != stem:
                unreadable.add(_report_id(name))
            else:
                states[stem] = state
        return cls(states, frozenset(unreadable))

    @property
    def complete(self) -> bool:
        """Every entry was read: only then may anything be deleted."""
        return not self.dir_unreadable and not self.unreadable

    @property
    def empty(self) -> bool:
        return self.complete and not self.states

    def unknown(self, attempt_id: str) -> bool:
        """Its record may be there but could not be read: held, alive."""
        return self.dir_unreadable or attempt_id in self.unreadable


class StagedIndex:
    """`routes.json`: which work-copy files belong to which film revision."""

    def __init__(self, root: Path) -> None:
        self.path = root / INDEX_NAME
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def entries(self) -> dict[str, Any]:
        with self._lock:
            return self._read()

    def mark(
        self,
        film: str,
        rev: int,
        paths: list[str],
        complete: bool,
        *,
        sizes: Mapping[str, int] | None = None,
        releasing: bool = False,
    ) -> None:
        """`sizes`: each path's verified size, kept from the last mark when
        not given; `releasing` (§11 IR2): being deleted, never claimable."""
        with self._lock:
            data = self._read()
            old = data.get(f"{film}/{rev}") or {}
            data[f"{film}/{rev}"] = {
                "film": film,
                "rev": rev,
                "paths": paths,
                "complete": complete and not releasing,
                "releasing": releasing,
                "sizes": dict(sizes) if sizes is not None else old.get("sizes", {}),
            }
            atomic_json(self.path, data)

    def complete(self, film: str, rev: int) -> bool:
        entry = self.entries().get(f"{film}/{rev}")
        return bool(entry and entry.get("complete") and not entry.get("releasing"))

    def begin_release(self, film: str, rev: int) -> list[str]:
        """§11 IR2: no longer complete before the first file is deleted."""
        with self._lock:
            data = self._read()
            entry = data.get(f"{film}/{rev}")
            if entry is None:
                return []
            entry.update(complete=False, releasing=True)
            atomic_json(self.path, data)
        return list(entry.get("paths", []))

    def remove(self, film: str, rev: int) -> list[str]:
        with self._lock:
            data = self._read()
            entry = data.pop(f"{film}/{rev}", None)
            atomic_json(self.path, data)
        return list(entry.get("paths", [])) if entry else []


@dataclass
class _Active:
    """The attempt the run thread holds (claimed, running or delivering)."""

    state: AttemptState
    started: float  # monotonic, for `elapsed_s`
    uses_gpu: bool
    cancel: threading.Event = field(default_factory=threading.Event)
    phase: str = "running"
    progress: str = ""
    ltx: dict[str, Any] | None = None
    energy_start: Any = None


@dataclass
class Transfer:
    film: str
    dir: str
    done: int
    total: int
    started: float
    rate: float = 0.0


def _report_id(name: str) -> str:
    """§9 CX18: the boot report id of an unreadable attempts-folder entry —
    its file name when that is an attempt id, else a stable id derived from
    the name (a valid boot body either way)."""
    stem = name[: -len(".json")] if name.endswith(".json") else None
    if stem is not None and _is_uuid(stem):
        return stem
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"taskpaw-attempt:{name}"))


def worker_busy(
    roots: Path | Iterable[Path | None] | None,
    dispatcher: RemoteDispatcher | None,
) -> bool:
    """P2-4 R9, §9 CX13: THE worker-busy rule — `/status`, the settings guard
    and keep-awake all read this. Busy while the dispatcher holds a run, an
    unsettled Job, an attempt to deliver or a held child, or while the
    attempts folder of any staging folder the worker may be using (`roots`
    and the dispatcher's own, §9 CX21) holds anything: a record, an entry
    that does not read, or a folder that cannot be listed (unknown is busy;
    only the `refused/` archive and a missing folder are idle)."""
    if dispatcher is not None and dispatcher.holds_work():
        return True
    listed = [roots] if roots is None or isinstance(roots, Path) else list(roots)
    if dispatcher is not None:
        listed.append(dispatcher.staging.root)
    return any(
        not AttemptInventory.scan(root / ATTEMPTS_DIR).empty
        for root in _distinct(listed)
    )


def _distinct(roots: Iterable[Path | None]) -> list[Path]:
    """The staging folders, each once (by filesystem identity when it can be
    read, else by its normalized path — a duplicate is only checked twice)."""
    seen: set[object] = set()
    kept = []
    for root in roots:
        if root is None:
            continue
        key: object = os.path.normcase(os.path.abspath(root))
        try:
            st = os.stat(root)
        except OSError:
            pass
        else:
            if st.st_ino:
                key = (st.st_dev, st.st_ino)
        if key not in seen:
            seen.add(key)
            kept.append(root)
    return kept


# ── the dispatcher ──────────────────────────────────────────────────────
class RemoteDispatcher:
    def __init__(
        self,
        staging: Staging,
        client: Callable[[], WorkerClient],
        executors: Mapping[str, StageExecutor],
        runtime: WorkerRuntime,
        *,
        boot_id: str,
        lease: Any = gpu_lease,
        power: Callable[[], Any] | None = None,
        poll: float = POLL_SECONDS,
        stop_settle: float = STOP_SETTLE_SECONDS,
        backoff_base: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.staging, self._client = staging, client
        self.executors, self.runtime = dict(executors), runtime
        self.boot_id, self.lease = boot_id, lease
        self.power = power or power_estimate.get_sampler
        self.poll, self.stop_settle = poll, stop_settle
        self.backoff_base, self._clock = backoff_base, clock
        # never equal to a manual task's RunId (C5); the sampler reads it (R14)
        self.run_id: gpu_lease.RunId = ("workflow", next_generation())
        self.attempt_dir = staging.root / ATTEMPTS_DIR
        self.index = StagedIndex(staging.root)
        self._lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._threads: list[threading.Thread] = []
        # what the heartbeat last said (the gate's inputs)
        self._routes: list[dict[str, Any]] = []
        self._reconciled = False
        self._paused = False
        self._tiers: tuple[str, ...] = ()
        self._asr_ready = False
        self._hold = "none"
        self._held: set[str] = set()  # boot: a child of an earlier boot runs
        # §9 CX20: resolved attempts whose records were not collected because
        # their inventory was not complete; collected by the next full sweep
        self._resolved: set[str] = set()
        self._deliver: list[str] = []  # attempts to upload/finish (boot: finish)
        self._cancelled: set[str] = set()
        self._active: _Active | None = None
        self._unsettled: tuple[_Active, ExecOutcome, Any] | None = None
        self._holding = False
        self._lease_lock = threading.Lock()  # `_holding` and the lease calls
        self._tick_lock = threading.Lock()  # held by the run thread per tick
        # §11 CX10: the final pause check and the claim's first send are one step
        self._admission = threading.Lock()
        # §11 CX6: settled outcomes whose file never landed (retried first)
        self._unsaved: dict[str, AttemptState] = {}
        self._started: dict[str, float] = {}  # §11 IR8: claim time per attempt
        self._fetching: str | None = None
        self._swept = clock()  # §11 CX14: the last periodic orphan sweep
        self._transfers: dict[tuple[str, str], Transfer] = {}
        self._envelopes: dict[tuple[str, int], dict[str, Any]] = {}
        self.reason = "idle"
        self.notice: str | None = None  # §11 IR3: what needs the operator

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._threads:
            return
        self.attempt_dir.mkdir(parents=True, exist_ok=True)
        for target, name in (
            (self._fetch_loop, "workflow-worker-fetch"),
            (self._run_loop, "workflow-worker-run"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 60.0) -> bool:
        """Cancel the running Job, let it settle (its outcome is persisted,
        never finished on the way out) and join both threads. §11 CX1: False
        while the Job's processes survive — they keep the GPU, and a later
        call settles them again (C4)."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            active = self._active
        if active is not None:
            active.cancel.set()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in self._threads):
            return False
        if self._unsettled is not None and not self._settle_now(
            max(0.0, deadline - time.monotonic())
        ):
            return False
        if self._held_alive():  # §11 CX13: they keep the GPU, unconfirmed
            self.reason = HELD
            return False
        self._release(force=True)
        return True

    def _held_alive(self) -> bool:
        """§11 CX13: a child of an earlier boot (a `held` verdict) still runs.
        P2-4 §9 CX18/CX22: a held record that cannot be read, or held in a
        folder that cannot be listed, is unknown, which is alive; only a
        record that is gone, or read with its children dead, is not."""
        with self._lock:
            held = list(self._held)
        if not held:
            return False
        inventory = self._inventory()
        for attempt_id in held:
            if inventory.unknown(attempt_id):
                return True
            state = inventory.states.get(attempt_id)
            if state is not None and any(process_alive(c) for c in state.children):
                return True
        return False

    @property
    def halted(self) -> bool:
        """`stop()` was called: this dispatcher never runs again."""
        return self._stop.is_set()

    def rebind(self, gpu: LocalGpu, factory: Callable[[WorkerRuntime], Any]) -> bool:
        """§11 CX4: the executors for a GPU read after start, only between
        attempts; False (tried again at the next probe) while one runs."""
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                busy = self._active is not None or self._unsettled is not None
            if busy:
                return False
            self.runtime.gpu = gpu
            self.executors = {e.stage: e for e in factory(self.runtime)}
            return True
        finally:
            self._tick_lock.release()

    def wake(self) -> None:
        self._wake.set()

    # ── what the heartbeat thread reads and writes ───────────────────────
    def observe(
        self,
        reply: Mapping[str, Any],
        *,
        reconciled: bool,
        tiers: Iterable[str],
        asr_ready: bool,
        hold: str,
        paused: bool,
    ) -> None:
        """A heartbeat reply: routes, cancel (R9), release (R10) and the
        claim gate's other inputs (R6)."""
        with self._lock:
            self._routes = sorted(
                reply.get("routes") or [], key=lambda r: r.get("position", 0)
            )
            self._reconciled = reconciled
            self._paused = paused or bool(reply.get("paused"))
            self._tiers, self._asr_ready = tuple(tiers), asr_ready
            self._hold = hold
            active = self._active
        for attempt_id in reply.get("cancel") or []:
            self._cancel(str(attempt_id), active)
        for item in reply.get("release") or []:
            self._release_film(str(item["film"]), int(item["rev"]))
        self._wake.set()

    def acknowledge_pause(self) -> None:
        """§11 CX10: returns once no claim can start any more. Called after
        `observe(paused=True)`: an admission that read `paused` earlier either
        has its first claim already sent or sees the flag and sends nothing."""
        with self._admission:
            pass

    def _cancel(self, attempt_id: str, active: _Active | None) -> None:
        """(a) 停止并重新排队 → the Job stops, the finish reports `cancelled`;
        (b) a stale attempt → the same, and the controller's 409 discards it."""
        with self._lock:
            self._cancelled.add(attempt_id)
        if active is not None and active.state.attempt_id == attempt_id:
            active.cancel.set()
        log.info("worker attempt %s cancelled by the controller", attempt_id)

    def holds_work(self) -> bool:
        """In memory: a run, an unsettled Job, an attempt to deliver or a
        held child (the disk half is `worker_busy`'s inventories)."""
        with self._lock:
            return bool(
                self._active is not None
                or self._unsettled is not None
                or self._deliver
                or self._held
            )

    def claimed(self) -> bool:
        """An unresolved claim is held (keep-awake, R11): `worker_busy`."""
        return worker_busy(self.staging.root, self)

    def current_stage(self) -> tuple[str, str] | None:
        """(film_id, stage) of the running stage, for the power sampler (R14)."""
        with self._lock:
            active = self._active
        if active is None or active.phase != "running":
            return None
        return active.state.film_id, active.state.stage

    def current(self) -> dict[str, Any] | None:
        """The heartbeat's typed `current` (§2)."""
        with self._lock:
            active = self._active
        if active is None:
            return None
        value: dict[str, Any] = {
            "film_id": active.state.film_id,
            "stage": active.state.stage,
            "attempt_id": active.state.attempt_id,
            "phase": active.phase,
            "progress": active.progress[-400:],
            "elapsed_s": min(999_999.0, max(0.0, self._clock() - active.started)),
            "ltx": active.ltx,
            "energy": self._energy_dict(active),
            "power": _watts(self._live_power()) if active.uses_gpu else None,
            "uses_gpu": active.uses_gpu,
        }
        return _bounded(value)

    def transfers(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._transfers.values())
        return [
            {
                "film": t.film,
                "dir": t.dir,
                "done": t.done,
                "total": t.total,
                "rate": round(t.rate, 1),
            }
            for t in items
        ][:8]

    def staged(self) -> list[dict[str, Any]]:
        """Staged revisions, those being released included (§11 IR2): the
        controller names them in `release` again until their files are gone.
        §11 CX8: a download cut short is reported too, flagged `incomplete`,
        so a route that moved on can release it; a fetch under way is never
        deleted (`_release_film` leaves it and is asked again)."""
        return [
            {"film": e["film"], "rev": e["rev"]}
            if e.get("complete") or e.get("releasing")
            else {"film": e["film"], "rev": e["rev"], "incomplete": True}
            for e in self.index.entries().values()
        ][:64]

    def films(self) -> list[dict[str, Any]]:
        """The worker monitor's routed and claimed films (§6)."""
        with self._lock:
            routes = list(self._routes)
            active = self._active
            envelopes = dict(self._envelopes)
            fetching = self._fetching
        rows = []
        for route in routes:
            key = (str(route["film"]), int(route["rev"]))
            envelope = envelopes.get(key) or {}
            state = "queued"
            if fetching == f"{key[0]}/{key[1]}":
                state = "fetching"
            elif self.index.complete(*key):
                state = "staged"
            if active is not None and active.state.film_id == key[0]:
                state = active.phase
            rows.append(
                {
                    "film": key[0],
                    "rev": key[1],
                    "name": envelope.get("name") or key[0],
                    "position": route.get("position", 0),
                    "state": state,
                    "stages": {
                        name: value.get("status")
                        for name, value in (envelope.get("stages") or {}).items()
                    },
                }
            )
        return rows

    # ── boot (R8, §5) ─────────────────────────────────────────────────────
    def _inventory(self) -> AttemptInventory:
        """§9 CX20: the one way this dispatcher reads its attempts folder."""
        return AttemptInventory.scan(self.attempt_dir)

    def _gc(self, attempt_id: str, inventory: AttemptInventory) -> None:
        """§9 CX20: a resolved attempt is collected only on a complete
        inventory; otherwise the next complete sweep collects it."""
        if inventory.complete:
            self._collect(attempt_id)
        else:
            with self._lock:
                self._resolved.add(attempt_id)

    def _path(self, attempt_id: str) -> Path:
        return self.attempt_dir / f"{attempt_id}.json"

    def _save(self, state: AttemptState) -> bool:
        """Write the attempt file; a sharing violation (antivirus, the
        indexer) is retried briefly. False when it never landed: the caller
        decides (the request must be on disk before the claim is sent)."""
        for attempt in range(SAVE_TRIES):
            try:
                self.attempt_dir.mkdir(parents=True, exist_ok=True)
                atomic_json(self._path(state.attempt_id), asdict(state))
                return True
            except OSError:
                if attempt + 1 == SAVE_TRIES:
                    log.exception(
                        "worker attempt file %s not written", state.attempt_id
                    )
                    return False
                time.sleep(SAVE_PAUSE * (attempt + 1))
        return False

    def recover(self, client: WorkerClient) -> bool:
        """At start, before the first boot: an attempt the controller does
        not know (404: the claim never landed) or has resolved (the finish
        reply was lost) is collected, its work folder included (§11 IR5). The
        rest go to the boot. Once every file was answered, work folders no
        attempt file names are swept; an error leaves everything (unknown).
        P2-4 §9 CX13: a file that does not parse is logged and kept (busy
        until the operator resolves it), and named in `notice`. §9 CX20: one
        inventory; nothing is deleted while it is not complete."""
        inventory = self._inventory()
        if not inventory.complete:
            log.error(
                "worker attempt files kept unread in %s: %s",
                ATTEMPTS_DIR,
                "the folder"
                if inventory.dir_unreadable
                else ", ".join(sorted(inventory.unreadable)),
            )
            self.notice = UNREADABLE_ATTEMPTS
        for state in inventory.states.values():
            try:
                found = client.attempt(state.attempt_id)
            except ApiError as exc:
                if exc.status == 404:
                    self._gc(state.attempt_id, inventory)
                    continue
                raise
            if found.get("state") == "resolved":
                self._gc(state.attempt_id, inventory)
        self._sweep_orphans(inventory)
        return True

    def _sweep_due(self) -> None:
        """§11 CX14: a deletion that failed (Windows: an output still open)
        left a folder no attempt file names; it is swept again while idle."""
        now = self._clock()
        if now - self._swept >= SWEEP_SECONDS:
            self._swept = now
            self._sweep_orphans()

    def _sweep_orphans(self, inventory: AttemptInventory | None = None) -> None:
        """§11 IR5: `work/<uuid>/` folders no attempt file (or the active
        attempt) names; work copies (`copies/…`) are never touched. Skipped
        while a tick runs: a claim it makes meanwhile is never swept. §9
        CX18/CX20: on an inventory that is not complete (an entry may name
        any folder) nothing is swept; a complete one also collects the
        resolved attempts an incomplete one deferred."""
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            if inventory is None:
                inventory = self._inventory()
            if not inventory.complete:
                return
            with self._lock:
                resolved = sorted(self._resolved)
            for attempt_id in resolved:
                self._collect(attempt_id)
            known = set(inventory.states) - set(resolved)
            with self._lock:
                if self._active is not None:
                    known.add(self._active.state.attempt_id)
                known.update(self._unsaved)  # §11 CX6: file not landed yet
            try:
                folders = list(self.staging.work_dir.iterdir())
            except OSError:
                return
            for folder in folders:
                if (
                    folder.is_dir()
                    and _is_uuid(folder.name)
                    and folder.name not in known
                ):
                    log.info("orphan worker work folder %s removed", folder.name)
                    shutil.rmtree(folder, ignore_errors=True)
        finally:
            self._tick_lock.release()

    def reports(self) -> list[dict[str, Any]]:
        """§5: each local attempt file as found now. P2-4 §9 CX18: an entry
        that cannot be read (a file raising on read, one that does not parse,
        an unreadable folder) is reported too, as `unreadable` with its
        children unknown (alive), so the controller holds the boot instead
        of settling an unreported assignment as interrupted. The read is
        tried again at every report (each boot re-post). §9 CX20: one
        inventory, one read per entry — a read that fails once is reported
        unreadable, never left out because a second read worked."""
        values = []
        inventory = self._inventory()
        unknown = sorted(inventory.unreadable)
        if inventory.dir_unreadable:  # an unknown inventory: the boot holds
            unknown = [_report_id(ATTEMPTS_DIR)]
        # every file, the one being delivered included: a re-post that left
        # it out would let the controller settle it as interrupted and run
        # the stage again (the controller ignores this boot's own claims)
        for state in inventory.states.values():
            ledger = self.staging.work_dir / state.attempt_id / RESULT_NAME
            settled = state.settled
            values.append(
                {
                    "attempt_id": state.attempt_id,
                    "assignment_id": state.assignment_id,
                    "children_alive": any(process_alive(c) for c in state.children),
                    "ledger": ledger.is_file(),
                    "outputs_intact": self._intact(state),
                    "outcome": None
                    if settled is None
                    else ("succeeded" if settled.get("ok") else "failed"),
                    "unreadable": False,
                }
            )
        for attempt_id in unknown:
            values.append(
                {
                    "attempt_id": attempt_id,
                    "assignment_id": None,
                    "children_alive": True,  # unknown is never stopped
                    "ledger": False,
                    "outputs_intact": False,
                    "outcome": None,
                    "unreadable": True,
                }
            )
        return values

    def _intact(self, state: AttemptState) -> bool:
        """The ledger parses and every output it names is there in full."""
        ledger = self.staging.work_dir / state.attempt_id / RESULT_NAME
        try:
            result = StageResult.model_validate_json(ledger.read_bytes())
        except (OSError, ValueError):
            return False
        for ref in ledger_refs(result):
            path = self.staging.work_dir / ref.path
            try:
                if path.stat().st_size != ref.size:
                    return False
            except OSError:
                return False
        return True

    def booted(self, reply: Mapping[str, Any]) -> None:
        """The boot reply: interrupted attempts are collected, held ones wait,
        `finish` ones are delivered without a re-run; staged sources missing
        from `keep` go (nothing while an attempt is unknown: those are kept).
        P2-4 §9 CX18/CX20: a record that cannot be read now (or sits in a
        folder that cannot be listed) is never collected, whatever the
        verdict: it is held; on an incomplete inventory nothing is deleted
        (an interrupted record is collected by the next complete sweep)."""
        verdicts = dict(reply.get("attempts") or {})
        held, deliver = set(), []
        inventory = self._inventory()
        for attempt_id, verdict in verdicts.items():
            if verdict == "held" or inventory.unknown(attempt_id):
                held.add(attempt_id)
            elif verdict == "interrupted":
                self._gc(attempt_id, inventory)
            elif verdict == "finish":
                state = inventory.states.get(attempt_id)
                if state is not None:
                    self._prepare_finish(state)
                    deliver.append(attempt_id)
        # §11 IR1: a settled local attempt is delivered whatever the reply
        # says (finish is idempotent by state; a stale one answers 409)
        with self._lock:
            active = self._active.state.attempt_id if self._active else None
        for state in inventory.states.values():
            if (
                state.settled is not None
                and verdicts.get(state.attempt_id) not in ("interrupted", "held")
                and state.attempt_id not in deliver
                and state.attempt_id != active
            ):
                deliver.append(state.attempt_id)
        with self._lock:
            self._held = held
            for attempt_id in deliver:
                if attempt_id not in self._deliver:
                    self._deliver.append(attempt_id)
            self._reconciled = bool(reply.get("reconciled"))
        if held:  # §11 CX2: the GPU stays reserved until they are gone
            unknown = any(inventory.unknown(a) for a in held)
            self._acquire(UNREADABLE_ATTEMPTS if unknown else HELD)
        if "keep" in reply:
            keep = {(str(k["film"]), int(k["rev"])) for k in reply.get("keep") or []}
            for entry in list(self.index.entries().values()):
                key = (str(entry["film"]), int(entry["rev"]))
                if key not in keep:
                    self._release_film(*key)
        self._wake.set()

    def _prepare_finish(self, state: AttemptState) -> None:
        """A ledger found after a restart: settled from the ledger itself
        (no energy: the sampler restarted, R14); the timing is the ledger's
        restore timings (§11 IR9)."""
        if state.settled is not None:
            return
        ledger = self.staging.work_dir / state.attempt_id / RESULT_NAME
        try:
            raw = ledger.read_bytes()
        except OSError:
            state.settled = _settled(ExecOutcome.failed("needs_attention", "结果缺失"))
        else:
            state.settled = _settled(
                ExecOutcome.succeeded(
                    "", hashlib.sha256(raw).hexdigest(), _ledger_timing(raw)
                ),
                None,
            )
        self._save(state)

    # ── GC (R10) ─────────────────────────────────────────────────────────
    def _drop(self, attempt_id: str) -> None:
        try:
            self._path(attempt_id).unlink()
        except FileNotFoundError:
            pass

    def _collect(self, attempt_id: str) -> None:
        """A resolved attempt: its work folder and its file go."""
        shutil.rmtree(self.staging.work_dir / attempt_id, ignore_errors=True)
        self._drop(attempt_id)
        with self._lock:
            self._cancelled.discard(attempt_id)
            self._held.discard(attempt_id)
            self._resolved.discard(attempt_id)
            self._unsaved.pop(attempt_id, None)
            self._started.pop(attempt_id, None)

    def _release_film(self, film: str, rev: int) -> None:
        """Delete a staged film revision's source files — never while an
        attempt of it is local or it is being fetched. §11 IR2: the entry is
        marked releasing first, so no claim ever sees it complete again."""
        with self._lock:
            if self._fetching == f"{film}/{rev}":
                return
            active = self._active
        if active is not None and active.state.film_id == film:
            return
        inventory = self._inventory()  # §9 CX20: unknown may be of this film
        if not inventory.complete or any(
            s.film_id == film and s.rev == rev for s in inventory.states.values()
        ):
            return
        root = self.staging.work_dir
        kept = []
        # CX11: a path another staged or downloading revision lists (a folder
        # cover shared by one intake package) stays until its last holder goes
        shared = {
            relative
            for key, other in self.index.entries().items()
            if key != f"{film}/{rev}" and isinstance(other, dict)
            for relative in other.get("paths", [])
        }
        for relative in self.index.begin_release(film, rev):
            if relative in shared:
                continue
            for path in (root / relative, root / f"{relative}.part"):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:  # held open: kept, released again later
                    log.warning("staged file %s could not be deleted", relative)
                    kept.append(relative)
            _prune(root, (root / relative).parent)
        if kept:  # still releasing: the next release (or boot) deletes it
            self.index.mark(film, rev, sorted(set(kept)), False, releasing=True)
            return
        self.index.remove(film, rev)
        with self._lock:
            self._envelopes.pop((film, rev), None)
        log.info("released the staged source of %s/%s", film, rev)

    def _staged_ok(self, film: str, rev: int) -> bool:
        """§11 IR2: complete in the index and every file still there at its
        verified size; a missing or changed one is fetched again first."""
        entry = self.index.entries().get(f"{film}/{rev}")
        if not entry or not entry.get("complete") or entry.get("releasing"):
            return False
        sizes = entry.get("sizes") or {}
        for relative in entry.get("paths", []):
            try:
                size = (self.staging.work_dir / relative).stat().st_size
            except OSError:
                size = -1
            if size < 0 or (relative in sizes and size != int(sizes[relative])):
                log.warning(
                    "staged file %s is gone or changed; fetching again", relative
                )
                self.index.mark(film, rev, list(entry["paths"]), False)
                return False
        return True

    # ── fetch (R7) ───────────────────────────────────────────────────────
    def _fetch_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                worked = self._fetch_once()
                failures = 0
            except ApiError as exc:
                if not exc.transient and exc.status != 404:
                    log.warning("worker fetch refused: %s", exc)
                failures += 1
                worked = False
            except Exception:
                log.exception("worker fetch failed")
                failures += 1
                worked = False
            if not worked:
                delay = (
                    backoff(failures - 1, base=self.backoff_base)
                    if failures
                    else self.poll
                )
                self._stop.wait(delay)

    def _fetch_once(self) -> bool:
        """Position 0 first, then position 1; one film per pass so a newly
        routed position 0 is never stuck behind a prefetch. A download error
        never creates an attempt."""
        with self._lock:
            routes = list(self._routes[:2])
        client = self._client()
        for route in routes:
            film, rev = str(route["film"]), int(route["rev"])
            if self.index.complete(film, rev):
                continue
            envelope = client.envelope(film, rev)
            with self._lock:
                self._envelopes[film, rev] = envelope
            sizes = {
                str(c["relative_path"]): int(c["size"]) for c in envelope["workcopy"]
            }
            paths = list(sizes)
            self.index.mark(film, rev, paths, complete=False, sizes=sizes)
            key = f"{film}/{rev}"
            with self._lock:
                self._fetching = key
            total = sum(int(c["size"]) for c in envelope["workcopy"])
            started = self._clock()
            self._progress(film, "down", 0, total, started)
            try:
                self.staging.work_dir.mkdir(parents=True, exist_ok=True)
                client.download_film(
                    envelope,
                    self.staging,
                    cancel=self._stop,
                    on_progress=lambda done, t: self._progress(
                        film, "down", done, t, started
                    ),
                )
                # §11 IR2: complete while still `_fetching`, so a release
                # never deletes the files between the download and the mark
                self.index.mark(film, rev, paths, complete=True, sizes=sizes)
            except StagingError as exc:
                self.reason = str(exc)  # 空间不足: try the next position
                continue
            finally:
                with self._lock:
                    self._fetching = None
                    self._transfers.pop((film, "down"), None)
            self._wake.set()
            return True
        return False

    def _progress(
        self, film: str, direction: str, done: int, total: int, started: float
    ) -> None:
        elapsed = max(1e-6, self._clock() - started)
        with self._lock:
            self._transfers[film, direction] = Transfer(
                film, direction, done, total, started, done / elapsed
            )

    # ── run (R6) ─────────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with self._tick_lock:
                    worked = self._tick()
            except Exception:
                log.exception("worker dispatch tick failed")
                worked = False
            if not worked and not self._stop.is_set():
                self._sweep_due()
                self._wake.wait(self.poll)
                self._wake.clear()
        if self._unsettled is None:  # a still-running tree keeps the GPU (C4)
            self._release()

    def _tick(self) -> bool:
        if self._unsettled is not None:
            return self._finish_unsettled()
        with self._lock:
            pending = list(self._deliver)
        if pending:
            return self._deliver_one(pending[0])
        return self._admit()

    def _gate(self) -> tuple[dict[str, Any], str] | None:
        """R6: the position-0 route and its next stage, or None (the reason
        is kept for the monitor)."""
        with self._lock:
            routes, reconciled = list(self._routes), self._reconciled
            paused, hold, held = self._paused, self._hold, set(self._held)
            tiers, asr_ready = self._tiers, self._asr_ready
        if not routes:
            self.reason = "idle"
            return None
        if not reconciled or held:
            self.reason = "等待对账"
            return None
        if paused:
            self.reason = "暂停领取"
            return None
        if hold == "unknown":
            self.reason = UNKNOWN_GPU
            return None
        route = routes[0]
        film, rev = str(route["film"]), int(route["rev"])
        if not self._staged_ok(film, rev):
            self.reason = "等待源文件"
            return None
        envelope = self._client().envelope(film, rev)  # fresh job versions
        with self._lock:
            self._envelopes[film, rev] = envelope
        stages = envelope.get("stages") or {}
        stage = next(
            (
                name
                for name in STAGES
                if (stages.get(name) or {}).get("status") == "pending"
                and all(
                    (stages.get(before) or {}).get("status") in (*_FINISHED, None)
                    for before in STAGES[: STAGES.index(name)]
                )
            ),
            None,
        )
        if stage is None or stage not in self.executors:
            self.reason = "等待主控" if stage is None else "没有可用的执行器"
            return None
        if stage == "restore" and envelope.get("ltx_model") not in (*tiers, None):
            self.reason = "缺少画质模型"
            return None
        if stage == "asr" and not asr_ready:
            self.reason = "识别未就绪"
            return None
        return envelope, stage

    def _admit(self) -> bool:
        try:
            gate = self._gate()
        except ApiError as exc:  # the envelope read: shown, tried again next tick
            self.reason = (
                "无法连接主控" if exc.transient else f"主控拒绝了请求（{exc.code}）"
            )
            self._release()
            return False
        if gate is None:
            self._release()
            return False
        envelope, stage = gate
        label = f"影片工作流：{envelope.get('name') or envelope['film']}"
        if not self._acquire(label):
            self.reason = "等待 GPU：" + (
                self.lease.blocking_label()
                if hasattr(self.lease, "blocking_label")
                else ""
            )
            return False
        return self._claim_and_run(envelope, stage)

    def _job(self, envelope: Mapping[str, Any], stage: str) -> m.PendingComputeJob:
        record = envelope["stages"][stage]
        return m.PendingComputeJob(
            job_id=record["job_id"],
            film_id=envelope["film"],
            display_name=envelope.get("name") or envelope["film"],
            stage=stage,  # type: ignore[arg-type]
            input_revision=envelope["rev"],
            version=record["version"],
            film_created_at=datetime.now(timezone.utc),
            ltx_model=envelope.get("ltx_model") if stage == "restore" else None,
        )

    def _claim_and_run(self, envelope: Mapping[str, Any], stage: str) -> bool:
        executor = self.executors[stage]
        try:  # everything that can refuse runs before the claim
            job = self._job(envelope, stage)
            manifest = m.InputManifest.model_validate(envelope["manifest"])
            tools = executor.tools(job)
        except Exception:
            log.exception("worker cannot prepare %s of %s", stage, envelope["film"])
            self._release()
            self.reason = "主控的影片信息无法读取"
            return False
        attempt_id = str(uuid.uuid4())
        state = AttemptState(
            attempt_id=attempt_id,
            film_id=job.film_id,
            rev=job.input_revision,
            stage=stage,
            name=job.display_name,
            request={
                "attempt_id": attempt_id,
                "operation_id": str(uuid.uuid4()),
                "job_id": job.job_id,
                "expected_version": job.version,
                "boot_id": self.boot_id,
                "tool_versions": tools.model_dump(mode="json"),
            },
        )
        if not self._save(state):  # crash point `request`: before sending
            self._release()
            self.reason = "无法写入任务记录（暂存目录）"
            return False
        claimed = self._send_claim(state)
        if claimed is None:
            return False
        state.assignment_id = claimed.get("assignment_id")
        self._save(state)  # crash point `claimed`
        self._renew_once(state)
        uses_gpu = True
        active = _Active(state, self._clock(), uses_gpu)
        active.energy_start = self._energy_reading(job.film_id)
        with self._lock:
            self._active = active
            self._started[attempt_id] = active.started
        self.runtime.use(envelope)
        if attempt_id in self._cancelled or self._stop.is_set():
            active.cancel.set()

        def launched(identity: str) -> None:
            state.children.append(identity)
            self._save(state)

        def progress(text: str, ltx: dict[str, Any] | None = None) -> None:
            active.progress = text[-200:]
            active.ltx = ltx

        self.reason = "running"
        result = run_stage(
            executor,
            job,
            manifest,
            self.staging.work_dir / attempt_id,
            active.cancel,
            on_launch=launched,
            progress=progress,
            stop_settle=self.stop_settle,
        )
        if not result.settled:  # C4: the lease stays, settle again each tick
            active.phase = "settling"
            self._unsettled = (active, result.outcome, result.processes)
            self.reason = UNSETTLED
            return True
        self._settle(active, result.outcome)
        return True

    def _send_claim(self, state: AttemptState) -> dict[str, Any] | None:
        """The same bytes until a definite answer (C2 replay); a refusal drops
        the attempt file and frees the GPU."""
        failures, first = 0, True
        while not self._stop.is_set():
            try:
                if first:  # §11 CX10: re-check under the admission lock
                    first = False
                    with self._admission:
                        with self._lock:
                            paused = self._paused
                        if paused:
                            self._drop(state.attempt_id)
                            self._release()
                            self.reason = "暂停领取"
                            return None
                        return self._client().claim(state.request)
                return self._client().claim(state.request)
            except ApiError as exc:
                if exc.transient:
                    failures += 1
                    self._stop.wait(backoff(failures - 1, base=self.backoff_base))
                    continue
                self._drop(state.attempt_id)
                self._release()
                self.reason = f"领取失败：{exc.code}"
                return None
        return None  # stopped: the file stays; the next start asks the controller

    def _renew_once(self, state: AttemptState) -> None:
        """C5: one renew records the owner; a failure never stops the Job."""
        try:
            self._client().renew(
                state.attempt_id,
                {
                    "owner_id": str(uuid.uuid4()),
                    "generation": 1,
                    "boot_id": self.boot_id,
                    "process_identity": own_identity(),
                },
            )
        except ApiError as exc:
            log.warning("worker attempt renew failed: %s", exc)

    def _finish_unsettled(self) -> bool:
        return self._settle_now(0.0)

    def _settle_now(self, budget: float) -> bool:
        """C4: settle the unsettled Job again; True once its processes are
        gone and its outcome is recorded."""
        assert self._unsettled is not None
        active, outcome, processes = self._unsettled
        if processes is not None and not processes.settle(budget):
            self.reason = UNSETTLED
            return False
        self._unsettled = None
        self._settle(active, outcome)
        return True

    def _settle(self, active: _Active, outcome: ExecOutcome) -> None:
        """Crash point `settled`: the outcome, ledger digest, timing and the
        film's energy since the claim (R14) are persisted before any upload.
        §11 CX6: a write that never lands keeps the outcome in memory; it is
        written again before the delivery, which waits for it."""
        state = active.state
        energy = self._energy(active)
        state.settled = _settled(outcome, energy)
        saved = self._save(state)
        with self._lock:
            if not saved:
                self._unsaved[state.attempt_id] = state
            self._active = None
            if not self._stop.is_set() and state.attempt_id not in self._deliver:
                self._deliver.insert(0, state.attempt_id)
        if self._stop.is_set():  # shut down: the next start finishes it
            return
        self._deliver_one(state.attempt_id)

    # ── upload and finish ────────────────────────────────────────────────
    def _deliver_one(self, attempt_id: str) -> bool:
        """Upload (success only) then finish, retried with backoff until a
        definite answer; the GPU stays held until the finish resolves (D3)."""
        with self._lock:
            unsaved = self._unsaved.get(attempt_id)
            started = self._started.get(attempt_id)
        if unsaved is not None:  # §11 CX6: durable before anything is sent
            if not self._save(unsaved):
                self.reason = "无法写入任务记录（暂存目录），正在重试"
                return False
            with self._lock:
                self._unsaved.pop(attempt_id, None)
        state = unsaved
        if state is None:
            inventory = self._inventory()
            state = inventory.states.get(attempt_id)
            if state is None and inventory.unknown(attempt_id):
                self.reason = UNREADABLE_ATTEMPTS  # §9 CX20: kept, tried again
                return False
        if state is None or state.settled is None:
            with self._lock:
                self._deliver = [a for a in self._deliver if a != attempt_id]
            return True
        # §11 IR8: 用时 goes on from the claim (a boot finish starts now)
        active = _Active(
            state,
            self._clock() if started is None else started,
            False,
            phase="uploading",
        )
        with self._lock:
            self._active = active
        failures = 0
        try:
            while not self._stop.is_set():
                body: dict[str, Any] | None = None
                try:
                    self._check_result(state)
                    body = self._finish_body(state, active)
                    active.phase = "finishing"
                    state.finish_sent = True
                    self._save(state)  # crash point `finish sent`
                    self._client().finish(attempt_id, body)
                    self._done(attempt_id)
                    return True
                except ApiError as exc:
                    if exc.status == 409 and exc.code == "stale_attempt":
                        log.info("worker attempt %s is stale; discarded", attempt_id)
                        self._done(attempt_id)
                        return True
                    failing = body is not None and body.get("outcome") == "failed"
                    if exc.status == 422 and state.settled.get("ok") and not failing:
                        state.settled = _settled(
                            ExecOutcome.failed(
                                "needs_attention", f"主控拒绝了结果（{exc.code}）"
                            ),
                            state.settled.get("energy"),
                        )
                        self._save(state)
                        continue
                    if failing and exc.status in _PERMANENT:  # §11 IR3
                        self._refused(state, exc)
                        return True
                    if not exc.transient and exc.status != 422:
                        log.warning("worker finish refused: %s", exc)
                    failures += 1
                    self.reason = "回传失败，正在重试"
                    if self._stop.wait(backoff(failures - 1, base=self.backoff_base)):
                        return False
            return False
        finally:
            with self._lock:
                if self._active is active:
                    self._active = None
                self._transfers.pop((state.film_id, "up"), None)

    def _check_result(self, state: AttemptState) -> None:
        """§11 CX7: a settled success is delivered only with its ledger there,
        matching the settled digest, and every output intact (a restart, the
        antivirus or an operator may have taken them). Otherwise it finishes
        failed `cancelled` (「本地结果丢失」, a free retry, T08-P1a R3) — once,
        so the attempt leaves the delivery and the worker claims again."""
        settled = state.settled or {}
        if not settled.get("ok"):
            return
        ledger = self.staging.work_dir / state.attempt_id / RESULT_NAME
        try:
            digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
        except OSError:
            digest = None
        if (
            digest is not None
            and digest == settled.get("digest")
            and self._intact(state)
        ):
            return
        log.error("worker attempt %s: its local result is gone", state.attempt_id)
        state.settled = _settled(
            ExecOutcome.failed("cancelled", "本地结果丢失"), settled.get("energy")
        )
        self._save(state)

    def _refused(self, state: AttemptState, exc: ApiError) -> None:
        """§11 IR3: the controller refused a failed finish for good. The
        attempt file moves to `attempts/refused/` for inspection; the GPU is
        free; the controller's assignment stays until a later boot settles it
        (not reported → interrupted, a free retry)."""
        attempt_id = state.attempt_id
        log.error(
            "worker attempt %s: the controller refused its failed finish (%s);"
            " its record is kept in %s/%s",
            attempt_id,
            exc,
            ATTEMPTS_DIR,
            REFUSED_DIR,
        )
        folder = self.attempt_dir / REFUSED_DIR
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.replace(self._path(attempt_id), folder / f"{attempt_id}.json")
        except OSError:
            log.exception("refused worker attempt %s not moved", attempt_id)
        self.notice = f"主控拒绝了 {state.name or state.film_id} 的失败结果（{exc.code}），记录已保留"
        with self._lock:
            self._deliver = [a for a in self._deliver if a != attempt_id]
            self._cancelled.discard(attempt_id)
            self._started.pop(attempt_id, None)
            self._unsaved.pop(attempt_id, None)
        self._release()
        self._wake.set()

    def _finish_body(self, state: AttemptState, active: _Active) -> dict[str, Any]:
        settled = dict(state.settled or {})
        with self._lock:
            cancelled = state.attempt_id in self._cancelled
        # R9: the operator's stop wins over a failure; §11 CX5: never over a
        # stage that already settled ok (finished GPU work is delivered)
        if cancelled and not settled.get("ok"):
            return {
                "outcome": "failed",
                "error_class": "cancelled",
                "message": "已停止",
                "energy": settled.get("energy"),  # §11 CX9: None only if unknown
            }
        if not settled.get("ok"):
            return {
                "outcome": "failed",
                "error_class": settled.get("error_class") or "needs_attention",
                "message": _cap(settled.get("message") or "失败"),
                "energy": settled.get("energy"),
            }
        ledger = self.staging.work_dir / state.attempt_id / RESULT_NAME
        raw = ledger.read_bytes()
        self._upload(state, raw, active)
        return {
            "outcome": "succeeded",
            "message": _cap(settled.get("message") or ""),
            "ledger": raw.decode("utf-8"),
            "payload_digest": hashlib.sha256(raw).hexdigest(),
            "timing": settled.get("timing"),
            "energy": settled.get("energy"),
        }

    def _upload(self, state: AttemptState, raw: bytes, active: _Active) -> None:
        """Each output under `<a>/` the ledger names (C3: work-copy refs are
        never uploaded); three whole-file mismatches need the operator — the
        client's own three sends, never retried around it (§11 IR7). The
        stop interrupts a send; the controller keeps its offset (§11 IR6)."""
        result = StageResult.model_validate_json(raw)
        prefix = f"{state.attempt_id}/"
        files = [ref for ref in ledger_refs(result) if ref.path.startswith(prefix)]
        if not files:
            return
        client = self._client()
        client.plan_outputs(
            state.attempt_id,
            [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in files],
        )
        total = sum(f.size for f in files)
        started, before = self._clock(), 0
        for ref in files:
            done_before = before

            def sent(offset: int, done_before: int = done_before) -> None:
                self._progress(
                    state.film_id, "up", done_before + offset, total, started
                )

            try:
                client.upload_file(
                    state.attempt_id,
                    ref.path,
                    self.staging.work_dir / ref.path,
                    size=ref.size,
                    sha256=ref.sha256,
                    attempts=MAX_MISMATCHES,
                    on_progress=sent,
                    cancel=self._stop,
                )
            except ApiError as exc:
                if exc.code == "hash_mismatch":
                    raise ApiError(422, "hash_mismatch") from None
                raise
            before += ref.size
            active.progress = f"回传 {before}/{total}"

    def _done(self, attempt_id: str) -> None:
        """The finish resolved (or the attempt is stale): GC, free the GPU."""
        self._collect(attempt_id)
        with self._lock:
            self._deliver = [a for a in self._deliver if a != attempt_id]
        self._release()
        self._wake.set()

    def _acquire(self, label: str) -> bool:
        with self._lease_lock:
            if not self.lease.try_acquire(self.run_id, self.poll, label):
                return False
            self._holding = True
            return True

    def _release(self, *, force: bool = False) -> None:
        """Free the GPU (or stop waiting for it). §11 CX2: never while a
        child of an earlier boot is held, except at `stop()`."""
        with self._lease_lock:
            if self._held and not force:
                return
            if self._holding:
                self.lease.release(self.run_id)
                self._holding = False
            else:
                self.lease.withdraw(self.run_id)

    # ── power (R14) ──────────────────────────────────────────────────────
    def _sampler(self) -> Any:
        try:
            return self.power()
        except Exception:
            return None

    def _energy_reading(self, film_id: str) -> Any:
        sampler = self._sampler()
        if sampler is None:
            return None
        try:
            return sampler.reading(film_id)
        except Exception:
            log.exception("worker power reading failed")
            return None

    def _energy(self, active: _Active) -> dict[str, Any] | None:
        """The film's energy from the claim to now; None (never 0) without a
        reading, or when the sampler restarted in between."""
        start = active.energy_start
        sampler = self._sampler() if start is not None else None
        if sampler is None:
            return None
        try:
            delta = sampler.energy_between(start, sampler.reading(start.owner))
            if delta is None:
                return None
            value = m.AttemptEnergy.model_validate(delta.to_dict())
        except Exception:
            log.exception("worker attempt energy unavailable")
            return None
        data = value.model_dump(mode="json")
        data.pop("schema_version", None)
        return data

    def _energy_dict(self, active: _Active) -> dict[str, Any] | None:
        return self._energy(active) if active.phase == "running" else None

    def _live_power(self) -> dict[str, Any] | None:
        sampler = self._sampler()
        try:
            return sampler.live() if sampler is not None else None
        except Exception:
            return None


def _settled(
    outcome: ExecOutcome, energy: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": outcome.ok,
        "error_class": outcome.error_class,
        "message": _cap(outcome.message or ""),
        "digest": outcome.result_digest,
        "timing": None
        if outcome.timing is None
        else outcome.timing.model_dump(mode="json", exclude={"schema_version"}),
        "energy": energy,
    }


def _cap(text: str) -> str:
    """§11 IR3: a finish message within the model's limit."""
    return text if len(text) <= MESSAGE_CAP else text[: MESSAGE_CAP - 1] + "…"


def _is_uuid(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


def _ledger_timing(raw: bytes) -> m.RestoreTiming | None:
    """§11 IR9: the restore timings a ledger records, summed (or None)."""
    try:
        result = StageResult.model_validate_json(raw)
    except ValueError:
        return None
    return merge_timing(
        *(v.restore.timing for v in result.videos if v.restore is not None)
    )


def _watts(live: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """§11 IR11: only the live watts travel in a heartbeat."""
    if not live:
        return None
    return {**{k: live.get(k) for k in _WATTS}, "estimated": True}


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    """§11 IR11: `current` within `CURRENT_CAP` — the console line is cut,
    then the 本片参数, then the LTX detail, then the energy go."""
    if _size(value) <= CURRENT_CAP:
        return value
    ltx = dict(value.get("ltx") or {})
    console = ltx.get("console")
    if isinstance(console, str):
        ltx["console"] = console[-400:]
    elif isinstance(console, list):
        ltx["console"] = [str(line)[-400:] for line in console[-5:]]
    value = {**value, "ltx": ltx or None}
    if _size(value) > CURRENT_CAP:
        ltx.pop("params", None)
        value["ltx"] = ltx or None
    if _size(value) > CURRENT_CAP:
        value["ltx"] = None
    if _size(value) > CURRENT_CAP:
        value["energy"] = None
    return value


def _prune(root: Path, folder: Path) -> None:
    """Remove empty folders up to (not including) `root`."""
    root = root.resolve()
    current = folder.resolve()
    while current != root and root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent
