"""数据分析 route on the loopback control API, controller only (EPIC #387 C2).

`GET /control/workflow/analytics?since=<ISO 8601 with offset>` (design §7):
per-film facts, failures, machines, device spans, translation outages and the
INBOX, aggregated by the page. The body is built of plain values and returned
as a `JSONResponse`: no `jsonable_encoder` walk over thousands of rows, and a
non-finite number fails loudly instead of producing invalid JSON.
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.tasklog import get_task_log

from . import workflow_reads as reads
from .workflow_reads import WorkflowProvider

PATH = "/control/workflow/analytics"
NOT_CONTROLLER = "数据分析只在主控上"
NOT_READY = "数据分析尚未启动"


def register_analytics_routes(
    app: FastAPI, settings: WorkflowSettings, provider: WorkflowProvider | None
) -> None:
    @app.get(PATH)
    def workflow_analytics(since: Optional[AwareDatetime] = None) -> JSONResponse:
        from taskpaw_v3.workflow import analytics

        if settings.role != "controller":
            raise HTTPException(status_code=409, detail=NOT_CONTROLLER)
        host = reads.require_host(settings, provider)
        cache = getattr(host, "analytics", None)
        if cache is None:
            raise HTTPException(status_code=503, detail=NOT_READY)
        dispatcher = getattr(host, "dispatcher", None)
        with reads._mapped():
            body = analytics.build(
                reads.runtime(host),
                cache,
                since=since,
                library=getattr(host, "library", None),
                device_history=getattr(host, "device_history", None),
                fleet=getattr(host, "fleet", None),
                dispatcher_paused=bool(getattr(dispatcher, "paused", False)),
                task_log=get_task_log(),
                inbox_root=settings.inbox_root,
            )
        return JSONResponse(body)
