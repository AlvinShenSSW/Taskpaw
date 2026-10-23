"""#299 R6/§4: the Windows inbound firewall rule for this agent.

Adding the rule needs elevation, so the agent never runs netsh itself: it
asks Windows (`ShellExecuteExW`, verb `runas`, hidden) to start the signed
backend with the one fixed route `agent-firewall allow --ports P[,P]` — one
UAC prompt under the TaskPaw publisher. That route accepts nothing else (1–2
integer ports, 1–65535), runs only in a frozen Windows build, and runs two
fixed `%SystemRoot%\\System32\\netsh.exe` command lines: delete the old
"TaskPaw Agent" rule, add it again for the backend program on the private
profile. Error 1223 from ShellExecuteExW is the user's 「否」 — declined.

Reading the status needs no elevation: one fixed `powershell -NoProfile
-NonInteractive` command prints JSON (not localised), 8 s timeout, cached
30 s. Nothing here logs a path or the output. Tests replace the launcher and
the runners; no test ever starts netsh or asks for elevation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("taskpaw.agent.firewall")

RULE_NAME = "TaskPaw Agent"
ROUTE = "agent-firewall"
ELEVATED_TIMEOUT = 120.0
STATUS_TIMEOUT = 8.0
STATUS_TTL = 30.0
ERROR_CANCELLED = 1223  # ERROR_CANCELLED: the UAC prompt was declined
_PORTS = re.compile(r"[0-9]{1,5}(,[0-9]{1,5})?")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_STATUS_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    f"$r=Get-NetFirewallRule -DisplayName '{RULE_NAME}'"
    "|Where-Object{$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound'}"
    "|Select-Object -First 1;"
    "$o=@{rule=$false;ports=@();program='';profile=''};"
    "if($r){$o.rule=$true;"
    "$o.ports=@(($r|Get-NetFirewallPortFilter).LocalPort|ForEach-Object{[string]$_});"
    "$o.program=[string](($r|Get-NetFirewallApplicationFilter).Program)};"
    "$c=Get-NetConnectionProfile|Select-Object -First 1;"
    "if($c){$o.profile=[string]$c.NetworkCategory};"
    "$o|ConvertTo-Json -Compress"
)


def parse_ports(text: str) -> Optional[list[int]]:
    """`P[,P]`: one or two distinct ports 1–65535, else None."""
    if not isinstance(text, str) or not _PORTS.fullmatch(text):
        return None
    ports = [int(p) for p in text.split(",")]
    if any(not 1 <= p <= 65535 for p in ports) or len(set(ports)) != len(ports):
        return None
    return ports


def _system32() -> str:
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemDirectoryW(buffer, 260):
            return buffer.value
    return os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")


def netsh_commands(ports: Sequence[int], program: str, system32: str) -> list[str]:
    """The two fixed netsh command lines (CreateProcess strings, no shell)."""
    if '"' in program or not ports:
        raise ValueError("invalid firewall rule")
    netsh = f'"{os.path.join(system32, "netsh.exe")}"'
    local = ",".join(str(int(p)) for p in ports)
    return [
        f'{netsh} advfirewall firewall delete rule name="{RULE_NAME}"',
        f'{netsh} advfirewall firewall add rule name="{RULE_NAME}" dir=in '
        f"action=allow protocol=TCP localport={local} profile=private "
        f'program="{program}"',
    ]


Runner = Callable[[str], int]


def _run_netsh(command: str) -> int:
    done = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        creationflags=_NO_WINDOW,
    )
    return done.returncode


def route_main(
    argv: list[str],
    *,
    run: Optional[Runner] = None,
    frozen: Optional[bool] = None,
    platform: Optional[str] = None,
    program: Optional[str] = None,
    system32: Optional[str] = None,
) -> int:
    """`taskpaw-backend agent-firewall allow --ports P[,P]` (elevated)."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    if len(argv) != 3 or argv[0] != "allow" or argv[1] != "--ports":
        print("usage: agent-firewall allow --ports P[,P]", file=sys.stderr)
        return 2
    ports = parse_ports(argv[2])
    if ports is None:
        print("invalid ports", file=sys.stderr)
        return 2
    if not frozen or platform != "win32":
        print("agent-firewall runs only in the installed Windows app", file=sys.stderr)
        return 2
    commands = netsh_commands(ports, program or sys.executable, system32 or _system32())
    run = run or _run_netsh
    try:
        run(commands[0])  # no rule yet is fine
        return 0 if run(commands[1]) == 0 else 1
    except (OSError, subprocess.SubprocessError):
        return 1


