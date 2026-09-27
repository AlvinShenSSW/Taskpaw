"""#204: FFmpeg discovery and a paste-safe, isolated-registry setup script."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.monitors.subs import child
from taskpaw_v3.monitors.subs import ffmpeg as F

EXE = r"C:\WhisperJAV\Scripts\whisperjav.exe"
DIR = r"C:\WhisperJAV\Library\bin"
FFMPEG = DIR + r"\ffmpeg.exe"
ENDING = "完成后请关闭 TaskPaw 窗口（会完全退出），再从开始菜单重新打开"


@pytest.fixture
def discovery(monkeypatch):
    monkeypatch.setattr(F.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Tester\Local")
    monkeypatch.setattr(F.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(F.shutil, "which", lambda *a, **k: None)
    registry(monkeypatch)


def registry(monkeypatch, machine=("", 1), user=("", 1)):
    expansions = []

    def expand(value):
        expansions.append(value)
        return value.replace("%TOOLS%", r"C:\O'Brien\tools")

    fake = SimpleNamespace(
        HKEY_LOCAL_MACHINE="machine",
        HKEY_CURRENT_USER="user",
        REG_EXPAND_SZ=2,
        OpenKey=lambda root, sub: nullcontext(root),
        QueryValueEx=lambda key, name: machine if key == "machine" else user,
        ExpandEnvironmentStrings=expand,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return expansions


@pytest.mark.parametrize(
    "exe",
    [
        "",
        "whisperjav.exe",
        r"Scripts\whisperjav.exe",
        r"C:whisperjav.exe",
        r"\\server\share\whisperjav.exe",
        "//server/share/whisperjav.exe",
        r"\\?\C:\WhisperJAV\Scripts\whisperjav.exe",
        EXE + ";evil",
        EXE + '"',
        EXE.replace("Scripts", "Scr\nipts"),
        EXE.replace("Scripts", "Scr\tipts"),
        EXE + "\x00",
        EXE + "\x7f",
        EXE.replace("Scripts", "Scr\x85ipts"),
    ],
)
def test_unsafe_exe_is_ignored(discovery, exe):
    assert F.bundled_ffmpeg_dir(exe) is None
    assert F.bundled_ffmpeg(exe) is None
    status = F.ffmpeg_status(exe)
    assert status["exe_ok"] is False
    assert status["bundled"] is None
    assert status["error"] is False


def test_shared_dir_normalizes_without_requiring_a_file(discovery, monkeypatch):
    assert F.bundled_ffmpeg_dir("  C:/WhisperJAV/Scripts/./whisperjav.exe  ") == DIR
    assert F.bundled_ffmpeg(EXE) is None
    monkeypatch.setattr(F.os.path, "isfile", lambda p: p == FFMPEG)
    assert F.bundled_ffmpeg(EXE) == FFMPEG
    assert F.ffmpeg_status(EXE)["bundled"] == FFMPEG


@pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\r\n\t ", "\x85"])
def test_pasted_exe_is_stripped_before_validation(discovery, monkeypatch, whitespace):
    exe = whitespace + EXE + whitespace
    monkeypatch.setattr(F.os.path, "isfile", lambda p: p == FFMPEG)
    assert F.bundled_ffmpeg_dir(exe) == DIR
    assert F.bundled_ffmpeg(exe) == FFMPEG
    status = F.ffmpeg_status(exe)
    assert status["exe_ok"] is True
    assert status["bundled"] == FFMPEG
    assert child.asr_env({"Path": ""}, exe)["Path"] == DIR


@pytest.mark.parametrize("key", ["PATH", "Path", "pAtH", None])
def test_child_appends_in_place_and_scrubs_without_mutation(
    discovery, monkeypatch, key
):
    monkeypatch.setattr(F.os.path, "isfile", lambda p: p == FFMPEG)
    base = {"TASKPAW_LLM_API_KEY": "secret", "KEEP": "yes"}
    if key:
        base[key] = r"C:\tools;;"
    original = dict(base)
    agent = dict(os.environ)
    calls = []
    monkeypatch.setattr(F.shutil, "which", lambda name, **kw: calls.append(kw) or None)
    env = child.asr_env(base, EXE)
    assert env == {"KEEP": "yes", key or "PATH": ("C:\\tools;" if key else "") + DIR}
    assert calls == [{"path": original.get(key, "")}]
    assert base == original and dict(os.environ) == agent


@pytest.mark.parametrize(
    "found,bundled,exe",
    [
        (True, True, EXE),
        (False, False, EXE),
        (False, True, "whisperjav.exe"),
        (False, True, r"\\host\share\whisperjav.exe"),
    ],
)
def test_child_leaves_path_alone(discovery, monkeypatch, found, bundled, exe):
    monkeypatch.setattr(F.os.path, "isfile", lambda p: bundled)
    monkeypatch.setattr(
        F.shutil, "which", lambda *a, **k: "existing" if found else None
    )
    assert child.asr_env({"Path": "original;"}, exe) == {"Path": "original;"}


def test_default_environment_and_log_once_per_folder(discovery, monkeypatch, caplog):
    monkeypatch.setattr(F.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(child, "_bundled_logged", set())
    before = dict(os.environ)
    with caplog.at_level(logging.INFO, logger=child.log.name):
        child.asr_env(whisperjav_exe=EXE)
        child.asr_env(whisperjav_exe=EXE.lower())
        child.asr_env(whisperjav_exe=EXE.replace("C:", "D:"))
    assert dict(os.environ) == before
    messages = [
        r.message for r in caplog.records if "using the bundled FFmpeg" in r.message
    ]
    assert messages == [
        f"whisperjav: using the bundled FFmpeg ({DIR})",
        f"whisperjav: using the bundled FFmpeg ({DIR.replace('C:', 'D:')})",
    ]


def test_status_missing_and_candidates(discovery):
    status = F.ffmpeg_status(EXE)
    assert status == {
        "on_path": None,
        "bundled": None,
        "effective": None,
        "exe_ok": True,
        "saved_path_ok": None,
        "pending_restart": False,
        "platform": "windows",
        "error": False,
        "candidates": [
            {"dir": p, "exists": False}
            for p in [
                DIR,
                r"C:\Users\Tester\Local\WhisperJAV\Library\bin",
                r"C:\Jasna\tools",
                r"C:\Lada\_internal\bin",
            ]
        ],
    }


def test_status_priority_and_candidate_exists(discovery, monkeypatch):
    monkeypatch.setattr(F.os.path, "isfile", lambda p: p == FFMPEG)
    assert F.ffmpeg_status(EXE)["effective"] == FFMPEG
    assert F.ffmpeg_status(EXE)["candidates"][0] == {"dir": DIR, "exists": True}
    monkeypatch.setattr(F.shutil, "which", lambda *a, **k: r"C:\path\ffmpeg.exe")
    status = F.ffmpeg_status(EXE)
    assert status["effective"] == status["on_path"] == r"C:\path\ffmpeg.exe"
    assert status["pending_restart"] is False


@pytest.mark.parametrize("machine_only", [True, False])
def test_saved_path_expands_each_expand_string_entry_only(
    discovery, monkeypatch, machine_only
):
    expand_value = (' ; "%TOOLS%\\bin" ;; %TOOLS%\\more ', 2)
    literal = (r"%LITERAL%\bin", 1)
    expansions = registry(
        monkeypatch,
        expand_value if machine_only else literal,
        literal if machine_only else expand_value,
    )
    paths = []

    def which(name, path=None):
        if path is None:
            return None
        paths.append(path)
        return r"C:\O'Brien\tools\bin\ffmpeg.exe"

    monkeypatch.setattr(F.shutil, "which", which)
    status = F.ffmpeg_status()
    expanded = [r"C:\O'Brien\tools\bin", r"C:\O'Brien\tools\more"]
    assert paths == [
        ";".join(expanded + [literal[0]] if machine_only else [literal[0]] + expanded)
    ]
    assert expansions == [r"%TOOLS%\bin", r"%TOOLS%\more"]
    assert status["saved_path_ok"] == r"C:\O'Brien\tools\bin\ffmpeg.exe"
    assert status["pending_restart"] is True


@pytest.mark.parametrize("operation", ["OpenKey", "QueryValueEx"])
@pytest.mark.parametrize("failed_key", ["machine", "user"])
def test_saved_path_access_failure_keeps_other_key(
    discovery, monkeypatch, operation, failed_key
):
    registry(monkeypatch, machine=(DIR, 1), user=(DIR, 1))
    fake = sys.modules["winreg"]
    original = getattr(fake, operation)

    def denied(key, name):
        if key == failed_key:
            raise PermissionError("test registry access denied")
        return original(key, name)

    monkeypatch.setattr(fake, operation, denied)
    paths = []

    def which(name, path=None):
        paths.append(path)
        return FFMPEG if path == DIR else None

    monkeypatch.setattr(F.shutil, "which", which)
    monkeypatch.setattr(F.os.path, "isfile", lambda p: p == FFMPEG)
    status = F.ffmpeg_status(EXE)
    assert status["error"] is False
    assert paths == [None, DIR]
    assert status["saved_path_ok"] == FFMPEG
    assert status["on_path"] is None
    assert status["bundled"] == status["effective"] == FFMPEG
    assert status["pending_restart"] is False
    assert status["candidates"][0] == {"dir": DIR, "exists": True}


@pytest.mark.parametrize("invalid_key", ["machine", "user"])
@pytest.mark.parametrize("other_path", ["", DIR])
def test_saved_path_non_string_keeps_other_key(
    discovery, monkeypatch, invalid_key, other_path
):
    invalid = (123, 4)
    valid = (other_path, 1)
    registry(
        monkeypatch,
        machine=invalid if invalid_key == "machine" else valid,
        user=invalid if invalid_key == "user" else valid,
    )
    paths = []

    def which(name, path=None):
        paths.append(path)
        return FFMPEG if path == DIR else None

    monkeypatch.setattr(F.shutil, "which", which)
    status = F.ffmpeg_status(EXE)
    assert status["error"] is False
    assert paths == [None, other_path]
    assert status["saved_path_ok"] == (FFMPEG if other_path else None)
    assert status["on_path"] is None
    assert status["bundled"] is None
    assert status["effective"] is None
    assert status["pending_restart"] is bool(other_path)
    assert status["candidates"][0] == {"dir": DIR, "exists": False}


def test_status_failure_never_raises(discovery, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("test failure")

    monkeypatch.setattr(F.shutil, "which", fail)
    assert F.ffmpeg_status(EXE)["error"] is True


def test_non_windows_does_not_import_registry(discovery, monkeypatch):
    monkeypatch.setattr(F.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "winreg", None)
    status = F.ffmpeg_status()
    assert status["platform"] == "other"
    assert status["saved_path_ok"] is None and status["error"] is False


def test_script_static_contract():
    script = F.setup_script([r"C:\O'Brien\bin", r"C:\O’Brien\bin", "C:\\bad\npath"])
    assert script.startswith("& {\n") and script.endswith("}\n")
    assert "\r" not in script and "\t" not in script
    for forbidden in [
        r"\bexit\b",
        r"\bsetx\b",
        "ExecutionPolicy",
        "admin",
        "托盘",
        r"SetEnvironmentVariable\('Path'",
    ]:
        assert not re.search(forbidden, script, re.I)
    assert "'C:\\O''Brien\\bin'" in script
    assert "'C:\\O’’Brien\\bin'" in script and "bad" not in script
    assert "$env:USERNAME" in script
    assert "$key.GetValue('Path','')" in script
    assert script.count("[Environment]::GetEnvironmentVariable('Path','Machine')") == 1
    assert "$key.GetValue('Path','','DoNotExpandEnvironmentNames')" in script
    assert "$key.SetValue('Path',$new,'ExpandString')" in script
    assert (
        "[Environment]::SetEnvironmentVariable('TASKPAW_PATH_REFRESH',$null,'User')"
        in script
    )
    assert all(
        "-LiteralPath" in line for line in script.splitlines() if "Test-Path" in line
    )
    assert ENDING in script.splitlines()[-2]
    assert "winget install Gyan.FFmpeg" in script


def test_script_skips_unavailable_paths_quietly():
    script = F.setup_script()
    assert "Join-Path" not in script
    checks = [line for line in script.splitlines() if "Test-Path" in line]
    assert len(checks) == 2
    assert all("-ErrorAction SilentlyContinue" in line for line in checks)
    assert script.count(r"($folder.TrimEnd('\') + '\ffmpeg.exe')") == 2
    assert "\f" not in script


def test_script_guards_localappdata_and_preserves_candidate_order():
    script = F.setup_script([r"D:\custom\bin"])
    assert "if ($env:LOCALAPPDATA) {" in script
    assert (
        script.index(r"'D:\custom\bin'")
        < script.index("if ($env:LOCALAPPDATA) {")
        < script.index(r"($env:LOCALAPPDATA + '\WhisperJAV\Library\bin')")
        < script.index(r"'C:\WhisperJAV\Library\bin'")
    )


def powershell(script):
    exe = shutil.which("powershell")
    if not exe:
        pytest.skip("powershell unavailable")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout.decode("utf-8-sig").strip()


def test_script_parses_in_powershell():
    script = F.setup_script([r"C:\O'Brien\bin", r"C:\O’Brien\bin", r"C:\a‘b‚c‛d\bin"])
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    result = powershell(
        "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
        f"$source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))\n"
        "$tokens = $null; $errors = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)\n"
        "$errors.Count"
    )
    assert result == "0"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows private registry hive")
@pytest.mark.parametrize("missing_drive", [False, True])
@pytest.mark.parametrize("empty_localappdata", [False, True])
def test_script_executed_twice_in_private_app_hive(
    tmp_path, missing_drive, empty_localappdata
):
    # RegLoadAppKey creates a process-private hive. No HKCU/HKLM writes, no broadcast.
    folder = tmp_path / "O'Brien’s [tools]"
    folder.mkdir()
    (folder / "ffmpeg.exe").write_bytes(b"dummy; never executed")

    def literal(value):
        return "'" + re.sub("['‘’‚‛]", lambda m: m[0] * 2, str(value)) + "'"

    extra_dirs = [str(folder)]
    machine_path = "''"
    if missing_drive:
        drive = next(
            (
                chr(n)
                for n in range(ord("Z"), ord("D") - 1, -1)
                if not os.path.exists(f"{chr(n)}:\\")
            ),
            None,
        )
        if drive is None:
            pytest.skip("No unavailable drive letter")
        extra_dirs.insert(0, drive + r":\WhisperJAV\Library\bin")
        machine_path = literal(drive + r":\stale\bin")
    if empty_localappdata:
        # Intercept only the drive-relative probe in this child PowerShell process.
        # Treat it as present so the test catches an unsafe candidate without
        # creating anything at the drive root or depending on installed tools.
        extra_dirs = []
        machine_path = "$( $env:LOCALAPPDATA = ''; " + machine_path + " )"
    generated = F.setup_script(
        extra_dirs,
        _test_hook={
            "key": "$privateRoot.CreateSubKey('Environment')",
            "machine_path": machine_path,
            "broadcast": False,
        },
    )
    assert "CurrentUser" not in generated and "TASKPAW_PATH_REFRESH" not in generated
    assert "GetEnvironmentVariable('Path','Machine')" not in generated
    script = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class PrivateHive204 {
    [DllImport("advapi32.dll", CharSet=CharSet.Unicode)]
    public static extern int RegLoadAppKey(string file, out IntPtr key, int access, int options, int reserved);
}
'@
$handle = [IntPtr]::Zero
"""
    script += f"$code = [PrivateHive204]::RegLoadAppKey({literal(tmp_path / 'private.hive')},[ref]$handle,983103,1,0)\n"
    script += r"""
if ($code -ne 0) { throw "RegLoadAppKey: $code" }
$safe = [Microsoft.Win32.SafeHandles.SafeRegistryHandle]::new($handle,$true)
$privateRoot = [Microsoft.Win32.RegistryKey]::FromHandle($safe)
try {
    $seed = $privateRoot.CreateSubKey('Environment')
    $seed.SetValue('Path','%USERPROFILE%\x','ExpandString')
    $seed.Close()
    $Error.Clear()
"""
    if empty_localappdata:
        script += r"""
    $script:relativeProbes = 0
    function Test-Path {
        param($LiteralPath, $PathType, $ErrorAction)
        if ($LiteralPath -eq '\WhisperJAV\Library\bin\ffmpeg.exe') {
            $script:relativeProbes++
            return $true
        }
        return $LiteralPath -eq 'C:\WhisperJAV\Library\bin\ffmpeg.exe'
    }
"""
    script += (
        generated
        + r"""
    $read = $privateRoot.OpenSubKey('Environment')
    $first = $read.GetValue('Path','','DoNotExpandEnvironmentNames')
    $kind1 = $read.GetValueKind('Path').ToString()
    $read.Close()
"""
    )
    script += (
        generated
        + r"""
    $read = $privateRoot.OpenSubKey('Environment')
    $second = $read.GetValue('Path','','DoNotExpandEnvironmentNames')
    $kind2 = $read.GetValueKind('Path').ToString()
    $read.Close()
    @{first=$first;second=$second;kind1=$kind1;kind2=$kind2;errors=$Error.Count;relativeProbes=$script:relativeProbes} | ConvertTo-Json -Compress
} finally {
    $privateRoot.Close()
    $safe.Dispose()
}
"""
    )
    result = json.loads(powershell(script).splitlines()[-1])
    assert result["errors"] == 0
    assert result["kind1"] == result["kind2"] == "ExpandString"
    if empty_localappdata:
        assert result["relativeProbes"] == 0
        assert result["first"] == result["second"] == "%USERPROFILE%\\x;" + DIR
        return
    assert result["first"] == result["second"] == "%USERPROFILE%\\x;" + str(folder)
    assert result["second"].count(str(folder)) == 1


def test_readme_first_fenced_block_matches_generator():
    readme = (Path(__file__).parents[2] / "README.md").read_text(encoding="utf-8")
    section = readme.split("FFmpeg（AV 翻译识别需要）", 1)[1]
    block = re.search(r"```[^\n]*\n(.*?)```", section, re.S)
    assert block is not None
    assert block[1] == F.setup_script()
