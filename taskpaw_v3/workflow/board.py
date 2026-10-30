"""The 媒体入库 page's view of the workflow (T06-P1 §3, §7 D02–D04, D11, D12, N01).

`film_row` is pure over one `film_board` row and the host's waiting reasons;
`film_detail` adds the final package, its NFO summary and the cleanup results.
Every path is resolved here from the film id, never taken from a client (U6).

U1 V': a film whose restore waits for its plan is in the `planning` group
(待处理); every row carries its plan and the line gains the `plan` step. What a
film is (title, number, actors, cover) comes from the final package, else the
Miko lookup (`rt.lookup`), else the input (precedence final > lookup > input).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from . import energy as en
from . import intake_models as im
from . import models as m
from .auth import STAGE_ROLES
from .exec_common import MISSING_LTX
from .image_size import is_landscape
from .local_gpu import FULL_HD
from .local_runtime import LocalRuntime
from .manifest import parse_nfo_summary
from .plan import NO_RESTORE, TIER_LABELS
from .stage_results import StageResultError, main_videos, workcopy_path
from .timing import film_timing

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
PLAN_NOTE = "待安排"
# H2 R5: the one fixed lifecycle line of every film and package row; U1 adds
# `plan` (待处理) after 入库扫描.
STEP_KEYS = (
    "intake",
    "plan",
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
# reported with the LTX state, shown on the step (U1c; U1e the live `timing`)
_STEP_KEYS = ("console", "params", "timing")
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
# What an NFO summary says: title, actors, number.
Summary = tuple[str | None, tuple[str, ...], str | None]
GROUPS = ("planning", "active", "attention", "confirm", "archiving", "done")
LOST_NOTE = "设备失联：{name} · 后台重连中"  # P2-4 R3 (OD2)
DEVICE_LOST = "device_lost"
SOURCE_MISSING = "source_missing"  # P3 E1 (#304): 片源丢失
SOURCE_NOTE = "INBOX 里的文件已不存在 · 不再派发"


@dataclass(frozen=True)
class RowExtra:
    """P2-4 R2/R3: what remote mode adds to a film's row (`reads.board_inputs`):
    its open route, its pin, and the 失联 worker `{id, name}` when the
    route's worker stopped answering (an attention state; nothing moves,
    OD2) — from `scheduler.lost_films`, so a route left on an older input
    revision names it too (§9 CX27)."""

    route: dict[str, Any] | None = None
    pin: dict[str, Any] | None = None
    lost: dict[str, str] | None = None
    # R2: {can_reassign, stop} — 改派 and 判定已停止 (`operator_actions`)
    actions: dict[str, Any] | None = None
    # P3 E1/E3 (#304): `{paths, found_at}` of a 片源丢失 film, and whether
    # 删除此任务 is offered (`operator_actions.removal_refusal`)
    source_missing: dict[str, Any] | None = None
    can_remove: bool = False


class Looked(Protocol):
    """What a lookup result offers the board (`lookup.LookupResult`)."""

    @property
    def ok(self) -> bool: ...

    number: str | None
    title: str | None
    actors: tuple[str, ...]


@dataclass(frozen=True)
class Facts:
    """What a film is, by precedence final > lookup > input (U1 L)."""

    title: str | None = None
    actors: tuple[str, ...] = ()
    number: str | None = None
    input_number: str | None = None
    video: str | None = None
    lookup_number: str | None = None
    # the cover's source token by the same precedence; None without a cover
    poster_source: str | None = None
    # U1b: the full landscape jacket's source token (`jacket`); None without one
    cover_source: str | None = None

    @property
    def label(self) -> Label:
        """The input's own name parts (H3 D06)."""
        return self.input_number, self.video

    @classmethod
    def merge(
        cls,
        *,
        final: Summary,
        lookup: Looked | None,
        input_nfo: Summary,
        video: str | None,
    ) -> Facts:
        found = lookup if lookup is not None and lookup.ok else None
        looked: Summary = (
            (found.title, tuple(found.actors), found.number)
            if found
            else (None, (), None)
        )
        layers = (final, looked, input_nfo)
        return cls(
            title=next((s[0] for s in layers if s[0]), None),
            actors=next((s[1] for s in layers if s[1]), ()),
            number=next((s[2] for s in layers if s[2]), None),
            input_number=input_nfo[2],
            video=video,
            lookup_number=looked[2],
        )


