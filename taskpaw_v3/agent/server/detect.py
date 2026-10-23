"""#299 §5 R3/R7/R8/R13: what this machine has for the media workflow.

`GET /control/workflow/detect` (cached 60 s, `?refresh=1`) reports the GPU,
each tool (ffprobe, Jasna, WhisperJAV, Miko) and suggested directories;
`POST /control/workflow/settings/directory/inspect {path}` reports one
directory (POST keeps the path out of access logs).

Every probe runs a fixed argv with no window, stdin closed and a timeout
(the process tree is killed on a timeout), reads at most 4 KB and parses it
by regex; nothing it prints is logged. ffprobe (`-version`) and nvidia-smi
are the only programs ever executed: Jasna's version comes from its PE
file-version resource, WhisperJAV and Miko are only looked for. A network
path (UNC, a mapped network drive) is never touched. Everything the probes
read — the file tree, PATH, the environment, the runner — is injectable so
tests use fake trees and fake runners only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from taskpaw_v3.agent.server.admin import MonitorAdmin
    from taskpaw_v3.core.config import WorkflowSettings

OUTPUT_LIMIT = 4096
FFPROBE_TIMEOUT = 3.0
GPU_TIMEOUT = 5.0
CACHE_TTL = 60.0
WORKER_VRAM_MIB = 15360  # a 16 GB-class card (`local_gpu.FOUR_K_VRAM_MIB`)
MIKO_SCRIPT = ("mdcx", "cmd", "postprocess_one.py")
SUGGESTED = {
    "inbox": "TaskpawInbox",
    "data_dir": "TaskpawWork",
    "staging": "TaskpawStaging",
}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_FFPROBE_VERSION = re.compile(r"ffprobe version n?(\d+(?:\.\d+){0,3})")

Runner = Callable[[list[str], float], Optional[str]]


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        import psutil

        for child in psutil.Process(proc.pid).children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_probe(argv: list[str], timeout: float) -> Optional[str]:
    """Run one fixed probe; its first 4 KB of stdout, or None."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        return None
    if proc.returncode != 0:
        return None
    return out[:OUTPUT_LIMIT].decode("utf-8", "replace")


