"""P2-4 (#295) R7/R8: 改派 and 判定已停止 for the controller's operator.

**判定已停止 (C4).** The operator's word replaces a worker's own stop proof
only when the worker is dead on both channels for `QUIET_SECONDS`: (a) no
heartbeat on 5682 (`FleetLive.silent_for`; a revoked worker's tombstone keeps
its last one, a worker never heard from counts from the controller's start),
and (b) the 5680 `/status` read kept failing (unreachable/timeout) or kept
answering `workflow_worker.busy == false`, last checked within
`FRESH_SECONDS` (`StatusChannel`, `PowerFleet.quiet`), and it answered at
least once since the controller started (§9 IR2: a channel that never
connected may be a wrong address). Any other answer — a 401, 404, an invalid
body, no `busy` — means the machine is up. The IR4 guard
(no renewal for five minutes) still applies; the store checks it again. After
the attest, `retry_if_due` (a cancelled stage is a free retry) and the caller
wakes the scheduler and the dispatcher, so the film is routed again.

**改派 (C2, IR8).** A new pin for an unclaimed planned film whose restore is
unfinished (OD7: only restore is routed). 自动 for a film whose route sits on a 失联 worker (`lost`;
one still in the start grace is `waiting` and keeps it, §9 IR7) also
withdraws that route (control layer only, OD2). The store checks "no
unresolved compute assignment" in the pin's own transaction and withdraws
the route in it (§9 CX4), so a claim racing the request answers
`route_claimed` (「已开始处理」) and changes nothing.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from taskpaw_v3.core.power_fleet import StatusQuiet

from . import models as m
from . import scheduler as sc
from .executors import failed_kind
from .local_dispatch import retry_if_due
from .route_store import QUIET_SECONDS
from .route_store import WITHDRAWN_LOST as WITHDRAWN_LOST  # re-exported

if TYPE_CHECKING:
    from .fleet_live import FleetLive
    from .local_runtime import LocalRuntime

FRESH_SECONDS = 10.0  # C4: the /status cache (read every 5 s) is this fresh
ATTESTED = "operator_attested_stopped"
ATTEST_TEXT = "我确认该机器已关机或任务已停止"
NEVER_ANSWERED = "状态通道从未连通"
_FINISHED = ("succeeded", "skipped")
_COMPUTE = ("restore", "asr", "translate")
_UP = {
    "unauthorized": "/status 返回 401（令牌不一致），机器仍在运行",
    "busy": "工作机报告任务仍在运行",
    "no_busy": "/status 有应答，机器仍在运行",
}


class StatusChannel(Protocol):
    def quiet(self, address: str) -> StatusQuiet | None: ...


class ActionRefused(Exception):
    """A refused 判定已停止 with its code and the reason shown."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class StopCheck:
    allowed: bool
    code: str | None
    reason: str | None
    heartbeat_silent_s: int | None
    status_quiet_s: int | None
    status_state: str | None = None  # how the last /status read ended


def worker_address(worker: m.WorkerRecord) -> str:
    """`host:port` of the worker's 5680 agent (`[v6]:port`), as power peers."""
    host = worker.address
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    return f"{host}:{worker.port}"


