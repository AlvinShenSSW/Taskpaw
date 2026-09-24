"""Process-wide run generation (#177).

Every monitor run takes a fresh generation from this allocator: `RunId =
(instance_id, generation)` tags translation results, GPU-lease holders and
temp-file names. It is process-wide (not per instance) because the UI's
Stop/Start and `reconfigure` re-create the instance object — an in-instance
counter would restart at 1 and collide with the previous object's leftovers.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_last = 0


def next_generation() -> int:
    """1, 2, 3, … — monotonic and never reused within the process."""
    global _last
    with _lock:
        _last += 1
        return _last
