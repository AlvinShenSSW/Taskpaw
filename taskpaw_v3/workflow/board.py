"""The 媒体入库 page's view of the workflow (T06-P1 §3, §7 D02–D04, D11, D12, N01).

`film_row` is pure over one `film_board` row and the host's waiting reasons;
`film_detail` adds the final package, its NFO summary and the cleanup results.
Every path is resolved here from the film id, never taken from a client (U6).
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Any

from . import models as m
from .auth import STAGE_ROLES
from .local_runtime import LocalRuntime
from .manifest import parse_nfo_summary

STAGE_ORDER: tuple[m.Stage, ...] = (
    "intake",
    "restore",
    "asr",
    "translate",
    "return",
    "scrape",
    "archive",
    "cleanup_input",
    "cleanup_workcopy",
)
UNKNOWN_NOTE = "等待确认停止"
NFO_LIMIT = 262144
_FINISHED = ("succeeded", "skipped")
_TROUBLE = ("failed", "blocked", "unknown")
_CLEANUPS: tuple[m.Stage, ...] = ("cleanup_input", "cleanup_workcopy")
_RETRYABLE = ("failed", "blocked")
_KEPT_OK = ("pending", "deleted", "already_absent")
_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def _time(value: Any) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _place(film: m.BoardFilm) -> dict[str, Any]:
    """Group, shown stage and actions (D04): done → attention → archiving → confirm → active."""
    stages = {stage.stage: stage for stage in film.stages}
    statuses = {name: stage.status for name, stage in stages.items()}
    ordered = [name for name in STAGE_ORDER if name in stages]
    first = next((name for name in ordered if statuses[name] not in _FINISHED), None)
    place: dict[str, Any] = {
        "group": "active",
        "stage": first or (ordered[-1] if ordered else None),
        "can_confirm": False,
        "retry_stage": None,
    }
    archive = statuses.get("archive")
    if ordered and all(statuses.get(name) == "succeeded" for name in _CLEANUPS):
        place["group"] = "done"
        return place
    trouble = next(
        (name for name in ordered if name != "archive" and statuses[name] in _TROUBLE),
        None,
    )
    if trouble is not None:
        place.update(group="attention", stage=trouble)
        if trouble in STAGE_ROLES and statuses[trouble] in _RETRYABLE:
            place["retry_stage"] = trouble
        return place
    if (
        film.archive_open
        or archive == "succeeded"
        or (film.approval_active and archive in ("pending", "failed"))
    ):
        place["group"] = "archiving"
    elif statuses.get("scrape") == "succeeded" and archive in ("pending", "failed"):
        place.update(group="confirm", stage="archive", can_confirm=True)
    return place


def _note(
    film: m.BoardFilm, stage: m.BoardStage | None, waiting: dict[str, str]
) -> str | None:
    """D02: a failure's own reason first, otherwise why the film waits."""
    if stage is not None and stage.status == "unknown":
        return UNKNOWN_NOTE
    if stage is not None and stage.status in _RETRYABLE:
        reason = (
            film.archive_error or stage.note if stage.stage == "archive" else stage.note
        )
        if reason:
            return reason
    return waiting.get(film.film_id)


def film_row(film: m.BoardFilm, waiting: dict[str, str]) -> dict[str, Any]:
    """One FilmRow of `GET /control/workflow/films` (§8)."""
    place = _place(film)
    stage = next((s for s in film.stages if s.stage == place["stage"]), None)
    # confirmed again after a failed archive: the old failure is no longer the reason
    note = (
        waiting.get(film.film_id)
        if place["group"] == "archiving"
        else _note(film, stage, waiting)
    )
    return {
        "film_id": film.film_id,
        "name": film.final_name or film.display_name,
        "group": place["group"],
        "stage": stage.stage if stage else None,
        "status": stage.status if stage else None,
        "note": note,
        "can_confirm": place["can_confirm"],
        "can_retry": place["retry_stage"] is not None,
        "retry_stage": place["retry_stage"],
        "created_at": _time(film.created_at),
    }


def film_rows(
    films: tuple[m.BoardFilm, ...], waiting: dict[str, str]
) -> list[dict[str, Any]]:
    return [film_row(film, waiting) for film in films]


# ── detail (U2) ──────────────────────────────────────────────────────────
def board_film(rt: LocalRuntime, film_id: str) -> m.BoardFilm | None:
    films = rt.store.film_board(rt.operator, film_id=film_id)
    return films[0] if films else None


def final_manifest(rt: LocalRuntime, film_id: str) -> m.FinalManifest | None:
    film = rt.store.get_film(rt.operator, film_id).film
    if film.current_final_revision is None:
        return None
    return rt.store.get_final_manifest(
        rt.operator, film_id, film.current_final_revision
    )


