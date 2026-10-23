"""Shared workflow reads for local control and LAN monitoring (#286).

Only existing board/planning reads are used; no workflow state is changed here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException

from taskpaw_v3.core.config import WorkflowSettings

if TYPE_CHECKING:
    from taskpaw_v3.workflow.local_host import WorkflowHostLike
    from taskpaw_v3.workflow.local_runtime import LocalRuntime

WorkflowProvider = Callable[[], "WorkflowHostLike | None"]

_CONFLICTS = {
    "cas_conflict": "影片状态已变化，请刷新后重试",
    "precondition_failed": "当前状态不允许此操作",
    # P2-3 §11 CX5: 停止并重新排队 after the worker's stage settled ok
    "already_settled": "修复已完成，正在回传",
    # P2-4 R7: 改派 lost its race with the worker's claim
    "route_claimed": "已开始处理",
}


class CodedConflict(HTTPException):
    """A control conflict retaining the store's error code."""

    def __init__(self, detail: str, code: str):
        super().__init__(status_code=409, detail=detail)
        self.code = code


def off_state(settings: WorkflowSettings) -> tuple[str, str]:
    """Why there is no workflow host (D07)."""
    if not settings.enabled:
        return "off", "未启用"
    if not settings.inbox_root:
        return "off", "未设置收件目录"
    return "failed", "启动失败（见日志）"


@contextmanager
def _mapped() -> Iterator[None]:
    """Domain errors as HTTP statuses (§8); HTTPException passes through."""
    from taskpaw_v3.workflow.filmpaw_client import FilmpawRefused, FilmpawUnavailable
    from taskpaw_v3.workflow.finalize import ConfirmError
    from taskpaw_v3.workflow.models import WorkflowError

    try:
        yield
    except ConfirmError as exc:
        if exc.code in _CONFLICTS:
            raise CodedConflict(str(exc), exc.code) from None
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except WorkflowError as exc:
        if exc.code in _CONFLICTS:
            raise CodedConflict(_CONFLICTS[exc.code], exc.code) from None
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail="未知影片") from None
        raise HTTPException(
            status_code=503, detail=f"媒体流水线暂不可用：{exc.code}"
        ) from None
    except FilmpawUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except FilmpawRefused as exc:
        status = 404 if exc.status == 404 else 503
        raise HTTPException(status_code=status, detail=exc.message) from None
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from None
    except RuntimeError as exc:  # the host has not started its runtime yet
        raise HTTPException(status_code=503, detail=str(exc)) from None


def empty(state: str, reason: str) -> dict[str, Any]:
    from taskpaw_v3.workflow import board

    return {
        "state": state,
        "reason": reason,
        "current": None,
        "intake": None,
        "films": [],
        "packages": [],
        "counts": board.counts([], []),
        "dispatch": None,
        "gpu": None,
        "power": _cluster_power(),
        "energy_rates": [],
    }


def _cluster_power() -> dict[str, Any] | None:
    from taskpaw_v3.core import power_fleet

    return power_fleet.cluster_power()


def require_host(
    settings: WorkflowSettings, provider: WorkflowProvider | None
) -> WorkflowHostLike:
    host = provider() if provider is not None else None
    if host is None:
        raise HTTPException(status_code=503, detail=off_state(settings)[1])
    if host.state != "running" or host.runtime is None:
        raise HTTPException(status_code=503, detail=host.reason or host.state)
    return host


def runtime(host: WorkflowHostLike) -> LocalRuntime:
    assert host.runtime is not None  # checked by require_host
    return host.runtime


def films(
    settings: WorkflowSettings, provider: WorkflowProvider | None
) -> dict[str, Any]:
    host = provider() if provider is not None else None
    if host is None:
        return empty(*off_state(settings))
    if host.state != "running" or host.runtime is None:
        return empty(host.state, host.reason)
    from taskpaw_v3.workflow import board
    from taskpaw_v3.workflow import energy as en
    from taskpaw_v3.workflow.planning import max_output

    rt = host.runtime
    with _mapped():
        films = rt.store.film_board(rt.operator)
        inputs = board_inputs(host, rt, films)
        packages = rt.store.list_intake_board(rt.operator)
        facts = board.row_facts(rt, films)
        # PW13: the history apart from the display board's limit
        history = rt.store.energy_history(rt.operator)
    status = inputs.status
    dispatch = status.get("dispatch") or {}
    current, intake = dispatch.get("current"), status.get("intake")
    rows = board.film_rows(
        films,
        inputs.waiting,
        inputs.currents,
        status.get("ltx_download"),
        facts=facts,
        extras=inputs.extras,
        names=inputs.names,
    )
    package_rows = board.package_rows(packages, intake)
    gpu = rt.gpu
    return {
        "state": host.state,
        "reason": host.reason,
        "current": current,
        "intake": intake,
        "films": rows,
        "packages": package_rows,
        # U1 V' / V3 §7.4: the rail badges, the dispatch state and the card
        "counts": board.counts(rows, package_rows),
        "dispatch": {
            "state": dispatch.get("state", "idle"),
            "reason": dispatch.get("reason", ""),
            "paused": bool(dispatch.get("paused", False)),
        }
        if dispatch
        else None,
        # `max_output`: what the workflow restores, not what the card could (phase 1)
        "gpu": {
            "name": gpu.name,
            "vram_mib": gpu.vram_mib,
            "max_output": max_output(rt),
        }
        if gpu is not None
        else None,
        # PWR-2: the cluster's live estimate (this machine plus the 功耗
        # tab's peers) and the history behind 预计耗电 / 每片能耗
        "power": _cluster_power(),
        "energy_rates": en.rates(history),
    }


