"""U1 P3': the 待处理 list and plan confirmation behind the control routes.

`plan_list` builds `GET /control/workflow/plans`: every unplanned film with its
row data (cover, title, number, actors, resolution, duration, size, file
count), the lookup state, the recommendation with its reasons and the choices.
`confirm_plans` serves `POST /control/workflow/plans`, one store command (its
own CAS) per item, with a result for each.
"""

from __future__ import annotations

from typing import Any

from . import board
from . import models as m
from .exec_restore import needs_restore
from .local_gpu import FULL_HD, max_resolution
from .local_runtime import LocalRuntime
from .plan import (
    LOCAL_NAME,
    TIER_LABELS,
    TIERS,
    PastPlan,
    actor_key,
    machine_reason,
    recommend,
)
from .stage_results import main_videos

DEFAULT_TIER = "standard"
PLAN_ERRORS = {
    "cas_conflict": "收件内容已更新，请重新查看后确认",
    "plan_exists": "已确认，无需重复确认",
    "precondition_failed": "当前状态不允许安排",
    "not_found": "未知影片",
    "invalid_machine": "设备不可用",
    "tier_required": "请选择画质",
}


def default_tier(rt: LocalRuntime) -> str:
    """The restore executor's configured tier (its preset), else `standard`."""
    restore = rt.executors.get("restore")
    cfg = getattr(restore, "cfg", None)
    tier = getattr(cfg, "ltx_model", None)
    return tier if tier in TIERS else DEFAULT_TIER


def _gpu(rt: LocalRuntime) -> tuple[str | None, int]:
    gpu = rt.gpu
    return (gpu.name, gpu.vram_mib) if gpu is not None else (None, 0)


def max_output(rt: LocalRuntime) -> str:
    """The frame size this workflow restores on the local card: "4K" or "1080p".

    The restore executor's own limit (phase 1: 1080p on every card), the same
    one `LocalDispatcher.run_once()` checks; never the card's VRAM class.
    """
    gpu = rt.gpu
    vram = gpu.vram_mib if gpu is not None else 0
    restore = rt.executors.get("restore")
    _width, height = (restore.max_resolution if restore else max_resolution)(vram)
    return "4K" if height > FULL_HD[1] else "1080p"


def machines(rt: LocalRuntime) -> list[dict[str, Any]]:
    """Phase 1: the controller itself is the only machine (P2')."""
    name, vram = _gpu(rt)
    return [
        {
            "id": rt.identity.worker_id,
            "name": LOCAL_NAME,
            "gpu": name,
            "vram_mib": vram or None,
            "reason": machine_reason(name, vram, uhd=False),
        }
    ]


def choices(rt: LocalRuntime) -> dict[str, Any]:
    return {
        "tiers": [{"value": tier, "label": TIER_LABELS[tier]} for tier in TIERS],
        "machines": machines(rt),
    }


def _history(rt: LocalRuntime, films: dict[str, m.BoardFilm]) -> list[PastPlan]:
    """The operator's own past choices with their films' actors.

    A backfilled plan (the v3 upgrade's default tier, U04) is no choice and is
    left out (IR03). Actors are the snapshot taken when the plan was confirmed,
    which outlives the film's cleanup. Only a plan with no snapshot reads them
    as the card does, final NFO > lookup > input NFO, for its revision (IR04)."""
    history = []
    for plan in rt.store.list_plans(rt.operator):
        if plan.backfilled:
            continue
        film = films.get(plan.film_id)
        actors = plan.actors or (
            board.film_actors(rt, film, plan.input_revision) if film else ()
        )
        history.append(
            PastPlan(
                plan.film_id,
                plan.input_revision,
                plan.ltx_model,
                plan.confirmed_at,
                actors,
            )
        )
    return history


def media_totals(manifest: m.InputManifest) -> dict[str, Any]:
    """Resolution of the largest probe; duration and size summed over the main videos."""
    videos = main_videos(manifest)
    ids = {video.item_id for video in videos}
    probes = [p for p in manifest.readiness.media_probes if p.item_id in ids]
    durations = [p.duration_ms for p in probes if p.duration_ms is not None]
    width = max((p.width or 0 for p in manifest.readiness.media_probes), default=0)
    height = max((p.height or 0 for p in manifest.readiness.media_probes), default=0)
    return {
        "width": width or None,
        "height": height or None,
        "resolution": board.resolution(width, height),
        "uhd": width > FULL_HD[0] or height > FULL_HD[1],
        "duration_ms": sum(durations) if durations else None,
        "size_bytes": sum(video.size_bytes for video in videos),
        "video_count": len(videos),
        "item_count": len(manifest.items),
    }


