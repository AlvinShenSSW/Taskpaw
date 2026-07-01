"""Render the Hub's `status.md` (OpenClaw compat, #38).

OpenClaw reads a human-readable Markdown snapshot — NOT an API. The format matches
V2's `taskpaw_hub.write_status_file` so `idle-detector-v2.py` / `daily-report.py`
parse it unchanged:

    # TaskPaw Hub Status

    Last updated: YYYY-MM-DD HH:MM:SS

    ## <server>: ONLINE
    - <monitor>: <state>
    ## <server>: OFFLINE (last seen HH:MM:SS)
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any, Optional


def _is_num(v: Any) -> bool:
    # Reject bool (an int subclass) and non-finite floats — a backend metric like
    # gpu_pct: NaN (nvidia-smi "nan") must not render as "GPU nan%" (Kimi).
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
    )


def _status_text(snap: Any) -> str:
    """The human status string for one monitor — the V3 `state` enriched with its
    measured metrics in V2's exact format, so the OpenClaw readers (daily-report /
    idle-detector) get CPU/RAM/GPU/VRAM and queue counts, not just "ok" (V2 parity).
    V3 keeps state + a structured `metrics` dict; V2 baked it all into one string,
    so we rebuild that string here."""
    if not isinstance(snap, dict):
        return str(snap)
    state = snap.get("state") or snap.get("status") or "unknown"
    m = snap.get("metrics") or {}
    if not isinstance(m, dict):
        return str(state)
    parts: list[str] = []
    # Classify by the monitor's type_id (the discriminator the dashboard uses).
    # Fall back to a metric signature ONLY when the snapshot has no type_id at all
    # (older/pre-type agents) — so a *typed* plugin is never misclassified, e.g. a
    # folder monitor that emits `pending` isn't rendered as a ComfyUI queue, nor a
    # lada worker's cpu_pct mistaken for the host (Codex + Kimi).
    tid = snap.get("type_id")

    # host_metrics — CPU / RAM / GPU / VRAM. Each field guarded individually: a
    # host monitor can be typed yet carry empty/partial metrics (startup / disabled
    # stub with metrics={}), so unconditional indexing would KeyError and stop
    # status.md from updating (Codex + Kimi).
    is_host = tid == "host_metrics" or (
        tid is None and _is_num(m.get("mem_used_mb")) and _is_num(m.get("mem_total_mb"))
    )
    if is_host:
        if _is_num(m.get("cpu_pct")):
            parts.append(f"CPU {m['cpu_pct']:.0f}%")
        if _is_num(m.get("mem_used_mb")) and _is_num(m.get("mem_total_mb")) and m["mem_total_mb"] > 0:
            parts.append(f"RAM {m['mem_used_mb'] / 1024:.1f}/{m['mem_total_mb'] / 1024:.1f}GB")
        if _is_num(m.get("gpu_pct")):
            parts.append(f"GPU {m['gpu_pct']:.0f}%")
        if _is_num(m.get("gpu_mem_used_mb")) and _is_num(m.get("gpu_mem_total_mb")) and m["gpu_mem_total_mb"] > 0:
            parts.append(f"VRAM {m['gpu_mem_used_mb'] / 1024:.1f}/{m['gpu_mem_total_mb'] / 1024:.1f}GB")

    # A task plugin in a bad state must surface that state, not a (possibly stale)
    # queue sample — the plugins emit metrics even in error states, so rendering
    # "X/Y done" / "N running" would hide the outage from OpenClaw (Kimi).
    bad_state = str(state) in {"error", "stopped", "degraded", "unreachable"}

    # lada-style queue: "X/Y done (Z left)" + the current task between pipes.
    is_lada = tid == "lada" or (tid is None and _is_num(m.get("queue_total")))
    if is_lada and not bad_state and _is_num(m.get("queue_total")):
        done = int(m.get("queue_completed") or 0)
        total = int(m["queue_total"])
        left = int(m.get("queue_remaining", max(0, total - done)))
        seg = f"{done}/{total} done ({left} left)"
        if isinstance(m.get("current_file"), str) and m["current_file"]:
            seg += f" | {m['current_file']} |"
        parts.append(seg)

    # comfyui-style depth: "N running, M pending".
    is_comfyui = tid == "comfyui" or (
        tid is None and (_is_num(m.get("running")) or _is_num(m.get("pending")))
    )
    # Only when a queue metric is actually present — a typed-but-down ComfyUI
    # (state "error", empty metrics) must show its state, not "0 running, 0
    # pending", which would hide the outage from OpenClaw (Codex + Kimi).
    if is_comfyui and not bad_state and (_is_num(m.get("running")) or _is_num(m.get("pending"))):
        parts.append(f"{int(m.get('running', 0))} running, {int(m.get('pending', 0))} pending")

    return " | ".join(parts) if parts else str(state)


def _monitor_lines(status_json: Optional[str]) -> list[str]:
    """`- name: <status>` lines from an agent /status payload, where <status> is the
    metric-rich V2-format string (see _status_text). Tolerates the V3 dict shape
    (name → {state, metrics}) AND the V2 list shape ([{name, status, enabled}]) so
    status.md is correct for both agent versions (#38 review)."""
    if not status_json:
        return []
    try:
        data = json.loads(status_json)
    except Exception:
        return []
    monitors = data.get("monitors") if isinstance(data, dict) else None
    lines: list[str] = []
    if isinstance(monitors, dict):
        for name, snap in monitors.items():
            # V3 also carries enabled:False stubs for intentionally-stopped monitors
            # (merge_status); render them "disabled", matching the V2 list branch,
            # instead of their stale "stopped" state (Kimi).
            if isinstance(snap, dict) and snap.get("enabled") is False:
                lines.append(f"- {name}: disabled")
            else:
                lines.append(f"- {name}: {_status_text(snap)}")
    elif isinstance(monitors, list):
        for m in monitors:
            if not isinstance(m, dict):
                continue
            name = m.get("name", "unknown")
            if m.get("enabled") is False:           # V2 rendered disabled monitors
                lines.append(f"- {name}: disabled")
            else:
                # V2 already stored a rich status string; keep it verbatim.
                status = m.get("status") or m.get("state") or "unknown"
                lines.append(f"- {name}: {status}")
    return lines


def _last_seen_hms(ts: Optional[str]) -> str:
    """Format a stored localtime stamp as HH:MM:SS to match V2's status.md. Falls
    back to the raw value if it's already time-only / unparseable."""
    if not ts:
        return ""
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
    except Exception:
        return ts


def render_status_md(statuses: list[dict[str, Any]], now: str) -> str:
    """Build the status.md text. `statuses` = snapshot rows
    ({name, reachable, status_json, last_seen}); offline servers render last_seen.
    `now` is a preformatted timestamp."""
    lines = ["# TaskPaw Hub Status", "", f"Last updated: {now}", ""]
    for s in statuses:
        name = s.get("name", "unknown")
        if s.get("reachable"):
            lines.append(f"## {name}: ONLINE")
            lines.extend(_monitor_lines(s.get("status_json")))
        else:
            # last seen = the last time it was actually reachable (not this failed
            # poll), so the time doesn't advance during an outage (#38 review).
            seen = _last_seen_hms(s.get("last_seen"))
            lines.append(f"## {name}: OFFLINE (last seen {seen})" if seen
                         else f"## {name}: OFFLINE")
        lines.append("")
    return "\n".join(lines)


def write_status_md(path, statuses: list[dict[str, Any]], now: str) -> None:
    """Atomically write status.md (tmp + os.replace) so OpenClaw never reads a
    half-written file."""
    from pathlib import Path

    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(render_status_md(statuses, now), encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)   # no .tmp residue if render/write/replace fails
