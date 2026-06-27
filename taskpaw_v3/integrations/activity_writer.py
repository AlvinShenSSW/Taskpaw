"""Dev-agent activity writer (V3 §5c / #22).

A tiny, dependency-free wrapper that a Claude Code hook or Codex notify program
invokes to record whether the agent is busy / idle / waiting. It atomically
writes a small JSON file the `state_file` monitor reads:

    {"tool": "claude", "state": "busy", "session": "abc", "ts": 1750000000.0}

It records ONLY state + a timestamp — never prompts, code, or session content.

Usage:
    # explicit state (Codex notify, or any caller)
    activity_writer.py --tool codex --state idle

    # auto-detect from a Claude Code hook payload on stdin (hook_event_name):
    #   UserPromptSubmit / SessionStart -> busy
    #   Notification                    -> waiting
    #   Stop / SubagentStop             -> idle
    activity_writer.py --tool claude            # reads stdin JSON

The default path is ~/.taskpaw/agent-activity.json; pass --path to use a separate
file per tool when monitoring both Claude and Codex on one machine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

DEFAULT_PATH = "~/.taskpaw/agent-activity.json"

# Claude Code hook event -> activity state.
_CLAUDE_EVENT_STATE = {
    "UserPromptSubmit": "busy",
    "SessionStart": "busy",
    "PreToolUse": "busy",
    "PostToolUse": "busy",
    "Notification": "waiting",
    "Stop": "idle",
    "SubagentStop": "idle",
    "SessionEnd": "idle",
}


def state_from_stdin(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Map a Claude Code hook payload to (state, session_id). Best-effort."""
    try:
        data = json.loads(raw)
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    event = data.get("hook_event_name") or data.get("hookEventName") or ""
    session = data.get("session_id") or data.get("sessionId")
    return _CLAUDE_EVENT_STATE.get(str(event)), (str(session) if session else None)


def write_activity(path: str, tool: str, state: str,
                   session: Optional[str] = None, ts: Optional[float] = None) -> Path:
    """Atomically write the activity file (tmp in same dir + os.replace)."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool": tool,
        "state": state,
        "session": session or "",
        "ts": time.time() if ts is None else ts,
    }
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, p)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Write dev-agent activity state.")
    ap.add_argument("--tool", default="agent", help="agent label (claude|codex|...)")
    ap.add_argument("--state", default=None,
                    help="busy|idle|waiting; omit to auto-detect from stdin hook payload")
    ap.add_argument("--session", default=None, help="optional session id")
    ap.add_argument("--path", default=DEFAULT_PATH, help=f"output file (default {DEFAULT_PATH})")
    args = ap.parse_args(argv)

    state, session = args.state, args.session
    if state is None and not sys.stdin.isatty():
        detected, sess = state_from_stdin(sys.stdin.read())
        state = detected
        session = session or sess
    if state is None:
        # Unknown event / nothing to record — succeed quietly so we never break
        # the host hook chain.
        return 0

    write_activity(args.path, args.tool, state, session)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