def output_dir(rt: LocalRuntime, final: m.FinalManifest) -> Path:
    return rt.work_dir / final.output_relative_path


def _item_path(
    rt: LocalRuntime, final: m.FinalManifest, role: m.ItemRole
) -> Path | None:
    item = next((i for i in final.items if i.role == role), None)
    if item is None:
        return None
    path = output_dir(rt, final) / item.relative_path
    return path if path.is_file() else None


def poster_path(rt: LocalRuntime, film_id: str) -> Path | None:
    """The current final's poster while its work copy exists (D11)."""
    if board_film(rt, film_id) is None:
        return None
    final = final_manifest(rt, film_id)
    return _item_path(rt, final, "poster") if final else None


def image_type(path: Path) -> str:
    return _IMAGE_TYPES.get(path.suffix.lower(), "image/jpeg")


def folder(
    rt: LocalRuntime, inbox: Path | None, film_id: str, what: str
) -> Path | None:
    """The INBOX package or the film's output directory, when it exists (D09)."""
    if board_film(rt, film_id) is None:
        return None
    if what == "inbox":
        package = rt.store.film_workflow(rt.operator, film_id).package_path
        path = inbox / package if inbox is not None and package else None
    else:
        final = final_manifest(rt, film_id)
        path = output_dir(rt, final) if final else None
    return path if path is not None and path.is_dir() else None


def _read_nfo(path: Path | None) -> tuple[str | None, tuple[str, ...]]:
    if path is None:
        return None, ()
    try:
        with path.open("rb") as stream:
            payload = stream.read(NFO_LIMIT + 1)
    except OSError:
        return None, ()
    return parse_nfo_summary(payload, max_bytes=NFO_LIMIT)


def _kept(wf: m.FilmWorkflow, paths: dict[str, str]) -> list[dict[str, str]]:
    """Members the newest cleanup of each scope did not remove (U2 逐成员结果)."""
    latest: dict[str, m.WorkflowCleanupOperation] = {}
    for operation in wf.cleanup_operations:
        latest[operation.scope] = operation
    return [
        {"path": paths.get(item.item_id, item.item_id), "status": item.status}
        for scope in ("input", "workcopy")
        if scope in latest
        for item in latest[scope].items
        if item.status not in _KEPT_OK
    ]


def film_detail(
    rt: LocalRuntime, film_id: str, waiting: dict[str, str]
) -> dict[str, Any] | None:
    """`GET /control/workflow/films/{film_id}` (§8); None for an unknown film."""
    film = board_film(rt, film_id)
    if film is None:
        return None
    row = film_row(film, waiting)
    op = rt.operator
    view = rt.store.get_film(op, film_id).film
    wf = rt.store.film_workflow(op, film_id)
    final = final_manifest(rt, film_id)
    source = (
        rt.store.get_input_manifest(op, film_id, view.current_input_revision)
        if view.current_input_revision is not None
        else None
    )
    title, actors = _read_nfo(_item_path(rt, final, "nfo") if final else None)
    marks = set(final.postprocess.actual_marks) if final else set()
    target = wf.approval.binding.target if wf.approval else None
    paths = {
        item.item_id: item.relative_path
        for manifest in (source, final)
        if manifest is not None
        for item in manifest.items
    }
    return {
        "film_id": film_id,
        "name": row["name"],
        "number": film.final_name,
        "title": title,
        "actors": list(actors),
        "marks": {
            "restored": "restored" in marks,
            "chinese_subtitles": "chinese_subtitles" in marks,
            "uhd": "4k" in marks,
        },
        "files": [
            {
                "role": item.role,
                "path": item.relative_path,
                "size": item.size_bytes,
                "volume": item.volume_ordinal,
            }
            for item in (final.items if final else ())
        ],
        # intake items are already relative to their INBOX package (N01)
        "inputs": [
            {"path": item.relative_path, "size": item.size_bytes}
            for item in (source.items if source else ())
        ],
        "workcopy_count": len(final.items) if final else 0,
        "has_poster": final is not None and _item_path(rt, final, "poster") is not None,
        "group": row["group"],
        "can_confirm": row["can_confirm"],
        "can_retry": row["can_retry"],
        "retry_stage": row["retry_stage"],
        "note": row["note"],
        "stages": [
            {
                "stage": stage.stage,
                "status": stage.status,
                "note": None if stage.status == "pending" else stage.note,
            }
            for stage in film.stages
        ],
        "archive": {
            "status": wf.archive_status,
            "error": film.archive_error,
            "target_path": str(Path(target.export_identity) / target.relative_path)
            if target
            else None,
        },
        "cleanup": {
            "input": wf.cleanup_input_status,
            "workcopy": wf.cleanup_workcopy_status,
            "kept": _kept(wf, paths),
        },
    }
