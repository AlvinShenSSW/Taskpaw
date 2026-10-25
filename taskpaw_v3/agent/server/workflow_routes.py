"""媒体入库 routes on the loopback control API only (T06-P1 §3, §7 D05–D09, §8).

The workflow host starts after the agent announced readiness, so the routes
read it through a late-bound provider and answer only while it runs (D07).
Workflow modules are imported per request: a broken workflow never stops the
control API from starting. Every path is resolved from the film id (U6).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from starlette.concurrency import run_in_threadpool

from taskpaw_v3.core.config import WorkflowSettings

if TYPE_CHECKING:
    from taskpaw_v3.workflow.local_host import WorkflowHostLike
    from taskpaw_v3.workflow.local_runtime import LocalRuntime

from . import workflow_reads as reads
from .workflow_reads import CodedConflict, WorkflowProvider, _mapped
from .workflow_reads import off_state as off_state  # retained for callers


class ConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    performer_id: str | None = Field(default=None, min_length=1, max_length=256)
    target_dir: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def one_target(self) -> ConfirmBody:
        if (self.performer_id is None) == (self.target_dir is None):
            raise ValueError("exactly one of performer_id and target_dir is required")
        return self


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str = Field(min_length=1, max_length=64)


class OpenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    what: Literal["inbox", "output"]


class OpenFolderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    which: Literal["output", "archive"]


class PlanItem(BaseModel):
    """One film as the 待处理 card showed it (U1 P3')."""

    model_config = ConfigDict(extra="forbid")
    film_id: str = Field(min_length=1, max_length=256)
    input_revision: int = Field(gt=0, strict=True)
    ltx_model: Optional[Literal["standard", "good", "high"]]
    # P2-4 R6: null = 自动
    machine: Optional[str] = Field(..., min_length=1, max_length=256)


class PlansBody(BaseModel):
    """确认入队 of one card or 全部按推荐确认 of the list: each its own CAS."""

    model_config = ConfigDict(extra="forbid")
    plans: list[PlanItem] = Field(min_length=1, max_length=500)


class PauseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: StrictBool


class DeviceBody(BaseModel):
    """添加设备: a name and the worker's address (`host[:port]`)."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    address: str = Field(min_length=1, max_length=255)


class VersionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0, strict=True)


class ApproveBody(VersionBody):
    restore: StrictBool  # OD7 (#298 R10): restore only, no `asr`


class AdmissionBody(VersionBody):
    state: Literal["enabled", "draining"]


class RequeueBody(BaseModel):
    """停止并重新排队 of a running remote restore or ASR (P2-3 R9)."""

    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=64)


class PinBody(BaseModel):
    """改派 (P2-4 R7): the plan's new pin; null = 自动."""

    model_config = ConfigDict(extra="forbid")
    input_revision: int = Field(gt=0, strict=True)
    expected_version: int = Field(gt=0, strict=True)
    worker_id: Optional[str] = Field(..., min_length=1, max_length=64)


class AttestBody(BaseModel):
    """判定已停止 (P2-4 R8) of one unresolved remote assignment."""

    model_config = ConfigDict(extra="forbid")
    assignment_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(gt=0, strict=True)


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


def register_workflow_routes(
    app: FastAPI,
    settings: WorkflowSettings,
    provider: WorkflowProvider | None,
    *,
    worker_api_host: str = "127.0.0.1",
    worker_provider: Callable[[], Any] | None = None,
) -> None:
    @app.exception_handler(CodedConflict)
    async def _coded_conflict(_request: Any, exc: CodedConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": exc.detail, "code": exc.code}
        )

    def _host() -> WorkflowHostLike:
        return reads.require_host(settings, provider)

    def _runtime(host: WorkflowHostLike) -> LocalRuntime:
        return reads.runtime(host)

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

    def _dispatcher(host: WorkflowHostLike) -> Any:
        dispatcher = getattr(host, "dispatcher", None)
        if dispatcher is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        return dispatcher

    @app.get("/control/workflow/films")
    def workflow_films(done_page: int = Query(default=1, ge=1)) -> dict[str, Any]:
        return reads.films(settings, provider, done_page)

    @app.get("/control/workflow/films/{film_id}")
    def workflow_film(film_id: str) -> dict[str, Any]:
        return reads.film(settings, provider, film_id)

    @app.get("/control/workflow/films/{film_id}/poster")
    def workflow_poster(film_id: str) -> FileResponse:
        from taskpaw_v3.workflow import board

        path = reads.image_path(settings, provider, film_id, "poster")
        return FileResponse(path, media_type=board.image_type(path))

    @app.get("/control/workflow/films/{film_id}/cover")
    def workflow_cover(film_id: str) -> FileResponse:
        from taskpaw_v3.workflow import board

        path = reads.image_path(settings, provider, film_id, "cover")
        return FileResponse(path, media_type=board.image_type(path))

    @app.get("/control/workflow/plans")
    def workflow_plans() -> dict[str, Any]:
        return reads.plans(settings, provider)

    @app.post("/control/workflow/plans")
    def workflow_confirm_plans(body: PlansBody) -> dict[str, Any]:
        """确认入队 / 全部按推荐确认: a result per item; a success wakes dispatch."""
        from taskpaw_v3.workflow.planning import confirm_plans

        host = _host()
        with _mapped():
            results = confirm_plans(
                _runtime(host),
                [item.model_dump() for item in body.plans],
                remote=getattr(host, "fleet", None) is not None,
            )
        dispatcher = getattr(host, "dispatcher", None)
        if dispatcher is not None and any(r["ok"] for r in results):
            dispatcher.wake()
        scheduler = getattr(host, "scheduler", None)  # P2-3 R13
        if scheduler is not None and any(r["ok"] for r in results):
            scheduler.wake()
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
    def workflow_target(
        film_id: str, performer_id: str = "", target_dir: str | None = None
    ) -> dict[str, Any]:
        """V3 §7.3: the archive target preflight and the NFO actor's candidates."""
        host = _host()
        if host.finalizer is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        with _mapped():
            return host.finalizer.preflight(film_id, performer_id or None, target_dir)

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
            return {
                "approval_id": host.confirm(film_id, body.performer_id, body.target_dir)
            }

    @app.post("/control/workflow/films/{film_id}/retry")
    def workflow_retry(film_id: str, body: RetryBody) -> dict[str, bool]:
        host = _host()
        with _mapped():
            host.retry(film_id, body.stage)
        return {"ok": True}

    @app.post("/control/workflow/films/{film_id}/requeue")
    def workflow_requeue(film_id: str, body: RequeueBody) -> dict[str, bool]:
        """停止并重新排队: the worker cancels its Job at its next heartbeat and
        finishes `cancelled`, a free retry routed again (OD3)."""
        host = _host()
        requeue = getattr(host, "requeue", None)
        if not callable(requeue):
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        with _mapped():
            requeue(film_id, body.attempt_id)
        return {"ok": True}

    def _remote_host() -> WorkflowHostLike:
        host = _host()
        if not settings.remote_workers or getattr(host, "fleet", None) is None:
            raise HTTPException(
                status_code=409, detail="未开启远程设备（workflow.remote_workers）"
            )
        return host

    def _wake(host: Any) -> None:
        for name in ("scheduler", "dispatcher"):
            target = getattr(host, name, None)
            if target is not None:
                target.wake()

    @app.post("/control/workflow/films/{film_id}/pin")
    def workflow_pin(film_id: str, body: PinBody) -> dict[str, bool]:
        """改派 (R7): 409 `route_claimed` 「已开始处理」 when a claim won."""
        from taskpaw_v3.workflow import operator_actions as ops

        host = _remote_host()
        with _mapped():
            ops.reassign(
                _runtime(host),
                fleet=getattr(host, "fleet", None),
                film_id=film_id,
                input_revision=body.input_revision,
                expected_version=body.expected_version,
                worker_id=body.worker_id,
            )
        _wake(host)
        return {"ok": True}

    @app.post("/control/workflow/films/{film_id}/attest-stopped")
    def workflow_attest(film_id: str, body: AttestBody) -> dict[str, Any]:
        """判定已停止 (R8, C4): both channels dead for 5 min, then a free
        retry and a new routing pass."""
        from taskpaw_v3.workflow import operator_actions as ops

        host = _remote_host()
        with _mapped():
            try:
                result = ops.attest(
                    _runtime(host),
                    fleet=getattr(host, "fleet", None),
                    channel=reads.status_channel(host),
                    film_id=film_id,
                    assignment_id=body.assignment_id,
                    expected_version=body.expected_version,
                )
            except ops.ActionRefused as exc:
                raise CodedConflict(exc.detail, exc.code) from None
        _wake(host)
        return result

    @app.post("/control/workflow/films/{film_id}/recheck-source")
    def workflow_recheck_source(film_id: str) -> dict[str, Any]:
        """重新检查 (P3 E2): the INBOX existence check at once; a returned
        source takes the film out of 片源丢失 and wakes dispatch."""
        from taskpaw_v3.workflow import board

        host = _host()
        sources = getattr(host, "sources", None)
        if sources is None:
            raise HTTPException(status_code=503, detail="媒体流水线尚未启动")
        with _mapped():
            if board.board_film(_runtime(host), film_id) is None:
                raise HTTPException(status_code=404, detail="未知影片")
            missing = sources.check(film_id, force=True)
        _wake(host)
        return {
            "ok": True,
            # the share could not be reached: nothing was learned (#304 IR4)
            "unreachable": sources.unknown(film_id),
            "source_missing": None
            if missing is None
            else {
                "paths": missing["paths"],
                "found_at": board.iso(missing["found_at"]),
            },
        }

    @app.post("/control/workflow/films/{film_id}/remove")
    def workflow_remove(film_id: str) -> dict[str, Any]:
        """删除此任务 (P3 E3): refused (409, coded) while a stage runs or is
        claimed, a route is open, or the film was confirmed or archived;
        otherwise the task and its private work dirs go. INBOX, the output
        directory and NAS are never touched."""
        from taskpaw_v3.workflow import operator_actions as ops

        host = _host()
        with _mapped():
            try:
                result = ops.remove_film(
                    _runtime(host),
                    film_id=film_id,
                    sources=getattr(host, "sources", None),
                    log=getattr(host, "workflow_log", None),
                )
            except ops.ActionRefused as exc:
                raise CodedConflict(exc.detail, exc.code) from None
        _wake(host)
        return result

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

    @app.post("/control/workflow/films/{film_id}/open-folder", status_code=204)
    def workflow_open_folder(film_id: str, body: OpenFolderBody) -> Response:
        from taskpaw_v3.workflow import board

        with _mapped():
            path = board.folder(_runtime(_host()), None, film_id, body.which)
            if path is None:
                raise HTTPException(status_code=409, detail="目录已不存在")
            try:
                return _open(path)
            except HTTPException as exc:
                if exc.status_code == 500 and not path.is_dir():
                    raise HTTPException(
                        status_code=409, detail="目录已不存在"
                    ) from None
                raise

    # ── 设备 (P2-2): enrollment and admission of remote workers ─────────
    def _device(run: Any) -> Any:
        from taskpaw_v3.workflow.models import WorkflowError

        host = _host()
        with _mapped():
            try:
                return run(_runtime(host), getattr(host, "fleet", None))
            except WorkflowError as exc:
                if exc.code == "owner_unresolved":
                    raise CodedConflict(
                        "仍有未结束的任务，不能退休", exc.code
                    ) from None
                if exc.code == "invalid_request":
                    raise HTTPException(
                        status_code=422, detail="地址无效（主机名或 IP，可带端口）"
                    ) from None
                if exc.code == "not_found":
                    raise HTTPException(status_code=404, detail="未知设备") from None
                raise

    def _remote_on() -> None:
        if not settings.remote_workers:
            raise HTTPException(
                status_code=409, detail="未开启远程设备（workflow.remote_workers）"
            )

    @app.get("/control/workflow/devices")
    def workflow_devices() -> dict[str, Any]:
        from taskpaw_v3.workflow import devices

        channel = reads.status_channel(_host())
        return _device(
            lambda rt, fleet: devices.device_view(
                rt, fleet, remote_workers=settings.remote_workers, channel=channel
            )
        )

    @app.post("/control/workflow/devices")
    def workflow_add_device(body: DeviceBody) -> dict[str, Any]:
        """添加设备: the pairing code is shown once and never logged."""
        from taskpaw_v3.workflow import devices

        _remote_on()
        return _device(
            lambda rt, _fleet: devices.add_device(
                rt,
                body.name,
                body.address,
                host=worker_api_host,
                port=settings.worker_api_port,
            )
        )

    @app.post("/control/workflow/devices/{worker_id}/pairing")
    def workflow_repair_device(worker_id: str, body: VersionBody) -> dict[str, Any]:
        from taskpaw_v3.workflow import devices

        _remote_on()
        return _device(
            lambda rt, _fleet: devices.repair(
                rt,
                worker_id,
                body.expected_version,
                host=worker_api_host,
                port=settings.worker_api_port,
            )
        )

    @app.post("/control/workflow/devices/{worker_id}/approve")
    def workflow_approve_device(worker_id: str, body: ApproveBody) -> dict[str, bool]:
        from taskpaw_v3.workflow import devices

        _remote_on()  # IR1: approving hands the GPU stages to the worker
        _device(
            lambda rt, _fleet: devices.approve(
                rt,
                worker_id,
                body.expected_version,
                restore=body.restore,
            )
        )
        return {"ok": True}

    @app.post("/control/workflow/devices/{worker_id}/admission")
    def workflow_device_admission(
        worker_id: str, body: AdmissionBody
    ) -> dict[str, bool]:
        from taskpaw_v3.workflow import devices

        _remote_on()
        _device(
            lambda rt, _fleet: devices.set_admission(
                rt, worker_id, body.expected_version, body.state
            )
        )
        return {"ok": True}

    @app.post("/control/workflow/devices/{worker_id}/revoke")
    def workflow_revoke_device(worker_id: str, body: VersionBody) -> dict[str, bool]:
        from taskpaw_v3.workflow import devices

        _device(
            lambda rt, fleet: devices.revoke(
                rt, worker_id, body.expected_version, fleet
            )
        )
        return {"ok": True}

    @app.post("/control/workflow/devices/{worker_id}/retire")
    def workflow_retire_device(worker_id: str, body: VersionBody) -> dict[str, bool]:
        from taskpaw_v3.workflow import devices

        _device(lambda rt, _fleet: devices.retire(rt, worker_id, body.expected_version))
        return {"ok": True}

    # ── the worker console (P2-2, role=worker) ──────────────────────────
    def _worker_off() -> dict[str, Any]:
        if settings.role != "worker":
            reason = "本机是主控，不是工作机"
        elif not settings.enabled:
            reason = "未启用"
        else:
            reason = "启动失败（见日志）"
        return {
            "state": "off",
            "reason": reason,
            "role": settings.role,
            "controller": None,
            "credential": None,
            "staging_dir": settings.staging_dir,
            "staging_cap_gb": settings.staging_cap_gb,
        }

    @app.get("/control/workflow/worker")
    def workflow_worker() -> dict[str, Any]:
        host = worker_provider() if worker_provider is not None else None
        if host is None or settings.role != "worker":
            return _worker_off()
        return {**host.status(), "role": "worker"}

    @app.post("/control/workflow/worker/pause")
    def workflow_worker_pause(body: PauseBody) -> dict[str, Any]:
        """暂停领取 / 恢复领取 (P2-3 R12): no new claim; in-flight runs,
        uploads and prefetch continue; the controller sees it at the next
        heartbeat and treats the worker like 停领 (S8)."""
        host = worker_provider() if worker_provider is not None else None
        if host is None or settings.role != "worker":
            raise HTTPException(status_code=409, detail=_worker_off()["reason"])
        host.set_paused(body.paused)
        return {"paused": body.paused}

    @app.post("/control/workflow/worker/join")
    async def workflow_worker_join(request: Request) -> dict[str, Any]:
        """加入 / 重新加入 with `{code, staging_dir}`: parsed by hand so that no
        validation error ever echoes the code back."""
        from taskpaw_v3.workflow.remote_worker import JoinError

        try:
            body = await request.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        staging = body.get("staging_dir") if isinstance(body, dict) else None
        if (
            not isinstance(code, str)
            or not isinstance(staging, str)
            or not 0 < len(code) <= 1024
            or not 0 < len(staging) <= 1024
        ):
            raise HTTPException(status_code=422, detail="请填写配对码与暂存目录")
        host = worker_provider() if worker_provider is not None else None
        if host is None or settings.role != "worker":
            raise HTTPException(status_code=409, detail=_worker_off()["reason"])
        try:
            joined = await run_in_threadpool(host.join, code, staging)
        except JoinError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {**joined, "role": "worker"}

    @app.post("/control/workflow/packages/{package_id}/open", status_code=204)
    def workflow_open_package(package_id: str) -> Response:
        """打开文件夹 of a package row (H2 R2): a current package, under the INBOX."""
        from taskpaw_v3.workflow import board

        host = _host()
        inbox = host.finalizer.inbox if host.finalizer is not None else None
        with _mapped():
            return _open(board.package_folder(_runtime(host), inbox, package_id))
