"""#299 R6/§4: the Windows inbound firewall rule for this agent.

Adding the rule needs elevation, so the agent never runs netsh itself: it
asks Windows (`ShellExecuteExW`, verb `runas`, hidden) to start the signed
backend with the one fixed route `agent-firewall allow --ports P[,P]` — one
UAC prompt under the TaskPaw publisher. That route accepts nothing else (1–2
integer ports, 1–65535), runs only in a frozen Windows build, and runs two
fixed `%SystemRoot%\\System32\\netsh.exe` command lines: delete the old
"TaskPaw Agent" rule, add it again for the backend program on the private
profile. Error 1223 from ShellExecuteExW is the user's 「否」 — declined.

Review IR3: the backend has no window, so the prompt is owned by the shell's
foreground window (else Windows may leave it blinking in the taskbar), and
ShellExecuteExW runs on a worker thread: the request is bounded overall
(UAC prompt included, 120 s → `timeout`), and while one is in flight another
answers `pending` instead of a second prompt.

Reading the status needs no elevation: one fixed `powershell -NoProfile
-NonInteractive` command prints JSON (UTF-8, not localised) — the rule and
the connection profile of the adapter that owns `bind_host` (review IR7) —
8 s timeout, cached 30 s per address. Nothing here logs a path or the
output. Tests replace the launcher and the runners; no test ever starts
netsh or asks for elevation.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import ntpath
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
_STATUS_HEAD = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "$ErrorActionPreference='SilentlyContinue';"
    f"$r=Get-NetFirewallRule -DisplayName '{RULE_NAME}'"
    "|Where-Object{$_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound'}"
    "|Select-Object -First 1;"
    "$o=@{rule=$false;ports=@();program='';profile=''};"
    "if($r){$o.rule=$true;"
    "$o.ports=@(($r|Get-NetFirewallPortFilter).LocalPort|ForEach-Object{[string]$_});"
    "$o.program=[string](($r|Get-NetFirewallApplicationFilter).Program)};"
)
_STATUS_TAIL = (
    "if($c){$o.profile=[string]$c.NetworkCategory};$o|ConvertTo-Json -Compress"
)


def _lan_ipv4(host: Optional[str]) -> Optional[str]:
    """A non-loopback IPv4 literal in canonical form — the only text ever put
    into the status script — else None."""
    try:
        value = ipaddress.IPv4Address(str(host or "").strip())
    except ValueError:
        return None
    return None if value.is_loopback or value.is_unspecified else str(value)


def status_script(bind_host: Optional[str] = None) -> str:
    """The fixed status script; with a LAN `bind_host`, the connection
    profile is the one of the adapter that owns that address (IR7)."""
    host = _lan_ipv4(bind_host)
    if host is None:
        profile = "$c=Get-NetConnectionProfile|Select-Object -First 1;"
    else:
        profile = (
            f"$c=$null;$i=Get-NetIPAddress -IPAddress '{host}'|Select-Object -First 1;"
            "if($i){$c=Get-NetConnectionProfile -InterfaceIndex $i.InterfaceIndex"
            "|Select-Object -First 1};"
        )
    return _STATUS_HEAD + profile + _STATUS_TAIL


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
    # Windows-only paths are built with ntpath on every OS (identical values)
    return ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")


def netsh_commands(ports: Sequence[int], program: str, system32: str) -> list[str]:
    """The two fixed netsh command lines (CreateProcess strings, no shell)."""
    if '"' in program or not ports:
        raise ValueError("invalid firewall rule")
    netsh = f'"{ntpath.join(system32, "netsh.exe")}"'
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


def _foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _window_pid(hwnd: int) -> int:
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(
        wintypes.HWND(hwnd), ctypes.byref(pid)
    )
    return int(pid.value)


def _ancestor_pids() -> set[int]:
    """The shell that started this backend (and the frozen bootloader)."""
    import psutil

    return {p.pid for p in psutil.Process().parents()[:3]}


def owner_window(
    *,
    foreground: Optional[Callable[[], int]] = None,
    pid_of: Optional[Callable[[int], int]] = None,
    family: Optional[Callable[[], set[int]]] = None,
) -> Optional[int]:
    """The foreground window when it belongs to the TaskPaw shell (the
    operator just clicked in it): the UAC prompt's owner. Else None."""
    try:
        hwnd = (foreground or _foreground_window)()
        if not hwnd:
            return None
        pid = (pid_of or _window_pid)(hwnd)
        return hwnd if pid and pid in (family or _ancestor_pids)() else None
    except Exception:
        return None


