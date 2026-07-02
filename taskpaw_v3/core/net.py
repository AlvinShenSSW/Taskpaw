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
import re
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
# (`taskpaw-backend-<triple>[.exe]`, backend_command's fallback). Match EXACTLY those
# two shapes — base name, or base + a target triple (arch-vendor-os[-abi], 3–4 parts,
# underscores allowed e.g. `x86_64`), optional `.exe` — NOT a loose prefix, so a
# foreign helper like `taskpaw-backend-logger` can't be mistaken for ours (Kimi 终审).
# Used to identify a *stale instance of THIS app* so we only ever reclaim a port from
# ourselves — never from a foreign service.
_BACKEND_NAME_RE = re.compile(
    r"taskpaw-backend(?:-[a-z0-9_]+(?:-[a-z0-9_]+){2,3})?(?:\.exe)?"
)

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


def _has_m_module(cmd: list[str], module: str) -> bool:
    """True if argv contains `-m <module>` (or `-m <module>.<sub>`) as an actual flag +
    value PAIR — not merely the module string appearing somewhere in argv, which a
    foreign process could carry incidentally (Kimi 终审)."""
    for i, a in enumerate(cmd):
        if a == "-m" and i + 1 < len(cmd):
            val = cmd[i + 1]
            if val == module or val.startswith(module + "."):
                return True
    return False


def _role_from_module(cmd: list[str]) -> str | None:
    """Role for a documented headless `python -m taskpaw_v3.<role>[...]` launch
    (deployment.md: `python -m taskpaw_v3.agent`, `python -m taskpaw_v3.hub run`,
    `python -m taskpaw_v3.agent.server.service`), whose process name is just `python`.
    Only accepts the module as an actual `-m` value, or its resolved
    `.../taskpaw_v3/{agent,hub}/….py` script path — not a bare arg that merely contains
    the string, so a foreign python process can't be misidentified (Kimi 终审). Returns
    'agent'/'hub', or None."""
    for i, a in enumerate(cmd):
        norm = a.replace("\\", "/")
        if i > 0 and cmd[i - 1] == "-m":
            # `-m` value: the module must BE taskpaw_v3.agent|hub or a submodule of it —
            # anchored, so a foreign `-m my.taskpaw_v3.agent` can't match (Kimi 终审).
            for role, mod in (("agent", "taskpaw_v3.agent"), ("hub", "taskpaw_v3.hub")):
                if a == mod or a.startswith(mod + "."):
                    return role
        elif norm.endswith(".py"):
            # resolved script path: `.../taskpaw_v3/agent/….py` (leading slash anchors
            # it, so `.../mytaskpaw_v3/agent/…` doesn't match).
            if "/taskpaw_v3/agent/" in norm:
                return "agent"
            if "/taskpaw_v3/hub/" in norm:
                return "hub"
    return None


def _explicit_role(cmd: list[str]) -> str:
    """The role token from a dispatched backend's argv (sidecar / backend_main): the
    first argument that is EXACTLY 'agent' or 'hub', else 'agent' (backend_main's
    default). Matching whole tokens — not substrings — means a path or flag that merely
    contains 'hub'/'agent' (e.g. /Users/hubert/…, hub.yaml) can't be read as the role
    (Kimi 终审)."""
    for a in cmd:
        if a in ("agent", "hub"):
            return a
    return "agent"


def _backend_role(name: str, cmd: list[str], exe_base: str | None = None) -> str | None:
    """The role ('agent'|'hub') THIS app's backend is running, or None if the process
    isn't ours. Recognizes the bundled sidecar and the from-source packaging entrypoint
    (both dispatched by a role argv, default agent), plus the documented headless module
    entrypoints (Codex 外门).

    Positive identification (Kimi 终审): the sidecar must match the name regex AND, when
    the real executable path is available (`exe_base`), that on-disk basename must match
    too — argv[0]/`proc.name()` are mutable, but `proc.exe()` is the actual binary, so a
    foreign process can't pass just by spoofing its reported name. (We deliberately do
    NOT require a specific install directory: PyInstaller onefile runs the server from a
    `_MEI` temp path, so a dir check would miss our own stale backend and reintroduce
    the very port-in-use failure this fixes.)"""
    norm = [a.replace("\\", "/") for a in cmd]
    is_sidecar = _BACKEND_NAME_RE.fullmatch(name) is not None and (
        exe_base is None or _BACKEND_NAME_RE.fullmatch(exe_base) is not None
    )
    is_source = any(a.endswith(_BACKEND_SOURCE_SUFFIX) for a in norm) or _has_m_module(
        cmd, _BACKEND_MODULE
    )
    if is_sidecar or is_source:
        return _explicit_role(cmd)
    return _role_from_module(cmd)


