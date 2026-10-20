"""The 媒体入库 page's view of the workflow (T06-P1 §3, §7 D02–D04, D11, D12, N01).

`film_row` is pure over one `film_board` row and the host's waiting reasons;
`film_detail` adds the final package, its NFO summary and the cleanup results.
Every path is resolved here from the film id, never taken from a client (U6).
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import intake_models as im
from . import models as m
from .auth import STAGE_ROLES
from .exec_common import MISSING_LTX
from .local_runtime import LocalRuntime
from .manifest import parse_nfo_summary
from .stage_results import StageResultError, main_videos, workcopy_path

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
KEPT_NOTE = "待清理：部分文件未能删除，请在详情中查看"
CONFIRM_NOTE = "等你确认入库"
# H2 R5: the one fixed lifecycle line of every film and package row.
STEP_KEYS = (
    "intake",
    "restore",
    "asr",
    "translate",
    "return",
    "scrape",
    "confirm",
    "archive",
    "cleanup",
    "done",
)
_LOCAL: tuple[m.Stage, ...] = ("restore", "asr", "translate", "return", "scrape")
# R2a: package states that are still on their way in; the rest need the operator.
_ARRIVING = {
    "receiving",
    "waiting_stability",
    "snapshotting",
    "snapshot_ready",
    "waiting_capacity",
}
_COPYING = {"snapshotting", "snapshot_ready"}
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
# H3 M1: the model download session as the 修复 step carries it.
DOWNLOAD_KEYS = (
    "state",
    "request_id",
    "files",
    "total_bytes",
    "downloaded_bytes",
    "destination",
    "variant",
    "error",
)
# H3 D06: what the input says about a film: its NFO number and main video stem.
Label = tuple[str | None, str | None]


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


def _kept_note(kept: list[dict[str, str]]) -> str:
    """Why a cleanup stays unknown when the host has no reason yet (after a restart)."""
    if not kept:
        return KEPT_NOTE
    return f"待清理：{len(kept)} 个文件未能删除（{kept[0]['status']}）"


def _note(
    film: m.BoardFilm,
    stage: m.BoardStage | None,
    waiting: dict[str, str],
    kept: dict[str, list[dict[str, str]]],
) -> str | None:
    """D02: a failure's own reason first, otherwise why the film waits.

    Only a local stage's `unknown` waits for its runner to confirm the stop; a
    cleanup is `unknown` because members were kept, so it says which.
    """
    if stage is not None and stage.status == "unknown":
        if stage.stage in STAGE_ROLES:
            return UNKNOWN_NOTE
        if stage.stage in _CLEANUPS:
            return waiting.get(film.film_id) or _kept_note(kept.get(stage.stage, []))
    if stage is not None and stage.status in _RETRYABLE:
        reason = (
            film.archive_error or stage.note if stage.stage == "archive" else stage.note
        )
        if reason:
            return reason
    return waiting.get(film.film_id)


def _step(
    key: str, state: str, note: str | None = None, progress: Any = None
) -> dict[str, Any]:
    return {"key": key, "state": state, "note": note, "progress": progress}


def _steps(
    film: m.BoardFilm,
    place: dict[str, Any],
    waiting: dict[str, str],
    kept: dict[str, list[dict[str, str]]],
    current: dict[str, Any] | None,
    download: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """R5: the film's lifecycle line, from the stages and the host (G3–G5).

    A finished stage is done with its note (a skip or "no work" outcome, G3);
    the host's waiting reason belongs to the first step not yet finished.
    The running restore step adds the LTX detail (`ltx`, `current_file`); a
    restore that failed on missing models carries the download session (H3).
    """
    stages = {stage.stage: stage for stage in film.stages}

    def status(name: str) -> str:
        return stages[name].status if name in stages else "pending"

    wait = waiting.get(film.film_id)
    first = next((n for n in STAGE_ORDER if status(n) not in _FINISHED), None)
    steps = [_step("intake", "done")]
    for name in _LOCAL:
        stage = stages.get(name)
        why = (stage.note or stage.reason) if stage else None
        value = status(name)
        if value in _FINISHED:
            steps.append(_step(name, "done", why))
        elif value == "running":
            running = current or {}
            mine = (
                running.get("film_id") == film.film_id and running.get("stage") == name
            )
            step = _step(
                name,
                "running",
                why,
                (running.get("progress") or None) if mine else None,
            )
            ltx = running.get("ltx") if mine and name == "restore" else None
            if isinstance(ltx, dict):
                step.update(ltx=ltx, current_file=ltx.get("current_file"))
            steps.append(step)
        elif value in _RETRYABLE:
            step = _step(name, "failed", why)
            note = stage.note if stage else None
            if name == "restore" and download and (note or "").startswith(MISSING_LTX):
                step["download"] = {key: download.get(key) for key in DOWNLOAD_KEYS}
            steps.append(step)
        elif value == "unknown":
            steps.append(_step(name, "waiting", UNKNOWN_NOTE))
        elif name == first and wait:
            steps.append(_step(name, "waiting", wait))
        else:
            steps.append(_step(name, "pending"))
    archive = status("archive")
    archive_stage = stages.get("archive")
    error = film.archive_error or (archive_stage.note if archive_stage else None)
    if place["can_confirm"]:
        steps.append(
            _step("confirm", "waiting", error if archive == "failed" else CONFIRM_NOTE)
        )
    elif (
        film.approval_active
        or film.archive_open
        or archive not in ("pending", "failed")
    ):
        steps.append(_step("confirm", "done"))
    else:
        steps.append(_step("confirm", "pending"))
    if archive == "succeeded":
        steps.append(_step("archive", "done"))
    elif film.archive_open or archive == "running":
        steps.append(_step("archive", "running", wait))
    elif archive == "unknown" or film.approval_active:
        steps.append(_step("archive", "waiting", wait))
    elif archive in _RETRYABLE:
        steps.append(_step("archive", "failed", error))
    else:
        steps.append(_step("archive", "pending"))
    finished = all(status(name) == "succeeded" for name in _CLEANUPS)
    trouble = next((stages[n] for n in _CLEANUPS if status(n) in _TROUBLE), None)
    if finished:
        steps.append(_step("cleanup", "done"))
    elif trouble is not None:
        state = "waiting" if trouble.status == "unknown" else "failed"
        steps.append(_step("cleanup", state, _note(film, trouble, waiting, kept)))
    elif archive == "succeeded":
        steps.append(_step("cleanup", "running", wait))
    else:
        steps.append(_step("cleanup", "pending"))
    steps.append(_step("done", "done" if finished else "pending"))
    return steps


def film_name(
    film: m.BoardFilm, number: str | None = None, video: str | None = None
) -> str:
    """The one shown name of a film (H3 D06), never its grouping key: the final
    folder, else the package folder when it holds only this film, else the input
    NFO number, else the main video's stem. Only a film without an input
    manifest falls back to the display name."""
    if film.final_name:
        return film.final_name
    if film.package_path and film.package_films == 1:
        folder = PurePosixPath(film.package_path).name
        if folder:
            return folder
    return number or video or film.display_name


def _named_by_board(film: m.BoardFilm) -> bool:
    """Whether the board row alone names the film (no input read needed)."""
    return bool(film.final_name) or bool(film.package_path and film.package_films == 1)


def film_row(
    film: m.BoardFilm,
    waiting: dict[str, str],
    kept: dict[str, list[dict[str, str]]] | None = None,
    current: dict[str, Any] | None = None,
    download: dict[str, Any] | None = None,
    label: Label | None = None,
) -> dict[str, Any]:
    """One FilmRow of `GET /control/workflow/films` (§8) with its steps (H2 R5).

    `kept`: per cleanup stage, the members its newest cleanup did not remove
    (the detail view has them; the list falls back to `KEPT_NOTE`).
    `current`: the host's running stage, whose progress the step shows.
    `download`: the host's LTX model download session (H3 M1).
    `label`: the input's NFO number and video stem (`input_labels`, H3 D06).
    """
    place = _place(film)
    stage = next((s for s in film.stages if s.stage == place["stage"]), None)
    # confirmed again after a failed archive: the old failure is no longer the reason
    note = (
        waiting.get(film.film_id)
        if place["group"] == "archiving"
        else _note(film, stage, waiting, kept or {})
    )
    return {
        "film_id": film.film_id,
        "name": film_name(film, *(label or (None, None))),
        "group": place["group"],
        "stage": stage.stage if stage else None,
        "status": stage.status if stage else None,
        "note": note,
        "can_confirm": place["can_confirm"],
        "can_retry": place["retry_stage"] is not None,
        "retry_stage": place["retry_stage"],
        "created_at": _time(film.created_at),
        "steps": _steps(film, place, waiting, kept or {}, current, download),
    }


def film_rows(
    films: tuple[m.BoardFilm, ...],
    waiting: dict[str, str],
    current: dict[str, Any] | None = None,
    download: dict[str, Any] | None = None,
    labels: dict[str, Label] | None = None,
) -> list[dict[str, Any]]:
    return [
        film_row(
            film,
            waiting,
            current=current,
            download=download,
            label=(labels or {}).get(film.film_id),
        )
        for film in films
    ]


def _input_label(rt: LocalRuntime, manifest: m.InputManifest) -> Label:
    videos = main_videos(manifest)
    video = PurePosixPath(videos[0].relative_path).stem if videos else None
    found = _input_file(rt, manifest, "nfo")
    return _read_nfo(found[1] if found else None)[2], video


def _current_input(rt: LocalRuntime, film: m.BoardFilm) -> m.InputManifest | None:
    if film.input_revision is None:
        return None
    try:
        return rt.store.get_input_manifest(
            rt.operator, film.film_id, film.input_revision
        )
    except m.WorkflowError as exc:
        if exc.code != "not_found":
            raise
        return None


def input_labels(rt: LocalRuntime, films: tuple[m.BoardFilm, ...]) -> dict[str, Label]:
    """The input labels of the films the board row alone cannot name (D06)."""
    labels: dict[str, Label] = {}
    for film in films:
        if _named_by_board(film):
            continue
        manifest = _current_input(rt, film)
        if manifest is not None:
            labels[film.film_id] = _input_label(rt, manifest)
    return labels


def name_of(
    rt: LocalRuntime, film_id: str, manifest: m.InputManifest | None = None
) -> str | None:
    """`film_name` of one film: the running stage and the GPU lease label (D06)."""
    film = board_film(rt, film_id)
    if film is None:
        return None
    if _named_by_board(film):
        return film_name(film)
    manifest = manifest or _current_input(rt, film)
    return film_name(film, *(_input_label(rt, manifest) if manifest else ()))


# ── INBOX packages that are not films yet (H2 R2/R2a) ───────────────────
def package_group(state: str, reason: str) -> str:
    """`active` while the folder is still arriving (a reconfigured root rescans
    too), `attention` when the operator has to act."""
    if state in _ARRIVING or (state == "blocked" and reason == "changed"):
        return "active"
    return "attention"


def package_row(
    package: im.IntakeBoardPackage, intake: dict[str, Any] | None
) -> dict[str, Any]:
    """One package row: codes only, the UI owns the labels (frozen H2 API).

    `intake`: the host's running snapshot copy, shown on this package's step.
    """
    group = package_group(package.state, package.reason)
    copying = (
        intake if intake and intake.get("package_id") == package.package_id else None
    )
    state = (
        "failed"
        if group == "attention"
        else "running"
        if package.state in _COPYING or copying
        else "waiting"
    )
    return {
        "package_id": package.package_id,
        "name": package.relative_path,
        "state": package.state,
        "reason": package.reason,
        "grouping_reasons": list(package.grouping_reasons),
        "files": list(package.unassigned_paths),
        "group": group,
        "steps": [
            _step("intake", state, package.reason, copying),
            *(_step(key, "pending") for key in STEP_KEYS[1:]),
        ],
    }


def package_rows(
    packages: tuple[im.IntakeBoardPackage, ...], intake: dict[str, Any] | None
) -> list[dict[str, Any]]:
    return [package_row(package, intake) for package in packages]


def package_folder(
    rt: LocalRuntime, inbox: Path | None, package_id: str
) -> Path | None:
    """The folder of a package row, when it exists; resolved from its id (U6)."""
    package = next(
        (
            p
            for p in rt.store.list_intake_board(rt.operator)
            if p.package_id == package_id
        ),
        None,
    )
    if inbox is None or package is None:
        return None
    path = inbox.joinpath(*PurePosixPath(package.relative_path).parts)
    return path if path.is_dir() else None


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


def _input_file(
    rt: LocalRuntime,
    manifest: m.InputManifest,
    role: m.ItemRole,
    suffixes: dict[str, str] | None = None,
) -> tuple[m.ManifestItem, Path] | None:
    """The first input item of `role` whose intake work copy exists (H3 D11)."""
    for item in manifest.items:
        suffix = PurePosixPath(item.relative_path).suffix.lower()
        if item.role != role or (suffixes is not None and suffix not in suffixes):
            continue
        try:
            path = workcopy_path(rt, manifest, item.item_id)
        except StageResultError:
            continue
        if path.is_file():
            return item, path
    return None


def _poster(
    rt: LocalRuntime, final: m.FinalManifest | None, source: m.InputManifest | None
) -> tuple[Path, str] | None:
    """The cover and its source token (the UI's cache key): the final's poster
    once a final exists (D11), before that the input's poster, thumb or first
    image attachment, from the intake work copy (H3 M2)."""
    if final is not None:
        item = next((i for i in final.items if i.role == "poster"), None)
        path = _item_path(rt, final, "poster")
        return (path, f"final-{item.sha256[:16]}") if item and path else None
    if source is None:
        return None
    found = (
        _input_file(rt, source, "poster")
        or _input_file(rt, source, "thumb")
        or _input_file(rt, source, "attachment", _IMAGE_TYPES)
    )
    return (found[1], f"input-{found[0].sha256[:16]}") if found else None


def _source(rt: LocalRuntime, film_id: str) -> m.InputManifest | None:
    view = rt.store.get_film(rt.operator, film_id).film
    if view.current_input_revision is None:
        return None
    return rt.store.get_input_manifest(
        rt.operator, film_id, view.current_input_revision
    )


def poster_path(rt: LocalRuntime, film_id: str) -> Path | None:
    """The cover while its work copy exists (D11, H3 M2)."""
    if board_film(rt, film_id) is None:
        return None
    final = final_manifest(rt, film_id)
    poster = _poster(rt, final, None if final else _source(rt, film_id))
    return poster[0] if poster else None


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


def _read_nfo(path: Path | None) -> tuple[str | None, tuple[str, ...], str | None]:
    if path is None:
        return None, (), None
    try:
        with path.open("rb") as stream:
            payload = stream.read(NFO_LIMIT + 1)
    except OSError:
        return None, (), None
    return parse_nfo_summary(payload, max_bytes=NFO_LIMIT)


def _kept(wf: m.FilmWorkflow, paths: dict[str, str]) -> dict[str, list[dict[str, str]]]:
    """Per cleanup stage, members its newest cleanup did not remove (U2 逐成员结果)."""
    latest: dict[str, m.WorkflowCleanupOperation] = {}
    for operation in wf.cleanup_operations:
        latest[operation.scope] = operation
    return {
        f"cleanup_{scope}": [
            {"path": paths.get(item.item_id, item.item_id), "status": item.status}
            for item in latest[scope].items
            if item.status not in _KEPT_OK
        ]
        for scope in ("input", "workcopy")
        if scope in latest
    }


def film_detail(
    rt: LocalRuntime,
    film_id: str,
    waiting: dict[str, str],
    current: dict[str, Any] | None = None,
    download: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """`GET /control/workflow/films/{film_id}` (§8); None for an unknown film.

    Until a final exists, the NFO and the cover come from the input (H3 M2)."""
    film = board_film(rt, film_id)
    if film is None:
        return None
    op = rt.operator
    wf = rt.store.film_workflow(op, film_id)
    final = final_manifest(rt, film_id)
    source = _source(rt, film_id)
    if final is not None:
        title, actors, number = _read_nfo(_item_path(rt, final, "nfo"))
        number = number or film.final_name
    else:
        found = _input_file(rt, source, "nfo") if source else None
        title, actors, number = _read_nfo(found[1] if found else None)
    poster = _poster(rt, final, source)
    videos = main_videos(source) if source else []
    video = PurePosixPath(videos[0].relative_path).stem if videos else None
    marks = set(final.postprocess.actual_marks) if final else set()
    target = wf.approval.binding.target if wf.approval else None
    paths = {
        item.item_id: item.relative_path
        for manifest in (source, final)
        if manifest is not None
        for item in manifest.items
    }
    kept = _kept(wf, paths)
    row = film_row(film, waiting, kept, current, download, (number, video))
    return {
        "film_id": film_id,
        "name": row["name"],
        "number": number,
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
        "has_poster": poster is not None,
        "poster_source": poster[1] if poster else None,
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
        "steps": row["steps"],
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
            "kept": [item for items in kept.values() for item in items],
        },
    }
