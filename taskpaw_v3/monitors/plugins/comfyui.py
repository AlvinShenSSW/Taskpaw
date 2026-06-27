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


def queue_depth(host: str, port: int, timeout: float) -> Optional[int]:
    """running + pending count, or None if the API is unreachable / not JSON."""
    url = f"http://{host}:{port}/queue"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        return len(data.get("queue_running", [])) + len(data.get("queue_pending", []))
    except Exception:
        return None


class ComfyUIInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: ComfyUIConfig) -> None:
        super().__init__(instance_id, config)
        self._was_busy = False
        self._idle_count = 0

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: ComfyUIConfig = self.config  # type: ignore[assignment]
        depth = queue_depth(cfg.host, cfg.port, min(cfg.timeout, 10.0))
        if depth is None:
            return MonitorStatus(state="error", detail="ComfyUI unreachable")

        if depth > 0:
            self._was_busy = True
            self._idle_count = 0
            return MonitorStatus(state="running", detail=f"{depth} queued", metrics={"queue": depth})

        # depth == 0
        if self._was_busy:
            self._idle_count += 1
            if self._idle_count >= cfg.idle_confirm:
                emit("done", f"{cfg.name}: queue empty", "all ComfyUI tasks complete")
                self._was_busy = False
                self._idle_count = 0
        return MonitorStatus(state="ok", detail="idle", metrics={"queue": 0})


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
