"""`dev_activity` monitor — is this machine actively running AI? (#154)

Answers "is Claude Code / Codex / Kimi busy, idle, or just open here?" for a
**dev/agent machine** (AI runs only on agents; the Hub aggregates). Two signals,
never conflated:

- **present** (P1, config-free): the tool's process is running (VS Code + the CLI),
  via psutil. Coarse — "the tool is open", not "it's working".
- **state** (P2, precise): busy / waiting / idle from a small JSON file each tool's
  hook/notify writes via `integrations/activity_writer.py`
  (`{tool, state, ts}`). A stale/missing file → `unknown` (never silently `idle`),
  so a crashed "busy" can't stick.

Aggregation (`最忙者胜`): busy › waiting › idle › present_only › none. Freshness is
judged on THIS agent with its own clock (`time.time() - ts`) — never a cross-machine
comparison (#152). Privacy: reports only tool + state + timestamps; it never reads
prompts, code, or session content.

Kimi note (#154 P3): the Kimi Code CLI has no hook/notify mechanism (verified via
`kimi --help`: only `acp`/`server`, no lifecycle events), so Kimi is covered by
process-presence only unless the operator wires `activity_writer.py --tool kimi`
themselves.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
    State,
)
from taskpaw_v3.monitors.process_util import scan_matches

log = logging.getLogger("taskpaw.monitors.dev_activity")

# Built-in process patterns per tool (case-insensitive, matched against name +
# cmdline). VS Code's process is named differently per OS (Code.exe / Code Helper /
# code), so the pattern is deliberately broad for it.
_DEFAULT_PATTERNS: dict[str, str] = {
    "claude": r"\bclaude\b",
    "codex": r"\bcodex\b",
    "kimi": r"\bkimi\b",
    "vscode": r"Code Helper|Code\.exe|Visual Studio Code|[/\\]code[/\\]|\bvscode\b",
}

# The state values activity_writer.py emits that count as "an AI task is active".
_ACTIVE = {"busy", "waiting"}

# Context/host tools whose mere presence does NOT mean "AI is running" — VS Code is
# the editor, not an AI CLI. They're still shown (as context), but their presence
# alone can't make the headline "present_only" (Codex 外门). AI CLIs
# (claude/codex/kimi/custom) do count.
_CONTEXT_TOOLS = {"vscode"}


class DevActivityConfig(BaseMonitorConfig):
    # Which tools to watch. Unknown names still work for state files; only names in
    # _DEFAULT_PATTERNS (or process_patterns) get process-presence detection.
    tools: list[str] = Field(
        default_factory=lambda: ["claude", "codex", "kimi", "vscode"]
    )
    # Directory holding per-tool state files: <state_dir>/agent-activity-<tool>.json
    # (written by integrations/activity_writer.py --path ...).
    state_dir: str = "~/.taskpaw"
    # A tool's state file is trusted only if written within this many seconds.
    freshness_seconds: float = Field(300.0, gt=0)
    # Duty window for the "% busy over the last N seconds" bar.
    window_seconds: float = Field(1800.0, ge=60.0)
    # Optional per-tool process-pattern overrides (regex).
    process_patterns: dict[str, str] = Field(default_factory=dict)

    @field_validator("process_patterns")
    @classmethod
    def _compilable(cls, v: dict[str, str]) -> dict[str, str]:
        # Reject a bad override at config time (like ProcessConfig) — else it would
        # raise re.error on every check() and degrade the monitor (Codex 外门).
        for tool, pat in v.items():
            try:
                re.compile(pat)
            except re.error as e:
                raise ValueError(f"invalid regex for tool {tool!r}: {e}") from e
        return v


def _state_file(state_dir: str, tool: str) -> Path:
    return Path(state_dir).expanduser() / f"agent-activity-{tool}.json"


def _shared_file(state_dir: str) -> Path:
    # activity_writer.py's DEFAULT_PATH (no per-tool suffix) — a legacy/default hook
    # that omits --path writes here, tagging the row with its own `tool` field.
    return Path(state_dir).expanduser() / "agent-activity.json"


def _load(p: Path) -> Optional[dict]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # expected: the tool isn't wired / not installed
    except (OSError, ValueError) as e:
        # A permission-denied dir or corrupt file is worth surfacing (§4), not
        # silently reading as "none".
        log.warning("dev_activity: could not read %s: %s", p, e)
        return None
    return data if isinstance(data, dict) else None


def read_tool_state(
    state_dir: str, tool: str, freshness_seconds: float, now: float
) -> tuple[Optional[str], Optional[float]]:
    """Return (state, age_seconds) for a tool, or (None, None) if missing/unparseable/
    stale. Reads the per-tool file `agent-activity-<tool>.json` first, then falls back
    to the writer's shared default `agent-activity.json` when its `tool` field matches
    — so a default/legacy hook (no `--path`) still feeds this monitor (Codex 外门).
    `state` is one of busy|waiting|idle when fresh."""
    data = _load(_state_file(state_dir, tool))
    if data is None:
        shared = _load(_shared_file(state_dir))
        if shared is not None and shared.get("tool") == tool:
            data = shared
    if data is None:
        return None, None
    ts = data.get("ts")
    state = data.get("state")
    if not isinstance(ts, (int, float)) or not isinstance(state, str):
        return None, None
    age = now - float(ts)
    if age < -freshness_seconds:
        # Implausibly future-dated (beyond a small skew) → invalid, not "fresh
        # forever" (Kimi 终审): a bad/far-future write can't pin the state live.
        return None, None
    if age < 0:  # small clock skew — treat as just-written
        age = 0.0
    if age > freshness_seconds:
        return None, age  # stale → unknown, but report the age for display
    return state, age


def _detect_present(patterns: dict[str, str]) -> dict[str, bool]:
    """ONE psutil sweep → {tool: present} for the given {tool: regex} (via
    process_util.scan_matches). Presence is best-effort: enumeration can raise
    PermissionError/OSError (restricted macOS/service env) or RuntimeError (psutil
    missing) — log it (§4, not silent) and degrade every tool to absent rather than
    abort the check, so file-based activity is still read."""
    if not patterns:
        return {}
    compiled = {t: re.compile(p, re.IGNORECASE) for t, p in patterns.items()}
    try:
        return scan_matches(compiled, search_cmdline=True)
    except (OSError, RuntimeError) as e:
        # PermissionError/OSError (restricted enumeration) or RuntimeError (psutil
        # missing) → degrade all tools to absent; never abort the check.
        log.warning("dev_activity: process presence scan failed: %s", e)
        return {tool: False for tool in patterns}


def aggregate(tools: list[dict]) -> tuple[str, list[str]]:
    """Machine headline (最忙者胜) + the list of currently-busy tools.
    tools = [{tool,state,present,age_s}]; state is busy|waiting|idle|None(unknown)."""
    busy = [t["tool"] for t in tools if t["state"] == "busy"]
    if busy:
        return "busy", busy
    if any(t["state"] == "waiting" for t in tools):
        return "waiting", []
    if any(t["state"] == "idle" for t in tools):
        return "idle", []
    # Presence only counts for AI tools — a lone VS Code (context) isn't "AI".
    if any(t["present"] and t.get("ai", True) for t in tools):
        return "present_only", []
    return "none", []


# Headline → the generic MonitorStatus.state dot (the rich headline lives in metrics).
_STATE_MAP: dict[str, State] = {
    "busy": "running",
    "waiting": "running",
    "idle": "idle",
    "present_only": "idle",
    "none": "unknown",
}


class DevActivityInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: DevActivityConfig) -> None:
        super().__init__(instance_id, config)
        # Active class of the previous check: "busy" | "waiting" | "off".
        self._prev_class: Optional[str] = None
        # (ts, is_busy) samples for the duty bar; bounded by the window on read.
        self._samples: deque[tuple[float, bool]] = deque(maxlen=10_000)

    def _patterns(self, cfg: DevActivityConfig) -> dict[str, str]:
        out: dict[str, str] = {}
        for tool in cfg.tools:
            pat = cfg.process_patterns.get(tool) or _DEFAULT_PATTERNS.get(tool)
            if pat:
                out[tool] = pat
        return out

    def _duty(self, cfg: DevActivityConfig, now: float) -> dict:
        window = cfg.window_seconds
        recent = [(ts, b) for ts, b in self._samples if now - ts <= window]
        if not recent:
            return {"busy_s": 0.0, "ratio": 0.0}
        busy_n = sum(1 for _, b in recent if b)
        ratio = busy_n / len(recent)
        span = min(window, now - recent[0][0]) or 0.0
        return {"busy_s": round(ratio * span, 1), "ratio": round(ratio, 3)}

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: DevActivityConfig = self.config  # type: ignore[assignment]
        now = time.time()
        present = _detect_present(self._patterns(cfg))
        tools: list[dict] = []
        for tool in cfg.tools:
            state, age = read_tool_state(
                cfg.state_dir, tool, cfg.freshness_seconds, now
            )
            tools.append(
                {
                    "tool": tool,
                    "state": state,  # busy|waiting|idle when fresh, else None
                    "present": bool(present.get(tool, False)),
                    "age_s": None if age is None else round(age, 1),
                    # VS Code (context) presence doesn't count as "AI running".
                    "ai": tool not in _CONTEXT_TOOLS,
                }
            )

        headline, busy_tools = aggregate(tools)
        is_busy = headline == "busy"
        self._samples.append((now, is_busy))

        active = [t["tool"] for t in tools if t["state"] in _ACTIVE]
        waiting_tools = [t["tool"] for t in tools if t["state"] == "waiting"]

        # Emit only when the active class changes (busy / waiting / off), so the log
        # isn't noisy AND a busy→waiting transition surfaces the actionable
        # "needs input" signal instead of a misleading "idle" (Codex 外门).
        cls = "busy" if is_busy else "waiting" if headline == "waiting" else "off"
        if self._prev_class is not None and cls != self._prev_class:
            if cls == "busy":
                emit(
                    "info",
                    f"{cfg.name}: AI busy",
                    f"running AI: {', '.join(busy_tools)}",
                    dedupe_key=None,
                )
            elif cls == "waiting":
                emit(
                    "info",
                    f"{cfg.name}: AI waiting for input",
                    f"waiting: {', '.join(waiting_tools)}",
                    dedupe_key=None,
                )
            else:
                emit(
                    "info",
                    f"{cfg.name}: AI idle",
                    "no AI task running",
                    dedupe_key=None,
                )
        self._prev_class = cls

        detail = (
            f"running AI: {', '.join(busy_tools)}"
            if busy_tools
            else {
                "waiting": f"AI waiting: {', '.join(active)}",
                "idle": "AI idle",
                "present_only": "AI present (no activity reported)",
                "none": "no AI activity",
            }[headline]
        )
        return MonitorStatus(
            state=_STATE_MAP[headline],
            detail=detail,
            metrics={
                "ai_state": headline,
                "busy_tools": busy_tools,
                "tools": tools,
                "window_s": int(cfg.window_seconds),
                "duty": self._duty(cfg, now),
            },
        )


class DevActivityPlugin(MonitorPlugin):
    type_id = "dev_activity"
    display_name = "AI activity (Claude/Codex/Kimi)"
    category = "both"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return DevActivityConfig

    @classmethod
    def ui_schema(cls) -> dict:
        return {
            "state_dir": {
                "help": "dir holding agent-activity-<tool>.json (activity_writer.py)"
            }
        }

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return DevActivityInstance(instance_id, config)  # type: ignore[arg-type]
