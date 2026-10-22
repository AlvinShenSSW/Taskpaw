"""U1e: how long a film took — the restore's Jasna phase times, the film's
stage durations from the store, and the rows the 日志 tab shows.

`PhaseTimer` is pure over the parsed Jasna console state
(`parse_jasna_progress_line`) and a monotonic clock the caller reads.
`film_timing` is pure over one `BoardFilm`: each stage's current succeeded
attempt, the plan's confirmation and the restore's success evidence.
`WorkflowLog` writes task-log rows (`core.tasklog.TaskLog.record`); logging
never breaks the workflow.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Protocol

from . import models as m

log = logging.getLogger("taskpaw.workflow.timing")

SPEED_TEST = "speed_test"
_LTX = "ltx:"
_NAME_LIMIT = 32
_STAGE_SPAN = ("restore", "asr", "translate")  # 修复 → 翻译 (the operator's 全程)


def _ltx_stage(state: dict) -> tuple[str, int] | None:
    snapshot = state.get("ltx_progress")
    if not isinstance(snapshot, dict) or snapshot.get("mode") != "restore":
        return None
    name = str(snapshot.get("stage_name") or "").strip().casefold()[:_NAME_LIMIT]
    index = snapshot.get("stage_index")
    return (name, index) if name and isinstance(index, int) else None


def phase_of(state: dict) -> str | None:
    """The Jasna phase a parse state shows: `scan` (Mosaic scan), `basicvsr`,
    `speed_test` or `ltx:<stage>`; None for a state that names none (a new
    file's header, the auto split's note)."""
    snapshot = state.get("ltx_progress")
    if isinstance(snapshot, dict):
        if snapshot.get("mode") == "speed_test":
            return SPEED_TEST
        stage = _ltx_stage(state)
        return f"{_LTX}{stage[0]}" if stage else None
    if "scan_progress" in state:
        return "scan"
    if "percent" in state:
        return "basicvsr"
    return None


def _round(value: float) -> float:
    return round(max(0.0, value), 1)


class PhaseTimer:
    """One Jasna child's phase times. A phase runs from its first line until
    another phase begins, a state names none, or the child ends (`close`)."""

    def __init__(self, start: float) -> None:
        self.start = start
        self.end: float | None = None
        self.current: str | None = None
        self.since = start
        self.totals: dict[str, float] = {}
        self.runs = 0
        self._stage_index: int | None = None  # the current LTX run's last stage

    def observe(self, state: dict, now: float) -> None:
        phase = phase_of(state)
        if phase == self.current:
            return
        self._close(now)
        self.current, self.since = phase, now
        stage = _ltx_stage(state)
        if stage is None:
            self._stage_index = None
            return
        # a stage that does not follow a lower one of this run starts a new run
        if self._stage_index is None or stage[1] <= self._stage_index:
            self.runs += 1
        self._stage_index = stage[1]

    def _close(self, now: float) -> None:
        if self.current is not None:
            elapsed = max(0.0, now - self.since)
            self.totals[self.current] = self.totals.get(self.current, 0.0) + elapsed
        self.current = None

    def close(self, now: float) -> None:
        self._close(now)
        self._stage_index = None
        self.end = now

    def snapshot(self, now: float | None = None) -> m.RestoreTiming:
        """The times so far; a running phase counts up to `now`."""
        totals = dict(self.totals)
        at = self.end if self.end is not None else now
        if self.current is not None and at is not None:
            totals[self.current] = totals.get(self.current, 0.0) + max(
                0.0, at - self.since
            )
        stages: dict[str, float] = {}
        for key, value in totals.items():
            if key.startswith(_LTX) and (
                key[len(_LTX) :] in stages or len(stages) < m.LTX_STAGE_NAMES
            ):
                name = key[len(_LTX) :]
                stages[name] = stages.get(name, 0.0) + value
        speed = totals.get(SPEED_TEST, 0.0)
        return m.RestoreTiming(
            scan=_round(totals.get("scan", 0.0)),
            basicvsr=_round(totals.get("basicvsr", 0.0)),
            ltx=_round(sum(stages.values()) + speed),
            speed_test=_round(speed),
            ltx_stages={name: _round(value) for name, value in stages.items()},
            ltx_runs=self.runs,
            wall=_round((at if at is not None else self.start) - self.start),
        )


def merge(*timings: m.RestoreTiming | None) -> m.RestoreTiming | None:
    """The sum of several videos' timings; None when there is none."""
    present = [t for t in timings if t is not None]
    if not present:
        return None
    stages: dict[str, float] = {}
    for timing in present:
        for name, value in timing.ltx_stages.items():
            if name in stages or len(stages) < m.LTX_STAGE_NAMES:
                stages[name] = stages.get(name, 0.0) + value
    return m.RestoreTiming(
        scan=_round(sum(t.scan for t in present)),
        basicvsr=_round(sum(t.basicvsr for t in present)),
        ltx=_round(sum(t.ltx for t in present)),
        speed_test=_round(sum(t.speed_test for t in present)),
        ltx_stages={name: _round(value) for name, value in stages.items()},
        ltx_runs=sum(t.ltx_runs for t in present),
        wall=_round(sum(t.wall for t in present)),
    )


