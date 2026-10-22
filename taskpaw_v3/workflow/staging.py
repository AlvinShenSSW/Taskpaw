"""P2-2 (#293): a worker's local staging directory and its space rules (§4).

Staging must be a local disk: UNC, mapped network drives and relative paths
are refused. A download starts only while staging_used + need ≤ cap and
free − need ≥ 50 GB, with need = 2.2 × source + 2 GB (the source, its
restored output and a margin) — the scheduler's own `staging_need` rule.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from .scheduler import (
    GB,
    OUTPUT_FACTOR,
    STAGING_EXTRA_BYTES,
    STAGING_RESERVE_BYTES,
)

WORK = "work"


class StagingError(ValueError):
    """A refused staging directory or a full one; the message is UI text."""


def _network_drive(path: Path) -> bool:
    if sys.platform == "win32":
        import ctypes

        drive = path.drive
        if not drive or drive.startswith(("\\\\", "//")):
            return True
        kind = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
        return bool(kind == 4)  # DRIVE_REMOTE
    return False


def validate_dir(value: str | None) -> Path:
    raw = (value or "").strip()
    if not raw:
        raise StagingError("请设置暂存目录")
    if raw.startswith(("\\\\", "//")):
        raise StagingError("暂存目录必须在本机磁盘上，不能是网络路径")
    path = Path(raw)
    if not path.is_absolute():
        raise StagingError("暂存目录必须是完整路径")
    if _network_drive(path):
        raise StagingError("暂存目录必须在本机磁盘上，不能是网络路径")
    return path


def need(source_bytes: int) -> int:
    return int(source_bytes * (1 + OUTPUT_FACTOR) + STAGING_EXTRA_BYTES)


def admits(staging_used: int, needed: int, cap: int, free: int) -> bool:
    return staging_used + needed <= cap and free - needed >= STAGING_RESERVE_BYTES


def tree_bytes(root: Path) -> int:
    total = 0
    for folder, _, names in os.walk(root):
        for name in names:
            try:
                total += (Path(folder) / name).stat().st_size
            except OSError:
                continue
    return total


class Staging:
    def __init__(
        self,
        root: Path,
        cap_gb: int,
        *,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.root = root
        self.cap = cap_gb * GB
        self._free = free_bytes or (lambda path: shutil.disk_usage(path).free)

    @property
    def work_dir(self) -> Path:
        return self.root / WORK

    def used(self) -> int:
        return tree_bytes(self.root) if self.root.is_dir() else 0

    def free(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return self._free(self.root)

    def admit(self, source_bytes: int, present: int = 0) -> None:
        """Refuse a download the disk rules do not allow (空间不足); bytes of
        this film already staged count as used once only."""
        needed = need(source_bytes)
        if not admits(self.used() - present, needed, self.cap, self.free()):
            raise StagingError("空间不足")
