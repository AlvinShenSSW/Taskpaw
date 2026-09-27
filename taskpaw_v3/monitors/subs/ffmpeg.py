"""WhisperJAV's bundled FFmpeg, read-only status, and a user-run PATH script."""

from __future__ import annotations

import logging
import ntpath
import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

log = logging.getLogger("taskpaw.subs.ffmpeg")
_FALLBACK_DIRS = (
    r"C:\WhisperJAV\Library\bin",
    r"C:\Jasna\tools",
    r"C:\Lada\_internal\bin",
)


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in value)


def bundled_ffmpeg_dir(exe: str) -> str | None:
    """Derive the Windows install folder lexically, without accessing the exe."""
    if _has_control(exe) or ";" in exe or '"' in exe:
        return None
    exe = exe.strip()
    if not re.match(r"^[A-Za-z]:[\\/]", exe):
        return None
    # ntpath is Windows' os.path; keep these Windows paths lexical on other OSes too.
    exe = ntpath.normpath(exe)
    return ntpath.normpath(ntpath.join(ntpath.dirname(exe), "..", "Library", "bin"))


def bundled_ffmpeg(exe: str) -> str | None:
    folder = bundled_ffmpeg_dir(exe)
    if folder is None:
        return None
    path = ntpath.join(folder, "ffmpeg.exe")
    return path if os.path.isfile(path) else None


def _empty_status() -> dict[str, Any]:
    return {
        "on_path": None,
        "bundled": None,
        "effective": None,
        "exe_ok": False,
        "saved_path_ok": None,
        "pending_restart": False,
        "candidates": [],
        "platform": "windows" if sys.platform == "win32" else "other",
        "error": False,
    }


def _saved_path() -> str:
    import winreg

    entries = []
    for root, subkey in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            continue  # A missing PATH value/key is an ordinary empty PATH.
        for entry in value.split(";"):
            entry = entry.strip().strip('"').strip()
            if not entry:
                continue
            if kind == winreg.REG_EXPAND_SZ:
                entry = winreg.ExpandEnvironmentStrings(entry)
            entry = entry.strip().strip('"').strip()
            if entry:
                entries.append(entry)
    return ";".join(entries)


def ffmpeg_status(whisperjav_exe: str = "") -> dict[str, Any]:
    """Read discovery state; even an unexpected failure produces a status envelope."""
    result = _empty_status()
    try:
        folder = bundled_ffmpeg_dir(whisperjav_exe)
        result["exe_ok"] = folder is not None
        result["on_path"] = shutil.which("ffmpeg")
        result["bundled"] = bundled_ffmpeg(whisperjav_exe)
        result["effective"] = result["on_path"] or result["bundled"]
        if sys.platform == "win32":
            result["saved_path_ok"] = shutil.which("ffmpeg", path=_saved_path())
        result["pending_restart"] = (
            result["effective"] is None and result["saved_path_ok"] is not None
        )
        folders = [folder] if folder else []
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            folders.append(ntpath.join(local, "WhisperJAV", "Library", "bin"))
        folders.extend(_FALLBACK_DIRS)
        seen = set()
        for candidate in folders:
            candidate = ntpath.normpath(candidate)
            identity = ntpath.normcase(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            result["candidates"].append(
                {
                    "dir": candidate,
                    "exists": os.path.isfile(ntpath.join(candidate, "ffmpeg.exe")),
                }
            )
    except Exception as exc:  # This diagnostic must never break the control UI.
        log.warning("FFmpeg status check failed (%s)", type(exc).__name__)
        result["error"] = True
    return result


def _ps_literal(value: str) -> str:
    return "'" + re.sub("['‘’‚‛]", lambda match: match[0] * 2, value) + "'"


def setup_script(
    extra_dirs: Iterable[str] = (), *, _test_hook: Mapping[str, Any] | None = None
) -> str:
    """One pasteable block. The private hook isolates registry execution tests."""
    hook = _test_hook or {}
    key = hook.get(
        "key", "[Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')"
    )
    machine = hook.get(
        "machine_path", "[Environment]::GetEnvironmentVariable('Path','Machine')"
    )
    broadcast = (
        "                    [Environment]::SetEnvironmentVariable('TASKPAW_PATH_REFRESH',$null,'User')\n"
        if hook.get("broadcast", True)
        else ""
    )
    candidates = [
        _ps_literal(folder) for folder in extra_dirs if not _has_control(folder)
    ]
    candidates.append("($env:LOCALAPPDATA + '\\WhisperJAV\\Library\\bin')")
    candidates.extend(_ps_literal(folder) for folder in _FALLBACK_DIRS)
    candidate_lines = ",\n".join("                " + value for value in candidates)
    return f"""& {{
    Write-Host "当前 Windows 用户：$env:USERNAME"
    $key = {key}
    try {{
        $userPath = [string]$key.GetValue('Path','')
        $machinePath = {machine}
        $savedPath = [string]$machinePath + ';' + $userPath
        $found = $null
        foreach ($entry in ($savedPath -split ';')) {{
            $folder = $entry.Trim().Trim('"').Trim()
            if ($folder) {{
                $exe = Join-Path $folder 'ffmpeg.exe'
                if (Test-Path -LiteralPath $exe -PathType Leaf) {{
                    $found = $exe
                    break
                }}
            }}
        }}
        if ($found) {{
            Write-Host "已找到 FFmpeg：$found"
        }} else {{
            $candidates = @(
{candidate_lines}
            )
            $selected = $null
            foreach ($folder in $candidates) {{
                if (Test-Path -LiteralPath (Join-Path $folder 'ffmpeg.exe') -PathType Leaf) {{
                    $selected = $folder
                    break
                }}
            }}
            if ($selected) {{
                $raw = [string]$key.GetValue('Path','','DoNotExpandEnvironmentNames')
                $present = $false
                foreach ($entry in ($raw -split ';')) {{
                    $folder = $entry.Trim().Trim('"').Trim()
                    if ($folder) {{
                        $folder = [Environment]::ExpandEnvironmentVariables($folder)
                        if ($folder.TrimEnd('\\') -ieq $selected.TrimEnd('\\')) {{
                            $present = $true
                        }}
                    }}
                }}
                if ($present) {{
                    Write-Host "FFmpeg 文件夹已在用户 PATH：$selected"
                }} else {{
                    $new = $raw.TrimEnd(';')
                    if ($new) {{ $new += ';' }}
                    $new += $selected
                    $key.SetValue('Path',$new,'ExpandString')
{broadcast}                    Write-Host "已加入用户 PATH：$selected"
                }}
            }} else {{
                Write-Host '未找到 FFmpeg。可运行：winget install Gyan.FFmpeg'
            }}
        }}
    }} finally {{
        $key.Close()
    }}
    Write-Host '完成后请关闭 TaskPaw 窗口（会完全退出），再从开始菜单重新打开'
}}
"""
