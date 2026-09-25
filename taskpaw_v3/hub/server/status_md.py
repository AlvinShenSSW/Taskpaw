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


def _inline(s: str, cap: int = 200) -> str:
    """Sanitize a free-form string (e.g. a filename) for a single status.md line:
    replace control chars (newlines/tabs) with a space so it can't break the
    line-oriented format OpenClaw parses or inject fake monitor lines, and cap the
    length (Kimi)."""
    cleaned = "".join(c if c.isprintable() else " " for c in s).strip()
    return cleaned[:cap]


def _is_num(v: Any) -> bool:
    # Reject bool (an int subclass) and non-finite floats — a backend metric like
    # gpu_pct: NaN (nvidia-smi "nan") must not render as "GPU nan%" (Kimi).
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
    )


_MODEL_CHARS = 80  # #189: the translate model label's cap (as the agent's)


def _stage_text(m: dict) -> str:
    """#189 (D7): the live stage of a jasna/avsubs focus film, from `steps` —
    its active step, else the one waiting for the GPU: `等待 GPU（<holder>）` /
    `等待 GPU`; asr `识别 43% · 约剩 6 分`, `识别 43%` or `识别 · 已用 4 分`;
    translate `翻译 56% · 约剩 1 分 · <model>`. Nothing for a restore (the
    progress part covers it), for queued/pending/terminal steps, or when
    `steps` is malformed (not a list of {key, state} dicts). A bad number is
    dropped on its own. Minutes never read 0: ETA ceil, elapsed floor, min 1."""
    steps = m.get("steps")
    if not isinstance(steps, list) or not all(
        isinstance(s, dict)
        and isinstance(s.get("key"), str)
        and isinstance(s.get("state"), str)
        for s in steps
    ):
        return ""
    step = next((s for s in steps if s["state"] == "active"), None) or next(
        (s for s in steps if s["state"] == "waiting_gpu"), None
    )
    if step is None:
        return ""
    if step["state"] == "waiting_gpu":
        holder = _inline(step["holder"]) if isinstance(step.get("holder"), str) else ""
        return f"等待 GPU（{holder}）" if holder else "等待 GPU"
    label = {"asr": "识别", "translate": "翻译"}.get(step["key"])
    if label is None:
        return ""  # an active restore: the progress part covers it
    pct, eta, elapsed = step.get("percent"), step.get("eta_s"), step.get("elapsed_s")
    has_pct = _is_num(pct) and 0 <= pct <= 100
    parts = [f"{label} {pct:.0f}%" if has_pct else label]
    if _is_num(eta) and eta >= 0:
        parts.append(f"约剩 {max(1, math.ceil(eta / 60))} 分")
    elif not has_pct and _is_num(elapsed) and elapsed >= 0:
        parts.append(f"已用 {max(1, math.floor(elapsed / 60))} 分")
    if step["key"] == "translate":
        model = step.get("model")
        if not isinstance(model, str):
            model = m.get("model")
        if isinstance(model, str) and (model := _inline(model, _MODEL_CHARS)):
            parts.append(model)
    return " · ".join(parts)


