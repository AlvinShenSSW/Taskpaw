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
from typing import Any, Optional


def _monitor_lines(status_json: Optional[str]) -> list[str]:
    """`- name: state` lines from an agent /status payload. Tolerates the dict
    shape (name → {state}) and the list shape ([{name,state}])."""
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
            state = snap.get("state", "unknown") if isinstance(snap, dict) else snap
            lines.append(f"- {name}: {state}")
    elif isinstance(monitors, list):
        for m in monitors:
            if isinstance(m, dict):
                lines.append(f"- {m.get('name', 'unknown')}: {m.get('state', 'unknown')}")
    return lines


def render_status_md(statuses: list[dict[str, Any]], now: str) -> str:
    """Build the status.md text. `statuses` = rows from store.latest_statuses()
    ({name, reachable, status_json, timestamp}); `now` is a preformatted stamp."""
    lines = ["# TaskPaw Hub Status", "", f"Last updated: {now}", ""]
    for s in statuses:
        name = s.get("name", "unknown")
        if s.get("reachable"):
            lines.append(f"## {name}: ONLINE")
            lines.extend(_monitor_lines(s.get("status_json")))
        else:
            ts = s.get("timestamp")
            lines.append(f"## {name}: OFFLINE (last seen {ts})" if ts
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
    tmp.write_text(render_status_md(statuses, now), encoding="utf-8")
    os.replace(tmp, p)
