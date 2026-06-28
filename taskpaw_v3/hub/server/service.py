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
    """The Hub's SQLite DB, kept alongside its config (used when no config is
    loaded yet, e.g. the admin CLI). Once a HubConfig is loaded, data_dir wins."""
    return config_path.with_name("hub.db")


def db_path_for(config: HubConfig) -> Path:
    """hub.db lives in HubConfig.data_dir (default ~/.taskpaw-hub) so it sits
    next to status.md where OpenClaw reads both (#38)."""
    return Path(config.data_dir).expanduser() / "hub.db"


def run_from_config(config_path: Path | None = None, db_path: Path | None = None) -> int:
    path = config_path or default_config_path()
    if not path.exists():
        print(f"No hub config at {path}", file=sys.stderr)
        return 1
    config: HubConfig = load_yaml(HubConfig, path)  # type: ignore[assignment]
    resolved_db = db_path or db_path_for(config)
    # Loud warning if a hub.db sat next to the config (old default) but the
    # data_dir one doesn't exist yet — we'd otherwise start empty, silently
    # abandoning servers/acks/outbox (Kimi). The operator can move it or set data_dir.
    legacy_db = default_db_path(path)
    if not resolved_db.exists() and legacy_db.exists() and legacy_db != resolved_db:
        # Hard-fail rather than silently start empty (a missed log line would
        # abandon all servers/acks/outbox after the default DB path moved) (Kimi).
        print(
            f"error: hub DB not found at {resolved_db}, but an older one exists at "
            f"{legacy_db}.\n"
            f"  Move it:   mv '{legacy_db}' '{resolved_db}'\n"
            f"  or point data_dir at its folder in {path}.\n"
            f"  To start fresh, create an empty {resolved_db} (e.g. via "
            f"`python -m taskpaw_v3.hub add-server ...`).",
            file=sys.stderr)
        return 1
    store = HubStore(resolved_db)
    run_hub(config, store, block=True)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return run_from_config()


if __name__ == "__main__":
    raise SystemExit(main())
