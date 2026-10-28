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
import re
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


# #355: the legacy standalone media monitors; the media center does not run
# through them, so on a media machine their `disabled` is not "Jasna is off".
_LEGACY_MEDIA = ("lada", "jasna", "avsubs")
_LEGACY_NOTE = "disabled（旧版独立监控未使用；媒体任务见「媒体处理」）"


def _monitor_lines(status_json: Optional[str], *, media: bool = False) -> list[str]:
    """`- name: <status>` lines from an agent /status payload, where <status> is the
    metric-rich V2-format string (see _status_text). Tolerates the V3 dict shape
    (name → {state, metrics}) AND the V2 list shape ([{name, status, enabled}]) so
    status.md is correct for both agent versions (#38 review). `media`: the
    machine takes part in media-center work (#355)."""
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
                legacy = media and s.get("type_id") in _LEGACY_MEDIA
                lines.append(
                    f"- {_inline(str(name))}: {_LEGACY_NOTE if legacy else 'disabled'}"
                )
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


def _workflow_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone() if stamp.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


# Jasna prints h:mm:ss or mm:ss (`run 09:54 left`), passed on as `剩余 …`
_ETA = re.compile(r"剩余 ((?:\d+:)?\d{1,2}:\d{2})")


def _workflow_progress(current: Any, now: datetime) -> str:
    name, parts = _workflow_detail(current, now)
    return " · ".join([f"{name} {parts[0]}", *parts[1:]]) if name else ""


def _workflow_detail(current: Any, now: datetime) -> tuple[str, list[str]]:
    """A current's film and its parts: stage label first, then LTX / progress,
    the source's remaining time (#355) and the elapsed time."""
    if not isinstance(current, dict):
        return "", []
    name = _inline(str(current.get("film") or ""))
    if not name:
        return "", []
    labels = {
        "restore": "修复",
        "asr": "识别",
        "translate": "翻译",
        "return": "回传",
        "scrape": "刮削",
        "fetch": "下载",  # P2-4: a worker fetching the source
    }
    stage = current.get("stage")
    label = labels.get(stage, "处理中") if isinstance(stage, str) else "处理中"
    parts = [label]
    ltx = current.get("ltx")
    if isinstance(ltx, dict):
        index: Any = ltx.get("stage_index")
        total: Any = ltx.get("stage_total")
        stage_name = _inline(str(ltx.get("stage_name") or ""), 40)
        percent: Any = ltx.get("percent")
        if _is_num(index) and _is_num(total) and 1 <= index <= total:
            text = f"LTX {int(index)}/{int(total)} {stage_name}".rstrip()
            if _is_num(percent) and 0 <= percent <= 100:
                text += f" {percent:.0f}%"
            parts.append(text)
            progress = current.get("progress")
            if isinstance(progress, str) and (eta := _ETA.search(progress)):
                parts.append(f"剩余 {eta.group(1)}")
    elif isinstance(current.get("progress"), str) and current["progress"]:
        parts.append(_inline(current["progress"]))
    start = _workflow_time(current.get("started_at"))
    if start is not None and start <= now:
        seconds = int((now - start).total_seconds())
        parts.append(
            f"已用 {seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
        )
    return name, parts


def _media_text(value: Any, cap: int = 400) -> str:
    return _inline(value, cap) if isinstance(value, str) else ""


def _media_count(value: Any) -> int | None:
    return int(value) if _is_num(value) and value >= 0 else None


def _media_preview(label: str, workflow: dict, key: str, shown: int, unit: str) -> str:
    values = workflow.get(key)
    if not isinstance(values, list):
        return f"{label}未知"
    total = _media_count(workflow.get(f"{key}_total"))
    more = _media_count(workflow.get(f"{key}_more"))
    # Include both source omissions and this renderer's own defensive cap.
    omitted = max(0, len(values) - shown)
    if more is not None:
        omitted += more
    if total is not None:
        omitted = max(omitted, total - shown)
    # Conflicting metadata cannot establish a complete preview.
    if total is not None and total < len(values):
        total = None
    result = f"{label} {shown}/{total if total is not None else '未知'}"
    return result + (f" · 另有 {omitted} {unit}未展示" if omitted else "")