# ── the agent side (not elevated) ──────────────────────────────────────
class Declined(Exception):
    """The UAC prompt was answered 「否」."""


Launcher = Callable[[str, str, float], Optional[int]]


def _shell_execute_runas(file: str, params: str, timeout: float) -> Optional[int]:
    """Start `file params` elevated and hidden; its exit code, None on a
    timeout. Raises `Declined` on ERROR_CANCELLED, OSError otherwise."""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess, see_mask_flag_no_ui, sw_hide = 0x40, 0x400, 0
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess | see_mask_flag_no_ui
    info.lpVerb, info.lpFile, info.lpParameters = "runas", file, params
    info.nShow = sw_hide
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == ERROR_CANCELLED:
            raise Declined
        raise OSError(code, "ShellExecuteExW failed")
    if not info.hProcess:
        return None
    try:
        waited = kernel32.WaitForSingleObject(info.hProcess, int(timeout * 1000))
        if waited != 0:  # WAIT_OBJECT_0
            return None
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            return None
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def request_allow(
    ports: Sequence[int],
    *,
    launch: Optional[Launcher] = None,
    frozen: Optional[bool] = None,
    platform: Optional[str] = None,
    executable: Optional[str] = None,
) -> str:
    """R6: `added`, `declined`, `failed` or `unsupported` (not the installed
    Windows app). Never raises; at most one UAC prompt per call."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not frozen:
        return "unsupported"
    wanted = sorted({int(p) for p in ports})
    text = ",".join(str(p) for p in wanted)
    if parse_ports(text) is None:
        return "failed"
    try:
        code = (launch or _shell_execute_runas)(
            executable or sys.executable,
            f"{ROUTE} allow --ports {text}",
            ELEVATED_TIMEOUT,
        )
    except Declined:
        return "declined"
    except OSError as exc:
        if getattr(exc, "winerror", None) == ERROR_CANCELLED or (
            exc.args and exc.args[0] == ERROR_CANCELLED
        ):
            return "declined"
        log.warning("the firewall helper could not be started")
        return "failed"
    finally:
        _CACHE.clear()
    return "added" if code == 0 else "failed"


# ── status (no elevation) ───────────────────────────────────────────────
StatusRunner = Callable[[list[str], float], Optional[str]]


def _run_powershell(argv: list[str], timeout: float) -> Optional[str]:
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout[:4096].decode("utf-8", "replace")


def status_argv(system32: str) -> list[str]:
    powershell = os.path.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe")
    return [powershell, "-NoProfile", "-NonInteractive", "-Command", _STATUS_SCRIPT]


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: Optional[tuple[float, dict[str, Any]]] = None

    def clear(self) -> None:
        with self.lock:
            self.value = None


_CACHE = _Cache()


def _same_program(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(
        os.path.normpath(b)
    )


def status(
    ports: Sequence[int],
    *,
    run: Optional[StatusRunner] = None,
    platform: Optional[str] = None,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """`{platform, rule: present|absent|unknown, profile}`; present only when
    the enabled inbound rule covers every port for this backend program."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return {"platform": "macos", "rule": "unknown", "profile": None}
    if platform != "win32":
        return {"platform": "other", "rule": "unknown", "profile": None}
    with _CACHE.lock:
        cached = _CACHE.value
    now = clock()
    if cached is None or now - cached[0] >= STATUS_TTL:
        raw = (run or _run_powershell)(status_argv(_system32()), STATUS_TIMEOUT)
        facts: dict[str, Any] = {}
        try:
            parsed = json.loads(raw) if raw else None
            if isinstance(parsed, dict):
                facts = parsed
        except ValueError:
            facts = {}
        with _CACHE.lock:
            _CACHE.value = (now, facts)
        cached = (now, facts)
    facts = cached[1]
    profile_raw = str(facts.get("profile") or "").lower()
    profile = {
        "private": "private",
        "public": "public",
        "domainauthenticated": "domain",
    }.get(profile_raw)
    if "rule" not in facts:
        return {"platform": "windows", "rule": "unknown", "profile": profile}
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    present = bool(facts.get("rule"))
    if present:
        covered = {str(p) for p in facts.get("ports") or []}
        present = {str(int(p)) for p in ports} <= covered
        program = str(facts.get("program") or "")
        if present and frozen:
            present = _same_program(program, executable or sys.executable)
    return {
        "platform": "windows",
        "rule": "present" if present else "absent",
        "profile": profile,
    }
