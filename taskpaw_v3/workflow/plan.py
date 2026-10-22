"""U1 P2': the 待处理 recommendation — pure, deterministic and explained.

Tier, first match wins: the same film's previous revision's choice, then the
most recent tier chosen for any of its actors (exact name after NFKC and a
whitespace trim; several actors → the highest of their tiers), then the restore
default. A film whose inputs are all restored already needs no tier. The
machine is the controller itself (phase 1); its reason names the card, or the
1080p output cap for a 4K film (never a block).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

TIERS: tuple[str, ...] = ("standard", "good", "high")
TIER_LABELS = {"standard": "标准", "good": "良好", "high": "高"}
NO_RESTORE = "无需修复"
DEFAULT_REASON = "默认"
UHD_REASON = "本机修复输出上限 1080p"
NO_GPU_REASON = "本机 GPU 未就绪"
LOCAL_NAME = "本机"


@dataclass(frozen=True)
class PastPlan:
    """A confirmed plan and the actors its film was looked up with."""

    film_id: str
    input_revision: int
    ltx_model: str | None
    confirmed_at: datetime
    actors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    ltx_model: str | None
    ltx_reason: str
    machine: str
    machine_reason: str


def actor_key(name: str) -> str:
    """The matching key of an actor name: NFKC, surrounding whitespace trimmed."""
    return unicodedata.normalize("NFKC", name).strip()


def tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier, tier)


def actor_tiers(history: Iterable[PastPlan]) -> dict[str, str]:
    """Per actor key, the tier of the most recently confirmed plan naming them."""
    latest: dict[str, str] = {}
    ordered = sorted(
        history, key=lambda p: (p.confirmed_at, p.film_id, p.input_revision)
    )
    for plan in ordered:
        if plan.ltx_model is None:  # a film that needed no restore says nothing
            continue
        for actor in plan.actors:
            key = actor_key(actor)
            if key:
                latest[key] = plan.ltx_model
    return latest


def recommend_tier(
    *,
    film_id: str,
    needs_gpu: bool,
    actors: Sequence[str],
    history: Sequence[PastPlan],
    default: str,
) -> tuple[str | None, str]:
    """`(ltx_model, reason)` of one unplanned film."""
    if not needs_gpu:
        return None, NO_RESTORE
    previous = [p for p in history if p.film_id == film_id and p.ltx_model]
    if previous:
        tier = max(previous, key=lambda p: p.input_revision).ltx_model
        assert tier is not None
        return tier, f"上次选择 {tier_label(tier)}"
    tiers = actor_tiers(p for p in history if p.film_id != film_id)
    best: tuple[str, str] | None = None
    for actor in actors:
        tier = tiers.get(actor_key(actor))
        if tier is not None and (best is None or _rank(tier) > _rank(best[0])):
            best = (tier, actor_key(actor))
    if best is not None:
        return best[0], f"{best[1]} 上次选择 {tier_label(best[0])}"
    return default, DEFAULT_REASON


def _rank(tier: str) -> int:
    return TIERS.index(tier) if tier in TIERS else -1


def vram_text(vram_mib: int) -> str:
    return f"{round(vram_mib / 1024)} GB"


def machine_reason(gpu_name: str | None, vram_mib: int, *, uhd: bool) -> str:
    if uhd:
        return UHD_REASON
    if not gpu_name:
        return NO_GPU_REASON
    return f"{gpu_name} {vram_text(vram_mib)}"


def recommend(
    *,
    film_id: str,
    needs_gpu: bool,
    actors: Sequence[str],
    history: Sequence[PastPlan],
    default: str,
    machine: str,
    gpu_name: str | None,
    vram_mib: int,
    uhd: bool,
) -> Recommendation:
    tier, reason = recommend_tier(
        film_id=film_id,
        needs_gpu=needs_gpu,
        actors=actors,
        history=history,
        default=default,
    )
    return Recommendation(
        ltx_model=tier,
        ltx_reason=reason,
        machine=machine,
        machine_reason=machine_reason(gpu_name, vram_mib, uhd=uhd),
    )
