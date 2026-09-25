"""Small helpers shared by the plugins that run the subs engine (#179 S4).

Nothing here is imported from `lada.py` (C1): each caller passes its own limit.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Union

_HEAD_CHARS = 80
_SEP = " … "
# #189: what identifies a translator request rather than its progress — it
# never reaches the metrics.
_REQUEST_KEYS = frozenset({"job_id", "started_at"})


def bounded(text: str, limit: int) -> str:
    """An alert-sized detail: `text` stripped and, when longer than `limit`,
    its head (e.g. `exit code 1: …`, up to 80 chars) plus the END of the tail
    joined by ` … ` — never longer than `limit`. A limit too small for both
    keeps only the end."""
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head_n = min(_HEAD_CHARS, max(0, limit - len(_SEP)) // 2)
    tail_n = limit - len(_SEP) - head_n
    if head_n <= 0 or tail_n <= 0:
        return text[-limit:]
    head = text[:head_n].rstrip()
    tail = text[-tail_n:].lstrip()
    return f"{head}{_SEP}{tail}"


def step_numbers(snapshot: object) -> dict[str, Any]:
    """#189: the live numbers of one stepper entry — a fresh dict of the
    non-None values of a progress snapshot (the restore capture, the ASR
    job's or the translator's), without the request's own `job_id` /
    `started_at`. Anything that is not a dict → `{}`."""
    if not isinstance(snapshot, dict):
        return {}
    return {
        k: v for k, v in snapshot.items() if v is not None and k not in _REQUEST_KEYS
    }


def exists_quietly(path: Union[str, "os.PathLike[str]"]) -> bool:
    """`Path(path).exists()`, with an unreadable entry (long path, dead network
    drive, permission: `OSError`) or an invalid path (e.g. an embedded NUL:
    `ValueError`) reported as "not there yet" instead of raising."""
    try:
        return Path(path).exists()
    except (OSError, ValueError):
        return False