def pe_version(path: str) -> Optional[str]:
    """The PE file-version resource (`a.b.c[.d]`); never runs the file."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version")
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buffer):
        return None

    class Fixed(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
        ]

    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(
        buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)
    ):
        return None
    if not pointer.value or length.value < ctypes.sizeof(Fixed):
        return None
    fixed = Fixed.from_address(pointer.value)
    if fixed.dwSignature != 0xFEEF04BD:
        return None
    parts = [
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    ]
    if parts[3] == 0:
        parts = parts[:3]
    return ".".join(str(p) for p in parts)


def is_network(path: str) -> bool:
    """UNC or a mapped network drive: never probed."""
    if path.startswith(("\\\\", "//")):
        return True
    if sys.platform == "win32" and re.match(r"^[A-Za-z]:", path):
        import ctypes

        drive_remote = 4
        return bool(
            ctypes.windll.kernel32.GetDriveTypeW(path[:2] + "\\") == drive_remote
        )
    return False


def fixed_drives() -> list[tuple[str, int]]:
    """(root, free bytes) of each fixed local drive (Windows)."""
    if sys.platform != "win32":
        return []
    import ctypes

    drive_fixed = 3
    found: list[tuple[str, int]] = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(65 + index)}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) != drive_fixed:
            continue
        try:
            found.append((root, shutil.disk_usage(root).free))
        except OSError:
            continue
    return found


def default_candidates(env: Mapping[str, str], platform: str) -> dict[str, list[str]]:
    """The fixed install locations §5 looks in (after saved and monitor paths)."""
    if platform != "win32":
        return {"jasna": [], "whisperjav": []}
    programs = env.get("ProgramFiles") or r"C:\Program Files"
    local = env.get("LOCALAPPDATA") or ""
    jasna = [r"C:\Jasna\jasna.exe", os.path.join(programs, "Jasna", "jasna.exe")]
    whisper = [r"C:\WhisperJAV\Scripts\whisperjav.exe"]
    if local:
        jasna.append(os.path.join(local, "Programs", "Jasna", "jasna.exe"))
        whisper.append(
            os.path.join(local, "Programs", "WhisperJAV", "Scripts", "whisperjav.exe")
        )
    return {"jasna": jasna, "whisperjav": whisper}


@dataclass
class Detector:
    """One detection pass; every reader replaceable."""

    platform: str = field(default_factory=lambda: sys.platform)
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    which: Callable[[str], Optional[str]] = shutil.which
    run: Runner = run_probe
    version_of: Callable[[str], Optional[str]] = pe_version
    network: Callable[[str], bool] = is_network
    drives: Callable[[], list[tuple[str, int]]] = fixed_drives
    candidates: Optional[dict[str, list[str]]] = None

    # ── file checks that never touch a network path ──────────────────────
    def _file(self, path: Optional[str]) -> bool:
        if not path or self.network(path):
            return False
        try:
            return Path(path).is_file()
        except OSError:
            return False

    def _dir(self, path: Optional[Path]) -> bool:
        if path is None or self.network(str(path)):
            return False
        try:
            return path.is_dir()
        except OSError:
            return False

    def _first(
        self, options: list[tuple[Optional[str], str]]
    ) -> tuple[Optional[str], Optional[str]]:
        for path, source in options:
            if self._file(path):
                return path, source
        return None, None

    # ── tools ────────────────────────────────────────────────────────────
    def ffprobe(self, settings: "WorkflowSettings") -> dict[str, Any]:
        from taskpaw_v3.monitors.subs.ffmpeg import bundled_ffmpeg_dir

        bundled = None
        whisper = settings.whisperjav_exe_path
        if whisper and self.platform == "win32":
            folder = bundled_ffmpeg_dir(whisper)
            bundled = os.path.join(folder, "ffprobe.exe") if folder else None
        path, source = self._first(
            [
                (settings.ffprobe_path, "saved"),
                (self.which("ffprobe"), "path"),
                (bundled, "bundled"),
            ]
        )
        version = None
        if path is not None:
            out = self.run([path, "-version"], FFPROBE_TIMEOUT) or ""
            match = _FFPROBE_VERSION.search(out)
            version = match.group(1) if match else None
        return {
            "found": path is not None,
            "path": path,
            "source": source,
            "version": version,
        }

    def _monitor_paths(
        self, monitors: list[dict], key: str
    ) -> list[tuple[Optional[str], str]]:
        found: list[tuple[Optional[str], str]] = []
        for spec in monitors:
            config = spec.get("config") if isinstance(spec, dict) else None
            value = config.get(key) if isinstance(config, dict) else None
            if isinstance(value, str) and value.strip():
                found.append((value.strip(), "monitor"))
        return found

    def _tool(
        self,
        saved: Optional[str],
        monitors: list[dict],
        key: str,
        kind: str,
        command: str,
    ) -> tuple[Optional[str], Optional[str]]:
        fixed = (self.candidates or default_candidates(self.env, self.platform))[kind]
        return self._first(
            [
                (saved, "saved"),
                *self._monitor_paths(monitors, key),
                *[(path, "default") for path in fixed],
                (self.which(command), "path"),
            ]
        )

    def jasna(
        self, settings: "WorkflowSettings", monitors: list[dict]
    ) -> dict[str, Any]:
        path, source = self._tool(
            settings.jasna_exe_path, monitors, "jasna_exe_path", "jasna", "jasna"
        )
        version = None
        if path is not None:
            try:
                version = self.version_of(path)
            except OSError:
                version = None
        return {
            "found": path is not None,
            "path": path,
            "source": source,
            "version": version,
        }

    def whisperjav(
        self, settings: "WorkflowSettings", monitors: list[dict]
    ) -> dict[str, Any]:
        path, source = self._tool(
            settings.whisperjav_exe_path,
            monitors,
            "whisperjav_exe_path",
            "whisperjav",
            "whisperjav",
        )
        return {
            "found": path is not None,
            "path": path,
            "source": source,
            "version": None,
        }

    def miko(
        self, settings: "WorkflowSettings", config_dir: Optional[Path]
    ) -> dict[str, Any]:
        roots: list[Optional[Path]] = [
            Path(settings.mdcx_root) if settings.mdcx_root else None,
            Path(settings.data_dir) / "engines" / "MDCX304"
            if settings.data_dir
            else None,
            config_dir / "engines" / "MDCX304" if config_dir is not None else None,
        ]
        root = next(
            (
                r
                for r in roots
                if r is not None
                and self._dir(r)
                and self._file(str(r.joinpath(*MIKO_SCRIPT)))
            ),
            None,
        )
        python: Optional[str] = None
        if root is not None:
            venv = (
                root / ".venv" / "Scripts" / "python.exe"
                if self.platform == "win32"
                else root / ".venv" / "bin" / "python"
            )
            python = str(venv) if self._file(str(venv)) else None
        if settings.mdcx_python and self._file(settings.mdcx_python):
            python = settings.mdcx_python
        if self.platform == "win32":
            appdata = self.env.get("APPDATA") or ""
            default_profile = (
                os.path.join(appdata, "com.mdcx.desktop", "config.json")
                if appdata
                else None
            )
        else:
            default_profile = str(
                self.home
                / "Library"
                / "Application Support"
                / "com.mdcx.desktop"
                / "config.json"
            )
        profile, _ = self._first(
            [(settings.mdcx_profile, "saved"), (default_profile, "default")]
        )
        found = [root is not None, python is not None, profile is not None]
        status = "complete" if all(found) else "partial" if any(found) else "off"
        return {
            "status": status,
            "root": str(root) if root is not None else None,
            "python": python,
            "profile": profile,
        }

    # ── GPU ──────────────────────────────────────────────────────────────
    def gpu(self) -> Optional[dict[str, Any]]:
        out = self.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version,power.limit",
                "--format=csv,noheader,nounits",
            ],
            GPU_TIMEOUT,
        )
        if not out:
            return None
        line = next((line for line in out.splitlines() if line.strip()), "")
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4 or not parts[0]:
            return None
        try:
            vram = int(float(parts[1]))
        except ValueError:
            return None
        if vram <= 0:
            return None
        try:
            power: Optional[int] = round(float(parts[3]))
        except ValueError:
            power = None  # "[N/A]" on some cards
        driver = parts[2] if re.fullmatch(r"[0-9.]{1,20}", parts[2]) else None
        return {
            "name": parts[0][:120],
            "vram_mib": vram,
            "driver": driver,
            "power_limit_w": power,
            "worker_ok": vram >= WORKER_VRAM_MIB,
        }

    # ── directories ──────────────────────────────────────────────────────
    def suggested(self) -> dict[str, Optional[str]]:
        """§5: the fixed local drive with the most free space (macOS: home)."""
        if self.platform == "win32":
            drives = self.drives()
            if not drives:
                return {key: None for key in SUGGESTED}
            root = max(drives, key=lambda d: d[1])[0]
            base: Path = Path(root)
        else:
            base = self.home
        return {key: str(base / name) for key, name in SUGGESTED.items()}

    def detect(
        self,
        settings: "WorkflowSettings",
        monitors: list[dict],
        config_dir: Optional[Path],
    ) -> dict[str, Any]:
        return {
            "gpu": self.gpu(),
            "tools": {
                "ffprobe": self.ffprobe(settings),
                "jasna": self.jasna(settings, monitors),
                "whisperjav": self.whisperjav(settings, monitors),
                "miko": self.miko(settings, config_dir),
            },
            "suggested": self.suggested(),
        }


def inspect_directory(
    raw: Any, network: Callable[[str], bool] = is_network
) -> dict[str, Any]:
    """`{exists, free_bytes, volume, local}` of one directory; the path is
    validated like the workflow's directory fields (422 per field)."""
    from taskpaw_v3.agent.server.workflow_settings import Refused, _path_error

    if not isinstance(raw, str) or not raw.strip():
        raise Refused(422, "设置有误", fields={"path": "请填写"})
    text = raw.strip()
    why = _path_error(text)
    if why:
        raise Refused(422, "设置有误", fields={"path": why})
    path = Path(text)
    volume = path.drive or path.anchor or "/"
    if network(text):
        return {"exists": None, "free_bytes": None, "volume": volume, "local": False}
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    free = None
    probe = path
    while True:
        try:
            if probe.is_dir():
                free = shutil.disk_usage(probe).free
                break
        except OSError:
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    return {"exists": exists, "free_bytes": free, "volume": volume, "local": True}


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: Optional[tuple[float, Any, dict[str, Any]]] = None


