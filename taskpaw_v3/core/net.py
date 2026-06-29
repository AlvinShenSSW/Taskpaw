"""Port helpers shared by the agent and Hub launchers.

`claim_port` binds and RETURNS the socket so the caller can hand it straight to
`uvicorn.Server.run(sockets=[...])` — eliminating the check-then-bind TOCTOU race
(a generic uvicorn OSError) in favour of an actionable PortInUseError held until
the server actually owns the socket.
"""

from __future__ import annotations

import json
import socket


class PortInUseError(RuntimeError):
    pass


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
    print(json.dumps({"taskpaw_ready": True, "role": role, "base_url": base_url}),
          flush=True)


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
