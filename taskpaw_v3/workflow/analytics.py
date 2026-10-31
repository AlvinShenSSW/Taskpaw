"""数据分析 (EPIC #387 C2): the controller's history as plain rows for the page.

Design: docs/specs/2026-10-31-media-analytics-epic.md §6.4, §7. The browser
aggregates; this module reads.

- **Film facts**, one per accepted archive receipt: the film as that receipt
  archived it (its final manifest names the input revision whose stages and
  attempts give every number). Those rows no longer change once the receipt
  exists, so each fact is built once and kept in memory by receipt rowid; a
  request reads only receipts above the high-water mark, at most `BATCH` per
  locked store read, under one cache lock so concurrent requests never read a
  batch twice.
- **Failures**: a failed attempt never changes again, so failed attempts
  are kept by attempt rowid too; each request reads new attempts by rowid
  range (`FAILURE_SPAN` per locked read: no index finds failures) and again
  those that were still running or unknown, which can turn failed in place.
  Each is typed by its last evidence kind, then its note (§7.3); its outcome
  reads the job's stage and attempts as they are now (§7.2).
- **Outages** come from the task log by kind, a day file at a time from a
  saved byte offset: closed days are kept after their first scan, today is
  read from where the last request stopped.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, NamedTuple

from . import board
from .device_history import lane, stamp
from .energy import STAGES as ENERGY_STAGES
from .exec_common import MISSING_LTX
from .executors import ASR_DONE, INTERRUPTED_KIND, failed_kind
from .models import AttemptEnergy
from .store_core import parse_time

if TYPE_CHECKING:
    from taskpaw_v3.core.tasklog import TaskLog

    from .device_history import DeviceHistory
    from .fleet_live import FleetLive
    from .library_store import LibraryStore
    from .local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.analytics")

BATCH = 200  # receipts per locked store read (§9: ≤ 50 ms each)
# attempts scanned for failures per locked store read (no index finds them)
FAILURE_SPAN = 5000
JOB_CHUNK = 500  # jobs whose outcome one locked read looks up
NOTE_CHARS = 200
WEEK = timedelta(days=7)
# the task log keeps at most 30 days before today (`TaskLog.prune`)
LOG_DAYS = 30
# the workflow's translator records under this task name
# (`exec_translate.default_translator`)
TRANSLATE_TASK = "workflow-translate"
DOWN, UP, PAUSED = (
    "translate.provider_down",
    "translate.provider_up",
    "translate.paused",
)
# the translator's breaker state starts over with the agent
BOUNDARIES = ("agent.started", "agent.stopping")
OUTAGE_KINDS = frozenset({DOWN, UP, PAUSED, *BOUNDARIES})
NO_RECORDER = "设备记录未启动"

_ASR_PREFIX = ASR_DONE.split("{summary}")[0]
DECISIONS = ("reused_zh", "source_ja", "generated_ja", "no_speech")
# the translate success notes (`TranslateExecutor`): translated, or nothing to do
_TRANSLATED, _NOT_NEEDED = "翻译完成", "无需翻译"
_PAUSE_NOTE = "翻译暂停"
_TRANSIENT = failed_kind("transient")


class Fact(NamedTuple):
    rowid: int
    accepted: datetime
    body: dict[str, Any]  # the §7 `films[*]` row
    energy_at: str | None  # the earliest attempt end with energy
    cues_at: str | None  # the translate success with a cue count


class _Day(NamedTuple):
    rows: list[dict[str, Any]]  # matching rows read from the file so far
    offset: int
    memory: list[dict[str, Any]]
    found: bool


def _z(value: str | None) -> str | None:
    return stamp(parse_time(value)) if value else None


def _seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    value = (parse_time(end) - parse_time(start)).total_seconds()
    return round(value) if value >= 0 else None


def _summary(last: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(last, dict) or not isinstance(last.get("summary"), dict):
        return {}
    return last["summary"]


def _energy(last: dict[str, Any] | None) -> AttemptEnergy | None:
    """`summary.energy` of an attempt's last evidence (as `read_store._energy`)."""
    value = _summary(last).get("energy")
    if not value:
        return None
    try:
        return AttemptEnergy.model_validate(value)
    except ValueError:
        return None