def _time(value: Any) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


iso = _time


def resolution(width: int | None, height: int | None) -> str | None:
    """V3 §7: "4K" beyond Full HD, "1080p" up to it, None without a probe."""
    if not width or not height:
        return None
    return "4K" if width > FULL_HD[0] or height > FULL_HD[1] else "1080p"


def counts(
    rows: Sequence[dict[str, Any]], packages: Sequence[dict[str, Any]]
) -> dict[str, int]:
    """The rail badges and the overview stats: films per group, packages that
    need the operator, and 等待您的确认 (待处理 + 待确认)."""
    value = {group: 0 for group in GROUPS}
    for row in rows:
        value[row["group"]] = value.get(row["group"], 0) + 1
    value["packages_attention"] = sum(1 for p in packages if p["group"] == "attention")
    value["waiting_for_you"] = value["planning"] + value["confirm"]
    return value


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
    if statuses.get("restore") == "pending" and not film.planned:
        place.update(group="planning", stage="restore")  # 待处理 (U1 V')
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
    The running restore step adds the LTX detail (`ltx`, `current_file`) and
    always its heartbeat (`started_at`, the latest `console` line) and the
    video's effective Jasna launch (`params`, U1c); a
    restore that failed on missing models carries the download session (H3).
    """
    stages = {stage.stage: stage for stage in film.stages}

    def status(name: str) -> str:
        return stages[name].status if name in stages else "pending"

    wait = waiting.get(film.film_id)
    first = next((n for n in STAGE_ORDER if status(n) not in _FINISHED), None)
    steps = [_step("intake", "done"), _plan_step(film, status("restore"))]
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
                # IR07: console and params live on the step, not inside `ltx`
                metrics = {k: v for k, v in ltx.items() if k not in _STEP_KEYS}
                step.update(ltx=metrics, current_file=ltx.get("current_file"))
            if mine and name == "restore":
                detail = ltx if isinstance(ltx, dict) else {}
                step.update(
                    started_at=running.get("started_at"),
                    console=detail.get("console"),
                    params=detail.get("params"),
                    timing=detail.get("timing"),
                )
            if mine and name == "asr":
                detail = running.get("ltx") or {}
                step.update(
                    progress=detail or step["progress"],
                    started_at=running.get("started_at"),
                    console=detail.get("console"),
                )
            if mine and running.get("machine"):  # P2-3 R15: a worker runs it
                step.update(
                    machine=running["machine"],
                    attempt_id=running.get("attempt_id"),
                    phase=running.get("phase"),
                    started_at=running.get("started_at"),
                )
                if running.get("transfer"):  # 「回传 x/y GB · rate」
                    step["note"] = running["transfer"]
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
        progress = (
            (current or {}).get("progress")
            if (current or {}).get("stage") == "archive"
            else None
        )
        step = _step("archive", "running", wait, progress)
        if progress:
            step["started_at"] = progress.get("started_at")
        steps.append(step)
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


def _plan_step(film: m.BoardFilm, restore: str) -> dict[str, Any]:
    """待处理: done with the chosen tier, waiting while the restore needs a plan."""
    if film.planned:
        tier = film.plan_ltx_model
        return _step(
            "plan", "done", TIER_LABELS.get(tier, tier) if tier else NO_RESTORE
        )
    if restore == "pending":
        return _step("plan", "waiting", PLAN_NOTE)
    return _step("plan", "done")


def film_name(
    film: m.BoardFilm,
    number: str | None = None,
    video: str | None = None,
    *,
    looked_up: str | None = None,
) -> str:
    """The one shown name of a film (H3 D06), never its grouping key: the final
    folder, else the Miko lookup's number (U1 L), else the package folder when it
    holds only this film, else the input NFO number, else the main video's stem.
    Only a film without an input manifest falls back to the display name."""
    if film.final_name:
        return film.final_name
    if looked_up:
        return looked_up
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
    facts: Facts | None = None,
    extra: RowExtra | None = None,
    names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One FilmRow of `GET /control/workflow/films` (§8) with its steps (H2 R5).

    `kept`: per cleanup stage, the members its newest cleanup did not remove
    (the detail view has them; the list falls back to `KEPT_NOTE`).
    `current`: the host's running stage, whose progress the step shows.
    `download`: the host's LTX model download session (H3 M1).
    `label`: the input's NFO number and video stem (`input_labels`, H3 D06).
    `facts`: title / number / actors by precedence (`row_facts`, U1); it names
    the film in place of `label`.
    `extra`: the route, pin and 失联 state of remote mode (P2-4); `names`:
    worker id → name for the energy's devices (R10).
    """
    # Confirmation can precede the finalizer's next tick while a local stage runs.
    if film.approval_active and waiting.get(film.film_id) == "等待确认入库":
        waiting = waiting.copy()
        del waiting[film.film_id]
    place = _place(film)
    stage = next((s for s in film.stages if s.stage == place["stage"]), None)
    # confirmed again after a failed archive: the old failure is no longer the reason
    note = (
        waiting.get(film.film_id)
        if place["group"] == "archiving"
        else _note(film, stage, waiting, kept or {})
    )
    attention_kind = None
    if extra is not None and extra.lost is not None and place["group"] == "active":
        place = {**place, "group": "attention"}  # R3: 设备失联, OD2
        attention_kind = DEVICE_LOST
        note = LOST_NOTE.format(name=extra.lost["name"])
    elif (
        extra is not None
        and extra.source_missing is not None
        and place["group"] == "active"
    ):
        place = {**place, "group": "attention"}  # P3 E1: never dispatched
        attention_kind = SOURCE_MISSING
        note = SOURCE_NOTE
    if facts is None:
        facts = Facts(
            number=(label or (None, None))[0],
            input_number=(label or (None, None))[0],
            video=(label or (None, None))[1],
        )
    return {
        "film_id": film.film_id,
        "name": film_name(film, *facts.label, looked_up=facts.lookup_number),
        "number": facts.number,
        "title": facts.title,
        "actors": list(facts.actors),
        # the list's cover: the one `film_detail` and `poster_path` serve
        "has_poster": facts.poster_source is not None,
        "poster_source": facts.poster_source,
        # U1b: the full jacket the 大图 shows (`jacket`, `cover_path`)
        "has_cover": facts.cover_source is not None,
        "cover_source": facts.cover_source,
        "resolution": resolution(film.max_width, film.max_height),
        "plan": {"ltx_model": film.plan_ltx_model, "machine": film.plan_machine}
        if film.planned
        else None,
        "receipt_id": film.receipt_id,
        "revisions": revisions(film),
        "group": place["group"],
        "attention_kind": attention_kind,
        "stage": stage.stage if stage else None,
        "status": stage.status if stage else None,
        "note": note,
        # P2-4 R2: the open route and the pin (null in single-machine mode)
        "route": extra.route if extra is not None else None,
        "pin": extra.pin if extra is not None else None,
        # §9 CX27: the 失联 worker {id, name}, whatever revision its route has
        "lost_worker": extra.lost if extra is not None else None,
        "actions": extra.actions if extra is not None else None,
        "source_missing": extra.source_missing if extra is not None else None,
        "can_remove": extra.can_remove if extra is not None else False,
        "can_confirm": place["can_confirm"],
        "can_retry": place["retry_stage"] is not None,
        "retry_stage": place["retry_stage"],
        "created_at": _time(film.created_at),
        "completed_at": _time(film.completed_at) if film.completed_at else None,
        "steps": _steps(film, place, waiting, kept or {}, current, download),
        # U1e: each finished stage's time, the restore breakdown and 修复→翻译
        "timing": film_timing(film),
        # PWR-2: the estimated energy per stage and in all, and the summed
        # probe duration (度 / 片长小时)
        "duration_ms": film.duration_ms,
        "energy": en.film_energy(film, current, names),
        "live_power": live_power(film, current),
    }


