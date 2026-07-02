"""Port helpers shared by the agent and Hub launchers.

`claim_port` binds and RETURNS the socket so the caller can hand it straight to
`uvicorn.Server.run(sockets=[...])` — eliminating the check-then-bind TOCTOU race
(a generic uvicorn OSError) in favour of an actionable PortInUseError held until
the server actually owns the socket.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import time

try:
    import psutil
except ImportError:  # pragma: no cover — psutil is a base dependency
    psutil = None

log = logging.getLogger("taskpaw.net")

# Our PyInstaller sidecar's process name (agent + hub share one binary, dispatched
# by the role argv). The Tauri shell may launch either the stripped basename
# (`taskpaw-backend[.exe]`) or the target-triple-suffixed sidecar
# (`taskpaw-backend-<triple>[.exe]`, backend_command's fallback), so we match by
# prefix. Used to identify a *stale instance of THIS app* so we only ever reclaim a
# port from ourselves — never from a foreign service.
_BACKEND_NAME_PREFIX = "taskpaw-backend"

# A from-source run (dev) is `python .../taskpaw_v3/packaging/backend_main.py <role>`
# or `python -m taskpaw_v3.packaging.backend_main <role>`. Match the FULL package
# path/module — not a bare `backend_main.py`, which an unrelated service could also
# use — so we never mistake a foreign process for ours (Codex 外门).
_BACKEND_SOURCE_SUFFIX = "taskpaw_v3/packaging/backend_main.py"
_BACKEND_MODULE = "taskpaw_v3.packaging.backend_main"


class PortInUseError(RuntimeError):
    pass


def _norm_host(host: str) -> str:
    """Normalize a bind host for classification: trim whitespace and surrounding
    brackets so `[::]` / `[0.0.0.0]` can't slip past as a non-wildcard. Centralized
    here so every caller (agent UI guard, Hub startup guard) classifies the same
    spelling — no per-caller stripping to forget (Kimi #114)."""
    return host.strip().strip("[]")


def bind_is_wildcard(host: str) -> bool:
    """All-interfaces bind? True for 0.0.0.0, :: and every IPv6 spelling of the
    unspecified address (e.g. 0:0:0:0:0:0:0:0), so an exposure guard can't be
    bypassed by an alternate spelling (Codex #43). Shared by the agent's UI guard
    and the Hub's startup guard (#114)."""
    host = _norm_host(host)
    if host in ("", "*"):
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False  # a hostname, not an IP literal — the loopback check handles it


def bind_is_loopback(host: str) -> bool:
    """On-host only? True for localhost and any loopback IP (127.0.0.0/8, ::1)."""
    host = _norm_host(host)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bind_is_global(host: str) -> bool:
    """True only for a globally-routable (public/WAN) IP literal. Private/LAN,
    loopback, link-local, and hostnames are False — a hostname can't be classified
    here, so the token rule still applies to it. Used to refuse public exposure of
    the Hub API even when a token is set (#114; constitution §2: LAN + Bearer only)."""
    host = _norm_host(host)
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def guard_bind_exposure(host: str, api_token: str, *, label: str) -> None:
    """Refuse an unsafe network exposure for a Bearer-gated read API (#114) —
    constitution §2: LAN + Bearer only, never default to all interfaces. Raised
    BEFORE the socket is claimed. Single source of truth so the agent UI guard,
    the agent startup, and the Hub startup can't drift (Kimi):

    - wildcard/all-interfaces (0.0.0.0, :: and any spelling) → refused outright;
    - globally-routable (public/WAN) address → refused even WITH a token;
    - non-loopback address with an empty token → refused (would be reachable
      off-host unauthenticated).
    """
    if bind_is_wildcard(host):
        raise ValueError(
            f"refusing to bind the {label} to all interfaces ({host!r}) — use "
            f"127.0.0.1 or a specific LAN address."
        )
    if bind_is_global(host):
        raise ValueError(
            f"refusing to bind the {label} to a public/WAN address ({host}) — the "
            f"API is LAN + Bearer only. Use a private LAN address (with a token) "
            f"or 127.0.0.1."
        )
    if not bind_is_loopback(host) and not (api_token or "").strip():
        raise ValueError(
            f"binding the {label} to a non-loopback address ({host}) requires an "
            f"api_token, or /status and /events would be reachable off-host without "
            f"auth. Set a token, or keep the bind on 127.0.0.1."
        )


def loopback_url(host: str, port: int) -> str:
    """A loopback http URL the local webview can reach for a server bound to
    (host, port) (#48). The UI is always local, so a wildcard bind maps to its
    loopback (0.0.0.0 → 127.0.0.1, :: → ::1) and an IPv6 host is bracketed — e.g.
    a control server on `::1` is announced as http://[::1]:<port>, not the wrong
    http://127.0.0.1. The result is one of the canonical loopback forms the shell
    accepts (loopback_base) and the CSP connect-src allows."""
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host in ("::", "[::]"):
        host = "::1"
    bracketed = f"[{host}]" if ":" in host else host
    return f"http://{bracketed}:{port}"


def announce_ready(role: str, base_url: str) -> None:
    """Print the §3.1 readiness handshake line to stdout (#48): one machine-
    readable JSON object the Tauri shell reads before loading the webview, then
    injects this base_url (so a custom port works and the UI never races the
    backend). All logs go to stderr (logging.basicConfig), so stdout carries only
    this line; flushed so a piped shell sees it immediately."""
    print(
        json.dumps({"taskpaw_ready": True, "role": role, "base_url": base_url}),
        flush=True,
    )


def _family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def port_available(host: str, port: int) -> bool:
    """Best-effort probe (advisory; prefer claim_port for the real bind).

    Deliberately does NOT set SO_REUSEADDR: on macOS/BSD it would let this bind
    succeed even when another listener already holds the port, defeating the
    "is it in use?" check on the primary platform.
    """
    with socket.socket(_family(host), socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _role_from_module(cmd: list[str]) -> str | None:
    """Role for a documented headless `python -m taskpaw_v3.<role>[...]` launch
    (deployment.md: `python -m taskpaw_v3.agent`, `python -m taskpaw_v3.hub run`,
    `python -m taskpaw_v3.agent.server.service`), whose process name is just `python`.
    Recognizes both the `-m` module string and its resolved `.../taskpaw_v3/agent/…py`
    path form. Returns 'agent'/'hub', or None if no such module appears."""
    for a in cmd:
        token = "." + a.replace("\\", "/").replace("/", ".") + "."
        if ".taskpaw_v3.agent." in token:
            return "agent"
        if ".taskpaw_v3.hub." in token:
            return "hub"
    return None


def _backend_role(name: str, cmd: list[str]) -> str | None:
    """The role ('agent'|'hub') THIS app's backend is running, or None if the process
    isn't ours. Recognizes the bundled sidecar and the from-source packaging
    entrypoint (both dispatched by a role argv, default agent), plus the documented
    headless module entrypoints (Codex 外门)."""
    norm = [a.replace("\\", "/") for a in cmd]
    dispatched = (
        name.startswith(_BACKEND_NAME_PREFIX)
        or any(a.endswith(_BACKEND_SOURCE_SUFFIX) for a in norm)
        or _BACKEND_MODULE in cmd
    )
    if dispatched:
        # The shell passes the role explicitly; a no-arg invocation defaults to agent
        # (backend_main). hub requires an explicit "hub".
        return "hub" if "hub" in cmd else "agent"
    return _role_from_module(cmd)


def _is_our_backend(proc: "psutil.Process", role: str) -> bool:
    """True only if `proc` is THIS app's own backend for `role` (agent|hub) — the
    PyInstaller sidecar, the from-source packaging entrypoint, or the documented
    `python -m taskpaw_v3.agent|hub` headless command — so we never mistake a foreign
    service for ours."""
    try:
        name = (proc.name() or "").lower()
        cmd = [str(a) for a in (proc.cmdline() or [])]
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False
    return _backend_role(name, cmd) == role


def _addr_conflicts(want_host: str, laddr_ip: str) -> bool:
    """Would a bind to `want_host` collide with an existing listener on `laddr_ip`
    (same port assumed)? True if either side is a wildcard (all-interfaces) or the
    two are the same address. Different IP families don't collide, so a foreign
    `127.0.0.1:P` listener never blocks an agent configured for `192.168.x.y:P`
    (Codex 外门)."""
    want = _norm_host(want_host)
    have = _norm_host(laddr_ip or "")
    if (":" in want) != (":" in have):  # IPv4 vs IPv6 — separate stacks
        return False
    if bind_is_wildcard(want) or bind_is_wildcard(have):
        return True
    # `localhost` (allowed by the Hub exposure guard) binds a loopback address, but
    # psutil reports the listener as a numeric 127.x/::1 — treat any two loopbacks as
    # conflicting so a stale localhost-bound backend is still reclaimed (Codex 外门).
    if bind_is_loopback(want) and bind_is_loopback(have):
        return True
    try:
        return ipaddress.ip_address(want) == ipaddress.ip_address(have)
    except ValueError:
        return want == have


def _listener_pids(host: str, port: int) -> list[int]:
    """PIDs LISTENing on `port` at an address that would actually conflict with a bind
    to `host` (same address, or a wildcard on either side). Best-effort; [] if psutil
    is unavailable or enumeration is denied."""
    if psutil is None:
        return []
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, RuntimeError, OSError) as e:
        log.warning("could not enumerate connections to reclaim a port: %s", e)
        return []
    pids = []
    for c in conns:
        if (
            c.status == psutil.CONN_LISTEN
            and c.laddr
            and c.laddr.port == port
            and c.pid
            and _addr_conflicts(host, c.laddr.ip)
        ):
            pids.append(c.pid)
    return pids


def _terminate_backend(proc: "psutil.Process", pid: int, wait: float) -> bool:
    """terminate() → wait → kill() only if it won't exit. Returns True if it exited,
    False if termination couldn't be confirmed (logged) — including a stuck process
    that survives kill(); the caller's bounded wait + claim_port still fail loudly if
    the port stays held."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=max(1.0, wait * 0.6))
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=max(1.0, wait * 0.4))
    except (
        psutil.NoSuchProcess,
        psutil.AccessDenied,
        psutil.TimeoutExpired,
        OSError,
    ) as e:
        log.warning("could not terminate stale backend pid %d: %s", pid, e)
        return False
    return True


def reclaim_ports_from_stale_instance(
    specs: list[tuple[str, int, str]], *, role: str, wait: float = 8.0
) -> bool:
    """Reclaim SEVERAL required ports for `role` from this app's own stale backend —
    but only if EVERY port is free or held solely by our backend. If ANY port is held
    by a foreign/unidentified process, reclaim NOTHING and return False: we must not
    kill our previous instance when startup would fail anyway on the foreign-held port
    (Codex 外门). claim_port then fails loudly on the real conflict. `specs` is a list
    of (host, port, what). Used by the agent, which needs BOTH its network and control
    ports viable before it supersedes the old agent.
    """
    if psutil is None:
        return False
    # Phase 1 — inspect every port; a single foreign holder anywhere aborts the whole
    # reclaim (so we leave the old, still-serving agent alone).
    to_kill: dict[int, "psutil.Process"] = {}
    for host, port, what in specs:
        for pid in _listener_pids(host, port):
            if pid == os.getpid():
                continue
            try:
                proc = psutil.Process(pid)
            except (psutil.NoSuchProcess, OSError):
                continue
            if not _is_our_backend(proc, role):
                log.warning(
                    "not reclaiming the %s ports: %s (%s:%d) is held by a foreign "
                    "process (pid %d) — leaving any stale %s backend running rather "
                    "than killing it when startup can't succeed here; claim_port will "
                    "fail loudly.",
                    role,
                    what,
                    host,
                    port,
                    pid,
                    role,
                )
                return False
            to_kill[pid] = proc
    if not to_kill:
        return False
    # Phase 2 — every holder is ours: terminate them, then wait for the OS to release
    # each socket before the caller binds.
    reclaimed = False
    for pid, proc in to_kill.items():
        log.warning(
            "reclaiming the %s ports from a stale TaskPaw %s backend (pid %d)",
            role,
            role,
            pid,
        )
        if _terminate_backend(proc, pid, wait):
            reclaimed = True
    if reclaimed:
        deadline = time.monotonic() + wait
        for host, port, _what in specs:
            while time.monotonic() < deadline and not port_available(host, port):
                time.sleep(0.2)
    return reclaimed


def reclaim_port_from_stale_instance(
    host: str, port: int, *, role: str, what: str, wait: float = 8.0
) -> bool:
    """If `port` is held by THIS app's own backend of the same role, terminate it and
    wait for the port to free, so a relaunch/update "just works". Returns True if it
    reclaimed one.

    Semantics: **last launch wins** for a single-agent/single-hub-per-machine box
    (the design invariant — one agent, one port per machine). The holder of *this
    configured port* is by definition the previous instance of *this* agent/hub, so
    a new launch supersedes it (the exact behavior needed for in-place updates,
    where the old version is still running). It does NOT try to distinguish "stale"
    from "actively serving" — on a single-instance box they're the same instance.
    Preventing an *accidental* double-launch of the same version is the Tauri shell's
    single-instance responsibility (a separate follow-up), not this port logic.

    Safety: it only ever terminates a process it can positively identify as this
    app's backend for this role (name prefix + role argv). A **foreign** service on
    the port is left untouched — `claim_port` then fails loudly as before. Never
    binds, so there's no TOCTOU with the subsequent claim_port.
    """
    if psutil is None:
        return False
    reclaimed = False
    for pid in _listener_pids(host, port):
        if pid == os.getpid():
            continue
        try:
            proc = psutil.Process(pid)
        except (psutil.NoSuchProcess, OSError):
            continue
        if not _is_our_backend(proc, role):
            continue  # foreign process on our port → do NOT kill; fail loud later
        log.warning(
            "reclaiming %s (%s:%s) from a stale TaskPaw %s backend (pid %d)",
            what,
            host,
            port,
            role,
            pid,
        )
        if _terminate_backend(proc, pid, wait):
            reclaimed = True
    if reclaimed:
        # Wait for the OS to actually release the socket before the caller binds.
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not port_available(host, port):
            time.sleep(0.2)
    return reclaimed


def claim_port(host: str, port: int, what: str) -> socket.socket:
    """Bind (host, port) and return the listening socket, or raise PortInUseError.

    The returned socket is owned by the caller and should be passed to
    `uvicorn.Server.run(sockets=[sock])` (or closed). No TOCTOU gap.

    No SO_REUSEADDR — we WANT bind to fail if another instance already owns the
    port (the "refuse to start if in use" contract); on macOS SO_REUSEADDR would
    silently allow a second agent to share 5680.
    """
    s = socket.socket(_family(host), socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.listen(128)
    except OSError as e:
        s.close()
        raise PortInUseError(
            f"{what} port {host}:{port} is already in use. Another TaskPaw "
            f"instance, a V2 agent (default 5678), or another service may hold "
            f"it. Stop it or change the port before starting."
        ) from e
    return s