def _shell_execute_runas(file: str, params: str, timeout: float) -> Optional[int]:
    """Start `file params` elevated and hidden, owned by the TaskPaw window;
    its exit code, None when `timeout` (counted from the call, the UAC prompt
    included) runs out. Raises `Declined` on ERROR_CANCELLED, OSError
    otherwise."""
    if sys.platform != "win32":
        raise OSError("ShellExecuteExW is Windows only")
    import ctypes
    from ctypes import wintypes

    deadline = time.monotonic() + timeout

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
    info.hwnd = owner_window()  # review IR3: the prompt comes to the front
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
        left = max(0.0, deadline - time.monotonic())
        waited = kernel32.WaitForSingleObject(info.hProcess, int(left * 1000))
        if waited != 0:  # WAIT_OBJECT_0
            return None
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            return None
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


# one elevation at a time; `_LAST` is set when the latest worker finished
_IN_FLIGHT = threading.Lock()
_LAST = threading.Event()
_LAST.set()


def request_allow(
    ports: Sequence[int],
    *,
    launch: Optional[Launcher] = None,
    frozen: Optional[bool] = None,
    platform: Optional[str] = None,
    executable: Optional[str] = None,
    timeout: float = ELEVATED_TIMEOUT,
) -> str:
    """R6: `added`, `declined`, `failed`, `unsupported` (not the installed
    Windows app), `timeout` (review IR3: not finished within `timeout`, the
    UAC prompt included) or `pending` (an earlier prompt is still open).
    Never raises; at most one UAC prompt at a time."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not frozen:
        return "unsupported"
    wanted = sorted({int(p) for p in ports})
    text = ",".join(str(p) for p in wanted)
    if parse_ports(text) is None:
        return "failed"
    if not _IN_FLIGHT.acquire(blocking=False):
        return "pending"
    run = launch or _shell_execute_runas
    outcome: dict[str, Any] = {}
    done = threading.Event()
    _LAST.clear()

    def work() -> None:
        try:
            outcome["code"] = run(
                executable or sys.executable, f"{ROUTE} allow --ports {text}", timeout
            )
        except BaseException as exc:  # mapped below; never escapes the thread
            outcome["error"] = exc
        finally:
            _CACHE.clear()
            done.set()
            _IN_FLIGHT.release()
            _LAST.set()

    threading.Thread(target=work, name="taskpaw-firewall", daemon=True).start()
    if not done.wait(timeout):
        log.warning("the administrator confirmation did not finish in time")
        return "timeout"
    error = outcome.get("error")
    if isinstance(error, Declined):
        return "declined"
    if isinstance(error, OSError):
        if getattr(error, "winerror", None) == ERROR_CANCELLED or (
            error.args and error.args[0] == ERROR_CANCELLED
        ):
            return "declined"
        log.warning("the firewall helper could not be started")
        return "failed"
    if error is not None:
        log.warning("the firewall helper failed")
        return "failed"
    return "added" if outcome.get("code") == 0 else "failed"


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


def status_argv(system32: str, bind_host: Optional[str] = None) -> list[str]:
    powershell = ntpath.join(system32, "WindowsPowerShell", "v1.0", "powershell.exe")
    return [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        status_script(bind_host),
    ]


class _Cache:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: Optional[tuple[float, dict[str, Any], Optional[str]]] = None

    def clear(self) -> None:
        with self.lock:
            self.value = None


_CACHE = _Cache()


def _same_program(a: str, b: str) -> bool:
    return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))


def status(
    ports: Sequence[int],
    *,
    bind_host: Optional[str] = None,
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
    host = _lan_ipv4(bind_host)
    with _CACHE.lock:
        cached = _CACHE.value
    now = clock()
    if cached is None or now - cached[0] >= STATUS_TTL or cached[2] != host:
        raw = (run or _run_powershell)(status_argv(_system32(), host), STATUS_TIMEOUT)
        facts: dict[str, Any] = {}
        try:
            parsed = json.loads(raw) if raw else None
            if isinstance(parsed, dict):
                facts = parsed
        except ValueError:
            facts = {}
        with _CACHE.lock:
            _CACHE.value = (now, facts, host)
        cached = (now, facts, host)
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
