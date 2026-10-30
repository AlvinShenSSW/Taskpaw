"""#299 §3: a planned restart. The control API asks for one; the service then
stops gracefully and exits with `EXIT_CODE`, which the desktop shell's
supervisor answers by starting the same backend again and rebuilding its
window. Any other exit code keeps today's behaviour."""

from __future__ import annotations

import threading
from typing import Callable

EXIT_CODE = 75  # EX_TEMPFAIL: "try again" — only the shell's supervisor reads it

_requested = threading.Event()


def request(stop: Callable[[], None]) -> None:
    """Mark the restart, then stop the service on its own thread (the HTTP
    reply that asked for it has already been sent). Not a daemon: the exit
    waits for it to return instead of finalizing under its last log write
    (0xC0000409, so the shell saw no 75 and did not restart us)."""
    _requested.set()
    # explicit: the API asks from a daemon worker thread, whose flag a new
    # thread would otherwise inherit
    threading.Thread(target=stop, name="taskpaw-restart", daemon=False).start()


def requested() -> bool:
    return _requested.is_set()


def exit_code(default: int = 0) -> int:
    return EXIT_CODE if _requested.is_set() else default


def _reset_for_tests() -> None:
    _requested.clear()
