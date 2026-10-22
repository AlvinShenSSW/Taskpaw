"""PWR-2 (#288): a film's estimated energy and the 待处理 prediction.

Design: docs/specs/2026-10-22-power-estimation.md, v2 PW04/PW05/PW13. Every
figure is an estimate (估算); there is no billing claim.

- **Film energy** is pure over one `BoardFilm`: per local stage, the sum of
  the current revision's resolved attempts' `summary.energy` (failed ones
  included), plus the running attempt's energy so far from the host's
  `current`. An attempt resolved without a value (cut off by a crash) makes
  the stage and the film 「部分」 (`partial`).
- **Prediction** (PW13): per machine × resolution (4K / 1080p) × tier, the
  median Wh per film-hour of the newest `HISTORY_FILMS` complete films; with
  fewer than `MIN_FILMS` the tier has no value (「历史不足」).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from statistics import median
from typing import Any

from . import models as m

STAGES: tuple[str, ...] = ("restore", "asr", "translate", "return", "scrape")
TIERS: tuple[str, ...] = ("standard", "good", "high", "none")  # none: 无需修复
HISTORY_FILMS = 10
MIN_FILMS = 3
LOCAL = "local"  # phase 1: every stage runs on this agent's machine
_FINISHED = ("succeeded", "skipped")
_MS_PER_HOUR = 3_600_000


def _r(value: float, digits: int = 4) -> float:
    return round(value, digits)


def _live(film_id: str, stage: str, current: dict[str, Any] | None) -> Any:
    """The running attempt's energy so far, when it is this film's stage."""
    if not current or current.get("film_id") != film_id:
        return None
    if current.get("stage") != stage or not isinstance(current.get("energy"), dict):
        return None
    try:
        return m.AttemptEnergy.model_validate(current["energy"])
    except ValueError:
        return None


def _stage(
    values: Sequence[m.AttemptEnergy | None], live: m.AttemptEnergy | None
) -> dict[str, Any]:
    parts = [v for v in values if v is not None] + ([live] if live else [])
    wh = sum(p.wh for p in parts)
    seconds = sum(p.seconds for p in parts)
    peaks = [p.peak_w for p in parts if p.peak_w is not None]
    return {
        "wh": _r(wh),
        "gpu_wh": _r(sum(p.gpu_wh for p in parts)),
        "cpu_wh": _r(sum(p.cpu_wh for p in parts)),
        "other_wh": _r(sum(p.other_wh for p in parts)),
        "seconds": round(seconds, 1),
        "avg_w": round(wh * 3600 / seconds, 1) if seconds > 0 else None,
        "peak_w": max(peaks) if peaks else None,
        "machine": LOCAL,
        "attempts": len(values) + (1 if live else 0),
        "partial": any(v is None for v in values),
        "running": live is not None,
    }


def film_energy(
    film: m.BoardFilm, current: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """`energy` of a film row: `{stages: {key: {wh, avg_w, peak_w, machine,
    …}}, total_wh, partial}`; None while no attempt has a value (nothing ran
    yet, sampling off, or evidence from before PWR-2)."""
    stages: dict[str, dict[str, Any]] = {}
    known = 0
    for stage in film.stages:
        if stage.stage not in STAGES:
            continue
        live = _live(film.film_id, stage.stage, current)
        if not stage.attempt_energy and live is None:
            continue
        known += sum(1 for v in stage.attempt_energy if v is not None) + bool(live)
        stages[stage.stage] = _stage(stage.attempt_energy, live)
    if known == 0:
        return None
    return {
        "stages": stages,
        "total_wh": _r(sum(s["wh"] for s in stages.values())),
        "partial": any(s["partial"] for s in stages.values()),
        "running": any(s["running"] for s in stages.values()),
        "estimated": True,
    }


# ── prediction (PW13) ───────────────────────────────────────────────────
def tier_of(film: m.BoardFilm) -> str:
    return film.plan_ltx_model or "none"


def _finished_at(film: m.BoardFilm) -> datetime | None:
    """When the film's translate finished; None while the run is not complete."""
    stage = next((s for s in film.stages if s.stage == "translate"), None)
    if stage is None or stage.status not in _FINISHED:
        return None
    return stage.ended_at or film.created_at


def history(films: Iterable[m.BoardFilm]) -> list[dict[str, Any]]:
    """Every complete film with a whole energy value: machine, resolution,
    tier, Wh and Wh per film-hour, newest first."""
    from .board import resolution

    out: list[dict[str, Any]] = []
    for film in films:
        if not film.planned or not film.duration_ms:
            continue
        res = resolution(film.max_width, film.max_height)
        done = _finished_at(film)
        energy = film_energy(film)
        if res is None or done is None or energy is None or energy["partial"]:
            continue
        hours = film.duration_ms / _MS_PER_HOUR
        out.append(
            {
                "film_id": film.film_id,
                "machine": film.plan_machine or LOCAL,
                "resolution": res,
                "tier": tier_of(film),
                "wh": energy["total_wh"],
                "wh_per_hour": _r(energy["total_wh"] / hours),
                "finished_at": done,
            }
        )
    out.sort(key=lambda row: row["finished_at"], reverse=True)
    return out


def rates(
    films: Iterable[m.BoardFilm], limit: int = HISTORY_FILMS
) -> list[dict[str, Any]]:
    """Per machine × resolution × tier: the films counted (newest `limit`)
    and their median Wh per film-hour, None below `MIN_FILMS`."""
    groups: dict[tuple[str, str, str], list[float]] = {}
    for row in history(films):
        key = (row["machine"], row["resolution"], row["tier"])
        values = groups.setdefault(key, [])
        if len(values) < limit:
            values.append(row["wh_per_hour"])
    return [
        {
            "machine": machine,
            "resolution": res,
            "tier": tier,
            "films": len(values),
            "wh_per_hour": _r(median(values)) if len(values) >= MIN_FILMS else None,
        }
        for (machine, res, tier), values in sorted(groups.items())
    ]


def predict(
    table: Sequence[dict[str, Any]],
    machine: str,
    resolution: str | None,
    duration_ms: int | None,
) -> dict[str, Any]:
    """`prediction` of a 待处理 entry: Wh per tier for this film's length on
    `machine`, None per tier with too little history (「历史不足」)."""
    by_tier = {
        row["tier"]: row
        for row in table
        if row["machine"] == machine and row["resolution"] == resolution
    }
    hours = duration_ms / _MS_PER_HOUR if duration_ms else None
    out: dict[str, Any] = {
        "machine": machine,
        "resolution": resolution,
        "min_films": MIN_FILMS,
        "films": {tier: by_tier.get(tier, {}).get("films", 0) for tier in TIERS},
        "estimated": True,
    }
    for tier in TIERS:
        rate = by_tier.get(tier, {}).get("wh_per_hour")
        out[tier] = _r(rate * hours, 1) if rate is not None and hours else None
    return out
