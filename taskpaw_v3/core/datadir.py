"""The agent's data directory (#192 C5): where a feature keeps its own files
(e.g. the translator's `subs-checkpoints/`).

A process-wide holder set ONLY by `run_agent`, from the folder of the
`config_path` it was given (the folder holding `agent.yaml`) — never from
`default_config_path()`, so a run without a config file (and every test) never
writes into the real `%APPDATA%` (#68). None = no persistence: a caller keeps
its state in memory. The tests' autouse fixture resets it to None.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
_data_dir: Optional[Path] = None


def set_data_dir(path: Optional[Path]) -> None:
    """Publish the data directory (None = no persistence). Creates nothing."""
    global _data_dir
    with _lock:
        _data_dir = path


def get_data_dir() -> Optional[Path]:
    """The published data directory, or None (not set, or no config file)."""
    with _lock:
        return _data_dir