def register_detect_routes(
    app: FastAPI,
    admin: Optional["MonitorAdmin"],
    config_dir: Optional[Path],
    saved_workflow: Callable[[], "WorkflowSettings"],
    *,
    detector: Optional[Callable[[], Detector]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    from taskpaw_v3.agent.server.workflow_settings import Refused, _refusal

    cache = _Cache()
    make = detector or Detector

    @app.get("/control/workflow/detect")
    def workflow_detect(refresh: int = 0) -> dict[str, Any]:
        settings = saved_workflow()
        key = settings.model_dump_json()
        now = clock()
        with cache.lock:
            hit = cache.value
        if refresh or hit is None or hit[1] != key or now - hit[0] >= CACHE_TTL:
            monitors = (
                (admin.config_view().get("monitors") or []) if admin is not None else []
            )
            result = make().detect(settings, list(monitors), config_dir)
            with cache.lock:
                cache.value = (now, key, result)
            return result
        return hit[2]

    @app.post("/control/workflow/settings/directory/inspect")
    async def workflow_directory_inspect(request: Request) -> Any:
        from starlette.concurrency import run_in_threadpool

        try:
            body = await request.json()
        except ValueError:
            body = None
        raw = body.get("path") if isinstance(body, dict) else None
        try:
            return await run_in_threadpool(inspect_directory, raw)
        except Refused as exc:
            return _refusal(exc)
        except Exception:
            return JSONResponse(
                status_code=422,
                content={"detail": "设置有误", "fields": {"path": "无法读取"}},
            )