def stop_check(
    assignment: m.AssignmentRecord,
    worker: m.WorkerRecord,
    *,
    fleet: FleetLive | None,
    channel: StatusChannel | None,
    now: datetime,
) -> StopCheck:
    """C4: both channels dead for 300 s, then IR4; the first failing rule
    names its channel."""
    silent = fleet.silent_for(worker.worker_id, now) if fleet is not None else None
    quiet = channel.quiet(worker_address(worker)) if channel is not None else None
    heard = None if silent is None else round(silent)
    status_s = None if quiet is None or quiet.quiet_s is None else round(quiet.quiet_s)
    outcome = quiet.outcome if quiet is not None else None

    def refused(code: str, reason: str) -> StopCheck:
        return StopCheck(False, code, reason, heard, status_s, outcome)

    if silent is None:
        return refused("still_alive", "心跳通道：未开启远程设备")
    if silent < QUIET_SECONDS:
        return refused("still_alive", f"心跳通道：{worker.name} {heard} 秒前仍有心跳")
    if quiet is None:
        return refused("still_alive", f"状态通道：尚未读取 {worker.name} 的 /status")
    if quiet.checked_s is None or quiet.checked_s > FRESH_SECONDS:
        age = "从未" if quiet.checked_s is None else f"已 {round(quiet.checked_s)} 秒未"
        return refused("still_alive", f"状态通道：/status {age}检查")
    if quiet.quiet_s is None:
        text = _UP.get(
            quiet.state or "", f"/status 有应答（{quiet.state}），机器仍在运行"
        )
        return refused("still_alive", f"状态通道：{text}")
    if not quiet.answered:  # §9 IR2: never connected is not dead
        return refused("still_alive", NEVER_ANSWERED)
    if quiet.quiet_s < QUIET_SECONDS:
        return refused("still_alive", f"状态通道：仅 {status_s} 秒无响应")
    renewed = (now - assignment.renewed_at).total_seconds()
    if renewed < QUIET_SECONDS:
        return refused("recently_renewed", "该任务 5 分钟内仍有续约，请稍后再判定")
    return StopCheck(True, None, None, heard, status_s, outcome)


def stop_action(
    rt: LocalRuntime,
    item: m.OpenAssignment,
    worker: m.WorkerRecord | None,
    *,
    fleet: FleetLive | None,
    channel: StatusChannel | None,
    now: datetime,
) -> dict[str, Any] | None:
    """`actions.stop` of an unresolved remote compute assignment."""
    assignment = item.assignment
    if worker is None or assignment.worker_id in (None, rt.identity.worker_id):
        return None
    check = stop_check(assignment, worker, fleet=fleet, channel=channel, now=now)
    return {
        "assignment_id": assignment.assignment_id,
        "expected_version": assignment.version,
        "stage": item.stage,
        "worker": {"id": worker.worker_id, "name": worker.name},
        "allowed": check.allowed,
        "reason": check.reason,
        "heartbeat_silent_s": check.heartbeat_silent_s,
        "status_quiet_s": check.status_quiet_s,
        # the dialog's 「192.168.1.34:5680 · 超时」: the status channel read
        "address": worker_address(worker),
        "status_state": check.status_state,
    }


def attest(
    rt: LocalRuntime,
    *,
    fleet: FleetLive | None,
    channel: StatusChannel | None,
    film_id: str,
    assignment_id: str,
    expected_version: int,
) -> dict[str, Any]:
    """判定已停止 of one of the film's unresolved remote compute assignments
    (§9 CX7: validated with its own revision and attempt, so a claim left on
    an older input revision can be settled too); ActionRefused
    (`stale_attempt`, `cas_conflict`, `still_alive`, `recently_renewed`)
    otherwise."""
    op = rt.operator
    item = next(
        (
            o
            for o in rt.store.list_open_assignments(op)
            if o.assignment.assignment_id == assignment_id
        ),
        None,
    )
    view = rt.store.get_film(op, film_id)
    stage = rt.store.get_stage(op, item.assignment.job_id) if item is not None else None
    if (
        item is None
        or stage is None
        or item.stage not in _COMPUTE
        or stage.film_id != film_id
        or stage.attempt_id != item.assignment.attempt_id
    ):
        raise ActionRefused("stale_attempt", "任务已变化，请刷新后重试")
    assignment = item.assignment
    if assignment.version != expected_version:
        raise ActionRefused("cas_conflict", "影片状态已变化，请刷新后重试")
    if assignment.worker_id is None or assignment.worker_id == rt.identity.worker_id:
        raise ActionRefused("precondition_failed", "本机的任务由本机自行确认")
    worker = rt.store.get_worker(op, assignment.worker_id).worker
    check = stop_check(
        assignment, worker, fleet=fleet, channel=channel, now=rt.clock.now()
    )
    if not check.allowed:
        assert check.code is not None and check.reason is not None
        raise ActionRefused(check.code, check.reason)
    operation_id = next(
        a.operation_id
        for a in rt.store.list_attempts(op, assignment.job_id)
        if a.attempt_id == assignment.attempt_id
    )
    try:
        result = rt.store.attest_stopped(
            op,
            rt.meta(),
            m.StoppedAttestation(
                assignment_id=assignment.assignment_id,
                attempt_id=assignment.attempt_id,
                operation_id=operation_id,
                expected_version=assignment.version,
                reason=ATTEST_TEXT,
                evidence=(
                    rt.evidence(ATTESTED, ATTEST_TEXT),
                    rt.evidence(failed_kind("cancelled"), "判定已停止"),
                ),
            ),
        )
    except m.WorkflowError as exc:
        if exc.code == "cas_conflict":
            raise ActionRefused(
                "cas_conflict", "影片状态已变化，请刷新后重试"
            ) from None
        if exc.code == "stale_attempt":
            raise ActionRefused("stale_attempt", "任务已变化，请刷新后重试") from None
        if exc.code == "precondition_failed":  # IR4 in the store's own clock
            raise ActionRefused(
                "recently_renewed", "该任务 5 分钟内仍有续约，请稍后再判定"
            ) from None
        raise
    # §9 CX7: a cancelled stage of the current revision is a free retry; an
    # older revision's claim is only settled (assignment, GPU owner, route)
    if stage.input_revision == view.film.current_input_revision:
        retry_if_due(rt, assignment.job_id)
    return {"ok": True, "status": result.status}


