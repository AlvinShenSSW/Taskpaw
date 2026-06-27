"""Monitor plugin abstractions (V3 design §4.1).

A monitor is a self-describing plugin: it declares its config schema, lifecycle,
and run logic, and is registered into a registry. Adding a monitor = write a
class + register it — no UI or core data-structure changes.

Contracts:
- `MonitorPlugin` (class-level): identity + config schema four-piece + `create()`.
- `MonitorInstance` (per running monitor): `start(emit)/stop/snapshot/reconfigure`.
- `EventEmitter`: the single output an instance has (dedupe/persist/throttle are
  the supervisor's + event queue's job).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["task", "service"]
State = Literal["unknown", "ok", "idle", "running", "degraded", "error", "stopped"]


class EventEmitter(Protocol):
    """An instance's only output. `dedupe_key` lets the supervisor/queue collapse
    repeats; level is info|warn|alert|done."""

    def __call__(
        self,
        level: str,
        title: str,
        message: str,
        data: Optional[dict] = None,
        dedupe_key: Optional[str] = None,
    ) -> None: ...


class BaseMonitorConfig(BaseModel):
    """Common config every monitor carries (design §4.4 resource caps)."""

    # Reject unknown/typo keys instead of silently dropping them.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    poll_interval: float = Field(10.0, ge=1.0)      # seconds, min 1s
    timeout: float = Field(30.0, gt=0)              # command/HTTP probe timeout
    max_events_per_minute: int = Field(60, ge=1)    # storm → folded summary
    max_line_bytes: int = Field(1_000_000, ge=1024)  # tail line cap


@dataclass
class MonitorStatus:
    """Runtime health snapshot (NOT health(cfg) — actual live state)."""

    state: State = "unknown"
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    last_update: Optional[str] = None


class MonitorInstance(abc.ABC):
    """A running monitor. The supervisor owns its thread + lifecycle."""

    def __init__(self, instance_id: str, config: BaseMonitorConfig) -> None:
        self.instance_id = instance_id
        self.config = config
        self._status = MonitorStatus()

    @abc.abstractmethod
    def check(self, emit: EventEmitter) -> MonitorStatus:
        """One observation cycle. Return the current status; emit events for
        state transitions. Called by the supervisor every `poll_interval`.
        Must not loop or sleep — the supervisor schedules cadence."""

    def snapshot(self) -> MonitorStatus:
        return self._status

    def stop(self, timeout: float = 5.0) -> None:
        """Release any owned resources (subprocess/socket/file watcher/tail).
        Default no-op; override when the instance owns something. Called by the
        supervisor on both shutdown AND reconfigure."""

    def reconfigure(self, config: BaseMonitorConfig) -> None:
        """Default: caller (supervisor) stop→recreate→start. Override to hot-apply."""
        self.config = config


class MonitorPlugin(abc.ABC):
    """Class-level plugin descriptor + factory."""

    type_id: str = ""
    display_name: str = ""
    category: Category = "service"
    config_version: int = 1

    @classmethod
    @abc.abstractmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        """Pydantic model — the server-side authority for validation."""

    @classmethod
    def json_schema(cls) -> dict:
        """Form schema for the UI (derived from the pydantic model)."""
        return cls.config_model().model_json_schema()

    @classmethod
    def ui_schema(cls) -> dict:
        """Widget hints (design §4.3). Default: none."""
        return {}

    @abc.abstractmethod
    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        """Build a runnable instance from validated config."""

    @classmethod
    def validate_config(cls, raw: dict) -> BaseMonitorConfig:
        """Validate a raw config dict against this plugin's model."""
        return cls.config_model()(**raw)
