"""Port helpers shared by the agent and Hub launchers.

`claim_port` binds and RETURNS the socket so the caller can hand it straight to
`uvicorn.Server.run(sockets=[...])` — eliminating the check-then-bind TOCTOU race
(a generic uvicorn OSError) in favour of an actionable PortInUseError held until
the server actually owns the socket.
"""

from __future__ import annotations

import socket


class PortInUseError(RuntimeError):
    pass


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
