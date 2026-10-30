"""片库 routes on the loopback control API, controller only (#361 §10, #362 L1).

The library lives on the running workflow host (`host.library` and its
`host.library_scanner`); the routes answer 503 until it is open. Library
modules are imported per request, as the other workflow routes do.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from taskpaw_v3.core.config import WorkflowSettings

from . import workflow_reads as reads
from .workflow_reads import WorkflowProvider

if TYPE_CHECKING:
    from taskpaw_v3.workflow.library_archive import LibraryArchive
    from taskpaw_v3.workflow.library_scan import LibraryScanner
    from taskpaw_v3.workflow.library_store import LibraryStore

BASE = "/control/workflow/library"


class LibraryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["actor", "category"]
    path: str = Field(min_length=1, max_length=1024)
    label: Optional[str] = Field(default=None, max_length=256)
    role: Optional[Literal["archive", "readonly"]] = None
    prefixes: list[str] = Field(default_factory=list, max_length=100)


class LibraryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(default=None, max_length=256)
    role: Optional[Literal["archive", "readonly"]] = None
    prefixes: Optional[list[str]] = Field(default=None, max_length=100)


class AliasBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(max_length=256)


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Optional[str] = Field(default=None, max_length=1024)


@contextmanager
def _errors() -> Iterator[None]:
    from taskpaw_v3.workflow.library_store import LibraryError

    try:
        yield
    except LibraryError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from None


def etag_matches(header: str | None, etag: str) -> bool:
    """RFC 7232 If-None-Match: a list, weak entries and `*` (Filmpaw's rule)."""
    for raw in (header or "").split(","):
        candidate = raw.strip()
        if candidate.startswith(("W/", "w/")):
            candidate = candidate[2:]
        if candidate in ("*", etag):
            return True
    return False


def thumb_response(
    store: LibraryStore | LibraryArchive, performer_id: str, if_none_match: str | None
) -> Response:
    """A performer's thumbnail with its ETag; 304 when the client has it."""
    found = store.thumb(performer_id)
    if found is None:
        raise HTTPException(status_code=404, detail="无缩略图")
    data, etag = found
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if etag_matches(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="image/jpeg", headers=headers)


def register_library_routes(
    app: FastAPI, settings: WorkflowSettings, provider: WorkflowProvider | None
) -> None:
    def _host() -> Any:
        if settings.role != "controller":
            raise HTTPException(status_code=409, detail="片库只在主控上")
        host = reads.require_host(settings, provider)
        if getattr(host, "library", None) is None:
            reason = getattr(host, "library_reason", "") or "片库尚未启动"
            raise HTTPException(status_code=503, detail=reason)
        return host

    def _store() -> LibraryStore:
        return _host().library

    def _scanner() -> LibraryScanner:
        return _host().library_scanner

    @app.get(f"{BASE}/libraries")
    def library_list() -> dict[str, Any]:
        return {"items": _store().libraries()}

    @app.post(f"{BASE}/libraries", status_code=201)
    def library_add(body: LibraryBody) -> dict[str, Any]:
        host = _host()
        with _errors():
            library = host.library.add_library(
                body.kind,
                body.path,
                label=body.label,
                role=body.role,
                prefixes=body.prefixes,
            )
        host.library_scanner.request(library["id"])
        return library

    @app.patch(f"{BASE}/libraries/{{library_id}}")
    def library_update(library_id: str, body: LibraryPatch) -> dict[str, Any]:
        with _errors():
            return _store().update_library(
                library_id, label=body.label, role=body.role, prefixes=body.prefixes
            )

    @app.delete(f"{BASE}/libraries/{{library_id}}", status_code=204)
    def library_remove(library_id: str) -> Response:
        with _errors():
            _store().remove_library(library_id)
        return Response(status_code=204)

    @app.post(f"{BASE}/libraries/{{library_id}}/scan")
    def library_scan(library_id: str) -> dict[str, Any]:
        host = _host()
        with _errors():
            host.library.library(library_id)  # 404 for an unknown id
        host.library_scanner.request(library_id)
        return host.library_scanner.status()

    @app.post(f"{BASE}/scan-all")
    def library_scan_all() -> dict[str, Any]:
        scanner = _scanner()
        scanner.request_all()
        return scanner.status()

    @app.get(f"{BASE}/scan-status")
    def library_scan_status() -> dict[str, Any]:
        return _scanner().status()

    @app.post(f"{BASE}/performers/{{performer_id}}/aliases", status_code=201)
    def library_add_alias(performer_id: str, body: AliasBody) -> dict[str, Any]:
        with _errors():
            return _store().add_alias(performer_id, body.alias)

    @app.delete(f"{BASE}/aliases/{{alias_id}}", status_code=204)
    def library_delete_alias(alias_id: int) -> Response:
        with _errors():
            _store().delete_alias(alias_id)
        return Response(status_code=204)

    @app.get(f"{BASE}/rules")
    def library_rules() -> dict[str, Any]:
        return _store().rules()

    @app.put(f"{BASE}/rules")
    def library_put_rules(body: dict[str, Any]) -> dict[str, Any]:
        with _errors():
            return _store().put_rules(body)

    @app.get(f"{BASE}/import/preview")
    def library_import_preview(path: str = "") -> dict[str, Any]:
        from taskpaw_v3.workflow import library_import

        with _errors():
            return library_import.preview(_store(), path or None)

    @app.post(f"{BASE}/import")
    def library_import_run(body: ImportBody) -> dict[str, Any]:
        from taskpaw_v3.workflow import library_import

        host = _host()
        with _errors():
            result = library_import.import_filmpaw(host.library, body.path or None)
        for library_id in result["added"]:
            host.library_scanner.request(library_id)
        return result
