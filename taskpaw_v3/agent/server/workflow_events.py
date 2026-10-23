"""Observe the local workflow into the existing Agent event queue (#286).

The sidecar dedupes normal restarts. It does not make the existing in-memory
EventQueue durable: a crash between enqueue and marker persistence still has
the queue's existing delivery window. Workflow records are never changed here.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.state import atomic_json
from taskpaw_v3.workflow import board, planning
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import ExecOutcome
from taskpaw_v3.workflow.plan import TIER_LABELS
from taskpaw_v3.workflow.timing import film_timing, summary_text

if TYPE_CHECKING:
    from taskpaw_v3.workflow.local_host import WorkflowHostLike
    from taskpaw_v3.workflow.local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.events")
POLL_SECONDS = 5.0
INSTANCE = "media-workflow"
MAX_CURRENTS = 4  # P2-4 R12: the summary's running films, one per machine
_CLEANUPS: tuple[m.Stage, ...] = ("cleanup_input", "cleanup_workcopy")


class WorkflowEvents:
    def __init__(self, queue: EventQueue, path: Path) -> None:
        self.queue, self.path = queue, path
        self._lock = threading.Lock()
        self._summary_lock = threading.Lock()
        self._seen: set[str] = set()
        self._loaded = False
        self._dirty = False
        self._summary: dict[str, Any] | None = None
        self._sampled = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _ready(self) -> bool:
        try:
            if not self._loaded:
                try:
                    value = json.loads(self.path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    value = {"version": 1, "seen": []}
                if (
                    not isinstance(value, dict)
                    or value.get("version") != 1
                    or not isinstance(value.get("seen"), list)
                    or not all(isinstance(k, str) for k in value["seen"])
                ):
                    raise ValueError("invalid event markers")
                self._seen = set(value["seen"])
                self._loaded = True
            if self._dirty:
                self._save()
            return True
        except (OSError, ValueError):
            log.warning("workflow event state unavailable; notifications paused")
            return False

    def _save(self) -> None:
        atomic_json(self.path, {"version": 1, "seen": sorted(self._seen)})
        self._dirty = False

    def emit(
        self,
        film_id: str,
        revision: int,
        kind: str,
        message: str,
        level: str = "info",
    ) -> bool:
        key = json.dumps([film_id, revision, kind], separators=(",", ":"))
        with self._lock:
            if self._stop.is_set() or not self._ready() or key in self._seen:
                return False
            try:
                self.queue.add(
                    monitor=INSTANCE,
                    message=message,
                    level=level,
                    data={
                        "instance": INSTANCE,
                        "film_id": film_id,
                        "input_revision": revision,
                        "kind": kind,
                    },
                )
            except Exception as exc:
                log.warning("workflow event enqueue failed: %s", type(exc).__name__)
                return False
            self._seen.add(key)
            self._dirty = True
            try:
                self._save()
            except (OSError, ValueError):
                # Keep this key in memory and flush before accepting another event.
                log.warning("workflow event state write failed; notifications paused")
            return True

    def resolved(
        self,
        rt: LocalRuntime,
        job: m.PendingComputeJob,
        name: str,
        outcome: ExecOutcome,
    ) -> None:
        if outcome.error_class == "cancelled":
            return
        if not outcome.ok:
            self.emit(
                job.film_id,
                job.input_revision,
                f"workflow.stage.failed:{job.stage}",
                f"{name} {outcome.message or '处理失败'}",
                "warn",
            )
        elif job.stage == "translate":
            films = rt.store.film_board(
                rt.operator, film_id=job.film_id, energies=False
            )
            if films and films[0].input_revision == job.input_revision:
                self._translated(films[0], name)

    def _translated(self, film: m.BoardFilm, name: str) -> None:
        if film.input_revision is not None:
            self.emit(
                film.film_id,
                film.input_revision,
                "workflow.film.translated",
                summary_text(name, film_timing(film)),
            )

    def observe(self, host: WorkflowHostLike) -> None:
        try:
            if host.state != "running" or host.runtime is None:
                self._clear()
                return
            from taskpaw_v3.agent.server.workflow_reads import board_inputs

            rt = host.runtime
            films = rt.store.film_board(rt.operator, done_limit=None, energies=False)
            inputs = board_inputs(host, rt, films)  # P2-4 R1: as the board
            status = inputs.status
            dispatch = status.get("dispatch") or {}
            facts = board.row_facts(rt, films)
            rows = board.film_rows(
                films,
                inputs.waiting,
                facts=facts,
                extras=inputs.extras,
                names=inputs.names,
            )
            packages = board.package_rows(
                rt.store.list_intake_board(rt.operator), status.get("intake")
            )
            plans = (
                {item["film_id"]: item for item in planning.plan_list(rt)["plans"]}
                if any(row["group"] == "planning" for row in rows)
                else {}
            )
            now = datetime.now(timezone.utc)
            completed = []
            for film, row in zip(films, rows):
                if row["group"] == "done":
                    ends = [
                        s.ended_at
                        for s in film.stages
                        if s.stage in _CLEANUPS and s.ended_at is not None
                    ]
                    # Recover recent outcomes, not the whole archive on first
                    # deployment. Unfinished films remain actionable at any age.
                    if (
                        len(ends) != 2
                        or not now - timedelta(days=2) <= max(ends) <= now
                    ):
                        continue
                    completed.append(max(ends).isoformat())
                self._observe_film(
                    film,
                    row,
                    plans.get(film.film_id),
                    remote=getattr(host, "fleet", None) is not None,
                )
            current = dispatch.get("current")
            summary = {
                "counts": board.counts(rows, packages),
                "current": _current(current),
                # R12: every running film with its machine (the Mac shows it)
                "currents": _currents(inputs.currents, rows),
                "completed_at": completed,
            }
            with self._summary_lock:
                if not self._stop.is_set():
                    self._summary = summary
                    self._sampled = time.monotonic()
        except Exception as exc:
            self._clear()
            log.warning("workflow observation failed: %s", type(exc).__name__)

    def _observe_film(
        self,
        film: m.BoardFilm,
        row: dict,
        plan: dict | None,
        *,
        remote: bool = False,
    ) -> None:
        revision = film.input_revision
        if revision is None:
            return
        name = row["name"]
        stages = {stage.stage: stage for stage in film.stages}
        archive = stages.get("archive")
        if row["group"] == "planning":
            tier = (plan or {}).get("recommendation", {}).get("ltx_model")
            recommendation = TIER_LABELS.get(tier, "无需修复") if tier else "无需修复"
            machine = "自动" if remote else "本机"  # R4: remote mode routes it
            self.emit(
                film.film_id,
                revision,
                "workflow.film.needs_planning",
                f"{name} 待安排：推荐 {recommendation} · {machine}",
            )
        if row.get("attention_kind") == board.DEVICE_LOST:
            # §9 CX27: the lost worker itself, even on an older revision's route
            worker = (row.get("lost_worker") or {}).get("id")
            if worker:  # R4: once per film, revision and worker (OD2)
                self.emit(
                    film.film_id,
                    revision,
                    f"workflow.film.device_lost:{worker}",
                    f"{name} {row.get('note') or '设备失联'}",
                    "warn",
                )
        elif row["group"] == "confirm" and not (archive and archive.status == "failed"):
            actors = row.get("actors") or []
            candidate = f"：候选 {'、'.join(actors)}" if actors else ""
            self.emit(
                film.film_id,
                revision,
                "workflow.film.needs_confirmation",
                f"{name} 待确认入库{candidate}",
            )
        if row["group"] == "attention" and row.get("attention_kind") is None:
            stage = row.get("stage")
            state = row.get("status")
            kind = "failed" if state == "failed" else "needs_attention"
            self.emit(
                film.film_id,
                revision,
                f"workflow.stage.{kind}:{stage}",
                f"{name} {row.get('note') or '需要处理'}",
                "warn",
            )
        translated = stages.get("translate")
        if translated is not None and translated.status == "succeeded":
            self._translated(film, name)
        if archive is not None and archive.status == "failed":
            self.emit(
                film.film_id,
                revision,
                "workflow.film.archive_failed",
                f"{name} 入库失败：{film.archive_error or archive.note or '需要处理'}",
                "warn",
            )
        elif archive is not None and archive.status == "succeeded":
            done = all(
                stages.get(k) is not None and stages[k].status == "succeeded"
                for k in _CLEANUPS
            )
            self.emit(
                film.film_id,
                revision,
                "workflow.film.completed" if done else "workflow.film.pending_cleanup",
                f"{name} 已入库 · {'清理完成' if done else '待清理'}",
                "info" if done else "warn",
            )

    def _clear(self) -> None:
        with self._summary_lock:
            self._summary = None

    def snapshot(self) -> dict[str, Any] | None:
        with self._summary_lock:
            if (
                self._stop.is_set()
                or time.monotonic() - self._sampled > POLL_SECONDS * 3
            ):
                return None
            return copy.deepcopy(self._summary)

    def start(self, host: WorkflowHostLike) -> None:
        def run() -> None:
            while not self._stop.is_set():
                self.observe(host)
                self._stop.wait(POLL_SECONDS)

        self._thread = threading.Thread(target=run, name="workflow-events", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 60.0) -> bool:
        self._stop.set()
        self._clear()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True


def _currents(
    currents: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """R12: up to four machines, the controller's first, each with one film:
    name, stage, progress and the machine (`本机` or the worker's name). §9
    CX12: a machine's running stage wins over its prefetch, and the limit
    counts machines, so a prefetch never displaces another machine."""
    names = {row["film_id"]: row["name"] for row in rows}
    best: dict[str, tuple[bool, dict[str, Any], int]] = {}
    for film_id, value in currents.items():
        machine = value.get("machine") or value.get("device") or {}
        item = _current(value) or {}
        item.update(
            film=value.get("film") or names.get(film_id) or "",
            stage=value.get("stage") or ("fetch" if value.get("fetching") else None),
            progress=value.get("progress") or None,
            machine=str(machine.get("name") or ""),
        )
        key = str(machine.get("id") or item["machine"] or film_id)
        fetching = bool(value.get("fetching"))
        if key not in best or (best[key][0] and not fetching):
            best[key] = (fetching, item, 0 if value.get("device") else 1)
    found = sorted(
        ((local, item["machine"], item) for _f, item, local in best.values()),
        key=lambda entry: (entry[0], entry[1]),
    )
    return [item for _local, _machine, item in found[:MAX_CURRENTS]]


def _current(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {
        k: value[k] for k in ("film", "stage", "progress", "started_at") if k in value
    }
    ltx = value.get("ltx")
    ltx = ltx.get("ltx_progress") if isinstance(ltx, dict) else None
    if isinstance(ltx, dict):
        result["ltx"] = {
            k: ltx[k]
            for k in ("stage_index", "stage_total", "stage_name", "percent", "mode")
            if k in ltx
        }
    return copy.deepcopy(result)