def _workflow_available(workflow: Any) -> bool:
    if not isinstance(workflow, dict):
        return False
    counts = workflow.get("counts")
    if isinstance(counts, dict) and any(
        _media_count(counts.get(key)) is not None
        for key in ("planning", "active", "archiving", "confirm", "attention", "done")
    ):
        return True
    current = workflow.get("current")
    if isinstance(current, dict) and _media_text(current.get("film")):
        return True  # older agents may supply only their current film
    currents = workflow.get("currents")
    if isinstance(currents, list) and any(
        isinstance(item, dict) and _media_text(item.get("film"))
        for item in currents[:16]
    ):
        return True
    return (
        isinstance(workflow.get("films"), list)
        and _media_count(workflow.get("films_total")) is not None
    )


def _workflow_film_lines(workflow: dict) -> list[str]:
    lines = []
    films = workflow.get("films")
    for film in films[:32] if isinstance(films, list) else []:
        if not isinstance(film, dict) or not (name := _media_text(film.get("name"))):
            continue
        parts = [name]
        parts.extend(
            text
            for key in ("stage", "status", "machine")
            if (text := _media_text(film.get(key)))
        )
        position = film.get("route_position")
        if type(position) is int and position >= 0:
            parts.append(f"队列第 {position + 1} 位")
        parts.extend(
            text
            for key in ("route_live", "note", "attention_kind")
            if (text := _media_text(film.get(key)))
        )
        lines.append("  - " + " · ".join(parts))
    return [
        "  " + _media_preview("队列预览", workflow, "films", len(lines), "部"),
        *lines,
    ]


