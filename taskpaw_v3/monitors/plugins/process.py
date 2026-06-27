"""`process` monitor — is a process alive? (V3 design §4.2, §5.1 ①②)

Matches a regex against each process's name and (optionally) its full command
line via psutil. Covers both "plain process by name" and pm2 cases — e.g. the
PM2 God Daemon (`PM2.*God`) or the orchestrator (`strategy_orchestrator\\.py`) —
without ever running `pm2` (which would spawn the daemon; see #13).
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import field_validator

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


class ProcessConfig(BaseMonitorConfig):
    pattern: str                      # regex matched against name / cmdline
    search_cmdline: bool = True       # also match the full command line
    category_label: str = "service"

    @field_validator("pattern")
    @classmethod
    def _compilable(cls, v: str) -> str:
        # Fail fast at config time, not as a runtime check loop → spurious DEGRADED.
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"invalid regex pattern: {e}") from e
        return v


def _scan(rx: "re.Pattern[str]", search_cmdline: bool) -> bool:
    """True if any running process matches the precompiled regex."""
    if psutil is None:
        raise RuntimeError("psutil not available")
    fields = ["name", "cmdline"] if search_cmdline else ["name"]
    for proc in psutil.process_iter(fields):
        try:
            info = proc.info
            name = info.get("name") or ""
            if rx.search(name):
                return True
            if search_cmdline:
                cmd = " ".join(info.get("cmdline") or [])
                if cmd and rx.search(cmd):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Process vanished or is inaccessible mid-iteration — skip, don't let
            # a transient race degrade a healthy monitor.
            continue
    return False


def process_matches(pattern: str, search_cmdline: bool) -> bool:
    """Convenience: compile + scan (used standalone / in tests)."""
    return _scan(re.compile(pattern, re.IGNORECASE), search_cmdline)


class ProcessInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: ProcessConfig) -> None:
        super().__init__(instance_id, config)
        self._prev_alive: Optional[bool] = None
        # Precompile once (design §4.4) — validated compilable at config time.
        self._rx = re.compile(config.pattern, re.IGNORECASE)

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: ProcessConfig = self.config  # type: ignore[assignment]
        alive = _scan(self._rx, cfg.search_cmdline)
        # Alert on a down state at startup (prev is None) too, not only on a
        # healthy→down transition.
        if not alive and self._prev_alive in (None, True):
            emit("alert", f"{cfg.name} down", f"no process matching /{cfg.pattern}/",
                 dedupe_key=None)
        elif alive and self._prev_alive is False:
            emit("done", f"{cfg.name} recovered", f"process matching /{cfg.pattern}/ is back",
                 dedupe_key=None)
        self._prev_alive = alive
        return MonitorStatus(
            state="ok" if alive else "error",
            detail="running" if alive else "not found",
            metrics={"alive": alive},
        )


class ProcessPlugin(MonitorPlugin):
    type_id = "process"
    display_name = "Process alive"
    category = "service"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return ProcessConfig

    @classmethod
    def ui_schema(cls) -> dict:
        return {"pattern": {"widget": "regex", "help": "regex matched against process name/cmdline"}}

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return ProcessInstance(instance_id, config)  # type: ignore[arg-type]
