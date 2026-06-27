"""`heartbeat` monitor — is a heartbeat file fresh? (V3 design §4.2, §5.1 ④)

Status-AWARE, not bare-mtime (the #13 moomoo finding): the heartbeat JSON carries
a `status` (e.g. `hibernating`) and a `next_check_due_utc`. When hibernating the
due time can be days out and that is NOT stale. The HUNG condition is:
`now > next_check_due_utc + grace` AND status is not a hibernating state.
Falls back to file mtime + grace only when no due field is present.
"""

from __future__ import annotations

import json
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


class HeartbeatConfig(BaseMonitorConfig):
    path: str = Field(..., min_length=1)
    status_field: str = "status"
    due_field: str = "next_check_due_utc"
    grace_seconds: float = Field(300.0, ge=0)
    hibernating_states: list[str] = ["hibernating", "sleeping", "paused"]


def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate_heartbeat(cfg: HeartbeatConfig, now: Optional[datetime] = None) -> MonitorStatus:
    """Pure evaluation (testable): returns the heartbeat status."""
    now = now or datetime.now(timezone.utc)
    p = Path(cfg.path)
    if not p.exists():
        return MonitorStatus(state="error", detail=f"heartbeat file missing: {cfg.path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return MonitorStatus(state="error", detail=f"unreadable heartbeat: {e}")
    if not isinstance(data, dict):
        return MonitorStatus(state="error", detail="heartbeat JSON must be an object")

    status = str(data.get(cfg.status_field, "")).lower()
    if status in {s.lower() for s in cfg.hibernating_states}:
        return MonitorStatus(state="ok", detail=f"{status} (not stale)", metrics={"status": status})

    due_raw = data.get(cfg.due_field)
    if due_raw:
        try:
            due = _parse_dt(str(due_raw))
        except Exception as e:
            return MonitorStatus(state="error", detail=f"bad {cfg.due_field}: {e}")
        overdue = (now - due).total_seconds() - cfg.grace_seconds
        if overdue > 0:
            return MonitorStatus(state="error", detail=f"HUNG: {int(overdue)}s past due+grace",
                                 metrics={"status": status, "overdue_s": int(overdue)})
        return MonitorStatus(state="ok", detail="fresh", metrics={"status": status})

    # Fallback: file mtime + grace.
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    overdue = (now - mtime).total_seconds() - cfg.grace_seconds
    if overdue > 0:
        return MonitorStatus(state="error", detail=f"stale mtime: {int(overdue)}s past grace")
    return MonitorStatus(state="ok", detail="fresh (mtime)")


class HeartbeatInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: HeartbeatConfig) -> None:
        super().__init__(instance_id, config)
        self._prev_state: Optional[str] = None

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: HeartbeatConfig = self.config  # type: ignore[assignment]
        status = evaluate_heartbeat(cfg)
        # Alert on an unhealthy state at startup too, not only on a transition.
        if status.state == "error" and self._prev_state != "error":
            emit("alert", f"{cfg.name} unhealthy", status.detail)
        elif status.state == "ok" and self._prev_state == "error":
            emit("done", f"{cfg.name} healthy", status.detail)
        self._prev_state = status.state
        return status


class HeartbeatPlugin(MonitorPlugin):
    type_id = "heartbeat"
    display_name = "Heartbeat freshness"
    category = "service"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return HeartbeatConfig

    @classmethod
    def ui_schema(cls) -> dict:
        return {"path": {"widget": "path"}}

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return HeartbeatInstance(instance_id, config)  # type: ignore[arg-type]