def timing_json(timing: m.RestoreTiming | None) -> dict[str, Any] | None:
    """The wire shape: the timing's fields without the model's schema version."""
    if timing is None:
        return None
    value = timing.model_dump(mode="json")
    value.pop("schema_version", None)
    return value


# ── a film's stage durations (E2) ──────────────────────────────────────
def film_timing(film: m.BoardFilm) -> dict[str, Any]:
    """`timing` of a film row: each succeeded stage's attempt duration, the
    restore's phase breakdown, 修复→翻译 (the first of restore / asr /
    translate attempts to the translate attempt's end) and the queue wait
    (plan confirmed → restore attempt start)."""
    stages: dict[str, m.BoardStage] = {stage.stage: stage for stage in film.stages}
    durations: dict[str, float] = {}
    spans: dict[str, tuple[datetime, datetime]] = {}
    for stage in film.stages:
        if (
            stage.status == "succeeded"
            and stage.started_at is not None
            and stage.ended_at is not None
        ):
            spans[stage.stage] = (stage.started_at, stage.ended_at)
            seconds = (stage.ended_at - stage.started_at).total_seconds()
            durations[stage.stage] = _round(seconds)
    starts = [spans[name][0] for name in _STAGE_SPAN if name in spans]
    total = None
    if "translate" in spans and starts:
        total = _round((spans["translate"][1] - min(starts)).total_seconds())
    restore = stages.get("restore")
    wait = None
    if (
        restore is not None
        and restore.started_at is not None
        and film.plan_confirmed_at is not None
        and not film.plan_backfilled
    ):
        seconds = (restore.started_at - film.plan_confirmed_at).total_seconds()
        wait = _round(seconds) if seconds >= 0 else None
    breakdown = (
        restore.timing if restore is not None and "restore" in durations else None
    )
    return {
        "stages": durations,
        "restore_breakdown": timing_json(breakdown),
        "total_restore_to_translate": total,
        "queue_wait": wait,
    }


def clock(seconds: float | None) -> str:
    """「h:mm:ss」, or 「—」 without a value."""
    if seconds is None or not math.isfinite(seconds):
        return "—"
    total = max(0, round(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def summary_text(film: str, timing: dict[str, Any]) -> str:
    """「{film} 修复→翻译完成，全程 h:mm:ss（修复 …，其中 LTX …；识别 …；翻译 …）」."""
    stages = timing.get("stages") or {}
    breakdown = timing.get("restore_breakdown") or {}
    restore = f"修复 {clock(stages.get('restore'))}"
    if breakdown.get("ltx"):
        restore += f"，其中 LTX {clock(breakdown['ltx'])}"
    return (
        f"{film} 修复→翻译完成，全程 {clock(timing.get('total_restore_to_translate'))}"
        f"（{restore}；识别 {clock(stages.get('asr'))}；"
        f"翻译 {clock(stages.get('translate'))}）"
    )


# ── the 日志 rows (E4) ──────────────────────────────────────────────────
TASK = "媒体处理中心"
TASK_TYPE = "workflow"


class TaskLogSink(Protocol):
    def record(
        self,
        task: str,
        kind: str,
        *,
        task_type: str,
        film: str | None = None,
        severity: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None: ...


class WorkflowLog:
    """The workflow's rows in the agent's task log (the 日志 tab)."""

    def __init__(self, sink: TaskLogSink | None) -> None:
        self.sink = sink

    def _record(self, kind: str, film: str, severity: str, data: dict) -> None:
        if self.sink is None:
            return
        try:
            self.sink.record(
                TASK, kind, task_type=TASK_TYPE, film=film, severity=severity, data=data
            )
        except Exception as exc:  # logging never breaks the workflow
            log.warning("workflow log row dropped: %s", type(exc).__name__)

    def stage(
        self,
        film: str,
        stage: str,
        *,
        ok: bool,
        duration: float | None,
        note: str | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "stage": stage,
            "duration": None if duration is None else _round(duration),
        }
        if not ok:
            data["detail"] = note
        self._record(
            "workflow.stage.succeeded" if ok else "workflow.stage.failed",
            film,
            "info" if ok else "warn",
            data,
        )

    def translated(self, film: str, timing: dict[str, Any]) -> None:
        stages = timing.get("stages") or {}
        breakdown = timing.get("restore_breakdown") or {}
        self._record(
            "workflow.film.translated",
            film,
            "info",
            {
                "run": timing.get("total_restore_to_translate"),
                "restore": stages.get("restore"),
                "ltx": breakdown.get("ltx") or None,
                "asr": stages.get("asr"),
                "translate": stages.get("translate"),
                "text": summary_text(film, timing),
            },
        )
