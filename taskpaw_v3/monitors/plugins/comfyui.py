"""`comfyui` monitor — ComfyUI queue state (V2 parity, §4.2).

Polls ComfyUI's /queue API and reports:
- done: queue busy → empty held for `idle_confirm` checks (the confirm count
  avoids a false "done" in the gap between two queued prompts — V2 double-fire fix).
- stalled (alert): nothing running but prompts still pending for `stall_confirm`
  checks (a prompt error leaves pending behind).
- stuck (alert): the SAME running prompt id persists for `stuck_checks` polls —
  a hung GPU job (opt-in; `stuck_checks=0` disables it).
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
    stuck_checks: int = Field(0, ge=0)   # same prompt running this many checks → stuck; 0=off


def queue_snapshot(host: str, port: int, timeout: float) -> Optional[tuple[list[str], int]]:
    """(running_prompt_ids, pending_count), or None if unreachable / not JSON.

    ComfyUI /queue entries are `[number, prompt_id, prompt, extra, outputs]`; we
    extract prompt_id (index 1) so a hung prompt can be detected by its id not
    changing across polls (Codex #20 r6)."""
    url = f"http://{host}:{port}/queue"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        running = data.get("queue_running", [])
        pending = data.get("queue_pending", [])
        ids: list[str] = []
        for item in running:
            try:
                ids.append(str(item[1]))
            except (IndexError, TypeError, KeyError):
                ids.append("?")
        return ids, len(pending)
    except Exception:
        return None


class ComfyUIInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: ComfyUIConfig) -> None:
        super().__init__(instance_id, config)
        self._was_busy = False
        self._idle_count = 0
        self._stall_count = 0
        self._stalled = False
        self._running_key: Optional[str] = None
        self._running_count = 0
        self._stuck = False

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: ComfyUIConfig = self.config  # type: ignore[assignment]
        snap = queue_snapshot(cfg.host, cfg.port, min(cfg.timeout, 10.0))
        if snap is None:
            return MonitorStatus(state="error", detail="ComfyUI unreachable")
        running_ids, pending = snap
        running = len(running_ids)
        depth = running + pending

        # Stalled shape: nothing running but prompts still queued (a prompt error
        # leaves pending behind). V2 flagged this as a halted queue (Codex #20).
        if running == 0 and pending > 0:
            self._reset_running()
            self._was_busy = True
            self._idle_count = 0
            self._stall_count += 1
            if self._stall_count >= cfg.stall_confirm:
                if not self._stalled:
                    # No dedupe_key: the supervisor's seen-set persists across
                    # episodes, which would permanently suppress a SECOND stall
                    # after a recovery. The _stalled flag (reset on recovery)
                    # already prevents duplicates within one episode (Codex r9).
                    emit("alert", f"{cfg.name}: queue stalled",
                         f"{pending} pending but nothing running")
                    self._stalled = True
                return MonitorStatus(state="error", detail=f"stalled: {pending} pending",
                                     metrics={"running": 0, "pending": pending})
            return MonitorStatus(state="running", detail=f"{pending} pending",
                                 metrics={"running": 0, "pending": pending})

        self._stall_count = 0
        self._stalled = False

        if running > 0:
            self._was_busy = True
            self._idle_count = 0
            # Stuck detection: the SAME running prompt id(s) across many polls
            # means a hung GPU job — V2 alerted on a stuck prompt (Codex #20 r6).
            key = ",".join(sorted(running_ids))
            if key == self._running_key:
                self._running_count += 1
            else:
                self._running_key = key
                self._running_count = 1
                self._stuck = False
            if cfg.stuck_checks and self._running_count >= cfg.stuck_checks:
                if not self._stuck:
                    # No dedupe_key — same reason as the stall path: avoid
                    # permanently suppressing a later independent stuck prompt.
                    emit("alert", f"{cfg.name}: prompt stuck",
                         f"prompt running for {self._running_count} polls without finishing")
                    self._stuck = True
                return MonitorStatus(state="error", detail=f"stuck: {self._running_count} polls",
                                     metrics={"running": running, "pending": pending})
            return MonitorStatus(state="running", detail=f"{depth} queued",
                                 metrics={"running": running, "pending": pending})

        # depth == 0 (nothing running, nothing pending)
        self._reset_running()
        if self._was_busy:
            self._idle_count += 1
            if self._idle_count >= cfg.idle_confirm:
                emit("done", f"{cfg.name}: queue empty", "all ComfyUI tasks complete")
                self._was_busy = False
                self._idle_count = 0
        return MonitorStatus(state="ok", detail="idle", metrics={"running": 0, "pending": 0})

    def _reset_running(self) -> None:
        self._running_key = None
        self._running_count = 0
        self._stuck = False


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
