"""`custom_cmd` monitor — run a command, exit code = state (V2 parity, §4.2).

exit 0 → ok (idle/complete); non-zero → error (busy/incomplete). Uses
shlex.split + shell=False (constitution §2 — no shell injection).
"""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Optional

from pydantic import Field

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)

_IS_WINDOWS = os.name == "nt"
# Don't pop a console window for each poll on Windows (V2 used CREATE_NO_WINDOW).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def split_command(command: str) -> list[str]:
    """Tokenize a command string for shell=False execution.

    POSIX everywhere except Windows: there, POSIX shlex treats backslashes as
    escapes, so `C:\\Tools\\x.bat` collapses to `C:Toolsx.bat` (Codex #20 r6).
    Non-POSIX mode keeps backslashes but RETAINS the quote characters in each
    token (Codex #20 r7), so `"C:\\Program Files\\x.exe"` would reach CreateProcess
    with literal quotes and fail to launch. Strip the balanced surrounding quotes
    each token so quoted paths / args with spaces work.
    """
    if not _IS_WINDOWS:
        return shlex.split(command, posix=True)
    out: list[str] = []
    for tok in shlex.split(command, posix=False):
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
            tok = tok[1:-1]
        out.append(tok)
    return out


class CustomCmdConfig(BaseMonitorConfig):
    command: str = Field(
        ...,
        min_length=1,
        description="Command to run each cycle; "
        "exit 0 = ok/idle, non-zero = busy/failed.",
    )


class CustomCmdInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: CustomCmdConfig) -> None:
        super().__init__(instance_id, config)
        self._prev_ok: Optional[bool] = None

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: CustomCmdConfig = self.config  # type: ignore[assignment]
        argv = split_command(cfg.command)
        if not argv:
            return MonitorStatus(state="error", detail="empty command")
        try:
            result = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=cfg.timeout,
                creationflags=_CREATE_NO_WINDOW,
            )
            ok = result.returncode == 0
            detail = f"exit {result.returncode}"
        except subprocess.TimeoutExpired:
            ok, detail = False, f"timed out after {cfg.timeout}s"
        except Exception as e:
            ok, detail = False, f"run error: {e}"

        if not ok and self._prev_ok in (None, True):
            emit("alert", f"{cfg.name} failed", detail)
        elif ok and self._prev_ok is False:
            emit("done", f"{cfg.name} ok", detail)
        self._prev_ok = ok
        return MonitorStatus(state="ok" if ok else "error", detail=detail)


class CustomCmdPlugin(MonitorPlugin):
    type_id = "custom_cmd"
    display_name = "Custom command"
    category = "both"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return CustomCmdConfig

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return CustomCmdInstance(instance_id, config)  # type: ignore[arg-type]