def _proc_identity(proc: "psutil.Process") -> tuple[str, list[str], str | None] | None:
    """(lowercased name, argv, lowercased exe basename) for `proc`, or None if it can't
    be inspected. Catches the full `psutil.Error` hierarchy — including ZombieProcess,
    whose name()/cmdline() raise — so a zombie degrades to "not ours" instead of
    crashing startup (Kimi 终审). exe() is best-effort (None if denied/zombie)."""
    try:
        name = (proc.name() or "").lower()
        cmd = [str(a) for a in (proc.cmdline() or [])]
    except (psutil.Error, OSError):
        return None
    try:
        exe = proc.exe() or ""
    except (psutil.Error, OSError):
        exe = ""
    exe_base = os.path.basename(exe).lower() if exe else None
    return name, cmd, exe_base


def _is_our_backend(proc: "psutil.Process", role: str) -> bool:
    """True only if `proc` is THIS app's own backend for `role` (agent|hub) — the
    PyInstaller sidecar, the from-source packaging entrypoint, or the documented
    `python -m taskpaw_v3.agent|hub` headless command — so we never mistake a foreign
    service for ours."""
    ident = _proc_identity(proc)
    if ident is None:
        return False
    name, cmd, exe_base = ident
    return _backend_role(name, cmd, exe_base) == role


def _addr_conflicts(want_host: str, laddr_ip: str) -> bool:
    """Would a bind to `want_host` collide with an existing listener on `laddr_ip`
    (same port assumed)? True if either side is a wildcard (all-interfaces), both are
    loopback, or the two are the same address. A foreign `127.0.0.1:P` listener never
    blocks an agent configured for a distinct `192.168.x.y:P` (Codex 外门)."""
    want = _norm_host(want_host)
    have = _norm_host(laddr_ip or "")
    if bind_is_wildcard(want) or bind_is_wildcard(have):
        return True
    # Any loopback ≈ any loopback, checked BEFORE the IPv4/IPv6 family split: `localhost`
    # (allowed by the Hub guard) binds loopback but psutil may report the stale listener
    # as 127.x OR ::1, so a stale ::1 backend must still be reclaimed for a `localhost`
    # start — and we only ever kill our OWN role backend, so this is safe (Kimi 终审).
    if bind_is_loopback(want) and bind_is_loopback(have):
        return True
    if (":" in want) != (":" in have):  # IPv4 vs IPv6 — otherwise separate stacks
        return False
    try:
        return ipaddress.ip_address(want) == ipaddress.ip_address(have)
    except ValueError:
        return want == have


def _same_bind_target(h1: str, h2: str) -> bool:
    """True only if binding `h1` and `h2` to the SAME port would actually collide: a
    wildcard on either side, the same literal address, or `localhost` vs a canonical
    loopback (127.0.0.1/::1). Distinct numeric loopbacks (127.0.0.1 vs 127.0.0.2) do
    NOT collide — stricter than `_addr_conflicts` so a valid two-loopback agent config
    isn't wrongly judged non-bindable (Kimi 终审)."""
    a, b = _norm_host(h1), _norm_host(h2)
    if bind_is_wildcard(a) or bind_is_wildcard(b):
        return True
    canon = {"localhost": {"127.0.0.1", "::1"}}
    sa = canon.get(a, {a})
    sb = canon.get(b, {b})
    for x in sa:
        for y in sb:
            try:
                if ipaddress.ip_address(x) == ipaddress.ip_address(y):
                    return True
            except ValueError:
                if x == y:
                    return True
    return False