def subtitles(note: str | None) -> dict[str, int]:
    """The ASR success note's decision counts (`ASR_DONE`); {} when it does
    not parse."""
    if not note or not note.startswith(_ASR_PREFIX):
        return {}
    counts: dict[str, int] = {}
    for part in note[len(_ASR_PREFIX) :].split("，"):
        name, _, number = part.strip().partition(" ")
        if name not in DECISIONS or not number.isdigit() or name in counts:
            return {}
        counts[name] = int(number)
    return counts


def gpu_label(model: Any, vram_mib: Any) -> str | None:
    """ "RTX 5060 Ti 16GB" from a probe's "NVIDIA GeForce RTX 5060 Ti", 16311."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = " ".join(w for w in model.split() if w not in ("NVIDIA", "GeForce"))
    if isinstance(vram_mib, int) and vram_mib > 0:
        return f"{name} {round(vram_mib / 1024)}GB"
    return name


# ── film facts ──────────────────────────────────────────────────────────
def _actor(
    kind: str | None,
    target: dict[str, Any],
    task: dict[str, Any] | None,
    performers: dict[str, str],
) -> str | None:
    """The actor folder of a performer / new-actor target (the target path's
    parent), labelled by its library performer when one matches."""
    if kind not in ("performer", "new_actor"):
        return None
    if task is not None and task.get("target_path"):
        path = PureWindowsPath(task["target_path"])
    else:
        path = PureWindowsPath(target.get("export_identity") or "") / str(
            target.get("relative_path") or ""
        )
    parent = path.parent
    return performers.get(_folder_key(str(parent))) or parent.name or None


def _folder_key(path: str) -> str:
    """A folder as the facts compare it: the controller's paths are Windows
    (UNC) paths; `/` parses the same, so a POSIX path still compares."""
    return str(PureWindowsPath(path)).casefold()


def build_fact(
    row: dict[str, Any],
    controller_id: str,
    task: dict[str, Any] | None = None,
    performers: dict[str, str] | None = None,
) -> Fact:
    """One receipt's film fact (§7, §7.1, D4) from `analytics_receipts`."""
    by_stage: dict[str, list[tuple[Any, ...]]] = {}
    for attempt in row["attempts"]:
        by_stage.setdefault(attempt[0], []).append(attempt)

    def won(stage: str) -> tuple[Any, ...] | None:
        done = [a for a in by_stage.get(stage, ()) if a[1] == "succeeded"]
        return done[-1] if done else None

    def took(stage: str) -> int | None:
        attempt = won(stage)
        return _seconds(attempt[3], attempt[4]) if attempt else None

    restore, asr, translate = won("restore"), won("asr"), won("translate")
    starts = [a[3] for a in by_stage.get("restore", ()) if a[3]]
    queue = None
    plan = row["plan"]
    if starts and plan is not None and not plan[1]:
        first = min(starts, key=parse_time)
        queue = _seconds(plan[0], first)
    translate_note = _summary(
        json.loads(translate[5]) if translate and translate[5] else None
    )
    note = str(translate_note.get("note") or "")
    translated = (
        True
        if note.startswith(_TRANSLATED)
        else False
        if note.startswith(_NOT_NEEDED)
        else None  # no success, or a note this build does not know
    )
    cues = translate_note.get("item_count") if translate else None
    asr_note = _summary(json.loads(asr[5]) if asr and asr[5] else None).get("note")
    per_worker: dict[str, list[float]] = {}
    controller_wh: float | None = None
    partial = False
    energy_at: str | None = None
    for stage in ENERGY_STAGES:
        for attempt in by_stage.get(stage, ()):
            if not attempt[4]:
                continue  # still running: no resolved energy yet
            value = _energy(json.loads(attempt[5]) if attempt[5] else None)
            if value is None:
                partial = True
                continue
            ended = stamp(parse_time(attempt[4]))
            energy_at = min(ended, energy_at or ended)
            if stage == "restore":
                entry = per_worker.setdefault(attempt[2] or controller_id, [0.0, 0.0])
                entry[0] += value.wh
                entry[1] += value.seconds
            else:
                controller_wh = (controller_wh or 0.0) + value.wh
    width, height = row["width"], row["height"]
    target = row["target"] or {}
    scheme = (target.get("native_identity") or {}).get("scheme")
    kind = board.TARGET_KINDS.get(scheme) if scheme else None
    accepted = parse_time(row["accepted_at"])
    body = {
        "code": row["display_name"],
        "archived_at": stamp(accepted),
        "created_at": _z(row["created_at"]),
        "res": board.resolution(width, height),
        "duration_s": round(row["duration_ms"] / 1000) if row["duration_ms"] else None,
        "input_bytes": row["input_bytes"],
        "worker": restore[2] if restore else None,
        "queue_s": queue,
        "restore_s": took("restore"),
        "asr_s": took("asr"),
        "translate_s": took("translate"),
        "translated": translated,
        "cues": cues if isinstance(cues, int) else None,
        "subtitles": subtitles(asr_note) if asr else {},
        "scrape_s": took("scrape"),
        "archive_s": took("archive"),
        "lead_s": _seconds(row["created_at"], row["accepted_at"]),
        "restore_wh": [
            [worker, round(wh, 1), round(seconds, 1)]
            for worker, (wh, seconds) in per_worker.items()
        ],
        "controller_wh": None if controller_wh is None else round(controller_wh, 1),
        "energy_partial": partial,
        "target": kind,
        "actor": _actor(kind, target, task, performers or {}),
        "second": bool(task and task.get("second")),
        "marks": list(row["marks"] or ()),
    }
    cues_at = _z(translate[4]) if translate and body["cues"] is not None else None
    return Fact(row["rowid"], accepted, body, energy_at, cues_at)