def _status_data(status: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(status.get("status_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _point(now: str) -> datetime:
    try:
        return datetime.strptime(now, "%Y-%m-%d %H:%M:%S").astimezone()
    except ValueError:
        return datetime.now().astimezone()


def _workflow_unavailable(status: dict[str, Any], workflow: Any) -> str | None:
    """Why a controller's media summary is not current, else None."""
    health = status.get("status_health")
    health = health if isinstance(health, dict) else {}
    if not status.get("reachable"):
        return "离线"
    if health.get("state", "ok") != "ok":
        return f"暂不可用（Hub数据 {_media_text(health.get('state'))}）"
    if not isinstance(workflow, dict) or not _workflow_available(workflow):
        return "暂不可用"
    age: Any = workflow.get("sample_age_seconds")
    if "sample_age_seconds" in workflow and (not _is_num(age) or not 0 <= age <= 15):
        return "暂不可用（源采样已过期或年龄无效）"
    return None


def _dispatch_text(workflow: dict) -> str:
    dispatch = workflow.get("dispatch")
    if not isinstance(dispatch, dict) or not _media_text(dispatch.get("state")):
        return "调度状态未知"
    parts = ["调度"]
    if dispatch.get("paused") is True:
        parts.append("已暂停")
    parts.append(_media_text(dispatch["state"]))
    if reason := _media_text(dispatch.get("reason")):
        parts.append(reason)
    return " ".join(parts[:2]) + "".join(f" · {p}" for p in parts[2:])


_NODE_STATES = {"online": "在线", "lost": "失联", "waiting": "等待心跳"}


def _worker_text(row: dict) -> str:
    """#355: one GPU worker's heartbeat as the controller saw it. A worker that
    is not online shows no GPU sample: its last one is not current."""
    state = row.get("state")
    online = state == "online"
    parts = [
        _NODE_STATES.get(state, "状态未知") if isinstance(state, str) else "状态未知"
    ]
    age: Any = row.get("heartbeat_age_s")
    if not online and _is_num(age) and age >= 0:
        parts.append(f"心跳 {int(age)}s 前")
    if row.get("paused") is True:
        parts.append("已暂停")
    admission: Any = row.get("admission")
    if admission in ("disabled", "draining"):
        parts.append("未启用" if admission == "disabled" else "排空中")
    gpu = row.get("gpu")
    if online and isinstance(gpu, dict):
        util: Any = gpu.get("util")
        used: Any = gpu.get("used_mib")
        total: Any = gpu.get("vram_mib")
        power: Any = gpu.get("power_w")
        if _is_num(util) and 0 <= util <= 100:
            parts.append(f"GPU {util:.0f}%")
        if _is_num(used) and _is_num(total) and total > 0 and 0 <= used:
            parts.append(f"显存 {used / 1024:.1f}/{total / 1024:.1f}GB")
        if _is_num(power) and power >= 0:
            parts.append(f"{power:.0f}W")
    if online:
        staged: Any = row.get("staged")
        if _is_num(staged) and staged > 0:
            parts.append(f"已预取 {int(staged)} 部")
        transfers = row.get("transfers")
        for item in transfers[:8] if isinstance(transfers, list) else []:
            if not isinstance(item, dict) or not (
                film := _media_text(item.get("film"))
            ):
                continue
            label = "下载" if item.get("dir") == "down" else "回传"
            percent: Any = item.get("percent")
            parts.append(
                f"{label} {film}"
                + (
                    f" {int(percent)}%"
                    if _is_num(percent) and 0 <= percent <= 100
                    else ""
                )
            )
    return " · ".join(parts)


def _workflow_lines(statuses: list[dict[str, Any]], now: str) -> list[str]:
    sources = []
    for status in statuses:
        data = _status_data(status)
        # Ordinary/older Agents need not host a media controller. An absent
        # workflow key is not a failed media service; preserve their old output.
        if "workflow" in data:
            sources.append((status, data))
    if not sources:
        return []
    point = _point(now)
    lines = ["## 媒体处理"]
    for status, data in sources:
        name = _inline(str(status.get("name", "unknown")))
        workflow: Any = data.get("workflow")
        health = status.get("status_health")
        health = health if isinstance(health, dict) else {}
        if reason := _workflow_unavailable(status, workflow):
            lines.append(f"- {name}: {reason}")
            continue
        age: Any = workflow.get("sample_age_seconds")
        source = _media_text(data.get("machine")) or name
        version = _media_text(data.get("version")) or "未知"
        lines.append(f"- {name}: 来源 {source} · Agent {version}")
        hub_age: Any = health.get("age_seconds")
        age_text = f"源采样年龄 {int(age)}s" if _is_num(age) else "源采样年龄未知"
        known_hub_age = _is_num(hub_age) and hub_age >= 0
        age_text += (
            f" · Hub缓存年龄 {int(hub_age)}s" if known_hub_age else " · Hub缓存年龄未知"
        )
        if _is_num(age) and known_hub_age:
            age_text += f" · 数据年龄约 {int(age + hub_age)}s"
        lines.append("  " + age_text)
        if sampled := _media_text(workflow.get("sampled_at")):
            lines.append(f"  源采样时间 {sampled}（仅诊断）")
        lines.append("  " + _dispatch_text(workflow))
        counts: Any = workflow.get("counts")

        def count(*keys: str) -> str:
            values = [_media_count(counts.get(key, 0)) for key in keys]
            return (
                "未知"
                if None in values
                else str(sum(v for v in values if v is not None))
            )

        completions = workflow.get("completed_at")
        today = (
            sum(
                1
                for value in completions
                if (stamp := _workflow_time(value)) is not None
                and stamp.date() == point.date()
            )
            if isinstance(completions, list)
            else 0
        )
        if isinstance(counts, dict) and counts:
            lines.append(
                f"  待处理 {count('planning')} · 排队/处理中 {count('active', 'archiving')} · "
                f"待确认 {count('confirm')} · "
                f"异常 {count('attention', 'packages_attention')} · 完成今日 {today}"
            )
        else:
            lines.append("  队列计数未知")
        lines.extend(_workflow_film_lines(workflow))
        # P2-4 R12: each running film with its machine; an older agent
        # sends only `current`
        currents = workflow.get("currents")
        if "currents_total" in workflow or "currents_more" in workflow:
            shown = (
                sum(bool(_workflow_progress(item, point)) for item in currents[:16])
                if isinstance(currents, list)
                else 0
            )
            lines.append(
                "  " + _media_preview("当前任务", workflow, "currents", shown, "台")
            )
        if isinstance(currents, list) and currents:
            for item in currents[:16]:
                progress = _workflow_progress(item, point)
                machine = (
                    _inline(str(item.get("machine") or ""), 64)
                    if isinstance(item, dict)
                    else ""
                )
                if progress:
                    lines.append(f"- {progress}" + (f" · {machine}" if machine else ""))
        else:
            progress = _workflow_progress(workflow.get("current"), point)
            if progress:
                lines.append(f"- {progress}")
        workers = workflow.get("workers")
        if isinstance(workers, list):  # #355: each GPU worker's heartbeat
            rows = [
                (worker_name, row)
                for row in workers[:16]
                if isinstance(row, dict)
                and (worker_name := _inline(str(row.get("name") or ""), 64))
            ]
            lines.append(
                "  " + _media_preview("GPU 节点", workflow, "workers", len(rows), "台")
            )
            lines.extend(f"  - {n}: {_worker_text(row)}" for n, row in rows)
    return [*lines, ""]


def _machine_keys(status: dict[str, Any], data: dict[str, Any]) -> set[str]:
    names = (status.get("name"), data.get("machine"))
    return {
        key
        for value in names
        if isinstance(value, str) and (key := _inline(value, 64).casefold())
    }


def _media_index(statuses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """#355: current controller summaries by machine (case-insensitive name):
    the controller's own summary, each machine's running work and each GPU
    worker's heartbeat. Only a current source is indexed, so a stale or offline
    controller never puts a live claim under a machine."""
    index: dict[str, dict[str, Any]] = {}

    def slot(key: str) -> dict[str, Any]:
        return index.setdefault(key, {"controller": None, "tasks": [], "worker": None})

    for status in statuses:
        data = _status_data(status)
        workflow: Any = data.get("workflow")
        if "workflow" not in data or _workflow_unavailable(status, workflow):
            continue
        own = _machine_keys(status, data)
        for key in own:
            slot(key)["controller"] = slot(key)["controller"] or workflow
        currents = workflow.get("currents")
        if not isinstance(currents, list):
            currents = (
                [dict(workflow["current"], machine="本机")]
                if isinstance(workflow.get("current"), dict)
                else []
            )
        for item in currents[:16]:
            if not isinstance(item, dict):
                continue
            machine = _inline(str(item.get("machine") or ""), 64)
            keys = own if machine in ("", "本机") else {machine.casefold()}
            for key in keys:
                slot(key)["tasks"].append(item)
        workers = workflow.get("workers")
        for row in workers[:16] if isinstance(workers, list) else []:
            if isinstance(row, dict) and (
                name := _inline(str(row.get("name") or ""), 64)
            ):
                entry = slot(name.casefold())
                entry["worker"] = entry["worker"] or row
    return index


def _machine_media(
    status: dict[str, Any],
    data: dict[str, Any],
    index: dict[str, dict[str, Any]],
    point: datetime,
) -> tuple[list[str], list[str]]:
    """#355: (monitor-style lines, indented detail lines) for one machine.

    The OpenClaw readers parse `- <monitor>: <state>`, so the work itself is a
    monitor line in the running-monitor shape `running | <film> | <progress>`."""
    keys = _machine_keys(status, data)
    controller = next((index[k]["controller"] for k in keys if k in index), None)
    worker = next(
        (index[k]["worker"] for k in keys if k in index and index[k]["worker"]), None
    )
    tasks = [item for k in sorted(keys) if k in index for item in index[k]["tasks"]]
    lines, details = [], []
    if controller is not None:
        details.append(f"  媒体主控: {_dispatch_text(controller)} · 详见「媒体处理」")
    if worker is not None:
        details.append(f"  媒体节点: {_worker_text(worker)}")
    seen = set()
    for item in tasks:
        name, parts = _workflow_detail(item, point)
        if name and id(item) not in seen:
            seen.add(id(item))
            lines.append(f"- 媒体处理中心: running | {name} | {' · '.join(parts)}")
    if not lines and worker is not None:
        state = worker.get("state")
        if state == "online":
            lines.append("- 媒体处理中心: idle（无运行任务）")
        elif state in ("lost", "waiting"):
            lines.append(f"- 媒体处理中心: {state}（{_worker_text(worker)}）")
    own = data.get("workflow_worker")
    if not lines and isinstance(own, dict) and status.get("reachable"):
        lines.append(f"- 媒体处理中心: {_self_report(own)}")
    return lines, details


_STAGES = {"restore": "修复", "asr": "识别", "fetch": "下载"}


def _self_report(own: dict) -> str:
    """A worker's own `/status.workflow_worker` when no current controller
    summary names it (#355): state, controller link, pause and its stage.
    `running` is the worker service's state, so a running service with no
    stage and not busy reads `idle`."""
    state = _media_text(own.get("state")) or "unknown"
    current = own.get("current")
    working = isinstance(current, dict) or own.get("busy") is True
    if state == "running" and not working:
        state = "idle"
    parts = ["本机自报", "已连接主控" if own.get("connected") is True else "未连接主控"]
    if own.get("paused") is True:
        parts.append("已暂停")
    if isinstance(current, dict) and isinstance(current.get("stage"), str):
        stage = _STAGES.get(current["stage"], _media_text(current["stage"], 32))
        phase = _media_text(current.get("phase"), 32)
        parts.append(f"{stage} {phase}".rstrip())
    elif own.get("busy") is not True:
        parts.append("无运行任务")
    return f"{state}（{' · '.join(parts)}）"


def render_status_md(
    statuses: list[dict[str, Any]], now: str, *, notifications: dict | None = None
) -> str:
    """Build the status.md text. `statuses` = snapshot rows
    ({name, reachable, status_json, last_seen}); offline servers render last_seen.
    `now` is a preformatted timestamp."""
    lines = ["# TaskPaw Hub Status", "", f"Last updated: {now}", ""]
    if notifications is not None:
        lines.extend(
            [
                "Notifications: " + _inline(str(notifications.get("state", "unknown"))),
                "  pending "
                + str(notifications.get("pending_count", "unknown"))
                + " · failed "
                + str(notifications.get("failed_count", "unknown"))
                + " · dead letters "
                + str(notifications.get("dead_letter_count", "unknown"))
                + " · quarantined "
                + str(notifications.get("quarantined_count", "unknown")),
                "  oldest pending "
                + (
                    f"{int(notifications['oldest_pending_age_seconds'])}s"
                    if _is_num(notifications.get("oldest_pending_age_seconds"))
                    else "unknown/none"
                )
                + " · last success "
                + _inline(str(notifications.get("last_success_at") or "never")),
                "  last failure "
                + _inline(str(notifications.get("last_failure_at") or "never"))
                + " · "
                + _inline(str(notifications.get("last_failure_reason") or "none")),
                "",
            ]
        )
    index = _media_index(statuses)
    point = _point(now)
    for s in statuses:
        name = _inline(str(s.get("name", "unknown")))  # sanitized: no line injection
        data = _status_data(s)
        media, details = _machine_media(s, data, index, point)
        if s.get("reachable"):
            lines.append(f"## {name}: ONLINE")
            lines.extend(
                _monitor_lines(
                    s.get("status_json"),
                    media=bool(media or details)
                    or "workflow" in data
                    or "workflow_worker" in data,
                )
            )
            lines.extend(media)
            lines.extend(details)
        else:
            # last seen = the last time it was actually reachable (not this failed
            # poll), so the time doesn't advance during an outage (#38 review).
            seen = _inline(_last_seen_hms(s.get("last_seen")))
            lines.append(
                f"## {name}: OFFLINE (last seen {seen})"
                if seen
                else f"## {name}: OFFLINE"
            )
            # #355: the controller may still hear a worker the Hub cannot reach;
            # indented, so an OFFLINE section keeps no monitor lines
            lines.extend("  " + line[2:] for line in media)
            lines.extend(details)
        health = s.get("status_health") or {}
        if health:
            age: Any = health.get("age_seconds")
            lines.append(
                "  Data: "
                + _inline(str(health.get("state", "unknown")), 32)
                + (
                    f" · age {int(age)}s"
                    if _is_num(age) and age >= 0
                    else " · age unknown"
                )
                + " · last success "
                + _inline(str(health.get("last_good_at") or "never"), 64)
            )
        if health.get("error_code"):
            reason = _inline(str(health["error_code"]), 64)
            age = health.get("age_seconds")
            detail = (
                f" · last good {int(age)}s ago"
                if isinstance(age, (int, float)) and _is_num(age) and age >= 0
                else " · age unknown"
            )
            lines.append(f"  Upstream: {reason}{detail}")
        lines.append("")
    lines.extend(_workflow_lines(statuses, now))
    return "\n".join(lines)


def write_status_md(
    path, statuses: list[dict[str, Any]], now: str, *, notifications: dict | None = None
) -> None:
    """Atomically write status.md (tmp + os.replace) so OpenClaw never reads a
    half-written file."""
    from pathlib import Path

    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            render_status_md(statuses, now, notifications=notifications),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)  # no .tmp residue if render/write/replace fails
