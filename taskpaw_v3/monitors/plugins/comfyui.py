"""`comfyui` monitor — ComfyUI queue empty = complete (V2 parity, §4.2).

Polls ComfyUI's /queue API. When the queue transitions from busy → empty and
stays empty for `idle_confirm` consecutive checks, it's considered complete (the
confirm count avoids a false "done" in the gap between two queued prompts — V2
double-fire fix).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from pydantic import Field

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)


class ComfyUIConfig(BaseMonitorConfig):
    host: str = "127.0.0.1"
    port: int = Field(8188, ge=1, le=65535)
    idle_confirm: int = Field(2, ge=1)
    stall_confirm: int = Field(3, ge=1)  # running==0 & pending>0 held this long → stalled


def queue_counts(host: str, port: int, timeout: float) -> Optional[tuple[int, int]]:
    """(running, pending) counts, or None if the API is unreachable / not JSON."""
    url = f"http://{host}:{port}/queue"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        return len(data.get("queue_running", [])), len(data.get("queue_pending", []))
    except Exception:
        return None


class ComfyUIInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: ComfyUIConfig) -> None:
        super().__init__(instance_id, config)
        self._was_busy = False
        self._idle_count = 0
        self._stall_count = 0
        self._stalled = False

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: ComfyUIConfig = self.config  # type: ignore[assignment]
        counts = queue_counts(cfg.host, cfg.port, min(cfg.timeout, 10.0))
        if counts is None:
            return MonitorStatus(state="error", detail="ComfyUI unreachable")
        running, pending = counts
        depth = running + pending

        # Stalled shape: nothing running but prompts still queued (a prompt error
        # leaves pending behind). V2 flagged this as a halted queue (Codex #20).
        if running == 0 and pending > 0:
            self._was_busy = True
            self._idle_count = 0
            self._stall_count += 1
            if self._stall_count >= cfg.stall_confirm:
                if not self._stalled:
                    emit("alert", f"{cfg.name}: queue stalled",
                         f"{pending} pending but nothing running",
                         dedupe_key=f"{self.instance_id}:stalled")
                    self._stalled = True
                return MonitorStatus(state="error", detail=f"stalled: {pending} pending",
                                     metrics={"running": 0, "pending": pending})
            return MonitorStatus(state="running", detail=f"{pending} pending",
                                 metrics={"running": 0, "pending": pending})

        self._stall_count = 0
        self._stalled = False

        if depth > 0:
            self._was_busy = True
            self._idle_count = 0
            return MonitorStatus(state="running", detail=f"{depth} queued",
                                 metrics={"running": running, "pending": pending})

        # depth == 0
        if self._was_busy:
            self._idle_count += 1
            if self._idle_count >= cfg.idle_confirm:
                emit("done", f"{cfg.name}: queue empty", "all ComfyUI tasks complete")
                self._was_busy = False
                self._idle_count = 0
        return MonitorStatus(state="ok", detail="idle", metrics={"running": 0, "pending": 0})


class ComfyUIPlugin(MonitorPlugin):
    type_id = "comfyui"
    display_name = "ComfyUI queue"
    category = "task"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return ComfyUIConfig

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return ComfyUIInstance(instance_id, config)  # type: ignore[arg-type]
