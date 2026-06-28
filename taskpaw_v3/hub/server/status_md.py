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
import os
from datetime import datetime
from typing import Any, Optional


def _monitor_lines(status_json: Optional[str]) -> list[str]:
    """`- name: state` lines from an agent /status payload. Tolerates the V3 dict
    shape (name → {state}) AND the V2 list shape ([{name, status, enabled}]) so
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
            if isinstance(snap, dict):
                state = snap.get("state") or snap.get("status") or "unknown"
            else:
                state = snap
            lines.append(f"- {name}: {state}")
    elif isinstance(monitors, list):
        for m in monitors:
            if not isinstance(m, dict):
                continue
            name = m.get("name", "unknown")
            if m.get("enabled") is False:           # V2 rendered disabled monitors
                lines.append(f"- {name}: disabled")
            else:
                state = m.get("state") or m.get("status") or "unknown"
                lines.append(f"- {name}: {state}")
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
