"""`state_file` monitor — read an agent/app state from a JSON file (V3 §5c).

General-purpose: an external producer atomically writes a small JSON file with a
state field (e.g. busy|idle|waiting) and a timestamp; this plugin reads it, maps
the value to a V3 state, emits on transitions, and runs two watchdogs:

- busy-too-long: state stays "busy" past `busy_alert_seconds` → alert (a task
  that may be stuck / waiting unnoticed).
- staleness: the file's timestamp (ts field, else mtime) is older than
  `stale_seconds` → degraded (the producer stopped updating / crashed).

The headline use is the dev-agent activity monitor (#22): a hook/notify wrapper
writes `~/.taskpaw/agent-activity.json` and this surfaces whether Claude Code /
Codex in VSCode is busy, idle, or waiting. It reads ONLY state + timestamp —
never prompts, code, or session content.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import Field

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)


class StateFileConfig(BaseMonitorConfig):
    path: str = Field(..., min_length=1)
    state_field: str = "state"
    ts_field: str = "ts"                    # epoch seconds or ISO8601; else mtime
    busy_states: list[str] = ["busy", "running"]
    waiting_states: list[str] = ["waiting", "blocked"]
    idle_states: list[str] = ["idle", "done", "ok"]
    busy_alert_seconds: float = Field(0.0, ge=0)   # 0 = no busy-too-long watchdog
    stale_seconds: float = Field(0.0, ge=0)        # 0 = no staleness watchdog
    missing_is_idle: bool = True            # no file yet = idle (agent not started)


def _classify(value: str, cfg: StateFileConfig) -> str:
    """Map a raw state value to one of busy|waiting|idle|unknown."""
    v = value.strip().lower()
    if v in {s.lower() for s in cfg.busy_states}:
        return "busy"
    if v in {s.lower() for s in cfg.waiting_states}:
        return "waiting"
    if v in {s.lower() for s in cfg.idle_states}:
        return "idle"
    return "unknown"


def _parse_ts(raw, fallback_mtime: float) -> float:
    """Return an epoch-seconds timestamp from the ts field, else the mtime."""
    if raw is None:
        return fallback_mtime
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return fallback_mtime


_STATE_MAP = {"busy": "running", "waiting": "idle", "idle": "idle", "unknown": "unknown"}


class StateFileInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: StateFileConfig) -> None:
        super().__init__(instance_id, config)
        self._prev: Optional[str] = None   # busy|waiting|idle|unknown|missing
        self._busy_alerted = False
        self._stale_alerted = False

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: StateFileConfig = self.config  # type: ignore[assignment]
        p = Path(cfg.path).expanduser()
        now = time.time()

        if not p.exists():
            self._busy_alerted = self._stale_alerted = False
            if cfg.missing_is_idle:
                self._maybe_emit_transition(emit, "missing", cfg)
                return MonitorStatus(state="idle", detail="no activity file yet")
            return MonitorStatus(state="unknown", detail=f"missing: {cfg.path}")

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state file must be a JSON object")
        except Exception as e:
            return MonitorStatus(state="error", detail=f"unreadable state file: {e}")

        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = now
        kind = _classify(str(data.get(cfg.state_field, "")), cfg)
        ts = _parse_ts(data.get(cfg.ts_field), mtime)
        age = max(0.0, now - ts)
        tool = str(data.get("tool", "")) or "agent"

        self._maybe_emit_transition(emit, kind, cfg, tool=tool)

        detail = f"{tool}: {kind}"
        metrics = {"activity": kind, "age_s": int(age)}
        state = _STATE_MAP.get(kind, "unknown")

        # Watchdog: file no longer being updated → producer likely stopped.
        if cfg.stale_seconds and age > cfg.stale_seconds:
            if not self._stale_alerted:
                emit("alert", f"{cfg.name}: activity stale",
                     f"{tool} state file not updated for {int(age)}s")
                self._stale_alerted = True
            return MonitorStatus(state="degraded", detail=f"stale {int(age)}s", metrics=metrics)
        self._stale_alerted = False

        # Watchdog: busy for too long → possibly stuck / waiting unnoticed.
        if kind == "busy" and cfg.busy_alert_seconds and age > cfg.busy_alert_seconds:
            if not self._busy_alerted:
                emit("alert", f"{cfg.name}: busy too long",
                     f"{tool} has been busy for {int(age)}s")
                self._busy_alerted = True
        elif kind != "busy":
            self._busy_alerted = False

        return MonitorStatus(state=state, detail=detail, metrics=metrics)

    def _maybe_emit_transition(self, emit: EventEmitter, kind: str,
                               cfg: StateFileConfig, tool: str = "agent") -> None:
        if kind == self._prev:
            return
        prev = self._prev
        self._prev = kind
        if prev is None:
            return  # first observation — establish baseline, don't notify
        if kind == "busy":
            emit("info", f"{cfg.name}: started", f"{tool} is now busy")
        elif kind == "waiting":
            emit("info", f"{cfg.name}: waiting", f"{tool} is waiting for input")
        elif kind == "idle" and prev in ("busy", "waiting"):
            emit("done", f"{cfg.name}: idle", f"{tool} finished / is idle")


class StateFilePlugin(MonitorPlugin):
    type_id = "state_file"
    display_name = "State file (agent activity)"
    category = "task"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return StateFileConfig

    @classmethod
    def ui_schema(cls) -> dict:
        return {"path": {"widget": "path"}}

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return StateFileInstance(instance_id, config)  # type: ignore[arg-type]