# ── 改派 ────────────────────────────────────────────────────────────────
def _claim(rt: LocalRuntime, film_id: str, revision: int) -> m.OpenAssignment | None:
    op = rt.operator
    for item in rt.store.list_open_assignments(op):
        if item.stage not in _COMPUTE:
            continue
        stage = rt.store.get_stage(op, item.assignment.job_id)
        if stage.film_id == film_id and stage.input_revision == revision:
            return item
    return None


def reassign(
    rt: LocalRuntime,
    *,
    fleet: FleetLive | None,
    film_id: str,
    input_revision: int,
    expected_version: int,
    worker_id: str | None,
) -> dict[str, Any]:
    """R7: the new pin; WorkflowError `route_claimed`, `cas_conflict`,
    `precondition_failed` otherwise."""
    op = rt.operator
    view = rt.store.get_film(op, film_id)
    if view.film.current_input_revision != input_revision:
        raise m.WorkflowError("cas_conflict")
    if worker_id is not None:
        target = rt.store.get_worker(op, worker_id).worker
        if (
            worker_id == rt.identity.worker_id  # OD1: 本机 is never a choice
            or target.enrollment_state != "verified"
            or target.admission_state == "revoked"
        ):
            raise m.WorkflowError("precondition_failed")
    pin = rt.store.get_pin(op, film_id, input_revision)
    unfinished = [
        s
        for s in view.stages
        if s.input_revision == input_revision
        and s.stage == "restore"  # OD7: only restore is routed
        and s.status not in _FINISHED
    ]
    if pin is None or not unfinished:
        raise m.WorkflowError("precondition_failed")
    if _claim(rt, film_id, input_revision) is not None:
        raise m.WorkflowError("route_claimed")
    route = rt.store.film_route(op, film_id, input_revision)
    lost = (  # §9 IR7: only 失联; a worker in its start grace keeps its route
        worker_id is None
        and route is not None
        and route.worker_id != rt.identity.worker_id
        and fleet is not None
        and fleet.state(route.worker_id, rt.clock.now()) == "lost"
    )
    # §9 CX4: the store re-checks the claim and withdraws in one transaction
    rt.store.set_pin(
        op,
        rt.meta(),
        m.PinSet(
            film_id=film_id,
            input_revision=input_revision,
            worker_id=worker_id,
            expected_version=expected_version,
            reason="改派",
            withdraw_route_id=route.route_id if lost and route is not None else None,
        ),
    )
    return {"ok": True}


