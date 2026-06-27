"""`folder` monitor — file stable = complete (V2 parity, §4.2).

Watches a directory; when a file's size stays unchanged for `stable_seconds` it's
considered done and a completion event fires (once per file). Zero-byte files are
skipped (V2 audit finding — failed/placeholder downloads shouldn't fire). Network
folder stat errors are skipped per-file, not fatal.
"""

from __future__ import annotations

import time
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


class FolderConfig(BaseMonitorConfig):
    path: str = Field(..., min_length=1)
    extensions: list[str] = []          # e.g. ["mp4","mkv"]; empty = all files
    stable_seconds: float = Field(30.0, ge=0)


class FolderInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: FolderConfig) -> None:
        super().__init__(instance_id, config)
        # name -> [size, first_seen_at_this_size, completed]
        self._files: dict[str, list] = {}

    def _matches(self, name: str) -> bool:
        exts = [e.lower().lstrip(".") for e in self.config.extensions]  # type: ignore[attr-defined]
        if not exts:
            return True
        return name.rsplit(".", 1)[-1].lower() in exts if "." in name else False

    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: FolderConfig = self.config  # type: ignore[assignment]
        base = Path(cfg.path).expanduser()
        now = time.monotonic()
        if not base.is_dir():
            return MonitorStatus(state="error", detail=f"not a directory: {cfg.path}")

        pending = 0
        try:
            entries = list(base.iterdir())
        except OSError as e:
            return MonitorStatus(state="error", detail=f"cannot read dir: {e}")

        # Purge tracked names no longer present (V2 parity): a deleted/moved file
        # that reappears with the same name must be treated as a NEW download,
        # not skipped as an already-completed record (Codex #20 finding).
        present = {p.name for p in entries if p.is_file()}
        for stale in [n for n in self._files if n not in present]:
            del self._files[stale]

        for p in entries:
            if not p.is_file() or not self._matches(p.name):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue  # vanished / network hiccup — skip, don't fail
            if size == 0:
                continue  # skip empty files (failed/placeholder)
            rec = self._files.get(p.name)
            if rec is None:
                self._files[p.name] = [size, now, False]
                pending += 1
            elif rec[2]:
                continue  # already completed
            elif rec[0] != size:
                rec[0], rec[1] = size, now  # size changed → reset stability clock
                pending += 1
            elif now - rec[1] >= cfg.stable_seconds:
                rec[2] = True  # stable long enough → complete
                emit("done", f"{cfg.name}: file complete", f"{p.name} ({size} bytes)")
            else:
                pending += 1

        return MonitorStatus(state="ok", detail=f"{pending} in progress",
                             metrics={"pending": pending, "tracked": len(self._files)})


class FolderPlugin(MonitorPlugin):
    type_id = "folder"
    display_name = "Folder (downloads)"
    category = "task"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return FolderConfig

    @classmethod
    def ui_schema(cls) -> dict:
        return {"path": {"widget": "path"}}

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return FolderInstance(instance_id, config)  # type: ignore[arg-type]
