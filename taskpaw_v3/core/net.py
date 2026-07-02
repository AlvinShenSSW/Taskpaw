"""Port helpers shared by the agent and Hub launchers.

`claim_port` binds and RETURNS the socket so the caller can hand it straight to
`uvicorn.Server.run(sockets=[...])` — eliminating the check-then-bind TOCTOU race
(a generic uvicorn OSError) in favour of an actionable PortInUseError held until
the server actually owns the socket.
"""

from __future__ import annotations

import ipaddress
import json
import socket


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
