"""`comfyui` monitor — ComfyUI queue state + error diagnostics (V2 parity, §4.2).

Polls ComfyUI's /queue API and reports:
- done: queue busy → empty held for `idle_confirm` checks (the confirm count
  avoids a false "done" in the gap between two queued prompts — V2 double-fire fix).
- stalled (alert): nothing running but prompts still pending for `stall_confirm`
  checks (a prompt error leaves pending behind).
- stuck (alert): the SAME running prompt id persists for `stuck_checks` polls —
  a hung GPU job (opt-in; `stuck_checks=0` disables it).

On a stall/stuck, it DIAGNOSES the cause the way V2 did (#60): the ComfyUI
/history API (the errored prompt's `exception_message`) and the tail of an
optional `comfyui_log_path` (CUDA OOM / RuntimeError / Traceback …), and folds
that into the alert + status so the operator sees WHY, not just "stalled".
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.request
from pathlib import Path
from typing import Optional

# urlopen failures: URLError/HTTPError/timeout (OSError), a bad URL (ValueError),
# and InvalidURL for a host with whitespace/control chars (HTTPException, NOT an
# OSError) — all must become a clean "unreachable", never a raised check error.
_NET_ERRORS = (OSError, ValueError, http.client.HTTPException)

from pydantic import Field

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)

# V2 log-scan error signatures (taskpaw.py:1285).
LOG_ERROR_PATTERNS = re.compile(
    r"(CUDA out of memory|RuntimeError|torch\.cuda\.OutOfMemoryError"
    r"|CUDA error|Traceback \(most recent|MemoryError"
    r"|allocation on device|out of memory)",
    re.IGNORECASE,
)
_LOG_TAIL_LINES = 50


class ComfyUIConfig(BaseMonitorConfig):
    host: str = Field("127.0.0.1", description="ComfyUI host/IP (the machine running ComfyUI).")
    port: int = Field(8188, ge=1, le=65535, description="ComfyUI port.")
    idle_confirm: int = Field(2, ge=1, description="Notify 'done' after the queue is "
                              "empty this many checks in a row (debounces the gap between prompts).")
    stall_confirm: int = Field(3, ge=1, description="Alert when nothing is running but "
                               "prompts stay pending this many checks (a prompt error left them stuck).")
    stuck_checks: int = Field(0, ge=0, description="Alert when the SAME prompt keeps "
                              "running this many checks without finishing (0 = off).")
    comfyui_log_path: str = Field("", description="Optional ComfyUI log file to tail "
                                  "for the actual error (CUDA OOM / RuntimeError / Traceback) on a stall/stuck.")


def _cap(s: str, n: int = 80) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def queue_snapshot(host: str, port: int, timeout: float) -> Optional[tuple[list[str], int]]:
    """(running_prompt_ids, pending_count), or None if unreachable / not JSON.

    ComfyUI /queue entries are `[number, prompt_id, prompt, extra, outputs]`; we
    extract prompt_id (index 1) so a hung prompt can be detected by its id not
    changing across polls (Codex #20 r6)."""
    url = f"http://{host}:{port}/queue"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except _NET_ERRORS:
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
    except (AttributeError, TypeError):
        return None


# ── error diagnostics (V2 taskpaw.py:1483-1579) ───────────────────────────--
def extract_history_error(entry) -> Optional[str]:
    """The error message from one ComfyUI /history entry, else None (V2:1515).
    An entry errors when it's not completed OR its status_str mentions error; the
    detail comes from an `execution_error` message's `exception_message`."""
    if not entry or not isinstance(entry, dict):
        return None
    status = entry.get("status", {}) or {}
    completed = status.get("completed", True)
    status_str = str(status.get("status_str", "") or "")
    if completed and "error" not in status_str.lower():
        return None
    for msg in status.get("messages", []) or []:
        if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "execution_error":
            detail = msg[1]
            if isinstance(detail, dict) and detail.get("exception_message"):
                return _cap(str(detail["exception_message"]))
    return status_str or "Unknown error"


def _get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_history_error(host: str, port: int, prompt_id: str, timeout: float) -> Optional[str]:
    try:
        data = _get_json(f"http://{host}:{port}/history/{prompt_id}", timeout)
    except _NET_ERRORS:
        return None
    return extract_history_error(data.get(prompt_id)) if isinstance(data, dict) else None


def check_recent_history_errors(host: str, port: int, timeout: float) -> Optional[str]:
    try:
        data = _get_json(f"http://{host}:{port}/history?max_items=5", timeout)
    except _NET_ERRORS:
        return None
    if not isinstance(data, dict):
        return None
    for entry in data.values():
        err = extract_history_error(entry)
        if err:
            return err
    return None


def tail_log_for_errors(log_path: str, last_position: int) -> tuple[Optional[str], int]:
    """Scan the tail of the ComfyUI log for an error signature. Returns
    (error_or_None, new_position); only reports when the file GREW past
    last_position so the same old error isn't re-alerted every poll (V2:1538)."""
    if not log_path:
        return None, last_position
    try:
        path = Path(log_path)
        if not path.is_file():
            return None, last_position
        size = path.stat().st_size
        if size < last_position:
            # Log rotated/truncated since we last read — re-scan from the new
            # start, else new errors in the smaller file are ignored until it
            # grows past the old offset (Codex #60).
            last_position = 0
        if size <= last_position:
            return None, last_position           # no new content
        # Scan ONLY the bytes written since last_position so an already-consumed
        # error still in the tail window isn't re-reported as a new episode's
        # cause (Codex #60). Bound the read to the last 8KB of new content.
        start = max(last_position, size - 8192)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            if start > last_position:
                f.readline()                     # capped past the offset → drop partial line
            new_lines = f.read().splitlines()
        for line in reversed(new_lines[-_LOG_TAIL_LINES:]):
            if LOG_ERROR_PATTERNS.search(line):
                return _cap(line), size
        return None, size
    except OSError:
        return None, last_position


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
        self._last_log_position = 0
        self._diag_error: Optional[str] = None        # diagnosed cause for the current episode
        self._recent_log_error: Optional[str] = None  # latest log error seen this run

    def start(self, emit: EventEmitter) -> None:
        # Prime the log offset to the file's current end so diagnostics only ever
        # consider errors written DURING this monitoring session — not a stale
        # historical OOM/Traceback already in the file when monitoring began
        # (Codex #60; V2 consumed the log incrementally while running).
        cfg: ComfyUIConfig = self.config  # type: ignore[assignment]
        if cfg.comfyui_log_path:
            try:
                self._last_log_position = Path(cfg.comfyui_log_path).stat().st_size
            except OSError:
                self._last_log_position = 0

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: ComfyUIConfig = self.config  # type: ignore[assignment]
        # Consume the log EVERY poll so its offset advances regardless of whether
        # we use it for diagnosis — else an error written during a completed
        # prompt stays unread and gets misattributed to a later unrelated stall
        # (Codex #60; V2 consumed the log each poll). Remember the latest error of
        # the current run; it's cleared when the queue goes idle (run boundary).
        if cfg.comfyui_log_path:
            log_err, self._last_log_position = tail_log_for_errors(
                cfg.comfyui_log_path, self._last_log_position)
            if log_err:
                self._recent_log_error = log_err
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
                    # Diagnose WHY (history + log tail) and fold it into the one
                    # alert per episode. No dedupe_key: the supervisor's seen-set
                    # persists across episodes and would permanently suppress a
                    # SECOND stall after recovery; the _stalled flag (reset on
                    # recovery) prevents duplicates within one episode (Codex r9).
                    self._diag_error = self._diagnose()
                    extra = f" — {self._diag_error}" if self._diag_error else ""
                    emit("alert", f"{cfg.name}: queue stalled",
                         f"{pending} pending but nothing running{extra}")
                    self._stalled = True
                return self._err_status("stalled", pending, 0, pending)
            return MonitorStatus(state="running", detail=f"{pending} pending",
                                 metrics={"running": 0, "pending": pending})

        self._stall_count = 0
        if self._stalled:                 # recovered from a stall episode
            self._stalled = False
            self._diag_error = None

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
                self._diag_error = None
            if cfg.stuck_checks and self._running_count >= cfg.stuck_checks:
                if not self._stuck:
                    # Diagnose the stuck prompt by id (then recent history / log).
                    self._diag_error = self._diagnose(running_ids[0] if running_ids else None)
                    extra = f" — {self._diag_error}" if self._diag_error else ""
                    emit("alert", f"{cfg.name}: prompt stuck",
                         f"prompt running for {self._running_count} polls without finishing{extra}")
                    self._stuck = True
                return self._err_status("stuck", self._running_count, running, pending)
            return MonitorStatus(state="running", detail=f"{depth} queued",
                                 metrics={"running": running, "pending": pending})

        # depth == 0 (nothing running, nothing pending)
        self._reset_running()
        self._recent_log_error = None     # run finished → drop its log error
        if self._was_busy:
            self._idle_count += 1
            if self._idle_count >= cfg.idle_confirm:
                emit("done", f"{cfg.name}: queue empty", "all ComfyUI tasks complete")
                self._was_busy = False
                self._idle_count = 0
        return MonitorStatus(state="ok", detail="idle", metrics={"running": 0, "pending": 0})

    def _err_status(self, kind: str, count: int, running: int, pending: int) -> MonitorStatus:
        base = f"{kind}: {count} {'pending' if kind == 'stalled' else 'polls'}"
        detail = f"{base} ({self._diag_error})" if self._diag_error else base
        return MonitorStatus(state="error", detail=detail,
                             metrics={"running": running, "pending": pending})

    def _diagnose(self, prompt_id: Optional[str] = None) -> Optional[str]:
        """Find the underlying error, matching V2's two cases:
        - STUCK (a specific running prompt): only THAT prompt's /history error —
          NOT a recent-history scan, else an unrelated recently-errored prompt
          would be blamed on a prompt that's merely still running (V2:1412).
        - STALLED (nothing running, pending): scan recent /history (V2:1353).
        Both fall back to the current run's log error (consumed each poll)."""
        cfg: ComfyUIConfig = self.config  # type: ignore[assignment]
        to = min(cfg.timeout, 10.0)
        if prompt_id:
            err = check_history_error(cfg.host, cfg.port, prompt_id, to)
        else:
            err = check_recent_history_errors(cfg.host, cfg.port, to)
        if err is None:
            err = self._recent_log_error
        return err

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

    @classmethod
    def ui_schema(cls) -> dict:
        # Help text comes from the field descriptions; flag the log path for the
        # file picker widget (#71).
        return {
            "ui:order": ["name", "host", "port", "idle_confirm", "stall_confirm",
                         "stuck_checks", "comfyui_log_path", "poll_interval", "timeout", "*"],
            "comfyui_log_path": {"ui:options": {"taskpawPath": "file"}},
        }

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return ComfyUIInstance(instance_id, config)  # type: ignore[arg-type]