def _library_reads(
    library: LibraryStore | None, operation_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The archive engine's receipts of these operations and every
    performer's folder (casefolded) → name; empty without the library."""
    if library is None or not operation_ids:
        return {}, {}
    marks = ",".join("?" for _ in operation_ids)
    with library.reading() as conn:
        tasks = {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                "SELECT operation_id,receipt_json FROM archive_tasks"
                f" WHERE receipt_json IS NOT NULL AND operation_id IN ({marks})",
                operation_ids,
            )
        }
        performers = {
            _folder_key(row[0]): row[1]
            for row in conn.execute("SELECT path,name FROM performers")
        }
    return tasks, performers


# ── failures (§7.2, §7.3) ───────────────────────────────────────────────
def failure_type(kind: str, stage: str, note: str) -> tuple[str, str]:
    """(type, severity) of a failed attempt: its last evidence kind first,
    then its note; first match wins (§7.3)."""
    if kind == INTERRUPTED_KIND:
        return "interrupted", "warn"
    if kind == failed_kind("cancelled"):
        return "stopped", "warn"
    if kind == _TRANSIENT:
        if stage == "translate" and note.startswith(_PAUSE_NOTE):
            return "translate_paused", "warn"
        return "transient", "warn"
    if kind == failed_kind("machine") or "WinError 4551" in note:
        return "machine_fault", "fail"
    if MISSING_LTX.rstrip("：") in note:
        return "missing_model", "fail"
    if "ffprobe" in note:
        return "missing_ffprobe", "fail"
    if "LTX 失败" in note:
        return "ltx_failed", "fail"
    if stage == "scrape" and "白名单" in note:
        return "scrape_whitelist", "fail"
    return "other", "fail"


def outcome(
    stage_status: str,
    worker_id: str | None,
    attempts: list[tuple[int, str, str | None]],
) -> str:
    """§7.2: what became of a failed attempt's job. A stage that is failed,
    blocked or unknown needs the operator whichever attempt this is; only a
    pending or running stage is still retrying."""
    if stage_status in ("succeeded", "skipped"):
        done = [a for a in attempts if a[1] == "succeeded"]
        if done and done[-1][2] != worker_id:
            return "reassigned"
        return "retried_ok"
    if stage_status in ("pending", "running"):
        return "retrying"
    return "needs_action"


