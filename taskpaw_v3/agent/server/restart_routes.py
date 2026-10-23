"""#299 §3/R11: 立即重启 on the loopback control API.

`GET /control/restart` → `{required, fields, blocked}`: whether saved
settings wait for a restart (and which), and why one cannot happen now.
`POST /control/restart {boot_id}` (the boot the window was built for) takes
the settings admission lock and refuses while another save holds it
(`operation_busy`), while this machine's GPU runs a task (「本机正在运行：…」)
or while a GPU worker holds dispatched work (`restart_busy`). Remote work,
queued films and crash-safe stages never block. Success answers 202; after
the reply the service stops gracefully and exits with `restart.EXIT_CODE`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from taskpaw_v3.core import gpu_lease

if TYPE_CHECKING:
    from taskpaw_v3.agent.server.admin import MonitorAdmin

BUSY_WORKER = "本机正在处理主控派来的影片"
BUSY_SAVE = "正在保存其他设置，请稍后再试"


def pending_fields(admin: "MonitorAdmin") -> list[str]:
    """The saved, not-live fields that differ from the running ones;
    `workflow` is listed per field (`workflow.inbox_root`)."""
    running = admin.running()
    fields: list[str] = []
    for name in admin._NON_LIVE_CONFIG:
        saved, now = admin.desired(name), getattr(running, name)
        if saved == now:
            continue
        if name == "workflow":
            a, b = saved.model_dump(), now.model_dump()
            fields.extend(f"workflow.{k}" for k in a if a[k] != b.get(k))
        else:
            fields.append(name)
    return fields


def blocked_reason(worker_provider: Optional[Callable[[], Any]]) -> Optional[str]:
    if gpu_lease.holder() is not None:
        label = gpu_lease.blocking_label() or "GPU 任务"
        return f"本机正在运行：{label}"
    host = worker_provider() if worker_provider is not None else None
    if host is not None:
        try:
            busy = bool(host.summary().get("busy"))
        except Exception:
            busy = True  # unknown is busy
        if busy:
            return BUSY_WORKER
    return None


def register_restart_routes(
    app: FastAPI,
    admin: Optional["MonitorAdmin"],
    *,
    boot_id: Optional[str],
    on_restart: Optional[Callable[[], None]],
    worker_provider: Optional[Callable[[], Any]] = None,
) -> None:
    @app.get("/control/restart")
    def restart_view() -> dict[str, Any]:
        fields = pending_fields(admin) if admin is not None else []
        return {
            "required": bool(fields),
            "fields": fields,
            "blocked": blocked_reason(worker_provider),
            "available": on_restart is not None,
        }

    @app.post("/control/restart")
    async def restart_now(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError:
            body = None
        asked = body.get("boot_id") if isinstance(body, dict) else None
        if on_restart is None or admin is None:
            return JSONResponse(
                status_code=409,
                content={"detail": "请退出并重新打开 TaskPaw", "code": "unsupported"},
            )
        if not isinstance(asked, str) or asked != boot_id:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "TaskPaw 已重新启动过，请刷新",
                    "code": "stale_boot",
                },
            )
        try:
            with admin._settings_admission():
                why = blocked_reason(worker_provider)
        except ValueError:
            return JSONResponse(
                status_code=409,
                content={"detail": BUSY_SAVE, "code": "operation_busy"},
            )
        if why:
            return JSONResponse(
                status_code=409,
                content={"detail": f"暂不能重启：{why}", "code": "restart_busy"},
            )
        return JSONResponse(
            {"restarting": True}, status_code=202, background=BackgroundTask(on_restart)
        )
