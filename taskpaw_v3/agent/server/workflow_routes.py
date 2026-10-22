"""媒体入库 routes on the loopback control API only (T06-P1 §3, §7 D05–D09, §8).

The workflow host starts after the agent announced readiness, so the routes
read it through a late-bound provider and answer only while it runs (D07).
Workflow modules are imported per request: a broken workflow never stops the
control API from starting. Every path is resolved from the film id (U6).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from taskpaw_v3.core.config import WorkflowSettings

if TYPE_CHECKING:
    from taskpaw_v3.workflow.local_host import WorkflowHostLike
    from taskpaw_v3.workflow.local_runtime import LocalRuntime

WorkflowProvider = Callable[[], Optional["WorkflowHostLike"]]

_CONFLICTS = {
    "cas_conflict": "影片状态已变化，请刷新后重试",
    "precondition_failed": "当前状态不允许此操作",
}


class CodedConflict(HTTPException):
    """A 409 whose body also names the store's error code (U1 Codex r4 F2)."""

    def __init__(self, detail: str, code: str):
        super().__init__(status_code=409, detail=detail)
        self.code = code


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    performer_id: str = Field(min_length=1, max_length=256)


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(min_length=1, max_length=64)


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: Literal["inbox", "output"]


class PlanItem(BaseModel):
    """One film as the 待处理 card showed it (U1 P3')."""

    model_config = ConfigDict(extra="forbid")
    film_id: str = Field(min_length=1, max_length=256)
    input_revision: int = Field(gt=0, strict=True)
    ltx_model: Optional[Literal["standard", "good", "high"]]
    machine: str = Field(min_length=1, max_length=256)


class PlansBody(BaseModel):
    """确认入队 of one card or 全部按推荐确认 of the list: each its own CAS."""

    model_config = ConfigDict(extra="forbid")
    plans: list[PlanItem] = Field(min_length=1, max_length=500)


class PauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class LtxDownloadBody(BaseModel):
    """The 下载模型 answer to the offered request (H3 M1), as the monitor's."""

    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    approved: StrictBool


def open_folder(path: Path) -> None:
    """Show a folder in Explorer (D09); replaced in tests."""
    if sys.platform == "win32":
        os.startfile(str(path))
        return
    raise NotImplementedError("只支持在 Windows 上打开文件夹")


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