def film(
    settings: WorkflowSettings, provider: WorkflowProvider | None, film_id: str
) -> dict[str, Any]:
    from taskpaw_v3.workflow import board

    host = require_host(settings, provider)
    with _mapped():
        rt = runtime(host)
        found = board.board_film(rt, film_id)
        inputs = board_inputs(host, rt, (found,) if found is not None else ())
        mine = inputs.currents.get(film_id)
        detail = board.film_detail(
            rt,
            film_id,
            inputs.waiting,
            None if mine is None or mine.get("fetching") else mine,
            inputs.status.get("ltx_download"),
            extra=inputs.extras.get(film_id),
            names=inputs.names,
        )
    if detail is None:
        raise HTTPException(status_code=404, detail="未知影片")
    fleet = getattr(host, "fleet", None)
    if fleet is not None:  # P2-4 §2: the 改派 dialog's choices
        from taskpaw_v3.workflow import operator_actions as ops

        with _mapped():
            detail["reassign_options"] = ops.reassign_options(rt, fleet, film_id)
    return detail


LOCAL_NAME = "本机"
ROUTED = "已派往 {name}"  # P2-4 R1: an open route the worker has not claimed yet
PREFETCH = " · 预取"  # its second route: fetched while the first runs (S4)


@dataclass
class BoardInputs:
    """P2-4 R1: what the film list, the detail and the #286 observer read
    beside the store: the host's status, the merged currents, each film's
    note by precedence, its route, pin and 失联 state, and the names."""

    status: dict[str, Any]
    currents: dict[str, dict[str, Any]]
    waiting: dict[str, str]
    extras: dict[str, Any] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)


def claimed_films(rt: LocalRuntime) -> dict[str, Any]:
    """Film → its unresolved compute assignment (a claimed route, C9)."""
    films: dict[str, Any] = {}
    for item in rt.store.list_open_assignments(rt.operator):
        if item.stage in ("restore", "asr", "translate"):
            film_id = rt.store.get_stage(rt.operator, item.assignment.job_id).film_id
            films.setdefault(film_id, item)
    return films


def status_channel(host: Any) -> Any:
    """C4's 5680 `/status` channel: the host's own (tests) or the agent's
    power peers (`PowerFleet.quiet`)."""
    from taskpaw_v3.core import power_fleet

    channel = getattr(host, "status_channel", None)
    if callable(channel):
        return channel()
    return power_fleet.get_fleet()


def pass_inputs(host: Any, rt: LocalRuntime) -> dict[str, Any] | None:
    """The routing pass's live inputs for 待处理's preview (remote mode)."""
    own = getattr(host, "pass_inputs", None)
    if callable(own):
        value: dict[str, Any] | None = own()
        return value
    fleet = getattr(host, "fleet", None)
    if fleet is None:
        return None
    return {
        "live": fleet.snapshot(),
        "now": rt.clock.now(),
        "controller_id": rt.identity.worker_id,
        "controller_free_bytes": None,
        "live_since": fleet.started_at,
    }


