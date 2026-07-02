"""Hub SQLite store: servers, status_log, events, delivery_outbox.

Carries forward V2 #14 hardening: WAL + foreign_keys + busy_timeout, rollback on
write failure, the durable delivery outbox (pending/failed/dead_letter with the
due index), and local-ISO timestamps compared lexically (consistent format).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("taskpaw.hub")

# A SQLite identifier we're willing to splice into SQL (table names are always
# code-controlled here, but validate so the store never establishes an
# injection-prone pattern — Kimi).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LEGACY_EVENTS_RE = re.compile(r"^events_v2_legacy(_\d+)?$")


def _safe_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _dt(value: Optional[datetime] = None) -> str:
    """UTC ISO-8601 — tz-aware and lexically sortable, so comparisons survive
    DST changes / clock jumps (unlike naive local time)."""
    return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")


class HubStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=10
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            c = self._conn.cursor()
            # Migrate existing tables FIRST — before any CREATE INDEX — so an index
            # (e.g. delivery_outbox.dedupe_key) is never built on a column a
            # pre-existing V2/old-V3 table doesn't have yet (Codex).
            self._migrate(c)
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5680,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS status_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    reachable INTEGER NOT NULL,
                    status_json TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    monitor TEXT,
                    message TEXT,
                    level TEXT,
                    received_at TEXT NOT NULL,
                    UNIQUE(server_id, event_id)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('event', 'summary')),
                    delivery_state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (delivery_state IN ('pending', 'failed', 'dead_letter')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    next_attempt_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    dead_letter_alerted INTEGER NOT NULL DEFAULT 0,
                    dedupe_key TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due "
                "ON delivery_outbox(delivery_state, next_attempt_at)"
            )
            # Idempotent enqueue: at-least-once replay (crash before ack persist)
            # must not create duplicate OpenClaw deliveries.
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_dedupe "
                "ON delivery_outbox(dedupe_key) WHERE dedupe_key IS NOT NULL"
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            # status_log grows one row per server per poll; index the access paths
            # (latest-per-server + prune-by-time) to avoid full scans (Kimi).
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_log_server_time "
                "ON status_log(server_id, timestamp, id)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_log_time "
                "ON status_log(timestamp)"
            )
            # Partial index for the last_seen (last reachable) subquery.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_log_reachable "
                "ON status_log(server_id, timestamp, id) WHERE reachable = 1"
            )
            self._conn.commit()

    def _legacy_event_tables(self) -> list[str]:
        """Names of preserved V2 event tables (events_v2_legacy[_N]) currently
        present — they keep an FK to servers and need cleanup on remove_server."""
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'events_v2_legacy%'"
        ).fetchall()
        # Strict allowlist — never splice an arbitrary sqlite_master name into SQL.
        return [r[0] for r in rows if _LEGACY_EVENTS_RE.match(r[0])]

    def _migrate(self, c) -> None:
        """Bring EXISTING tables up to the current schema (CREATE IF NOT EXISTS
        won't alter them) — runs before the CREATEs/indexes. data_dir defaults to
        ~/.taskpaw-hub, which may already hold a V2 hub.db or an early-V3 one;
        without this the first poll/open crashes on a missing/renamed column (#38
        review). servers is column-compatible with V2, so it needs no change."""

        def cols(table: str) -> set[str]:
            return {
                r[1]
                for r in c.execute(
                    f"PRAGMA table_info({_safe_ident(table)})"
                ).fetchall()
            }

        slog = cols("status_log")
        if slog:  # table pre-existed (else the later CREATE makes the right shape)
            if "payload_json" in slog and "status_json" not in slog:
                c.execute(
                    "ALTER TABLE status_log RENAME COLUMN payload_json TO status_json"
                )
            if "status_json" not in slog and "payload_json" not in slog:
                c.execute("ALTER TABLE status_log ADD COLUMN status_json TEXT")
            if "reachable" not in slog:
                # V2 only logged reachable agents → legacy rows are reachable=1.
                c.execute(
                    "ALTER TABLE status_log ADD COLUMN reachable INTEGER NOT NULL DEFAULT 1"
                )

        # V2 `events` has a different shape (no event_id) and isn't read by
        # OpenClaw. PRESERVE it as events_v2_legacy (no silent data loss — Kimi)
        # and let the later CREATE rebuild the V3 events table.
        ev = cols("events")
        if ev and "event_id" not in ev:
            # Rename to the first FREE legacy name so we never DROP (lose) rows,
            # even if a prior events_v2_legacy already exists (Codex/Kimi).
            name, n = "events_v2_legacy", 1
            while cols(name):
                n += 1
                name = f"events_v2_legacy_{n}"
            c.execute(f"ALTER TABLE events RENAME TO {_safe_ident(name)}")
            log.warning(
                "Migrated V2 'events' table to '%s' (V3 uses a new events "
                "schema; old rows preserved there)",
                name,
            )

        # An old delivery_outbox without dedupe_key would break the dedupe index.
        ob = cols("delivery_outbox")
        if ob:
            if "dedupe_key" not in ob:
                c.execute("ALTER TABLE delivery_outbox ADD COLUMN dedupe_key TEXT")
            if "dead_letter_alerted" not in ob:
                c.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN "
                    "dead_letter_alerted INTEGER NOT NULL DEFAULT 0"
                )

    # ── config ────────────────────────────────────────────────────────────
    def get_config(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM config WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set_config(self, key: str, value: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO config(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ── servers ───────────────────────────────────────────────────────────
    def add_server(
        self, name: str, ip: str, port: int = 5680, enabled: bool = True
    ) -> int:
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO servers(name, ip, port, enabled) VALUES(?, ?, ?, ?)",
                    (name, ip, port, int(enabled)),
                )
                self._conn.commit()
                assert cur.lastrowid is not None  # set by INSERT
                return cur.lastrowid
            except Exception:
                self._conn.rollback()
                raise

    def list_servers(self) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, name, ip, port, enabled FROM servers ORDER BY id"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_server_enabled(self, server_id: int, enabled: bool) -> bool:
        """Enable/disable polling of a server. Returns True if a row changed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE servers SET enabled=? WHERE id=?", (int(enabled), server_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_server(
        self,
        server_id: int,
        *,
        name: Optional[str] = None,
        ip: Optional[str] = None,
        port: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> bool:
        """Edit a server's name/ip/port/enabled from the dashboard (#124), all in
        ONE UPDATE (one transaction) so a partial edit can't be left behind (Kimi).
        Only the given fields change. Returns True if the row exists; raises
        sqlite3.IntegrityError on a duplicate name (UNIQUE) → the API 400s."""
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            sets.append("name=?")
            params.append(name)
        if ip is not None:
            sets.append("ip=?")
            params.append(ip)
        if port is not None:
            sets.append("port=?")
            params.append(int(port))
        if enabled is not None:
            sets.append("enabled=?")
            params.append(int(enabled))
        if not sets:
            return self.get_server(server_id) is not None
        params.append(server_id)
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"UPDATE servers SET {', '.join(sets)} WHERE id=?", params
                )
                self._conn.commit()
                return cur.rowcount > 0
            except Exception:
                self._conn.rollback()
                raise

    def get_server(self, server_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, name, ip, port, enabled FROM servers WHERE id=?",
                (server_id,),
            )
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None

    def remove_server(self, server_id: int) -> bool:
        """Delete a server and ALL its child rows. We delete status_log/events
        EXPLICITLY rather than rely on ON DELETE CASCADE, because a migrated V2
        table has FKs without cascade (→ FK-violation) — and delivery_outbox keys
        on server_name with no FK at all (Kimi). Returns True if a row was removed."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT name FROM servers WHERE id=?", (server_id,)
                ).fetchone()
                if row is None:
                    return False
                self._conn.execute(
                    "DELETE FROM status_log WHERE server_id=?", (server_id,)
                )
                self._conn.execute("DELETE FROM events WHERE server_id=?", (server_id,))
                # Migrated V2 events_v2_legacy retains an FK to servers — clear its
                # rows too or DELETE servers raises FK-violation (Codex/Kimi).
                for legacy in self._legacy_event_tables():
                    self._conn.execute(
                        f"DELETE FROM {_safe_ident(legacy)} WHERE server_id=?",
                        (server_id,),
                    )
                self._conn.execute(
                    "DELETE FROM delivery_outbox WHERE server_name=?", (row[0],)
                )
                cur = self._conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
                self._conn.commit()
                return cur.rowcount > 0
            except Exception:
                self._conn.rollback()
                raise

    # ── status_log (OpenClaw compat: status.md + 24h history, #38) ──────────
    def log_status(
        self, server_id: int, reachable: bool, status_json: Optional[str] = None
    ) -> None:
        """Append a status snapshot for a server (one row per poll). Timestamp is
        SQLite localtime to match V2's status_log so OpenClaw's queries work."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO status_log(server_id, timestamp, reachable, status_json) "
                    "VALUES(?, datetime('now','localtime'), ?, ?)",
                    # Coalesce None→'{}' so a migrated V2 table (status_json NOT NULL)
                    # doesn't IntegrityError on an unreachable snapshot (Kimi).
                    (
                        server_id,
                        int(reachable),
                        status_json if status_json is not None else "{}",
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def latest_statuses(self) -> list[dict[str, Any]]:
        """Latest status row per registered server (LEFT JOIN, so never-polled
        servers appear too) — the source for status.md."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT s.id, s.name, sl.reachable, sl.status_json, sl.timestamp,
                    (SELECT timestamp FROM status_log
                     WHERE server_id = s.id AND reachable = 1
                     ORDER BY timestamp DESC, id DESC LIMIT 1) AS last_seen
                FROM servers s
                LEFT JOIN status_log sl ON sl.id = (
                    SELECT id FROM status_log WHERE server_id = s.id
                    ORDER BY timestamp DESC, id DESC LIMIT 1)
                WHERE s.enabled = 1
                ORDER BY s.id
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def prune_status_logs(self, days: int = 7) -> int:
        """Drop status_log rows older than `days` (bounded history). Returns the
        number deleted. days <= 0 means keep all (no-op) — matches the config
        contract, so a stray prune(0) can't wipe history (Kimi)."""
        if days <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM status_log WHERE timestamp < datetime('now','localtime',?)",
                (f"-{int(days)} days",),
            )
            self._conn.commit()
            return cur.rowcount

    # ── events ────────────────────────────────────────────────────────────
    def recent_events(
        self,
        server_id: Optional[int] = None,
        level: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Most-recent events across servers (newest first) for the Hub dashboard's
        event log (#44). Joins the server name and optionally filters by server
        and/or level. `limit` is clamped by the caller (the route)."""
        clauses: list[str] = []
        params: list[Any] = []
        if server_id is not None:
            clauses.append("e.server_id = ?")
            params.append(int(server_id))
        if level:
            clauses.append("e.level = ?")
            params.append(str(level))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            cur = self._conn.execute(
                "SELECT e.event_id, s.id AS server_id, s.name AS server, e.monitor, "
                "e.message, e.level, e.received_at "
                "FROM events e JOIN servers s ON s.id = e.server_id"
                f"{where} ORDER BY e.received_at DESC, e.id DESC LIMIT ?",
                params,
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def store_event(self, server_id: int, ev: dict) -> None:
        """Idempotent on (server_id, event_id) — at-least-once delivery may
        re-store after a crash; the UNIQUE constraint makes that a no-op."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO events"
                    "(server_id, event_id, monitor, message, level, received_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        server_id,
                        int(ev.get("id", -1)),
                        ev.get("monitor"),
                        ev.get("message"),
                        ev.get("level"),
                        _dt(),
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ── outbox ────────────────────────────────────────────────────────────
    def enqueue_delivery(
        self,
        server_name: str,
        kind: str,
        payload_json: str,
        delivery_state: str = "pending",
        attempts: int = 0,
        last_error: Optional[str] = None,
        next_attempt_at: Optional[datetime] = None,
        dedupe_key: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a delivery. When `dedupe_key` is given, the insert is
        idempotent (INSERT OR IGNORE on the unique partial index), so an
        at-least-once replay after a crash does not double-deliver to OpenClaw.

        Returns the new row id, or ``None`` when a dedupe_key collision made the
        INSERT OR IGNORE a no-op (no row inserted → ``lastrowid`` is meaningless).
        """
        verb = "INSERT OR IGNORE INTO" if dedupe_key is not None else "INSERT INTO"
        with self._lock:
            try:
                cur = self._conn.execute(
                    f"{verb} delivery_outbox"
                    "(server_name, payload_json, kind, delivery_state, attempts, "
                    " last_error, next_attempt_at, created_at, dedupe_key) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        server_name,
                        payload_json,
                        kind,
                        delivery_state,
                        attempts,
                        last_error,
                        _dt(next_attempt_at),
                        _dt(),
                        dedupe_key,
                    ),
                )
                self._conn.commit()
                # INSERT OR IGNORE that hit the dedupe index inserts no row
                # (rowcount == 0) → lastrowid is stale/meaningless; report None.
                return cur.lastrowid if cur.rowcount else None
            except Exception:
                self._conn.rollback()
                raise

    def due_deliveries(
        self, now: Optional[datetime] = None, limit: int = 10
    ) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, server_name, payload_json, kind, delivery_state, attempts, "
                "       last_error, next_attempt_at, created_at, dead_letter_alerted "
                "FROM delivery_outbox WHERE delivery_state IN ('pending','failed') "
                "AND next_attempt_at <= ? ORDER BY next_attempt_at, id LIMIT ?",
                (_dt(now), limit),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def delete_delivery(self, delivery_id: int) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM delivery_outbox WHERE id=?", (delivery_id,)
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def mark_delivery_failed(
        self,
        delivery_id: int,
        attempts: int,
        last_error: str,
        next_attempt_at: datetime,
    ) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE delivery_outbox SET delivery_state='failed', attempts=?, "
                    "last_error=?, next_attempt_at=? WHERE id=?",
                    (attempts, last_error, _dt(next_attempt_at), delivery_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def mark_delivery_dead_letter(
        self, delivery_id: int, attempts: int, last_error: str
    ) -> bool:
        """Mark dead-lettered; return True if a local alert is due (once)."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT dead_letter_alerted FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return False
                should_alert = row[0] == 0
                self._conn.execute(
                    "UPDATE delivery_outbox SET delivery_state='dead_letter', attempts=?, "
                    "last_error=?, dead_letter_alerted=1 WHERE id=?",
                    (attempts, last_error, delivery_id),
                )
                self._conn.commit()
                return should_alert
            except Exception:
                self._conn.rollback()
                raise

    def prune_dead_letters(self, days: int = 7) -> None:
        cutoff = _dt(datetime.now() - timedelta(days=days))
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM delivery_outbox WHERE delivery_state='dead_letter' "
                    "AND created_at < ?",
                    (cutoff,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