def reassign_options(
    rt: LocalRuntime, fleet: FleetLive | None, film_id: str
) -> list[dict[str, Any]]:
    """The drawer's 改派 choices: 自动, then each verified worker with the
    reason it could not take the film now (None: it can). Each choice that
    can take it carries `scheduler.preview`'s finish and energy (预计,
    advisory); 自动 also names the device the next pass would pick."""
    from .board import iso
    from .routing import gpu_stages

    op = rt.operator
    snapshot = rt.store.routing_snapshot(op)
    film = next((f for f in snapshot.films if f.film_id == film_id), None)
    if film is None or not film.planned:
        return []
    found = gpu_stages(rt.store, op, snapshot, rt.executors)
    stages = sc.routed_stages(film, found)
    if not stages:  # OD7 R6: nothing left to route, no choice to offer
        return []
    live = fleet.snapshot() if fleet is not None else {}
    now = rt.clock.now()
    # §9 CX31: `reassign` withdraws the film's route for a different worker (or
    # a 失联 one under 自动) and keeps it otherwise — so the previews are built
    # without that route (it must not count as queued work of its own); an
    # option that keeps the route reports the route worker's own estimate
    current = next(
        (
            r
            for r in snapshot.routes
            if r.film_id == film_id and r.input_revision == film.input_revision
        ),
        None,
    )
    full = snapshot
    if current is not None:
        snapshot = snapshot.model_copy(
            update={"routes": tuple(r for r in snapshot.routes if r is not current)}
        )
    seen = sc.preview(  # §9 CX33: at the film's own place in the confirm order
        snapshot,
        film_id,
        (film.ltx_model,),
        live=live,
        now=now,
        controller_id=rt.identity.worker_id,
        gpu_stages=found,
        live_since=fleet.started_at if fleet is not None else None,
        keep_confirmed=True,
    ).get(film.ltx_model)
    names = {w.worker_id: w.name for w in snapshot.workers}
    by_id = {d.worker_id: d for d in seen.devices} if seen is not None else {}

    def estimate(finish: datetime | None, energy: float | None) -> dict[str, Any]:
        return {"finish_at": iso(finish) if finish else None, "energy_wh": energy}

    machine = seen.machine if seen is not None else None
    finish = seen.finish_at if seen is not None else None
    energy = seen.energy_wh if seen is not None else None
    kept = (  # 自动 keeps a route unless its worker is 失联 (IR7/IR8)
        current is not None
        and current.worker_id != rt.identity.worker_id
        and not (fleet is not None and fleet.state(current.worker_id, now) == "lost")
    )
    # §9 CX33: where the route stands — only the routes ahead of it count
    queued = (
        sc.queued_finish(full, film_id, now=now, gpu_stages=found)
        if current is not None
        else None
    )
    if kept and current is not None:
        machine = current.worker_id
        finish, energy = queued if queued is not None else (None, None)
    options: list[dict[str, Any]] = [
        {
            "worker_id": None,
            "name": "自动",
            "reason": None,
            "current": film.pin_worker_id is None,
            "machine": {"id": machine, "name": names.get(machine, "")}
            if machine is not None
            else None,
            **estimate(finish, energy),
        }
    ]
    for worker in snapshot.workers:
        if (
            worker.worker_id == rt.identity.worker_id
            or worker.enrollment_state == "retired"
            or worker.admission_state == "revoked"
        ):
            continue
        reason = sc.eligibility(
            worker,
            film,
            stages,
            live.get(worker.worker_id),
            now,
            queued_need=None,
        )
        device = by_id.get(worker.worker_id) if reason is None else None
        if (  # M6: pinning to the route's own worker keeps the route (as 自动)
            reason is None
            and queued is not None
            and current is not None
            and current.worker_id == worker.worker_id
        ):
            figures = estimate(*queued)
        else:
            figures = estimate(
                device.finish_at if device is not None else None,
                device.energy_wh if device is not None else None,
            )
        options.append(
            {
                "worker_id": worker.worker_id,
                "name": worker.name,
                "reason": reason,
                "current": film.pin_worker_id == worker.worker_id,
                **figures,
            }
        )
    return options


def can_reassign(film: m.BoardFilm, claimed: bool) -> bool:
    """R7: an unclaimed planned film whose restore is unfinished (OD7)."""
    return (
        film.planned
        and not claimed
        and any(s.stage == "restore" and s.status not in _FINISHED for s in film.stages)
    )
