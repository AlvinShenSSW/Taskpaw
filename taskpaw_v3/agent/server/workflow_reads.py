"""Shared workflow reads for local control and LAN monitoring (#286).

Only existing board/planning reads are used; no workflow state is changed here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
    }


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
    from taskpaw_v3.workflow.planning import max_output

    status = host.status()
    dispatch = status.get("dispatch") or {}
    current, intake = dispatch.get("current"), status.get("intake")
    rt = host.runtime
    with _mapped():
        films = rt.store.film_board(rt.operator)
        packages = rt.store.list_intake_board(rt.operator)
        facts = board.row_facts(rt, films)
    rows = board.film_rows(
        films,
        dict(dispatch.get("waiting") or {}),
        current,
        status.get("ltx_download"),
        facts=facts,
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
    }


def film(
    settings: WorkflowSettings, provider: WorkflowProvider | None, film_id: str
) -> dict[str, Any]:
    from taskpaw_v3.workflow import board

    host = require_host(settings, provider)
    status = host.status()
    dispatch = dict(status.get("dispatch") or {})
    with _mapped():
        detail = board.film_detail(
            runtime(host),
            film_id,
            dict(dispatch.get("waiting") or {}),
            dispatch.get("current"),
            status.get("ltx_download"),
        )
    if detail is None:
        raise HTTPException(status_code=404, detail="未知影片")
    return detail


def plans(
    settings: WorkflowSettings, provider: WorkflowProvider | None
) -> dict[str, Any]:
    from taskpaw_v3.workflow.planning import plan_list

    host = require_host(settings, provider)
    with _mapped():
        return plan_list(runtime(host))


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