def register_workflow_routes(
    app: FastAPI, settings: WorkflowSettings, provider: WorkflowProvider | None
) -> None:
    @app.exception_handler(CodedConflict)
    async def _coded_conflict(_request: Any, exc: CodedConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": exc.detail, "code": exc.code}
        )

    def _host() -> WorkflowHostLike:
        host = provider() if provider is not None else None
        if host is None:
            raise HTTPException(status_code=503, detail=off_state(settings)[1])
        if host.state != "running" or host.runtime is None:
            raise HTTPException(status_code=503, detail=host.reason or host.state)
        return host

    def _runtime(host: WorkflowHostLike) -> LocalRuntime:
        assert host.runtime is not None  # checked by _host
        return host.runtime

    def _not_found() -> HTTPException:
        return HTTPException(status_code=404, detail="未知影片")

    def _open(path: Path | None) -> Response:
        if path is None:
            raise HTTPException(status_code=404, detail="文件夹不存在")
        try:
            open_folder(path)
        except OSError as exc:  # Explorer refused (access, vanished share)
            raise HTTPException(
                status_code=500, detail=f"无法打开文件夹：{exc}"
            ) from None
        return Response(status_code=204)

    def _empty(state: str, reason: str) -> dict[str, Any]:
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

    def _dispatcher(host: WorkflowHostLike) -> Any:
        dispatcher = getattr(host, "dispatcher", None)
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        return dispatcher

    @app.get("/control/workflow/films")
    def workflow_films() -> dict[str, Any]:
        host = provider() if provider is not None else None
        if host is None:
            return _empty(*off_state(settings))
        if host.state != "running" or host.runtime is None:
            return _empty(host.state, host.reason)
        from taskpaw_v3.workflow import board
        from taskpaw_v3.workflow import energy as en
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
            # PWR-2: the cluster's live estimate (this machine plus the 功耗
            # tab's peers) and the history behind 预计耗电 / 每片能耗
            "power": _cluster_power(),
            "energy_rates": en.rates(films),
        }

    @app.get("/control/workflow/films/{film_id}")
    def workflow_film(film_id: str) -> dict[str, Any]:
        from taskpaw_v3.workflow import board

        host = _host()
        status = host.status()
        dispatch = dict(status.get("dispatch") or {})
        with _mapped():
            detail = board.film_detail(
                _runtime(host),
                film_id,
                dict(dispatch.get("waiting") or {}),
                dispatch.get("current"),
                status.get("ltx_download"),
            )
        if detail is None:
            raise _not_found()
        return detail

    @app.get("/control/workflow/films/{film_id}/poster")
    def workflow_poster(film_id: str) -> FileResponse:
        from taskpaw_v3.workflow import board

        host = _host()
        with _mapped():
            path = board.poster_path(_runtime(host), film_id)
        if path is None:
            raise HTTPException(status_code=404, detail="没有封面")
        return FileResponse(path, media_type=board.image_type(path))

    @app.get("/control/workflow/films/{film_id}/cover")
    def workflow_cover(film_id: str) -> FileResponse:
        """U1b: the full landscape jacket the 大图 shows (`board.jacket`)."""
        from taskpaw_v3.workflow import board

        host = _host()
        with _mapped():
            path = board.cover_path(_runtime(host), film_id)
        if path is None:
            raise HTTPException(status_code=404, detail="没有完整封面")
        return FileResponse(path, media_type=board.image_type(path))

    # ── U1 P3': 待处理 ──────────────────────────────────────────────────
    @app.get("/control/workflow/plans")
    def workflow_plans() -> dict[str, Any]:
        from taskpaw_v3.workflow.planning import plan_list

        host = _host()
        with _mapped():
            return plan_list(_runtime(host))

    @app.post("/control/workflow/plans")
    def workflow_confirm_plans(body: PlansBody) -> dict[str, Any]:
        """确认入队 / 全部按推荐确认: a result per item; a success wakes dispatch."""
        from taskpaw_v3.workflow.planning import confirm_plans

        host = _host()
        with _mapped():
            results = confirm_plans(
                _runtime(host), [item.model_dump() for item in body.plans]
            )
        dispatcher = getattr(host, "dispatcher", None)
        if dispatcher is not None and any(r["ok"] for r in results):
            dispatcher.wake()
        return {"results": results}

    @app.post("/control/workflow/films/{film_id}/lookup")
    def workflow_requery(film_id: str) -> dict[str, bool]:
        """重新查询 (U1 L): the film's current revision is looked up again."""
        from taskpaw_v3.workflow.models import WorkflowError

        host = _host()
        rt = _runtime(host)
        if rt.lookup is None:
            raise HTTPException(status_code=503, detail="未配置 Miko，无法查询资料")
        with _mapped():
            film = rt.store.get_film(rt.operator, film_id).film
            if film.current_input_revision is None:
                raise HTTPException(status_code=409, detail="影片还没有收件内容")
            if film.current_final_revision is not None:  # the final NFO names it
                raise HTTPException(status_code=409, detail="影片已刮削，无需重新查询")
            try:
                rt.lookup.requery(film_id, film.current_input_revision)
            except WorkflowError as exc:
                if exc.code != "precondition_failed":
                    raise
                raise HTTPException(
                    status_code=409, detail="正在查询，或文件名中没有番号"
                ) from None
        return {"ok": True}

    @app.post("/control/workflow/pause")
    def workflow_pause(body: PauseBody) -> dict[str, bool]:
        """V3 §7.5: stop new claims; in-flight work and the finalizer continue."""
        host = _host()
        _dispatcher(host).set_paused(body.paused)
        return {"paused": body.paused}

    @app.get("/control/workflow/films/{film_id}/target")
    def workflow_target(film_id: str, performer_id: str = "") -> dict[str, Any]:
        """V3 §7.3: the archive target preflight and the NFO actor's candidates."""
        host = _host()
        if host.finalizer is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        with _mapped():
            return host.finalizer.preflight(film_id, performer_id or None)

    @app.get("/control/workflow/performers")
    def workflow_performers(q: str = "") -> dict[str, Any]:
        host = _host()
        if host.finalizer is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        with _mapped():
            return {"items": host.finalizer.filmpaw.performers(q)}

    @app.get("/control/workflow/performers/{performer_id}/thumb")
    def workflow_thumb(performer_id: str) -> Response:
        host = _host()
        if host.finalizer is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        with _mapped():
            data, media_type = host.finalizer.filmpaw.thumb(performer_id)
        return Response(content=data, media_type=media_type)

    @app.post("/control/workflow/films/{film_id}/confirm")
    def workflow_confirm(film_id: str, body: ConfirmBody) -> dict[str, str]:
        host = _host()
        with _mapped():
            return {"approval_id": host.confirm(film_id, body.performer_id)}

    @app.post("/control/workflow/films/{film_id}/retry")
    def workflow_retry(film_id: str, body: RetryBody) -> dict[str, bool]:
        host = _host()
        with _mapped():
            host.retry(film_id, body.stage)
        return {"ok": True}

    @app.post("/control/workflow/ltx-download")
    def workflow_ltx_download(body: LtxDownloadBody) -> dict[str, bool]:
        """下载模型 (H3 M1): the session's own handshake refuses a stale or a
        second click; nothing downloads without it."""
        host = _host()
        if not host.respond_ltx_download(body.request_id, body.approved):
            raise HTTPException(status_code=409, detail="下载请求已过期，请刷新后重试")
        return {"ok": True}

    @app.post("/control/workflow/ltx-download/stop")
    def workflow_ltx_download_stop() -> dict[str, bool]:
        host = _host()
        with _mapped():
            host.stop_ltx_download()
        return {"ok": True}

    @app.post("/control/workflow/films/{film_id}/open", status_code=204)
    def workflow_open(film_id: str, body: OpenBody) -> Response:
        from taskpaw_v3.workflow import board

        host = _host()
        inbox = host.finalizer.inbox if host.finalizer is not None else None
        with _mapped():
            return _open(board.folder(_runtime(host), inbox, film_id, body.what))

    @app.post("/control/workflow/packages/{package_id}/open", status_code=204)
    def workflow_open_package(package_id: str) -> Response:
        """打开文件夹 of a package row (H2 R2): a current package, under the INBOX."""
        from taskpaw_v3.workflow import board

        host = _host()
        inbox = host.finalizer.inbox if host.finalizer is not None else None
        with _mapped():
            return _open(board.package_folder(_runtime(host), inbox, package_id))
