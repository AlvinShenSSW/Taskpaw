"""Agent runtime state — the persisted monotonic event-id counter.

The agent's event ids MUST be monotonic and persisted across restarts
(constitution §3), otherwise after a restart the agent re-issues low ids that
the Hub's `last_event_ids` cursor already passed, silently dropping every new
event. This is the V3 home for the V2 `state.json` counter.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("taskpaw.state")


def load_next_id(path: Path) -> int:
    """Read the next event id from disk; default 1 if missing/corrupt."""
    path = Path(path)
    if not path.exists():
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return max(1, int(data.get("next_event_id", 1)))
    except Exception as e:
        log.warning("Failed to load agent state %s, starting id at 1: %s", path, e)
        return 1


def save_next_id(path: Path, next_id: int) -> None:
    """Atomically persist the next event id (tmp + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"next_event_id": int(next_id)}), encoding="utf-8")
    os.replace(tmp, path)
