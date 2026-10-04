"""AI activity: hooks → session metadata → attributed CPU → presence.

All observation is local. VS Code is context only; no session contents are read.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator

from taskpaw_v3.integrations.activity_writer import (
    ActivityStoreError,
    _confirm_session_ends,
    _projection_matches,
    read_facts,
)
from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
    State,
)
from taskpaw_v3.monitors.process_util import common_host, cpu_percents, scan_activity
from taskpaw_v3.monitors.session_activity import SessionActivity, safe_path

log = logging.getLogger("taskpaw.monitors.dev_activity")

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
    # External CPU probe (#163): when a tool is present but has no fresh hook state,
    # infer busy/idle from the CPU of its process subtree. Pure observation — no writes
    # to / no impact on the tool. Off → presence-only for un-hooked tools.
    observe: bool = True
    # Subtree CPU% (of one core) at/above which the probe reports the tool busy.
    busy_cpu_percent: float = Field(8.0, gt=0)
    # Optional per-tool process-pattern overrides (regex).
    process_patterns: dict[str, str] = Field(default_factory=dict)

    session_activity: bool = Field(
        True, description="Infer activity from session metadata before CPU."
    )
    session_busy_seconds: float = Field(
        30.0,
        ge=1,
        le=600,
        allow_inf_nan=False,
        description="Recent session write window (seconds).",
    )
    session_idle_seconds: float = Field(
        300.0,
        ge=1,
        le=3600,
        allow_inf_nan=False,
        description="Maximum quiet-session age eligible for idle (seconds).",
    )
    session_scan_interval_seconds: float = Field(
        30.0,
        ge=1,
        le=300,
        allow_inf_nan=False,
        description="Discovery refresh interval (seconds).",
    )
    session_max_files: int = Field(
        64, ge=1, le=256, description="Newest discovered session candidates per tool."
    )
    session_roots: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per-tool directory overrides; an empty list disables lookup.",
    )

    @model_validator(mode="after")
    def _session_config(self):
        if self.session_idle_seconds < self.session_busy_seconds:
            raise ValueError("session idle window must be at least the busy window")
        for paths in self.session_roots.values():
            if len(paths) > 8:
                raise ValueError("at most eight session roots per tool")
            normalized = [Path(os.path.realpath(Path(p).expanduser())) for p in paths]
            for i, path in enumerate(normalized):
                if any(
                    path.is_relative_to(other) or other.is_relative_to(path)
                    for other in normalized[:i]
                ):
                    raise ValueError("session roots must not overlap")
                try:
                    if not safe_path(path) or (path.exists() and not path.is_dir()):
                        raise ValueError("session root must be a regular directory")
                except OSError:
                    # Runtime probe reports denied metadata without leaking the path.
                    continue
        self.tools = list(dict.fromkeys(self.tools))
        return self

    @field_validator("process_patterns")
    @classmethod
    def _compilable(cls, v: dict[str, str]) -> dict[str, str]:
        # Reject a bad override at config time (like ProcessConfig) — else it would
        # raise re.error on every check() and degrade the monitor (Codex 外门).
        for tool, pat in v.items():
            if tool == "vscode":
                raise ValueError(
                    "VS Code is context only; process_patterns override is unsupported"
                )
            if not pat:
                # An empty regex matches every process → every tool false-present.
                raise ValueError(f"process pattern for tool {tool!r} must not be empty")
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


def _load(p: Path, errors: list[str] | None = None) -> Optional[dict]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None  # expected: the tool isn't wired / not installed
    except (OSError, ValueError):
        # A permission-denied dir or corrupt file is worth surfacing (§4), not
        # silently reading as "none".
        if errors is not None:
            errors.append("unavailable")
        return None
    return data if isinstance(data, dict) else None


def read_tool_state(
    state_dir: str,
    tool: str,
    freshness_seconds: float,
    now: float,
    errors: list[str] | None = None,
) -> tuple[Optional[str], Optional[float]]:
    """Return (state, age_seconds) for a tool, or (None, None) if missing/unparseable/
    stale. Reads the per-tool file `agent-activity-<tool>.json` first, then falls back
    to the writer's shared default `agent-activity.json` when its `tool` field matches
    — so a default/legacy hook (no `--path`) still feeds this monitor (Codex 外门).
    `state` is one of busy|waiting|idle when fresh."""
    data = _load(_state_file(state_dir, tool), errors)
    if data is None:
        shared = _load(_shared_file(state_dir), errors)
        if shared is not None and shared.get("tool") == tool:
            data = shared
    if data is None:
        return None, None
    if data.get("tool", tool) != tool:
        return None, None
    ts = data.get("ts")
    state = data.get("state")
    # Reject non-finite ts (NaN/±inf): it would make `age` non-finite and FastAPI
    # would emit NaN/Infinity JSON tokens, breaking the browser JSON parse (Kimi 终审).
    if (
        not isinstance(ts, (int, float))
        or isinstance(ts, bool)
        or not math.isfinite(ts)
        or state not in ("busy", "waiting", "idle")
    ):
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


def read_hook_activity(
    state_dir: str,
    tool: str,
    freshness: float,
    now: float,
    sample: dict,
    bound: set[tuple],
    errors: list[str],
    stop_event: threading.Event | None = None,
    confirmed: dict | None = None,
) -> dict:
    """Commutative bounded facts; no arrival order or expired-unresolved idle."""
    facts: dict[str, dict] = {}
    summaries: dict[tuple, dict] = {}
    unknown = False
    unresolved = False
    limited = False
    watermark = None
    projections = []
    for path in (_state_file(state_dir, tool), _shared_file(state_dir)):
        if stop_event is not None and stop_event.is_set():
            break
        projection = _load(path, errors)
        if stop_event is not None and stop_event.is_set():
            break
        try:
            store = read_facts(path, tool)
        except ActivityStoreError:
            errors.append("unavailable")
            unknown = unresolved = True
            continue
        if stop_event is not None and stop_event.is_set():
            break
        if confirmed is not None:
            expected = confirmed.get(path)
            local = {r["id"]: r for r in store["facts"]}
            if (
                expected is None
                or store["identity"] != expected["identity"]
                or any(local.get(i) != w for i, w in expected["witnesses"].items())
            ):
                unknown = unresolved = True
        link = store["projection_link"]
        rich = projection is not None and any(
            k in projection
            for k in ("activity_schema", "fact_id", "fact_committed", "link_nonce")
        )
        matched = bool(
            rich and projection is not None and _projection_matches(projection, store)
        )
        # Compare the one physical projection/slot BEFORE filtering its tool.
        owner = projection.get("tool") if projection is not None else None
        if rich and (
            not isinstance(owner, str)
            or not owner
            or len(owner.encode("utf-8", errors="replace")) > 256
        ):
            errors.append("unavailable")
            unknown = unresolved = True
        if not matched and (
            (link is not None and link["tool"] == tool) or (rich and owner == tool)
        ):
            unknown = unresolved = True
        if projection is not None and projection.get("tool", tool) == tool:
            projections.append((projection, link if matched else None, rich))
        unknown |= store["overflow"]
        unresolved |= store["overflow"]
        limited |= store["overflow"]
        value = store["watermark"]
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value <= now + min(freshness, 5)
        ):
            watermark = max(watermark or value, value)
        for row in store["facts"]:
            old = facts.get(row["id"])
            if old is not None and any(old[k] != row[k] for k in row if k != "ts"):
                unknown = unresolved = True
                errors.append("unavailable")
                continue
            if (
                old is not None
                and row["kind"] == "verified_session_end"
                and old["ts"] != row["ts"]
            ):
                unknown = unresolved = True
                errors.append("unavailable")
                continue
            if old is None or row["ts"] < old["ts"]:
                facts[row["id"]] = row
        for row in store["summaries"]:
            summaries[
                tuple(
                    row[k]
                    for k in ("tool", "session", "turn", "actor", "pid", "created")
                )
            ] = row
    roots = {
        (r.get("pid"), r.get("created")): r
        for r in sample.get("roots", [])
        if r.get("created") is not None
    }
    for row in facts.values():
        if (row["pid"], row["created"]) in roots:
            bound.add((tool, row["pid"], row["created"]))
    scan_complete = (
        sample.get("complete", False)
        and not sample.get("errors")
        and not sample.get("limited")
    )
    finals = [
        r
        for r in facts.values()
        if r["kind"] in ("interrupt", "session_end", "verified_session_end")
        and -min(freshness, 5) <= now - r["ts"] <= 86400
    ]

    def scope(row):
        return tuple(
            row[k] for k in ("tool", "session", "turn", "actor", "pid", "created")
        )

    def proven(row):
        pair = (row["pid"], row["created"])
        was_bound = (tool, *pair) in bound
        if was_bound and scan_complete and pair not in roots:
            return True
        for final in finals:
            if row.get("ts", final["ts"]) > final["ts"] + 86400:
                continue
            if (
                final["kind"] == "interrupt"
                and row["actor"] == "parent"
                and scope(row) == scope(final)
            ):
                return True
            if (
                final["kind"] in ("session_end", "verified_session_end")
                and (was_bound or final["kind"] == "verified_session_end")
                and row["session"]
                and (row["session"], *pair)
                == (final["session"], final["pid"], final["created"])
            ):
                return True
        return False

    groups: dict[tuple, list[dict]] = {}
    for row in facts.values():
        if row["kind"] in ("verified_session_end", "interrupt") and row not in finals:
            continue  # Expired proof is not unresolved work or current authority.
        groups.setdefault(scope(row), []).append(row)
    for row in summaries.values():
        if not proven(row):
            unknown = unresolved = True
    states: list[str] = []
    ages: list[float] = []
    state: str | None
    covered: set[tuple] = set()
    for rows in groups.values():
        row = rows[0]
        kinds = {r["kind"] for r in rows}
        if proven(row):
            state = "idle"
        elif kinds <= {"presence"}:
            continue
        elif "stop_attempt" in kinds or "unknown" in kinds:
            unknown = unresolved = True
            continue
        else:
            fresh = [
                r
                for r in rows
                if isinstance(r["ts"], (int, float))
                and math.isfinite(r["ts"])
                and -min(freshness, 5) <= now - r["ts"] <= min(freshness, 86400)
            ]
            if not fresh:
                unknown = unresolved = True
                continue
            if "waiting" in kinds:
                # Both official PermissionRequest schemas omit tool_use_id.
                # A call-unit progress conflicts with an unpaired request; no order.
                if any(r["kind"] == "busy" and r["unit"] for r in fresh):
                    unknown = unresolved = True
                    continue
                state = "waiting"
            elif any(r["kind"] == "busy" for r in fresh):
                state = "busy"
            else:
                unknown = unresolved = True
                continue
        states.append(state)
        ages.extend(max(0, now - r["ts"]) for r in rows if math.isfinite(r["ts"]))
        if (row["pid"], row["created"]) in roots:
            covered.add((row["pid"], row["created"]))
    for data, link, rich in projections:
        ts = data.get("ts")
        state = data.get("state")
        if (
            not isinstance(ts, (int, float))
            or isinstance(ts, bool)
            or not math.isfinite(ts)
            or state not in ("busy", "waiting", "idle")
            or now - ts < -min(freshness, 5)
        ):
            continue
        if rich:
            if link is not None:
                if link["resolved"] is not None:
                    states.append("idle")
                    ages.append(max(0, now - link["resolved"]["ts"]))
                    pair = (link["fact"]["pid"], link["fact"]["created"])
                    if (
                        pair in roots
                        and -min(freshness, 5) <= now - link["resolved"]["ts"] <= 86400
                    ):
                        covered.add(pair)
                continue
            # A rich projection cannot be treated as a second independent idle.
            unknown = unresolved = True
            continue
        if state == "idle":
            watermark = max(watermark or ts, ts)
        # Legacy/missing identities never prove whole-tool idle; fresh positives
        # remain useful without inventing ordering or host attribution.
        unknown = True
        unresolved |= state in _ACTIVE
        if now - ts <= freshness and state in _ACTIVE:
            states.append(state)
            ages.append(max(0, now - ts))
    state = (
        "busy"
        if "busy" in states
        else "waiting"
        if "waiting" in states
        else None
        if unknown
        else "idle"
        if "idle" in states
        else None
    )
    if unknown:
        covered.clear()
    host = common_host([roots[pair] for pair in covered]) if covered else "unknown"
    relevant = {
        (tool, r["pid"], r["created"])
        for r in [*facts.values(), *summaries.values()]
        if r["pid"]
    }
    bound.difference_update(
        {pair for pair in bound if pair[0] == tool and pair not in relevant}
    )
    return {
        "state": state,
        "unresolved": unresolved,
        "limited": limited,
        "age": min(ages) if ages else None,
        "unknown": unknown or bool(errors),
        "covered": covered,
        "host": host,
        "watermark": watermark,
    }


def aggregate(tools: list[dict]) -> tuple[str, list[str]]:
    """Machine headline (最忙者胜) + the list of currently-busy tools.
    tools = [{tool,state,present,age_s,ai}]; state is busy|waiting|idle|None(unknown).

    The headline reflects **AI activity**, so busy/waiting/idle count AI tools only
    (`ai` truthy) — a busy VS Code (context/editor, `ai=false`) shows its own state in
    its row but never makes the machine "AI busy" (#163). `present_only` was already
    AI-gated."""
    busy = list(
        dict.fromkeys(
            t["tool"] for t in tools if t["state"] == "busy" and t.get("ai", True)
        )
    )
    if busy:
        return "busy", busy
    if any(t["state"] == "waiting" and t.get("ai", True) for t in tools):
        return "waiting", []
    if any(t["state"] == "idle" and t.get("ai", True) for t in tools):
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
        self._announced_class: Optional[str] = None
        self._idle_pending: bool = False
        self._active_tools: set[str] = set()
        self._compiled = {
            tool: re.compile(config.process_patterns[tool], re.IGNORECASE)
            if tool in config.process_patterns
            else None
            for tool in config.tools
        }
        self._lock = threading.RLock()
        self._probe_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cleanup_complete = False
        self._stopped = False
        self._bound_producers: set[tuple] = set()
        self._watermarks: dict[str, float] = {}
        self._sessions = SessionActivity(config, self._stop_event)
        # (ts, is_busy) samples for the duty bar. Size the ring to actually hold the
        # configured window at the configured cadence (+margin), so a low
        # poll_interval + large window isn't silently truncated (Kimi 终审).
        max_samples = max(1000, int(config.window_seconds / config.poll_interval) + 100)
        self._samples: deque[tuple[float, bool]] = deque(maxlen=max_samples)
        # External CPU probe (#163): previous per-tool subtree cpu_seconds + the
        # monotonic timestamp of that sample, for the busy/idle CPU% delta.
        self._prev_cpu: dict = {}
        self._prev_mono: Optional[float] = None
        self._prev_wall: Optional[float] = None

    def stop(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop_event.set()
        self._stopped = True
        if self._probe_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            try:
                self._close_owned()
            finally:
                self._probe_lock.release()
        else:
            log.warning("activity probe: all/lifecycle/late_cleanup")

    def _close_owned(self) -> None:
        if not self._cleanup_complete:
            self._sessions.close()
            self._cleanup_complete = True

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
        while not self._stop_event.is_set():
            if self._probe_lock.acquire(timeout=0.01):
                break
        else:
            return MonitorStatus(state="stopped")
        try:
            if self._stop_event.is_set():
                return MonitorStatus(state="stopped")
            return self._check(emit)
        finally:
            try:
                if self._stop_event.is_set():
                    self._close_owned()
            finally:
                self._probe_lock.release()

    def _check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: DevActivityConfig = self.config  # type: ignore[assignment]
        now = time.time()
        errors: list[dict] = []
        uncertain_tools: set[str] = set()
        process_unavailable = False
        try:
            snapshot = scan_activity(self._compiled)
        except (OSError, RuntimeError):
            snapshot = {}
            process_unavailable = True
            errors.append({"tool": "all", "layer": "process", "code": "unavailable"})
        if self._stop_event.is_set():
            return MonitorStatus(state="stopped")
        limited = any(s.get("limited", False) for s in snapshot.values())
        for tool, sample in snapshot.items():
            for code in sample.get("errors", []):
                errors.append(
                    {
                        "tool": self._diagnostic_tool(tool),
                        "layer": "process",
                        "code": code,
                    }
                )
        mono = time.monotonic()
        cpu_wall = time.time()
        cpu: dict[str, float] = {}
        if cfg.observe:
            cpu, self._prev_cpu = cpu_percents(
                self._prev_cpu,
                self._prev_mono if self._prev_mono is not None else mono,
                snapshot,
                mono,
                self._prev_wall,
                cpu_wall,
            )
            self._prev_mono = mono
            self._prev_wall = cpu_wall
        if self._stop_event.is_set():
            return MonitorStatus(state="stopped")
        hooks = {}
        uncovered = {}
        for tool in cfg.tools:
            if tool == "vscode":
                continue
            hook_errors: list[str] = []
            sample = snapshot.get(tool, {})
            self._bound_producers.update(
                (tool, r["pid"], r["created"])
                for r in sample.get("roots", [])
                if r.get("pid") and r.get("created") is not None
            )
            confirmation_failed = False
            try:
                confirmed = _confirm_session_ends(
                    (_state_file(cfg.state_dir, tool), _shared_file(cfg.state_dir)),
                    tool,
                    self._bound_producers,
                    now,
                    self._stop_event,
                )
            except ActivityStoreError:
                confirmed = None
                confirmation_failed = True
                hook_errors.append("unavailable")
            if self._stop_event.is_set():
                return MonitorStatus(state="stopped")
            hooks[tool] = read_hook_activity(
                cfg.state_dir,
                tool,
                cfg.freshness_seconds,
                now,
                snapshot.get(tool, {}),
                self._bound_producers,
                hook_errors,
                self._stop_event,
                confirmed,
            )
            if self._stop_event.is_set():
                return MonitorStatus(state="stopped")
            hook = hooks[tool]
            if confirmation_failed:
                hook["unknown"] = hook["unresolved"] = True
                hook["covered"].clear()
                if hook["state"] == "idle":
                    hook["state"] = None
            limited |= hook["limited"]
            if hook["unknown"]:
                uncertain_tools.add(tool)
            if hook["watermark"] is not None:
                self._watermarks[tool] = max(
                    self._watermarks.get(tool, hook["watermark"]), hook["watermark"]
                )
            self._sessions.watermarks = self._watermarks
            errors.extend(
                {"tool": self._diagnostic_tool(tool), "layer": "hook", "code": code}
                for code in dict.fromkeys(hook_errors)
            )
            data = snapshot.get(tool, {})
            roots = [
                r
                for r in data.get("roots", [])
                if (r.get("pid"), r.get("created")) not in hook["covered"]
            ]
            uncovered[tool] = {**data, "roots": roots}
        sessions = (
            self._sessions.sample(uncovered, set(), now)
            if cfg.observe and cfg.session_activity
            else {}
        )
        if self._stop_event.is_set():
            return MonitorStatus(state="stopped")
        tools: list[dict] = []
        for tool, hook in hooks.items():
            state, age = hook["state"], hook["age"]
            sample = snapshot.get(tool, {})
            roots = sample.get("roots", [])
            host = (
                hook["host"]
                if state is not None
                else (
                    common_host(roots)
                    if not sample.get("limited") and not sample.get("errors")
                    else "unknown"
                )
            )
            source = "hook" if state is not None else "presence"
            vscode_state = state if host == "vscode" else None
            session = sessions.get(tool, {})
            if session.get("errors"):
                uncertain_tools.add(tool)
            for code in session.get("errors", []):
                errors.append(
                    {
                        "tool": self._diagnostic_tool(tool),
                        "layer": "session",
                        "code": code,
                    }
                )
            limited |= session.get("limited", False)
            cpu_pct = None
            session_age = None
            # Hook authority covers only a proven producer root. Other roots'
            # positive observations remain useful even while one hook is idle.
            root_cpu = sample.get("root_cpu", {})
            uncovered_cpu = (
                sum(
                    value
                    for pair, value in root_cpu.items()
                    if pair not in hook["covered"]
                )
                if root_cpu
                else 0.0
                if roots
                and all((r["pid"], r["created"]) in hook["covered"] for r in roots)
                else cpu.get(tool)
            )
            if state != "busy" and session.get("state") == "busy":
                state, source = "busy", "session"
                host, vscode_state = session["host"], session["vscode_state"]
                session_age = session["age_s"]
            elif (
                state != "busy"
                and uncovered_cpu is not None
                and uncovered_cpu >= cfg.busy_cpu_percent
            ):
                state, source, cpu_pct = "busy", "cpu", round(uncovered_cpu, 1)
            elif state is None and tool not in uncertain_tools:
                if session.get("state") == "idle":
                    state, source = "idle", "session"
                    host, vscode_state = session["host"], session["vscode_state"]
                    session_age = session["age_s"]
                elif (
                    tool in cpu
                    and not session.get("errors")
                    and sample.get("cpu_complete", True)
                ):
                    state, source, cpu_pct = "idle", "cpu", round(cpu[tool], 1)
            if source == "cpu":
                active_roots = [
                    r
                    for r in roots
                    if (r["pid"], r["created"]) not in hook["covered"]
                    and (
                        state == "idle" or root_cpu.get((r["pid"], r["created"]), 0) > 0
                    )
                ]
                host = common_host(active_roots)
                if any(r["host"] == "vscode" for r in active_roots):
                    vscode_state = (
                        "busy"
                        if sum(
                            root_cpu.get((r["pid"], r["created"]), 0)
                            for r in active_roots
                            if r["host"] == "vscode"
                        )
                        >= cfg.busy_cpu_percent
                        else "idle"
                    )
            # Fresh hooks resolve activity even when process observation fails.
            if source != "hook" and (
                process_unavailable or sample.get("errors") or sample.get("limited")
            ):
                uncertain_tools.add(tool)
            # A discovery cap is informational when an independent, complete
            # layer resolved this tool. Unresolved session evidence still defers idle.
            if session.get("limited") and not (
                source == "hook"
                or (
                    source == "cpu"
                    and sample.get("complete", False)
                    and sample.get("cpu_complete", False)
                )
            ):
                uncertain_tools.add(tool)
            tools.append(
                {
                    "tool": tool,
                    "state": state,
                    "present": bool(sample.get("present")),
                    "age_s": None if age is None else round(age, 1),
                    "ai": True,
                    "observed": source == "cpu",
                    "cpu": cpu_pct,
                    "source": source,
                    "host": host,
                    "vscode_state": vscode_state,
                    **(
                        {"session_age_s": round(session_age, 1)}
                        if session_age is not None
                        else {}
                    ),
                }
            )
        if "vscode" in cfg.tools:
            candidates = [t for t in tools if t["vscode_state"] in ("busy", "waiting")]
            candidates.sort(
                key=lambda t: (
                    ("busy", "waiting").index(t["vscode_state"]),
                    ("hook", "session", "cpu", "presence").index(t["source"]),
                )
            )
            present = bool(snapshot.get("vscode", {}).get("present"))
            winner = candidates[0] if candidates else None
            tools.append(
                {
                    "tool": "vscode",
                    "state": winner["vscode_state"]
                    if winner
                    else "idle"
                    if present
                    else None,
                    "present": present,
                    "ai": False,
                    "age_s": None,
                    "observed": False,
                    "cpu": None,
                    "source": winner["source"] if winner else "presence",
                    "host": "vscode",
                }
            )
        # A finite diagnostic vocabulary; never paths, exception text or process IDs.
        errors = [dict(x) for x in dict.fromkeys(tuple(e.items()) for e in errors)][:32]
        for error in errors:
            log.warning(
                "activity probe: %s/%s/%s", error["tool"], error["layer"], error["code"]
            )
        with self._lock:
            if self._stop_event.is_set():
                return MonitorStatus(state="stopped")
            headline, busy_tools = aggregate(tools)
            unresolved_hooks = any(h["unresolved"] for h in hooks.values())
            if headline == "idle" and unresolved_hooks:
                headline = (
                    "present_only"
                    if any(t["present"] and t["ai"] for t in tools)
                    else "none"
                )
            is_busy = headline == "busy"
            self._samples.append((now, is_busy))

            active = [t["tool"] for t in tools if t["state"] in _ACTIVE and t["ai"]]
            waiting_tools = [
                t["tool"] for t in tools if t["state"] == "waiting" and t["ai"]
            ]

            # Emit only when the active class changes (busy / waiting / off), so the log
            # isn't noisy AND a busy→waiting transition surfaces the actionable
            # "needs input" signal instead of a misleading "idle" (Codex 外门).
            cls = "busy" if is_busy else "waiting" if headline == "waiting" else "off"
            uncertain_active = unresolved_hooks or bool(
                self._active_tools & uncertain_tools
            )
            if cls != "off":
                self._idle_pending = False
            elif self._prev_class in _ACTIVE and uncertain_active:
                self._idle_pending = True
            if (
                self._prev_class is not None
                and (cls != self._prev_class or self._idle_pending)
                and cls != self._announced_class
                and not (cls == "off" and uncertain_active)
            ):
                if self._stop_event.is_set():
                    return MonitorStatus(state="stopped")
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
                self._announced_class = cls
                self._idle_pending = False
            # Observations advance even when an idle notification must wait for recovery.
            self._prev_class = cls
            if cls != "off":
                self._active_tools = set(busy_tools if is_busy else waiting_tools) | (
                    self._active_tools & uncertain_tools
                )
            elif not self._idle_pending:
                self._active_tools.clear()

            detail = (
                f"running AI: {', '.join(busy_tools)}"
                if busy_tools
                else {
                    "waiting": f"AI waiting: {', '.join(active)}",
                    "idle": "AI idle",
                    "present_only": "AI present (no activity reported)",
                    "none": "AI activity unknown"
                    if unresolved_hooks
                    else "no AI activity",
                }[headline]
            )
            return MonitorStatus(
                state="degraded" if errors else _STATE_MAP[headline],
                detail=detail,
                metrics={
                    "ai_state": headline,
                    "busy_tools": busy_tools,
                    "tools": tools,
                    "window_s": int(cfg.window_seconds),
                    "duty": self._duty(cfg, now),
                    "probe_errors": errors,
                    "probe_limited": limited,
                },
            )

    @staticmethod
    def _diagnostic_tool(tool: str) -> str:
        return tool if tool in {"claude", "codex", "kimi", "vscode"} else "custom"


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
