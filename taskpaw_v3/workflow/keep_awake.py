"""P2-3 (#294) R11 / C11: keep a worker awake while it has GPU work.

`SetThreadExecutionState` is per thread: a state set from a short-lived thread
lapses silently when that thread ends. The worker therefore sets and clears it
only from its long-lived heartbeat thread, and `KeepAwake` records which
thread does so (risk 3). The call is injectable for tests.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

log = logging.getLogger("taskpaw.workflow.keep_awake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

StateCall = Callable[[int], int]


def _set_state(flags: int) -> int:
    """`SetThreadExecutionState` on Windows; elsewhere nothing to hold."""
    if sys.platform != "win32":
        return 1
    import ctypes

    return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))


class KeepAwake:
    def __init__(self, call: StateCall = _set_state) -> None:
        self._call = call
        self.held = False
        self.thread: int | None = None  # the thread that last changed it

    def hold(self, on: bool) -> None:
        """Hold (or clear) system-required; a no-op when unchanged."""
        if on == self.held:
            return
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS
        try:
            if not self._call(flags):
                log.warning("SetThreadExecutionState refused (%s)", on)
                return
        except Exception:
            log.exception("keep-awake call failed")
            return
        self.held, self.thread = on, threading.get_ident()