def plan_list(rt: LocalRuntime) -> dict[str, Any]:
    op, lookup = rt.operator, rt.lookup
    unplanned = rt.store.list_unplanned(op)
    films = {film.film_id: film for film in rt.store.film_board(op)}
    history = _history(rt, films)
    default = default_tier(rt)
    gpu_name, vram = _gpu(rt)
    entries = []
    for item in unplanned:
        film = films.get(item.film_id)
        if film is None:
            continue
        manifest = rt.store.get_input_manifest(op, item.film_id, item.input_revision)
        facts = board.film_facts(rt, film, manifest=manifest)
        totals = media_totals(manifest)
        needs = needs_restore(manifest)
        rec = recommend(
            film_id=item.film_id,
            needs_gpu=needs,
            actors=facts.actors,
            history=history,
            default=default,
            machine=rt.identity.worker_id,
            gpu_name=gpu_name,
            vram_mib=vram,
            uhd=totals["uhd"],
        )
        poster = board.cover(rt, None, manifest, item.film_id)
        entries.append(
            {
                "film_id": item.film_id,
                "input_revision": item.input_revision,
                "revision_changed": item.input_revision > 1,
                "name": board.film_name(
                    film, *facts.label, looked_up=facts.lookup_number
                ),
                "title": facts.title,
                "number": facts.number,
                "actors": list(facts.actors),
                "has_poster": poster is not None,
                "poster_source": poster[1] if poster else None,
                "resolution": totals["resolution"],
                "width": totals["width"],
                "height": totals["height"],
                "duration_ms": totals["duration_ms"],
                "size_bytes": totals["size_bytes"],
                "video_count": totals["video_count"],
                "item_count": totals["item_count"],
                "needs_gpu": needs,
                "created_at": board.iso(film.created_at),
                "lookup": lookup.state(item.film_id, item.input_revision)
                if lookup is not None
                else None,
                "recommendation": {
                    "ltx_model": rec.ltx_model,
                    "ltx_reason": rec.ltx_reason,
                    "machine": rec.machine,
                    "machine_reason": rec.machine_reason,
                },
            }
        )
    return {"plans": entries, "default_tier": default, "choices": choices(rt)}


def _manifest(rt: LocalRuntime, film_id: str, revision: int) -> m.InputManifest:
    """The input manifest a card was shown; a revision the store does not have
    for a known film is a stale card (`cas_conflict`), not an unknown film."""
    try:
        return rt.store.get_input_manifest(rt.operator, film_id, revision)
    except m.WorkflowError as exc:
        if exc.code != "not_found":
            raise
        rt.store.get_film(rt.operator, film_id)  # not_found: the film is unknown
        raise m.WorkflowError("cas_conflict") from None


def _snapshot(actors: tuple[str, ...]) -> tuple[str, ...]:
    """The names a plan keeps: matching keys (NFKC, trimmed), no blanks or repeats."""
    keys = dict.fromkeys(actor_key(name) for name in actors)
    return tuple(key[:2048] for key in keys if key)


def confirm_plans(
    rt: LocalRuntime, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Confirm each `{film_id, input_revision, ltx_model, machine}`; one result each."""
    op = rt.operator
    allowed = {machine["id"] for machine in machines(rt)}
    films = {film.film_id: film for film in rt.store.film_board(op)}
    results = []
    for item in items:
        film_id = item["film_id"]
        try:
            if item["machine"] not in allowed:
                raise m.WorkflowError("invalid_machine")
            manifest = _manifest(rt, film_id, item["input_revision"])
            needs = needs_restore(manifest)
            tier = item["ltx_model"] if needs else None
            if needs and tier is None:
                raise m.WorkflowError("tier_required")
            film = films.get(film_id)
            rt.store.confirm_plan(
                op,
                rt.meta(),
                m.FilmPlanConfirm(
                    film_id=film_id,
                    input_revision=item["input_revision"],
                    ltx_model=tier,
                    machine=item["machine"],
                    actors=_snapshot(
                        board.film_actors(rt, film, item["input_revision"])
                        if film
                        else ()
                    ),
                ),
            )
        except m.WorkflowError as exc:
            if exc.code in ("authority_unavailable", "unauthorized"):
                raise
            results.append(
                {
                    "film_id": film_id,
                    "ok": False,
                    "error": exc.code,
                    "message": PLAN_ERRORS.get(
                        exc.code, f"媒体流水线暂不可用：{exc.code}"
                    ),
                }
            )
            continue
        results.append({"film_id": film_id, "ok": True, "error": None, "message": None})
    return results