def failure_rows(
    rows: list[dict[str, Any]],
    stages: dict[str, str],
    jobs: dict[str, list[tuple[int, str, str | None]]],
    controller_id: str,
) -> list[dict[str, Any]]:
    """The §7 `failures` rows. A transient-typed attempt whose job ended
    needing the operator with it as the job's last attempt had its automatic
    retries used up: a failure (`exhausted`); earlier transients of that job
    were retried and stay warnings."""
    out = []
    for row in rows:
        summary = _summary(row["last"])
        kind = str((row["last"] or {}).get("kind") or "")
        note = str(summary.get("note") or "")
        type_, severity = failure_type(kind, row["stage"], note)
        attempts = jobs.get(row["job_id"], [])
        result = outcome(
            stages.get(row["job_id"], "unknown"), row["worker_id"], attempts
        )
        latest = max((a[0] for a in attempts), default=row["attempt_number"])
        exhausted = (
            type_ in ("transient", "translate_paused")
            and result == "needs_action"
            and row["attempt_number"] == latest
        )
        out.append(
            {
                "at": _z(row["ended_at"]),
                "code": row["display_name"],
                "stage": row["stage"],
                "machine": row["worker_id"] or controller_id,
                "severity": "fail" if exhausted else severity,
                "type": type_,
                "exhausted": exhausted,
                "note": note[:NOTE_CHARS],
                "outcome": result,
            }
        )
    return out


# ── outages (D7) ────────────────────────────────────────────────────────
def _day_start(day: str) -> str:
    """A task-log day key (the agent's local date) as its UTC start."""
    return stamp(datetime.strptime(day, "%Y%m%d").astimezone())