def _status_text(snap: Any) -> str:
    """The human status string for one monitor — the V3 `state` enriched with its
    measured metrics in V2's exact format, so the OpenClaw readers (daily-report /
    idle-detector) get CPU/RAM/GPU/VRAM and queue counts, not just "ok" (V2 parity).
    V3 keeps state + a structured `metrics` dict; V2 baked it all into one string,
    so we rebuild that string here."""
    # State is free-form from a plugin → _inline() the fallback returns so a
    # multi-line state can't break the line-oriented status.md (Kimi).
    if not isinstance(snap, dict):
        return _inline(str(snap))
    state = snap.get("state") or snap.get("status") or "unknown"
    m = snap.get("metrics") or {}
    if not isinstance(m, dict):
        return _inline(str(state))
    parts: list[str] = []
    # Classify by the monitor's type_id (the discriminator the dashboard uses).
    # Fall back to a metric signature ONLY when the snapshot has no type_id at all
    # (older/pre-type agents) — so a *typed* plugin is never misclassified, e.g. a
    # folder monitor that emits `pending` isn't rendered as a ComfyUI queue, nor a
    # lada worker's cpu_pct mistaken for the host (Codex + Kimi).
    # Treat an empty type_id as absent so a stub with type_id:"" still uses the
    # metric-signature fallback rather than being seen as a typed monitor (Kimi).
    tid = snap.get("type_id") or None
    st = str(state).lower()
    # A monitor in a HARD-bad state must surface that state, not a (possibly stale)
    # metric sample — plugins emit metrics even in error states, so rendering them
    # would hide the outage from OpenClaw (Kimi). `degraded` is an active-alert
    # state (host over-threshold, state_file stale, …), NOT an outage, so it keeps
    # rendering metrics — same rule for every monitor type. `unknown` (no state
    # reported yet, or an agent that lost track of the monitor) is an outage too:
    # its metrics can only be stale (#127).
    bad_state = st in {"error", "stopped", "unreachable", "unknown"}

    # host_metrics — CPU / RAM / GPU / VRAM. Each field guarded individually: a
    # host monitor can be typed yet carry empty/partial metrics (startup / disabled
    # stub with metrics={}), so unconditional indexing would KeyError and stop
    # status.md from updating (Codex + Kimi).
    # Fallback signature for agents whose host monitor snapshot omits type_id
    # (older V3): disk_pct / mem_used_mb are reported ONLY by host_metrics —
    # lada/comfyui carry cpu_pct/mem_pct/gpu_pct too (so those can't distinguish),
    # but never disk_pct or system RAM totals.
    is_host = tid == "host_metrics" or (
        tid is None and (_is_num(m.get("disk_pct")) or _is_num(m.get("mem_used_mb")))
    )
    if is_host and not bad_state:
        if _is_num(m.get("cpu_pct")):
            parts.append(f"CPU {m['cpu_pct']:.0f}%")
        if (
            _is_num(m.get("mem_used_mb"))
            and _is_num(m.get("mem_total_mb"))
            and m["mem_total_mb"] > 0
        ):
            parts.append(
                f"RAM {m['mem_used_mb'] / 1024:.1f}/{m['mem_total_mb'] / 1024:.1f}GB"
            )
        if _is_num(m.get("gpu_pct")):
            parts.append(f"GPU {m['gpu_pct']:.0f}%")
        if (
            _is_num(m.get("gpu_mem_used_mb"))
            and _is_num(m.get("gpu_mem_total_mb"))
            and m["gpu_mem_total_mb"] > 0
        ):
            parts.append(
                f"VRAM {m['gpu_mem_used_mb'] / 1024:.1f}/{m['gpu_mem_total_mb'] / 1024:.1f}GB"
            )

    # lada: "X/Y done (Z left)" + current task + per-task progress (#161). Each
    # piece is independent — a managed Lada that supplies I/O via `lada_extra_args`
    # (not the folder fields) has capture-mode progress but NO queue count
    # (`_queue_counts` reads the folder fields), so gating progress on queue_total
    # would hide it for that supported config (Codex 外门). Space-joined so the
    # legacy "X/Y done (Z left)" / "| file |" substrings the V2 scrapers match stay
    # byte-identical when present. hub.db carries the full set (elapsed /
    # processed_frames / remaining_frames) for programmatic reads.
    # `jasna` (#173) reports the SAME queue/progress metric names as lada, so it
    # renders through this exact block — the lada line stays byte-identical.
    # `avsubs` (#179) reports the same queue/current_file keys (its current_file
    # is the relpath being transcribed) and renders here too.
    # Tuple membership (==), not a set: a malformed agent can send a list/dict as
    # type_id and a set lookup would raise TypeError and stall status.md (Codex).
    is_lada = tid in ("lada", "jasna", "avsubs") or (
        tid is None and _is_num(m.get("queue_total"))
    )
    if is_lada and not bad_state:
        lada_parts: list[str] = []
        if _is_num(m.get("queue_total")):
            # Validate each optional field before int() — a malformed
            # queue_completed/queue_remaining (NaN / "n/a" / "abc") would raise and
            # stop status.md from updating (Codex + Kimi).
            total = int(m["queue_total"])
            done = int(m["queue_completed"]) if _is_num(m.get("queue_completed")) else 0
            left = (
                max(0, int(m["queue_remaining"]))
                if _is_num(m.get("queue_remaining"))
                else max(0, total - done)
            )
            lada_parts.append(f"{done}/{total} done ({left} left)")
        if isinstance(m.get("current_file"), str) and (
            cf := _inline(m["current_file"])
        ):
            lada_parts.append(f"| {cf} |")
        # Per-task progress — capture-mode-only fields, each guarded.
        prog: list[str] = []
        if _is_num(m.get("percent")) and 0 <= m["percent"] <= 100:
            prog.append(f"{m['percent']:.0f}%")
        if isinstance(m.get("eta"), str) and (eta := _inline(m["eta"])):
            prog.append(f"ETA {eta}")
        if _is_num(m.get("fps")):
            prog.append(f"{m['fps']:.0f}fps")
        if prog:
            lada_parts.append(" · ".join(prog))
        segment = " ".join(lada_parts)
        # jasna / avsubs (#189): the focus film's live stage (`_stage_text`),
        # then the counts part — jasna「AV 翻译」(#177) `subs S/T`, ONLY when
        # the agent reports a numeric subs_total, led by `修复 a/b` when it
        # reports queue_restored (#189). Without those keys the line above
        # stays byte-identical. A segment already ending in the "| file |"
        # delimiter is continued with a space so the line never shows an empty
        # "| |" cell.
        counts: list[str] = []
        if _is_num(m.get("queue_restored")) and _is_num(m.get("queue_total")):
            counts.append(f"修复 {int(m['queue_restored'])}/{int(m['queue_total'])}")
        if _is_num(m.get("subs_total")):
            subs_done = (
                int(m["subs_completed"]) if _is_num(m.get("subs_completed")) else 0
            )
            subs = f"subs {subs_done}/{int(m['subs_total'])}"
            if _is_num(m.get("subs_failed")) and m["subs_failed"] > 0:
                subs += f" ({int(m['subs_failed'])} failed)"
            counts.append(subs)
        for part in (_stage_text(m), " · ".join(counts)):
            if not part:
                continue
            if not segment:
                segment = part
            elif segment.endswith("|"):
                segment = f"{segment} {part}"
            else:
                segment = f"{segment} | {part}"
        if segment:
            parts.append(segment)

    # comfyui-style depth: "N running, M pending".
    is_comfyui = tid == "comfyui" or (
        tid is None and (_is_num(m.get("running")) or _is_num(m.get("pending")))
    )
    # Only when a queue metric is actually present — a typed-but-down ComfyUI
    # (state "error", empty metrics) must show its state, not "0 running, 0
    # pending", which would hide the outage from OpenClaw (Codex + Kimi).
    if (
        is_comfyui
        and not bad_state
        and (_is_num(m.get("running")) or _is_num(m.get("pending")))
    ):
        # Validate each side before int() — one valid + one non-finite must not
        # crash the render (Codex + Kimi).
        running = int(m["running"]) if _is_num(m.get("running")) else 0
        pending = int(m["pending"]) if _is_num(m.get("pending")) else 0
        parts.append(f"{running} running, {pending} pending")

    return " | ".join(parts) if parts else _inline(str(state))


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
    # Every name/status is _inline()-sanitized: a malicious/misconfigured agent
    # could otherwise put newlines in a monitor name/status and inject fake
    # `## server` / `- monitor` lines into the line-oriented status.md (Kimi).
    #
    # "disabled" is rendered ONLY for a monitor that is NOT actually running — a
    # merge_status stub for a configured-but-unstarted monitor (state "stopped",
    # empty metrics). A monitor that IS reporting live data (metrics, or a running
    # state / V2 status string) must show that data even if its config `enabled`
    # flag is stale/false — otherwise a running LADA whose config says enabled:false
    # shows "disabled" in status.md while the DB/UI show it running (fleet bug).
    if isinstance(monitors, dict):
        for name, snap in monitors.items():
            s = snap if isinstance(snap, dict) else {}
            not_running = str(s.get("state", "")).lower() in (
                "",
                "stopped",
                "unknown",
                "none",
            )
            if s.get("enabled") is False and not_running and not s.get("metrics"):
                lines.append(f"- {_inline(str(name))}: disabled")
            else:
                lines.append(f"- {_inline(str(name))}: {_status_text(snap)}")
    elif isinstance(monitors, list):
        for m in monitors:
            if not isinstance(m, dict):
                continue
            name = m.get("name", "unknown")
            # V2 contract: enabled:false ALWAYS renders "disabled" — a V2 agent
            # includes a `status` field (default "Stopped") even when disabled, so we
            # can't use status-presence to detect "running" here (Codex + Kimi). The
            # live-over-stale-flag fix is V3-dict-only (state/metrics are separate).
            if m.get("enabled") is False:
                lines.append(f"- {_inline(str(name))}: disabled")
            else:
                status = m.get("status") or m.get("state") or "unknown"
                lines.append(f"- {_inline(str(name))}: {_inline(str(status))}")
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
        name = _inline(str(s.get("name", "unknown")))  # sanitized: no line injection
        if s.get("reachable"):
            lines.append(f"## {name}: ONLINE")
            lines.extend(_monitor_lines(s.get("status_json")))
        else:
            # last seen = the last time it was actually reachable (not this failed
            # poll), so the time doesn't advance during an outage (#38 review).
            seen = _inline(_last_seen_hms(s.get("last_seen")))
            lines.append(
                f"## {name}: OFFLINE (last seen {seen})"
                if seen
                else f"## {name}: OFFLINE"
            )
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
        tmp.unlink(missing_ok=True)  # no .tmp residue if render/write/replace fails
