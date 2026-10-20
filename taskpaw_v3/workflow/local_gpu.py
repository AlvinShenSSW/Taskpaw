"""Stable identity and size of the controller's own GPU (T03-P1 C1, D03)."""

from __future__ import annotations

import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Callable

# Real 16 GB cards report about 16300 MiB; Jasna uses the same boundary.
FOUR_K_VRAM_MIB = 15360
FULL_HD = (1920, 1080)
UHD = (3840, 2160)

_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class LocalGpu:
    name: str
    uuid: str
    vram_mib: int


def max_resolution(vram_mib: int) -> tuple[int, int]:
    """The largest frame this card may process: 4K only on 16 GB-class cards."""
    return UHD if vram_mib >= FOUR_K_VRAM_MIB else FULL_HD


Runner = Callable[..., subprocess.CompletedProcess]


def query_gpu(run: Runner = subprocess.run, timeout: float = 10.0) -> LocalGpu | None:
    """GPU 0 from nvidia-smi, or None when it cannot be read."""
    try:
        done = run(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    try:
        vram = int(float(parts[2]))
    except ValueError:
        return None
    return LocalGpu(name=parts[0], uuid=parts[1], vram_mib=vram) if vram > 0 else None


def machine_identity() -> str:
    """Windows MachineGuid: stable across reboots and network changes."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                if isinstance(value, str) and value:
                    return value
        except OSError:
            pass
    return str(uuid.UUID(int=uuid.getnode()))