def board_inputs(host: Any, rt: LocalRuntime, films: Sequence[Any]) -> BoardInputs:
    """A film's note, highest first: its transfer, the last routing pass's
    waiting reason, 已派往 X for an open unclaimed route (· 预取 when second
    in line), the controller's dispatcher's reason. A pass reason is left
    out for a film routed or claimed since, so the controller-disk gate
    shows itself, never 等待 GPU 设备. Remote mode adds routes, pins and the
    设备失联 state (`scheduler.lost_films`)."""
    from taskpaw_v3.workflow import board
    from taskpaw_v3.workflow import operator_actions as ops
    from taskpaw_v3.workflow import scheduler as sc

    status = host.status()
    dispatch = status.get("dispatch") or {}
    controller = rt.identity.worker_id
    records = {w.worker_id: w for w in rt.store.list_workers(rt.operator)}
    names = {worker_id: w.name for worker_id, w in records.items()}
    names[controller] = LOCAL_NAME
    fleet = getattr(host, "fleet", None)
    local = dispatch.get("current")
    if isinstance(local, dict) and local.get("film_id"):
        # R10: the running local attempt's machine, for its energy's devices
        local = {**local, "device": {"id": controller, "name": LOCAL_NAME}}
    remote = dict(fleet.currents(rt.clock.now(), names)) if fleet is not None else {}
    currents = board.merged_currents(local, remote)
    waiting = dict(dispatch.get("waiting") or {})
    extras: dict[str, Any] = {}
    if fleet is not None:
        now = rt.clock.now()
        routes = rt.store.list_routes(rt.operator)
        lost = sc.lost_films(
            routes,
            live=fleet.snapshot(),
            now=now,
            controller_id=controller,
            live_since=fleet.started_at,
        )
        claimed = claimed_films(rt)
        position: dict[str, int] = {}
        queued: dict[str, int] = {}
        for item in routes:  # routing order
            position[item.route_id] = queued.get(item.worker_id, 0)
            queued[item.worker_id] = position[item.route_id] + 1
        revisions = {f.film_id: f.input_revision for f in films}
        mine = {
            item.film_id: item
            for item in routes
            if revisions.get(item.film_id) in (None, item.input_revision)
        }
        for film_id, item in mine.items():
            if film_id not in claimed and item.worker_id != controller:
                prefetch = PREFETCH if position[item.route_id] == 1 else ""
                waiting[film_id] = (
                    ROUTED.format(name=names.get(item.worker_id, "")) + prefetch
                )
        last = getattr(host, "last_pass", None)
        for film_id, why in dict(getattr(last, "waiting", None) or {}).items():
            if film_id not in mine and film_id not in claimed:
                waiting[film_id] = why
        channel = status_channel(host)
        for film in films:
            claim = claimed.get(film.film_id)
            actions = {
                "can_reassign": ops.can_reassign(film, claim is not None),
                "stop": None
                if claim is None
                else ops.stop_action(
                    rt,
                    claim,
                    records.get(claim.assignment.worker_id or ""),
                    fleet=fleet,
                    channel=channel,
                    now=now,
                ),
            }
            extras[film.film_id] = board.RowExtra(
                route=_route(
                    mine.get(film.film_id), position, names, fleet, now, controller
                ),
                pin=_pin(rt, film, names),
                lost={
                    "id": lost[film.film_id],
                    "name": names.get(lost[film.film_id], ""),
                }
                if film.film_id in lost
                else None,
                actions=actions,
            )
    for film_id, value in currents.items():
        if value.get("fetching") and value.get("transfer"):
            waiting[film_id] = value["transfer"]
    return BoardInputs(status, currents, waiting, extras, names)


def _route(
    item: Any,
    position: dict[str, int],
    names: dict[str, str],
    fleet: Any,
    now: Any,
    controller: str,
) -> dict[str, Any] | None:
    """R2: the film's open route, its place in its worker's queue and how
    long the worker has been silent (the controller's own: `local`)."""
    from taskpaw_v3.workflow import board

    if item is None:
        return None
    local = item.worker_id == controller
    return {
        "machine": {"id": item.worker_id, "name": names.get(item.worker_id, "")},
        "kind": item.kind,
        "reason": item.reason,
        "routed_at": board.iso(item.routed_at),
        "position": position[item.route_id],
        "live": "local" if local else fleet.state(item.worker_id, now),
        "silent_s": None if local else round(fleet.silent_for(item.worker_id, now)),
    }


def _pin(rt: LocalRuntime, film: Any, names: dict[str, str]) -> dict[str, Any] | None:
    """R2: the plan's mutable machine choice (None = 自动), while its restore
    or ASR is unfinished (the observer reads every film every 5 s)."""
    if (
        not film.planned
        or film.input_revision is None
        or all(
            s.status in ("succeeded", "skipped")
            for s in film.stages
            if s.stage in ("restore", "asr")
        )
    ):
        return None
    pin = rt.store.get_pin(rt.operator, film.film_id, film.input_revision)
    if pin is None:
        return None
    return {
        "worker_id": pin.worker_id,
        "name": "自动" if pin.worker_id is None else names.get(pin.worker_id, ""),
        "version": pin.version,
    }


def plans(
    settings: WorkflowSettings, provider: WorkflowProvider | None
) -> dict[str, Any]:
    from taskpaw_v3.workflow.planning import plan_list

    host = require_host(settings, provider)
    with _mapped():
        rt = runtime(host)
        return plan_list(rt, remote=pass_inputs(host, rt))


def image_path(
    settings: WorkflowSettings,
    provider: WorkflowProvider | None,
    film_id: str,
    kind: Literal["poster", "cover"],
) -> Path:
    from taskpaw_v3.workflow import board

    host = require_host(settings, provider)
    with _mapped():
        path = (board.poster_path if kind == "poster" else board.cover_path)(
            runtime(host), film_id
        )
    if path is None:
        raise HTTPException(
            status_code=404, detail="没有封面" if kind == "poster" else "没有完整封面"
        )
    return path
