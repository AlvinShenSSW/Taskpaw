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
import sqlite3
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


def _is_sqlite_db(path: Path) -> bool:
    """True if `path` is a real, openable SQLite database (not absent / 0-byte /
    a stray file) — so a junk file beside the config can't block startup (Kimi)."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA schema_version")
        finally:
            conn.close()
        return True
    except Exception:
        return False


def _db_has_servers(path: Path) -> bool:
    """True if the resolved db already holds registered servers (real data).
    Retries on SQLITE_BUSY so write pressure doesn't make us miss a real
    conflict (Kimi)."""
    if not _is_sqlite_db(path):
        return False
    for attempt in range(3):
        try:
            conn = sqlite3.connect(str(path), timeout=2)
            try:
                # Check the table EXISTS first (via sqlite_master, which never
                # raises "no such table") — an unrelated SQLite file without a
                # `servers` table has no TaskPaw data, so it's NOT a real db that
                # should suppress the legacy guard (Codex).
                t = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='servers'"
                ).fetchone()
                if not t:
                    return False
                row = conn.execute("SELECT COUNT(*) FROM servers").fetchone()
                return bool(row and row[0] > 0)
            finally:
                conn.close()
        except sqlite3.OperationalError:
            # Locked/busy — retry; if we STILL can't read it, fail closed (False).
            # For the resolved db that means "no confirmed data" → the legacy
            # guard fires and the hub refuses to start rather than risk starting
            # against an unverified/empty db (Kimi).
            if attempt == 2:
                return False
            continue
        except Exception:
            return False
    return False


def legacy_db_conflict(config_path: Path, resolved_db: Path) -> Path | None:
    """Return the config-adjacent hub.db (OLD default) if it's a real db AND the
    resolved data_dir db has no servers yet — opening/starting the resolved db
    would silently abandon the operator's data. An EMPTY resolved db (e.g. just
    created by a management command) still counts as a conflict, so `run` keeps
    hard-failing afterwards (Codex). Else None."""
    legacy = default_db_path(config_path)
    resolved_db = Path(resolved_db)
    # Normalize before comparing — a relative data_dir resolving to the config
    # dir is the SAME file and must not be flagged as a conflict (Kimi).
    # The legacy must be a real TaskPaw db WITH servers (a stray/empty/non-TaskPaw
    # sqlite file shouldn't block startup); the resolved must NOT already have data.
    if legacy.resolve() == resolved_db.resolve() or not _db_has_servers(legacy):
        return None
    if _db_has_servers(resolved_db):
        return None
    return legacy


def run_from_config(config_path: Path | None = None, db_path: Path | None = None) -> int:
    path = config_path or default_config_path()
    if not path.exists():
        print(f"No hub config at {path}", file=sys.stderr)
        return 1
    config: HubConfig = load_yaml(HubConfig, path)  # type: ignore[assignment]
    resolved_db = db_path or db_path_for(config)
    # Only guard the DEFAULT data_dir path — an explicit --db is the operator
    # opting into a specific/fresh db, so don't block it (Codex). Hard-fail rather
    # than silently start empty (a missed log line would abandon servers/acks/
    # outbox after the default DB path moved) (Kimi).
    if db_path is None:
        legacy_db = legacy_db_conflict(path, resolved_db)
        if legacy_db:
            print(
                f"error: hub DB not found at {resolved_db}, but an older one exists at "
                f"{legacy_db}.\n"
                f"  Move it:   mv '{legacy_db}' '{resolved_db}'\n"
                f"  or point data_dir at its folder in {path}.\n"
                f"  To start fresh, pass --db {resolved_db}.",
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