def outage_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair the translator's events, oldest first, into outages: one starts
    when a provider goes down (or a film pauses) with none open and ends when
    no provider is down any more; an agent start or stop ends it, since the
    translator starts over. `paused`: a film paused during it."""
    out: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    down: set[str] = set()
    for row in rows:
        try:
            at = stamp(datetime.fromisoformat(str(row.get("ts"))))
        except ValueError:
            log.warning("analytics: task log row %s has no valid time", row["id"])
            continue
        kind = row["kind"]
        model = str((row.get("data") or {}).get("model") or "")
        if kind in BOUNDARIES:
            if current is not None:
                current["end"] = at
                current = None
            down.clear()
            continue
        if row.get("task") != TRANSLATE_TASK:
            continue
        if kind in (DOWN, PAUSED) and current is None:
            current = {"start": at, "end": None, "paused": False}
            out.append(current)
        if kind == DOWN:
            down.add(model)
        elif kind == PAUSED and current is not None:
            current["paused"] = True
        elif kind == UP:
            down.discard(model)
            if current is not None and not down:
                current["end"] = at
                current = None
    return out


# ── the cache ───────────────────────────────────────────────────────────
class AnalyticsFacts:
    """Per-receipt film facts and scanned task-log days, held by the host."""

    def __init__(self, batch: int = BATCH) -> None:
        self.batch = batch
        self._lock = threading.Lock()
        self._store: Any = None  # the store the facts were read from
        self._after = 0  # receipt rowid high-water mark
        self._facts: list[Fact] = []
        self._failure_lock = threading.Lock()
        self._failure_store: Any = None
        self._attempts_after = 0  # attempt rowid high-water mark
        self._open: set[int] = set()  # running or unknown at the last read
        self._failed: dict[int, dict[str, Any]] = {}  # by attempt rowid
        self._log_lock = threading.Lock()
        self._log: Any = None
        self._days: dict[str, _Day] = {}

    def facts(self, rt: LocalRuntime, library: LibraryStore | None) -> list[Fact]:
        """Every fact, after reading the receipts that arrived since the last
        call; concurrent callers wait for the update in progress."""
        with self._lock:
            if self._store is not rt.store:
                self._store, self._after, self._facts = rt.store, 0, []
            while True:
                rows = rt.store.analytics_receipts(rt.operator, self._after, self.batch)
                if not rows:
                    break
                tasks, performers = _library_reads(
                    library, [row["operation_id"] for row in rows]
                )
                built = [  # all or nothing: a failed batch is read again
                    build_fact(
                        row,
                        rt.identity.worker_id,
                        tasks.get(row["operation_id"]),
                        performers,
                    )
                    for row in rows
                ]
                self._facts += built
                self._after = rows[-1]["rowid"]
                if len(rows) < self.batch:
                    break
            return list(self._facts)

    def failures(
        self, rt: LocalRuntime, since: datetime | None
    ) -> list[dict[str, Any]]:
        """The §7 `failures` rows: every failed attempt of the fact stages
        that ended at or after `since`. A failed attempt never changes, so it
        is kept by rowid; new attempts are read by rowid range and those still
        running or unknown last time again by rowid. The outcome reads each
        job's stage and attempts as they are now."""
        with self._failure_lock:
            if self._failure_store is not rt.store:
                self._failure_store, self._attempts_after = rt.store, 0
                self._open, self._failed = set(), {}
            after, recheck = self._attempts_after, sorted(self._open)
            found: dict[int, dict[str, Any]] = {}
            still: set[int] = set()
            done = False
            while not done:
                rows, after, done = rt.store.analytics_attempts(
                    rt.operator, after, FAILURE_SPAN, recheck
                )
                recheck = []
                for row in rows:
                    if row["status"] == "failed":
                        ended = row["ended_at"]
                        found[row["rowid"]] = {
                            **row,
                            "ended": parse_time(ended) if ended else None,
                        }
                    else:
                        still.add(row["rowid"])
            self._failed.update(found)
            self._open, self._attempts_after = still, after
            selected = [
                row
                for _, row in sorted(self._failed.items())
                if since is None or (row["ended"] is not None and row["ended"] >= since)
            ]
        stages: dict[str, str] = {}
        jobs: dict[str, list[tuple[int, str, str | None]]] = {}
        ids = sorted({row["job_id"] for row in selected})
        for start in range(0, len(ids), JOB_CHUNK):
            now_stages, now_jobs = rt.store.analytics_jobs(
                rt.operator, ids[start : start + JOB_CHUNK]
            )
            stages.update(now_stages)
            jobs.update(now_jobs)
        return failure_rows(selected, stages, jobs, rt.identity.worker_id)

    def outages(self, task_log: TaskLog) -> tuple[list[dict[str, Any]], str | None]:
        """(every outage, start of the oldest day scanned) from the task log's
        last `LOG_DAYS` days and today."""
        with self._log_lock:
            if self._log is not task_log:
                self._log, self._days = task_log, {}
            today = task_log.day
            first = datetime.strptime(today, "%Y%m%d") - timedelta(days=LOG_DAYS)
            window = [
                (first + timedelta(days=n)).strftime("%Y%m%d")
                for n in range(LOG_DAYS + 1)
            ]
            for day in list(self._days):
                if day not in window:
                    del self._days[day]
            for day in window:
                held = self._days.get(day)
                if held is not None and day < today and not held.found:
                    continue  # a closed day only memory had: nothing more comes
                read = task_log.kind_rows(
                    day, OUTAGE_KINDS, held.offset if held is not None else 0
                )
                if not read.found and not read.memory:
                    self._days.pop(day, None)  # none, or pruned from disk
                    continue
                rows = (held.rows if held is not None else []) + read.rows
                self._days[day] = _Day(rows, read.offset, read.memory, read.found)
            merged: dict[str, dict[str, Any]] = {}
            for held in self._days.values():
                for row in (*held.rows, *held.memory):
                    merged.setdefault(row["id"], row)
            oldest = min(self._days, default=None)
        ordered = sorted(merged.values(), key=_log_order)
        return outage_rows(ordered), _day_start(oldest) if oldest else None


def _log_order(row: dict[str, Any]) -> tuple[str, int]:
    day, _, number = str(row["id"]).partition("-")
    return day, int(number) if number.isdigit() else 0


