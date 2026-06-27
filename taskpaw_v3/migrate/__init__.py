"""V2 → V3 migration (design §8).

Read-only: parses a V2 `config.json` (taskpaw.py `AppConfig`) plus `state.json`
and produces a V3 `MigrationPlan` (monitors[] in `{type_id, name, config}` shape +
the event-id cursor). It NEVER writes anything — the caller previews the diff and
decides. MacSubs (macsubs.py) is intentionally excluded (being retired).
"""

from __future__ import annotations

from taskpaw_v3.migrate.migrator import (
    MigrationPlan,
    MigratedMonitor,
    MigrationWarning,
    migrate_config,
    migrate_state,
    plan_migration,
)

__all__ = [
    "MigrationPlan",
    "MigratedMonitor",
    "MigrationWarning",
    "migrate_config",
    "migrate_state",
    "plan_migration",
]
