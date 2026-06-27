"""Headless Hub service entrypoint (no UI).

Loads hub.yaml from the platform config dir, opens the SQLite store next to it,
and runs the Hub poller + API until a stop signal. The interactive (Tauri) mode
reuses run_hub() directly instead.

The list of agents the Hub polls lives in the store's `servers` table — manage it
with `python -m taskpaw_v3.hub add-server / list-servers / ...` (see hub.__main__).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from taskpaw_v3.core.config import HubConfig, load_yaml
from taskpaw_v3.hub.server.app import run_hub
from taskpaw_v3.hub.server.store import HubStore


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home()) / "TaskPaw"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "TaskPaw"
    else:
        base = Path("/etc/taskpaw")
    return base / "hub.yaml"


def default_db_path(config_path: Path) -> Path:
    """The Hub's SQLite DB, kept alongside its config."""
    return config_path.with_name("hub.db")


def run_from_config(config_path: Path | None = None, db_path: Path | None = None) -> int:
    path = config_path or default_config_path()
    if not path.exists():
        print(f"No hub config at {path}", file=sys.stderr)
        return 1
    config: HubConfig = load_yaml(HubConfig, path)  # type: ignore[assignment]
    store = HubStore(db_path or default_db_path(path))
    run_hub(config, store, block=True)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_from_config()


if __name__ == "__main__":
    raise SystemExit(main())