# ── the response (§7) ───────────────────────────────────────────────────
def _machines(
    rt: LocalRuntime,
    referenced: set[str],
    spans: list[dict[str, Any]],
    *,
    fleet: FleetLive | None,
    dispatcher_paused: bool,
    now: datetime,
) -> list[dict[str, Any]]:
    controller_id = rt.identity.worker_id
    seen = rt.store.analytics_workers(rt.operator)
    since = {s["machine"]: s["start"] for s in spans if s["open"]}
    out = []
    for worker in rt.store.list_workers(rt.operator):
        retired = worker.enrollment_state == "retired"
        if retired and worker.worker_id not in referenced:
            continue
        first, model, vram = seen.get(worker.worker_id, (None, None, None))
        state, reason = lane(
            worker,
            controller_id=controller_id,
            dispatcher_paused=dispatcher_paused,
            fleet=fleet,
            now=now,
        )
        out.append(
            {
                "id": worker.worker_id,
                "name": worker.name,
                "kind": "controller" if worker.worker_id == controller_id else "gpu",
                "model": gpu_label(model, vram),
                "first_seen": _z(first),
                "retired": retired,
                "state_now": state,
                "state_reason": reason,
                "state_since": since.get(worker.worker_id),
            }
        )
    return out


def _inbox(rt: LocalRuntime, facts: list[Fact], root: str | None, now: datetime) -> Any:
    if not root:
        return None
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        log.warning("analytics: INBOX disk usage unreadable: %s", type(exc).__name__)
        return None
    pending_bytes, pending_films = rt.store.analytics_inbox(rt.operator)
    week = [
        f.body["input_bytes"]
        for f in facts
        if f.accepted >= now - WEEK and f.body["input_bytes"] is not None
    ]
    return {
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "pending_bytes": pending_bytes,
        "pending_films": pending_films,
        "freed_bytes_per_day_7d": sum(week) // 7,
        "avg_input_bytes_7d": sum(week) // len(week) if week else 0,
    }


def build(
    rt: LocalRuntime,
    cache: AnalyticsFacts,
    *,
    since: datetime | None,
    library: LibraryStore | None = None,
    device_history: DeviceHistory | None = None,
    fleet: FleetLive | None = None,
    dispatcher_paused: bool = False,
    task_log: TaskLog | None = None,
    inbox_root: str | None = None,
) -> dict[str, Any]:
    """`GET /control/workflow/analytics` (§7): plain dicts, lists, strings and
    numbers only, ready for `JSONResponse`."""
    now = rt.clock.now()
    facts = cache.facts(rt, library)
    films = [f.body for f in facts if since is None or f.accepted >= since]
    failures = cache.failures(rt, since)
    if device_history is not None and device_history.available:
        spans = [
            {
                "machine": s.worker_id,
                "state": s.state,
                "reason": s.reason,
                "start": s.start,
                "end": s.end,
                "open": s.open,
            }
            for s in device_history.spans(since)
        ]
        devices = {"available": True, "reason": "", "spans": spans}
        devices_from = device_history.coverage_start()
    else:
        spans, devices_from = [], None
        reason = device_history.reason if device_history is not None else ""
        devices = {"available": False, "reason": reason or NO_RECORDER, "spans": spans}
    outages, outages_from = (
        cache.outages(task_log) if task_log is not None else ([], None)
    )
    if since is not None:
        floor = stamp(since)
        outages = [o for o in outages if o["end"] is None or o["end"] >= floor]
    referenced = {
        *(f["worker"] for f in films if f["worker"]),
        *(entry[0] for f in films for entry in f["restore_wh"]),
        *(f["machine"] for f in failures),
        *(s["machine"] for s in spans),
    }
    return {
        "server_time": stamp(now),
        "since": stamp(since) if since is not None else None,
        "coverage": {
            "archives": min((f.body["archived_at"] for f in facts), default=None),
            "energy": min((f.energy_at for f in facts if f.energy_at), default=None),
            "cues": min((f.cues_at for f in facts if f.cues_at), default=None),
            "devices": devices_from,
            "outages": outages_from,
        },
        "machines": _machines(
            rt,
            referenced,
            spans,
            fleet=fleet,
            dispatcher_paused=dispatcher_paused,
            now=now,
        ),
        "films": films,
        "failures": failures,
        "devices": devices,
        "outages": outages,
        "inbox": _inbox(rt, facts, inbox_root, now),
    }