def live_power(film: m.BoardFilm, current: dict[str, Any] | None) -> Any:
    """PW12: the machine's live estimate, only on the GPU-owning running film."""
    if not current or current.get("film_id") != film.film_id:
        return None
    return current.get("power") if current.get("uses_gpu") else None


def revisions(film: m.BoardFilm) -> dict[str, Any]:
    """V3 §7: the current input and final manifests, revision and hash."""
    return {
        "input": {"revision": film.input_revision, "hash": film.input_hash}
        if film.input_revision is not None
        else None,
        "final": {"revision": film.final_revision, "hash": film.final_hash}
        if film.final_revision is not None
        else None,
    }


def film_rows(
    films: tuple[m.BoardFilm, ...],
    waiting: dict[str, str],
    currents: Mapping[str, dict[str, Any]] | None = None,
    download: dict[str, Any] | None = None,
    labels: dict[str, Label] | None = None,
    facts: dict[str, Facts] | None = None,
    extras: Mapping[str, RowExtra] | None = None,
    names: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """P2-3 R15: `currents` maps a film to its running stage — the
    controller's own and each worker's (`FleetLive.currents`). A film being
    fetched before its claim shows the download as its waiting note.
    P2-4: `extras` per film and the workers' `names` (`reads.board_inputs`)."""
    currents = currents or {}
    waiting = dict(waiting)
    for film_id, value in currents.items():
        if value.get("fetching") and value.get("transfer"):
            waiting[film_id] = value["transfer"]
    return [
        film_row(
            film,
            waiting,
            current=_running_current(currents.get(film.film_id)),
            download=download,
            label=(labels or {}).get(film.film_id),
            facts=(facts or {}).get(film.film_id),
            extra=(extras or {}).get(film.film_id),
            names=names,
        )
        for film in films
    ]


def _running_current(value: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if value is None or value.get("fetching") else value


def merged_currents(
    local: dict[str, Any] | None, remote: Mapping[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """R15: the controller's running stage and the workers' (local wins)."""
    merged = dict(remote)
    if local and local.get("film_id"):
        merged[str(local["film_id"])] = local
    return merged


def _lookup(rt: LocalRuntime, film: m.BoardFilm) -> Any:
    lookup = getattr(rt, "lookup", None)
    return lookup.result(film.film_id, film.input_revision) if lookup else None


def film_facts(
    rt: LocalRuntime,
    film: m.BoardFilm,
    *,
    manifest: m.InputManifest | None = None,
    final: m.FinalManifest | None = None,
) -> Facts:
    """Title, number and actors of one film: final NFO > lookup > input NFO."""
    if final is None and film.final_revision is not None:
        final = rt.store.get_final_manifest(
            rt.operator, film.film_id, film.final_revision
        )
    final_nfo = _read_nfo(_item_path(rt, final, "nfo")) if final else (None, (), None)
    if final is not None and final_nfo == (None, (), None):
        try:
            summary = json.loads(
                (rt.data_dir / "retained" / film.film_id / "summary.json").read_text(
                    "utf-8"
                )
            )
            final_nfo = summary["title"], tuple(summary["actors"]), summary["number"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logging.getLogger(__name__).warning(
                "retained facts for %s: %s", film.film_id, exc
            )
    source = manifest or _current_input(rt, film)
    found = _input_file(rt, source, "nfo") if source else None
    videos = main_videos(source) if source else []
    looked = _lookup(rt, film)
    poster = _poster(rt, final, source, looked)
    facts = Facts.merge(
        final=final_nfo,
        lookup=looked,
        input_nfo=_read_nfo(found[1] if found else None),
        video=PurePosixPath(videos[0].relative_path).stem if videos else None,
    )
    full = jacket(rt, final, source)
    return replace(
        facts,
        poster_source=poster[1] if poster else None,
        cover_source=full[1] if full else None,
    )


def film_actors(rt: LocalRuntime, film: m.BoardFilm, revision: int) -> tuple[str, ...]:
    """A film's actors as of one input revision, by `film_facts`' precedence:
    final NFO > that revision's lookup > that revision's input NFO (IR04)."""
    final = (
        rt.store.get_final_manifest(rt.operator, film.film_id, film.final_revision)
        if film.final_revision is not None
        else None
    )
    try:
        source = rt.store.get_input_manifest(rt.operator, film.film_id, revision)
    except m.WorkflowError as exc:
        if exc.code != "not_found":
            raise
        source = None
    lookup = getattr(rt, "lookup", None)
    found = _input_file(rt, source, "nfo") if source else None
    return Facts.merge(
        final=_read_nfo(_item_path(rt, final, "nfo")) if final else (None, (), None),
        lookup=lookup.result(film.film_id, revision) if lookup else None,
        input_nfo=_read_nfo(found[1] if found else None),
        video=None,
    ).actors


def row_facts(rt: LocalRuntime, films: tuple[m.BoardFilm, ...]) -> dict[str, Facts]:
    """`film_facts` of every listed film (NFO reads are cached by mtime)."""
    return {film.film_id: film_facts(rt, film) for film in films}


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
    if film.final_name:
        return film_name(film)
    found = _lookup(rt, film)
    if found is not None and found.ok and found.number:
        return film_name(film, looked_up=found.number)
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
    return rt.final_dir(final)


def _item_path(
    rt: LocalRuntime, final: m.FinalManifest, role: m.ItemRole
) -> Path | None:
    item = next((i for i in final.items if i.role == role), None)
    if item is None:
        return None
    path = output_dir(rt, final) / item.relative_path
    return path if path.is_file() else None


def _poster_item(final: m.FinalManifest) -> m.ManifestItem | None:
    """The cover item: the poster Miko's postprocess recorded (the first volume's
    `<name>-poster.jpg`, #303), else any poster, else `folder.jpg`."""
    by_id = {i.item_id: i for i in final.items}
    recorded = by_id.get(final.postprocess.poster_item_id)
    if recorded is not None and recorded.role == "poster":
        return recorded
    for role in ("poster", "folder_cover"):
        item = next((i for i in final.items if i.role == role), None)
        if item is not None:
            return item
    return None


def _input_file(
    rt: LocalRuntime,
    manifest: m.InputManifest,
    role: m.ItemRole,
    suffixes: dict[str, str] | None = None,
) -> tuple[m.ManifestItem, Path] | None:
    """The first input item of `role` whose intake work copy exists (H3 D11)."""
    found = _input_files(rt, manifest, role, suffixes, first=True)
    return found[0] if found else None


def _input_files(
    rt: LocalRuntime,
    manifest: m.InputManifest,
    role: m.ItemRole,
    suffixes: dict[str, str] | None = None,
    *,
    first: bool = False,
) -> list[tuple[m.ManifestItem, Path]]:
    """Input items of `role` whose intake work copy exists, in manifest order."""
    found = []
    for item in manifest.items:
        suffix = PurePosixPath(item.relative_path).suffix.lower()
        if item.role != role or (suffixes is not None and suffix not in suffixes):
            continue
        try:
            path = workcopy_path(rt, manifest, item.item_id)
        except StageResultError:
            continue
        if path.is_file():
            found.append((item, path))
            if first:
                break
    return found


def _poster(
    rt: LocalRuntime,
    final: m.FinalManifest | None,
    source: m.InputManifest | None,
    looked: Any = None,
) -> tuple[Path, str] | None:
    """The cover and its source token (the UI's cache key): the final's poster
    once a final exists (D11), before that the Miko lookup's cover (U1 L), else
    the input's poster, thumb or first image attachment, from the intake work
    copy (H3 M2)."""
    if final is not None:
        item = _poster_item(final)
        path = output_dir(rt, final) / item.relative_path if item else None
        if item and path and not path.is_file():
            path = rt.data_dir / "retained" / final.film_id / item.relative_path
        return (
            (path, f"final-{item.sha256[:16]}")
            if item and path and path.is_file()
            else None
        )
    poster = getattr(looked, "poster", None)
    if poster is not None and poster.is_file():
        return poster, f"lookup-{looked.poster_sha or 'cover'}"
    if source is None:
        return None
    found = (
        _input_file(rt, source, "poster")
        or _input_file(rt, source, "thumb")
        or _input_file(rt, source, "attachment", _IMAGE_TYPES)
    )
    return (found[1], f"input-{found[0].sha256[:16]}") if found else None


def jacket(
    rt: LocalRuntime,
    final: m.FinalManifest | None,
    source: m.InputManifest | None,
) -> tuple[Path, str] | None:
    """The full jacket and its source token (U1b): the largest landscape image
    of the first tier that has one — the final's fanart, its thumb, then the
    input's fanart, thumb and image attachments, from the intake work copy. A
    downloaded jacket's front cover is its right part; the 大图 shows it whole."""
    tiers: list[tuple[str, list[tuple[m.ManifestItem, Path]]]] = []
    roles: tuple[m.ItemRole, ...] = ("fanart", "thumb")
    if final is not None:
        for role in roles:
            files = [
                (item, output_dir(rt, final) / item.relative_path)
                for item in final.items
                if item.role == role
            ]
            tiers.append(("final", files))
        tiers.append(
            (
                "retained",
                [
                    (
                        item,
                        rt.data_dir / "retained" / final.film_id / item.relative_path,
                    )
                    for item in final.items
                    if item.role == "thumb"
                ],
            )
        )
    if source is not None:
        for role in (*roles, "attachment"):
            tiers.append(("input", _input_files(rt, source, role, _IMAGE_TYPES)))
    for kind, files in tiers:
        sized = [
            (size[0] * size[1], item, path)
            for item, path in files
            if path.is_file() and (size := is_landscape(path)) is not None
        ]
        if sized:
            _, item, path = max(sized, key=lambda found: found[0])
            return path, f"{kind}-{item.sha256[:16]}"
    return None


def cover_path(rt: LocalRuntime, film_id: str) -> Path | None:
    """The full jacket while its file exists (U1b); None without one."""
    if board_film(rt, film_id) is None:
        return None
    found = jacket(rt, final_manifest(rt, film_id), _source(rt, film_id))
    return found[0] if found else None


def _source(rt: LocalRuntime, film_id: str) -> m.InputManifest | None:
    view = rt.store.get_film(rt.operator, film_id).film
    if view.current_input_revision is None:
        return None
    return rt.store.get_input_manifest(
        rt.operator, film_id, view.current_input_revision
    )


def cover(
    rt: LocalRuntime,
    final: m.FinalManifest | None,
    source: m.InputManifest | None,
    film_id: str,
) -> tuple[Path, str] | None:
    """`_poster` with the film's current lookup (final > lookup > input)."""
    lookup = getattr(rt, "lookup", None)
    revision = source.revision if source is not None else None
    looked = lookup.result(film_id, revision) if lookup and final is None else None
    return _poster(rt, final, source, looked)


def poster_path(rt: LocalRuntime, film_id: str) -> Path | None:
    """The cover while its work copy exists (D11, H3 M2, U1 L)."""
    if board_film(rt, film_id) is None:
        return None
    final = final_manifest(rt, film_id)
    poster = cover(rt, final, _source(rt, film_id), film_id)
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
    elif what == "archive":
        recorded = rt.store.film_workflow(rt.operator, film_id).archive_path
        path = Path(recorded) if recorded else None
    else:
        final = final_manifest(rt, film_id)
        path = output_dir(rt, final) if final else None
    return path if path is not None and path.is_dir() else None


_NFO_CACHE: dict[Path, tuple[tuple[int, int], Summary]] = {}
_NFO_CACHE_LIMIT = 1024


def _read_nfo(path: Path | None) -> Summary:
    """An NFO's summary; cached by size and mtime (every list poll reads them)."""
    if path is None:
        return None, (), None
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
        cached = _NFO_CACHE.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        with path.open("rb") as stream:
            payload = stream.read(NFO_LIMIT + 1)
    except OSError:
        return None, (), None
    value = parse_nfo_summary(payload, max_bytes=NFO_LIMIT)
    if len(_NFO_CACHE) >= _NFO_CACHE_LIMIT:
        _NFO_CACHE.clear()
    _NFO_CACHE[path] = (key, value)
    return value


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
    extra: RowExtra | None = None,
    names: Mapping[str, str] | None = None,
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
    facts = film_facts(rt, film, manifest=source, final=final)
    title, actors, number = facts.title, facts.actors, facts.number
    if final is not None:
        number = number or film.final_name
    poster = cover(rt, final, source, film_id)
    lookup = getattr(rt, "lookup", None)
    marks = set(final.postprocess.actual_marks) if final else set()
    target = wf.approval.binding.target if wf.approval else None
    paths = {
        item.item_id: item.relative_path
        for manifest in (source, final)
        if manifest is not None
        for item in manifest.items
    }
    kept = _kept(wf, paths)
    row = film_row(
        film, waiting, kept, current, download, facts=facts, extra=extra, names=names
    )
    return {
        "film_id": film_id,
        "name": row["name"],
        "number": number,
        "title": title,
        "actors": list(actors),
        "resolution": row["resolution"],
        "plan": row["plan"],
        # F2: the row's, the current final revision's; never an older one's
        "receipt_id": row["receipt_id"],
        "revisions": row["revisions"],
        "lookup": lookup.state(film_id, film.input_revision)
        if lookup is not None and film.input_revision is not None
        else None,
        "marks": {
            "restored": bool(marks & {"restored", "uncensored"}),
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
        "has_cover": facts.cover_source is not None,
        "cover_source": facts.cover_source,
        "group": row["group"],
        "attention_kind": row["attention_kind"],
        "route": row["route"],
        "pin": row["pin"],
        "actions": row["actions"],
        "source_missing": row["source_missing"],
        "can_remove": row["can_remove"],
        # §2: the 改派 dialog's choices (remote mode, `reads.film`)
        "reassign_options": None,
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
        "timing": row["timing"],
        "history": _history(film, wf, names or {}) if row["group"] == "done" else None,
        "duration_ms": row["duration_ms"],
        "energy": row["energy"],
        "live_power": row["live_power"],
        "archive": {
            "status": wf.archive_status,
            "error": film.archive_error,
            "manual": bool(
                target and target.native_identity.scheme == "manual_directory"
            ),
            "target_path": wf.archive_path
            or (
                str(Path(target.export_identity) / target.relative_path)
                if target
                else None
            ),
        },
        "cleanup": {
            "input": wf.cleanup_input_status,
            "workcopy": wf.cleanup_workcopy_status,
            "kept": [item for items in kept.values() for item in items],
        },
    }


def _completed_at(wf: m.FilmWorkflow) -> datetime | None:
    return max(
        (
            o.ended_at
            for o in wf.cleanup_operations
            if o.approval_id == wf.archive_receipt_approval_id
            and o.resolved
            and o.ended_at
        ),
        default=None,
    )


def _history(
    film: m.BoardFilm, wf: m.FilmWorkflow, names: Mapping[str, str]
) -> dict[str, Any]:
    """Recorded stage times, archive receipt and cleanup outcomes for a finished film."""
    durations = film_timing(film)["stages"]
    stages = {s.stage: s for s in film.stages}
    rows = []
    for key in ("intake", "restore", "return", "asr", "translate", "scrape"):
        stage = stages.get(key)
        devices = (
            dict.fromkeys(names.get(w, w) if w else "本机" for w in stage.worker_ids)
            if stage
            else {}
        )
        rows.append(
            {
                "key": key,
                "device": "、".join(devices) or "本机",
                "seconds": durations.get(key),
                "result": (
                    stage.note
                    or stage.reason
                    or ("已跳过" if stage.status == "skipped" else "已完成")
                )
                if stage
                else "—",
            }
        )
    rows.append(
        {
            "key": "confirm",
            "device": "主控",
            "seconds": None,
            "result": "已确认入库" if wf.approval else "—",
        }
    )
    archives = [
        o
        for o in wf.archive_operations
        if o.approval_id == wf.archive_receipt_approval_id
        and o.resolved
        and o.status == "verified"
    ]
    cleanups = [
        o
        for o in wf.cleanup_operations
        if o.approval_id == wf.archive_receipt_approval_id and o.resolved
    ]
    for key, selected, result in (
        ("archive", archives[-1:], "已校验入库" if wf.archive_receipt_id else "—"),
        ("cleanup", cleanups, "来源与工作副本已清理"),
    ):
        seconds = [
            (o.ended_at - o.started_at).total_seconds()
            for o in selected
            if o.started_at and o.ended_at
        ]
        rows.append(
            {
                "key": key,
                "device": "片库" if key == "archive" else "主控",
                "seconds": round(sum(seconds), 1) if seconds else None,
                "result": result,
            }
        )
    completed = _completed_at(wf)
    return {
        "steps": rows,
        "completed_at": _time(completed) if completed else None,
        "total_seconds": round(max(0, (completed - film.created_at).total_seconds()), 1)
        if completed
        else None,
    }