def _proc_listen_conns(proc: "psutil.Process") -> list:
    """A process's own LISTENing inet sockets. Uses PER-PROCESS enumeration, which —
    unlike the system-wide `psutil.net_connections()` — works for a same-user process
    WITHOUT root on macOS (there the system-wide call raises AccessDenied, so the
    takeover would silently no-op; Codex 外门). Handles the psutil 6 rename
    Process.connections → Process.net_connections."""
    getter = getattr(proc, "net_connections", None) or proc.connections
    return getter(kind="inet")


def _find_stale_backends(host: str, port: int, role: str):
    """Yield THIS app's own `role` backend processes that hold a bind-conflicting
    LISTEN on (host, port). Scans OUR OWN processes (process_iter → per-process
    sockets) rather than doing a system-wide socket scan, so it needs no root on macOS
    where the stale instance runs as the same logged-in user (Codex 外门)."""
    if psutil is None:
        return
    try:
        procs = list(psutil.process_iter())
    except (psutil.Error, OSError) as e:
        log.warning("could not enumerate processes to reclaim a port: %s", e)
        return
    me = os.getpid()
    for proc in procs:
        if proc.pid == me:
            continue
        ident = _proc_identity(proc)
        if ident is None:
            continue  # denied / zombie / vanished — can't inspect, so not ours
        name, cmd, exe_base = ident
        if _backend_role(name, cmd, exe_base) != role:
            continue
        try:
            conns = _proc_listen_conns(proc)
        except (psutil.Error, OSError):
            continue
        for c in conns:
            if (
                getattr(c, "status", None) == psutil.CONN_LISTEN
                and c.laddr
                and c.laddr.port == port
                and _addr_conflicts(host, c.laddr.ip)
            ):
                yield proc
                break


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
    except (psutil.Error, OSError) as e:
        # psutil.Error covers NoSuchProcess / AccessDenied / TimeoutExpired / Zombie —
        # a stuck or zombie process is logged and skipped, never crashes startup; the
        # bounded claim_port below still fails loudly if the port stays held (Kimi 终审).
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
    # A config whose required ports aren't mutually bindable (e.g. control_port ==
    # bind_port on the same/overlapping host — accepted by AgentConfig, editable in the
    # UI) can NEVER start: the new process would bind the first socket then self-collide
    # on the second. Reclaiming would kill our old, still-working instance for a startup
    # that cannot succeed. Detect it and reclaim NOTHING; claim_port then fails loudly
    # (Codex 外门).
    for i, (h1, p1, _w1) in enumerate(specs):
        for h2, p2, _w2 in specs[i + 1 :]:
            if p1 == p2 and _same_bind_target(h1, h2):
                log.warning(
                    "not reclaiming the %s ports: required ports are not mutually "
                    "bindable (%s:%d conflicts with %s:%d) — leaving any stale backend "
                    "running; claim_port will fail loudly.",
                    role,
                    h1,
                    p1,
                    h2,
                    p2,
                )
                return False
    # Phase 1 — classify every port as ours / free / foreign. We can only see OUR OWN
    # sockets without root (macOS), so a port that is neither ours nor bindable is
    # treated as foreign — and a single foreign holder anywhere aborts the whole
    # reclaim (leave the old, still-serving agent alone; claim_port fails loudly).
    to_kill: dict[int, "psutil.Process"] = {}
    for host, port, what in specs:
        ours = list(_find_stale_backends(host, port, role))
        if ours:
            for proc in ours:
                to_kill[proc.pid] = proc
        elif not port_available(host, port):
            log.warning(
                "not reclaiming the %s ports: %s (%s:%d) is held by a foreign process "
                "— leaving any stale %s backend running rather than killing it when "
                "startup can't succeed here; claim_port will fail loudly.",
                role,
                what,
                host,
                port,
                role,
            )
            return False
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
    app's backend for this role (name/module + role). A **foreign** service on the
    port is left untouched — `claim_port` then fails loudly as before. Never binds, so
    there's no TOCTOU with the subsequent claim_port.
    """
    if psutil is None:
        return False
    reclaimed = False
    for proc in _find_stale_backends(host, port, role):
        log.warning(
            "reclaiming %s (%s:%s) from a stale TaskPaw %s backend (pid %d)",
            what,
            host,
            port,
            role,
            proc.pid,
        )
        if _terminate_backend(proc, proc.pid, wait):
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
