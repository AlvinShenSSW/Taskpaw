"""The five bounded workflow reads exposed to the LAN Hub (#286)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from taskpaw_v3.core.config import WorkflowSettings

from . import workflow_reads as reads

MAX_JSON_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def register_workflow_network(
    app: FastAPI,
    settings: WorkflowSettings,
    provider: reads.WorkflowProvider | None,
    authorize: Callable[[Request], bool],
) -> None:
    def guard(request: Request) -> None:
        if not authorize(request):
            raise HTTPException(
                status_code=401,
                detail="unauthorized",
                headers={"WWW-Authenticate": 'Bearer realm="TaskPaw"'},
            )
        # P2-2 S-5: a worker is never the Hub's workflow source
        if not settings.enabled or settings.role == "worker":
            raise HTTPException(status_code=404, detail="未启用媒体处理")

    def bounded_json(value: dict) -> JSONResponse:
        response = JSONResponse(value)
        if len(response.body) > MAX_JSON_BYTES:
            raise HTTPException(status_code=413, detail="媒体处理快照过大")
        return response

    def image(film_id: str, kind: Literal["poster", "cover"]) -> Response:
        from taskpaw_v3.workflow import board

        path = reads.image_path(settings, provider, film_id, kind)
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_IMAGE_BYTES + 1)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="封面已不存在") from None
        except OSError:
            raise HTTPException(status_code=503, detail="封面暂不可用") from None
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="封面过大")
        return Response(content, media_type=board.image_type(path))

    @app.get("/workflow/films", dependencies=[Depends(guard)])
    def films(done_page: int = Query(default=1, ge=1)) -> JSONResponse:
        return bounded_json(reads.films(settings, provider, done_page))

    @app.get("/workflow/plans", dependencies=[Depends(guard)])
    def plans() -> JSONResponse:
        return bounded_json(reads.plans(settings, provider))

    @app.get("/workflow/films/{film_id}", dependencies=[Depends(guard)])
    def film(film_id: str) -> JSONResponse:
        return bounded_json(reads.film(settings, provider, film_id))

    @app.get("/workflow/films/{film_id}/poster", dependencies=[Depends(guard)])
    def poster(film_id: str) -> Response:
        return image(film_id, "poster")

    @app.get("/workflow/films/{film_id}/cover", dependencies=[Depends(guard)])
    def cover(film_id: str) -> Response:
        return image(film_id, "cover")
