"""Hub SQLite store: servers, status_log, events, delivery_outbox.

Carries forward V2 #14 hardening: WAL + foreign_keys + busy_timeout, rollback on
write failure, the durable delivery outbox (pending/failed/dead_letter with the
due index), and canonical UTC outbox timestamps; local status history remains unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from taskpaw_v3.core.state import (
    StateError,
    StateRecord,
    integer,
    parse_cursor,
    strict_json,
)

from .outbox_migration import (
    RowError,
    apply_plan,
    backup_database,
    outbox_time,
    plan_migration,
    report_quarantine,
    validate_row,
)

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
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._init_schema()
        except BaseException:
            self._conn.close()
            raise

    def _init_schema(self) -> None:
        with self._lock:
            try:
                c = self._conn.cursor()
                self._conn.execute("BEGIN IMMEDIATE")
                plan = plan_migration(self._conn)
                existing = self._conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
                backup = (
                    backup_database(self.db_path) if plan.changed and existing else None
                )
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
                c.execute(
                    "CREATE TABLE IF NOT EXISTS upstream_status(server_id INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,status_json TEXT,last_seen TEXT,last_good_at TEXT,attempted_at TEXT,error_code TEXT,scan_before INTEGER,scan_done INTEGER NOT NULL DEFAULT 0)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS upstream_consumed(server_id INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,stream_id TEXT NOT NULL,highwater INTEGER NOT NULL)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS upstream_quarantine(id INTEGER PRIMARY KEY,server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,stream_id TEXT NOT NULL,boot_id TEXT NOT NULL,offered_highwater INTEGER NOT NULL,fingerprint TEXT NOT NULL,reason TEXT NOT NULL,ordinal INTEGER NOT NULL,event_id INTEGER,byte_length INTEGER NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,repeat_count INTEGER NOT NULL,UNIQUE(server_id,stream_id,boot_id,fingerprint,reason))"
                )
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_upstream_quarantine_age ON upstream_quarantine(last_seen,id)"
                )
                c.execute(
                    "CREATE TABLE IF NOT EXISTS upstream_summary(server_id INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE,evicted_count INTEGER NOT NULL,last_evicted_at TEXT NOT NULL,last_reason TEXT NOT NULL)"
                )
                had_cursors = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_cursors'"
                ).fetchone()
                c.execute(
                    "CREATE TABLE IF NOT EXISTS event_cursors (server_id INTEGER PRIMARY KEY REFERENCES servers(id) ON DELETE CASCADE, state TEXT NOT NULL, identity_json TEXT, boot_id TEXT, resume_floor INTEGER)"
                )
                if not had_cursors:
                    # Capture pre-upgrade evidence once, within the same initial
                    # history admission budget later shared with Poller recovery.
                    from .upstream_worker import (
                        STATUS_BYTES,
                        UpstreamError,
                        decode_status,
                    )

                    remaining, examined = 2 * 1024 * 1024, 0
                    for (sid,) in c.execute(
                        "SELECT id FROM servers ORDER BY id"
                    ).fetchall():
                        previous = None
                        if examined < 128 and remaining > 0:
                            previous = c.execute(
                                "SELECT CASE WHEN length(CAST(status_json AS BLOB))<=? THEN substr(CAST(status_json AS BLOB),1,?) ELSE NULL END FROM status_log WHERE server_id=? ORDER BY id DESC LIMIT 1",
                                (min(STATUS_BYTES, remaining), STATUS_BYTES, sid),
                            ).fetchone()
                            if previous:
                                examined += 1
                                remaining -= len(previous[0]) if previous[0] else 0
                        identity = None
                        if previous and previous[0]:
                            try:
                                cursor = parse_cursor(
                                    decode_status(previous[0])[0].get("event_cursor")
                                )
                                identity = json.dumps(
                                    {k: cursor[k] for k in ("server_id", "stream_id")}
                                )
                            except (
                                ValueError,
                                TypeError,
                                AttributeError,
                                UpstreamError,
                            ):
                                log.warning(
                                    "Historical cursor status refused server=%s", sid
                                )
                        c.execute(
                            "INSERT INTO event_cursors(server_id,state,identity_json) VALUES(?, 'unverified', ?)",
                            (sid, identity),
                        )
                    self._initial_history_remaining = (remaining, examined)
                apply_plan(self._conn, plan, backup)
                self._conn.commit()
                report_quarantine(plan)
            except BaseException:
                self._conn.rollback()
                raise

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
                assert cur.lastrowid is not None
                sid = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO event_cursors(server_id,state) VALUES(?, 'fresh')",
                    (sid,),
                )
                self._conn.commit()
                return sid
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

    # R06: admitted upstream snapshots and atomic event dispositions.
    def record_status(self, server_id: int, raw: str | None, error: str | None) -> None:
        from .upstream_worker import decode_status

        good = decode_status(raw)[1] if raw is not None else None
        now = outbox_time()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR IGNORE INTO upstream_status(server_id) VALUES(?)",
                    (server_id,),
                )
                if good is not None:
                    seen = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._conn.execute(
                        "INSERT INTO status_log(server_id,timestamp,reachable,status_json) VALUES(?,?,1,?)",
                        (server_id, seen, good),
                    )
                    self._conn.execute(
                        "UPDATE upstream_status SET status_json=?,last_seen=?,last_good_at=?,attempted_at=?,error_code=NULL,scan_done=1 WHERE server_id=?",
                        (good, seen, now, now, server_id),
                    )
                else:
                    self._conn.execute(
                        "UPDATE upstream_status SET attempted_at=?,error_code=? WHERE server_id=?",
                        (now, error or "upstream_failed", server_id),
                    )
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _receipt(
        self, sid: int, stream: str, boot: str, offered: int, row: dict
    ) -> None:
        now = outbox_time()
        self._conn.execute(
            "INSERT INTO upstream_quarantine(server_id,stream_id,boot_id,offered_highwater,fingerprint,reason,ordinal,event_id,byte_length,first_seen,last_seen,repeat_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(server_id,stream_id,boot_id,fingerprint,reason) DO UPDATE SET last_seen=excluded.last_seen,repeat_count=min(9223372036854775807,repeat_count+1)",
            (
                sid,
                stream,
                boot,
                offered,
                row["fingerprint"],
                row["reason"],
                row["ordinal"],
                row["event_id"],
                row["bytes"],
                now,
                now,
            ),
        )

    def _retain_receipts(self) -> None:
        cutoff = outbox_time(datetime.now(timezone.utc) - timedelta(days=7))
        expired = self._conn.execute(
            "SELECT id,server_id,reason FROM upstream_quarantine WHERE last_seen<? ORDER BY id",
            (cutoff,),
        ).fetchall()
        ids = {r[0] for r in expired}
        for (sid,) in self._conn.execute(
            "SELECT DISTINCT server_id FROM upstream_quarantine"
        ):
            rows = self._conn.execute(
                "SELECT id,server_id,reason FROM upstream_quarantine WHERE server_id=? ORDER BY last_seen DESC,id DESC LIMIT -1 OFFSET 256",
                (sid,),
            ).fetchall()
            expired.extend(r for r in rows if r[0] not in ids)
            ids.update(r[0] for r in rows)
        rows = self._conn.execute(
            "SELECT id,server_id,reason FROM upstream_quarantine ORDER BY last_seen DESC,id DESC"
        ).fetchall()
        remaining = [r for r in rows if r[0] not in ids]
        expired.extend(remaining[4096:])
        for row_id, sid, reason in expired:
            self._conn.execute("DELETE FROM upstream_quarantine WHERE id=?", (row_id,))
            self._conn.execute(
                "INSERT INTO upstream_summary(server_id,evicted_count,last_evicted_at,last_reason) VALUES(?,1,?,?) ON CONFLICT(server_id) DO UPDATE SET evicted_count=min(9223372036854775807,evicted_count+1),last_evicted_at=excluded.last_evicted_at,last_reason=excluded.last_reason",
                (sid, outbox_time(), reason),
            )

    def quarantine_summary(self, server_id: int) -> dict:
        with self._lock:
            count = self._conn.execute(
                "SELECT count(*) FROM upstream_quarantine WHERE server_id=?",
                (server_id,),
            ).fetchone()[0]
            row = self._conn.execute(
                "SELECT evicted_count,last_evicted_at,last_reason FROM upstream_summary WHERE server_id=?",
                (server_id,),
            ).fetchone()
            last = self._conn.execute(
                "SELECT reason FROM upstream_quarantine WHERE server_id=? ORDER BY last_seen DESC,id DESC LIMIT 1",
                (server_id,),
            ).fetchone()
            return {
                "retained": count,
                "evicted": integer(row[0], 0, (1 << 63) - 1) if row else 0,
                "last_reason": last[0] if last else row[2] if row else None,
            }

    def recover_statuses(self) -> None:
        """Bound only Python candidate admission, not SQLite VM field loading."""
        import hashlib

        from .upstream_worker import STATUS_BYTES, UpstreamError, decode_status

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                ids = [
                    r[0]
                    for r in self._conn.execute(
                        "SELECT id FROM servers WHERE enabled=1 ORDER BY id"
                    )
                ]
                for sid in ids:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO upstream_status(server_id) VALUES(?)",
                        (sid,),
                    )
                # Cache reads are bounded too; a corrupt cache is not a safe seed.
                for sid in ids:
                    row = self._conn.execute(
                        "SELECT substr(CAST(status_json AS BLOB),1,?) FROM upstream_status WHERE server_id=?",
                        (STATUS_BYTES + 1, sid),
                    ).fetchone()
                    if row and row[0] is not None:
                        try:
                            decode_status(row[0])
                        except UpstreamError:
                            self._conn.execute(
                                "UPDATE upstream_status SET status_json=NULL,last_good_at=NULL,error_code='status_store_invalid',scan_done=0,scan_before=NULL WHERE server_id=?",
                                (sid,),
                            )
                turn = getattr(self, "_legacy_turn", 0)
                ids = ids[turn:] + ids[:turn]
                budget, count = getattr(
                    self, "_initial_history_remaining", (2 * 1024 * 1024, 0)
                )
                self._initial_history_remaining = (2 * 1024 * 1024, 0)
                while ids and count < 128:
                    before_count = count
                    next_ids = []
                    for sid in ids:
                        if count >= 128:
                            break
                        state = self._conn.execute(
                            "SELECT scan_before,scan_done FROM upstream_status WHERE server_id=?",
                            (sid,),
                        ).fetchone()
                        if state[1]:
                            continue
                        row = self._conn.execute(
                            "SELECT id,substr(CAST(timestamp AS BLOB),1,65),length(CAST(status_json AS BLOB)),CASE WHEN length(CAST(status_json AS BLOB))<=? THEN substr(CAST(status_json AS BLOB),1,?) ELSE NULL END FROM status_log WHERE server_id=? AND reachable=1 AND id<? ORDER BY id DESC LIMIT 1",
                            (
                                min(STATUS_BYTES, budget),
                                STATUS_BYTES,
                                sid,
                                state[0] if state[0] is not None else (1 << 63) - 1,
                            ),
                        ).fetchone()
                        if row is None:
                            self._conn.execute(
                                "UPDATE upstream_status SET scan_done=1 WHERE server_id=?",
                                (sid,),
                            )
                            continue
                        row_id, stamp, length, candidate = row
                        if (
                            candidate is None
                            and length is not None
                            and length <= STATUS_BYTES
                        ):
                            # In-limit candidate withheld by the remaining byte
                            # allowance: do not advance its durable continuation.
                            next_ids.append(sid)
                            continue
                        admitted = (
                            len(candidate)
                            if isinstance(candidate, bytes)
                            and length is not None
                            and length <= STATUS_BYTES
                            else 0
                        )
                        if admitted > budget:
                            next_ids.append(sid)
                            continue
                        budget -= admitted
                        count += 1
                        self._conn.execute(
                            "UPDATE upstream_status SET scan_before=? WHERE server_id=?",
                            (row_id, sid),
                        )
                        try:
                            if (
                                not isinstance(candidate, bytes)
                                or length > STATUS_BYTES
                            ):
                                raise UpstreamError("body_oversize")
                            _, good = decode_status(candidate)
                            try:
                                seen = (
                                    stamp.decode("utf-8") if stamp is not None else None
                                )
                                if seen is not None:
                                    datetime.strptime(seen, "%Y-%m-%d %H:%M:%S")
                            except (ValueError, UnicodeError):
                                seen = None
                            self._conn.execute(
                                "UPDATE upstream_status SET status_json=?,last_seen=?,last_good_at=NULL,scan_done=1 WHERE server_id=?",
                                (good, seen, sid),
                            )
                        except UpstreamError as exc:
                            self._conn.execute(
                                "UPDATE upstream_status SET error_code=coalesce(error_code,'historical_status_invalid') WHERE server_id=?",
                                (sid,),
                            )
                            self._receipt(
                                sid,
                                "legacy",
                                "legacy",
                                0,
                                {
                                    "fingerprint": hashlib.sha256(
                                        f"{sid}:{row_id}".encode()
                                    ).hexdigest(),
                                    "reason": exc.reason,
                                    "ordinal": row_id,
                                    "event_id": None,
                                    "bytes": length or 0,
                                },
                            )
                            next_ids.append(sid)
                    if budget <= 0 or count == before_count:
                        break
                    ids = next_ids
                self._legacy_turn = (turn + 1) % max(1, len(self.list_servers()))
                self._retain_receipts()
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def upstream_statuses(self) -> list[dict]:
        from .upstream_worker import STATUS_BYTES, UpstreamError, decode_status

        with self._lock:
            cur = self._conn.execute(
                "SELECT server_id,substr(CAST(status_json AS BLOB),1,?),substr(last_seen,1,64),substr(last_good_at,1,64),substr(attempted_at,1,64),substr(error_code,1,64),scan_done FROM upstream_status",
                (STATUS_BYTES + 1,),
            )
            result = []
            for sid, candidate, seen, good_at, attempted, error, done in cur:
                raw = parsed = None
                if candidate is not None:
                    try:
                        parsed, raw = decode_status(candidate)
                    except UpstreamError:
                        error = "status_store_invalid"
                for key, value in (
                    ("seen", seen),
                    ("good", good_at),
                    ("attempt", attempted),
                ):
                    try:
                        if value is not None:
                            value.encode("utf-8")
                            if key == "seen":
                                datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                            elif outbox_time(datetime.fromisoformat(value)) != value:
                                raise ValueError()
                    except (ValueError, TypeError, UnicodeError):
                        if key == "seen":
                            seen = None
                        elif key == "good":
                            good_at = None
                        else:
                            attempted = None
                        error = "status_store_invalid"
                if error is not None and not re.fullmatch("[a-z_]{1,64}", error):
                    error = "status_store_invalid"
                result.append(
                    {
                        "id": sid,
                        "status_json": raw,
                        "parsed_status": parsed,
                        "last_seen": seen,
                        "last_good_at": good_at,
                        "attempted_at": attempted,
                        "error_code": error,
                        "scan_done": bool(done),
                    }
                )
            return result

    def commit_upstream_batch(
        self, server: dict, batch: dict, active: bool
    ) -> dict[int, int]:
        from .upstream_worker import canonical, evidence

        sid = server["id"]
        proof = parse_cursor(batch["proof"])
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                binding = self.get_event_cursor(sid)
                if (
                    binding["state"] != "bound"
                    or binding["identity"]
                    != {k: proof[k] for k in ("server_id", "stream_id")}
                    or binding["boot_id"] != proof["boot_id"]
                    or binding["resume_floor"] != proof["resume_floor"]
                ):
                    raise StateError("event_cursor_mismatch")
                acks = self.read_acks()
                floor = self.event_floor(sid, acks)
                if proof["offered_highwater"] < floor:
                    raise StateError("event_cursor_mismatch")
                receipts = list(batch["receipts"])
                for ordinal, ev in enumerate(batch["events"]):
                    if ev["id"] <= floor:
                        old = self._conn.execute(
                            "SELECT monitor,message,level FROM events WHERE server_id=? AND event_id=?",
                            (sid, ev["id"]),
                        ).fetchone()
                        if (
                            old is None
                            or tuple(ev.get(k) for k in ("monitor", "message", "level"))
                            != old
                        ):
                            receipts.append(
                                evidence(ev, ordinal, "event_stale_conflict")
                            )
                        continue
                    self._conn.execute(
                        "INSERT OR IGNORE INTO events(server_id,event_id,monitor,message,level,received_at) VALUES(?,?,?,?,?,?)",
                        (
                            sid,
                            ev["id"],
                            ev.get("monitor"),
                            ev.get("message"),
                            ev.get("level"),
                            _dt(),
                        ),
                    )
                    if active:
                        payload = canonical(
                            {
                                "text": f"TaskPaw Event | {server['name']}: {ev.get('message', 'Unknown event')}"
                            }
                        )
                        self._conn.execute(
                            "INSERT OR IGNORE INTO delivery_outbox(server_name,payload_json,kind,delivery_state,attempts,last_error,next_attempt_at,created_at,dedupe_key) VALUES(?,?,'event','pending',0,NULL,?,?,?)",
                            (
                                server["name"],
                                payload,
                                outbox_time(),
                                outbox_time(),
                                f"{sid}:{ev['id']}",
                            ),
                        )
                for row in receipts:
                    self._receipt(
                        sid,
                        proof["stream_id"],
                        proof["boot_id"],
                        proof["offered_highwater"],
                        row,
                    )
                acks[sid] = proof["offered_highwater"]
                self._conn.execute(
                    "INSERT INTO upstream_consumed(server_id,stream_id,highwater) VALUES(?,?,?) ON CONFLICT(server_id) DO UPDATE SET stream_id=excluded.stream_id,highwater=excluded.highwater",
                    (sid, proof["stream_id"], acks[sid]),
                )
                self._conn.execute(
                    "INSERT INTO config(key,value) VALUES('last_event_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(acks),),
                )
                self._retain_receipts()
                self._conn.commit()
                return acks
            except BaseException:
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
                "DELETE FROM status_log WHERE timestamp < datetime('now','localtime',?) AND NOT EXISTS (SELECT 1 FROM upstream_status u WHERE u.server_id=status_log.server_id AND u.scan_done=0)",
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
                        outbox_time(next_attempt_at),
                        outbox_time(),
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
                "AND NOT EXISTS (SELECT 1 FROM outbox_quarantine q WHERE q.delivery_id=delivery_outbox.id) "
                "AND next_attempt_at <= ? ORDER BY next_attempt_at, id LIMIT ?",
                (outbox_time(now), limit),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def quarantine_delivery(self, delivery_id: int, reason: str, column: str) -> None:
        """Persist a malformed row's exclusion without deleting its original data."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO outbox_quarantine(delivery_id,reason,field,quarantined_at) VALUES(?,?,?,?)",
                    (delivery_id, reason, column, outbox_time()),
                )
                self._conn.commit()
                if cur.rowcount:
                    log.error(
                        "Outbox quarantined id=%s reason=%s field=%s",
                        delivery_id,
                        reason,
                        column,
                    )
            except Exception:
                self._conn.rollback()
                raise

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
                    (attempts, last_error, outbox_time(next_attempt_at), delivery_id),
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
        # created_at is UTC-aware ISO (_dt), so the cutoff MUST be UTC too — a naive
        # host-local cutoff would be off by the host's UTC offset and mis-prune on a
        # non-UTC Hub (#152). UTC follows no fixed tz; it's the Hub's absolute clock.
        cutoff = outbox_time(datetime.now(timezone.utc) - timedelta(days=days))
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "SELECT * FROM delivery_outbox WHERE delivery_state='dead_letter' "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_quarantine q WHERE q.delivery_id=delivery_outbox.id) "
                    "AND created_at < ?",
                    (cutoff,),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                quarantined = []
                expired = []
                for row in rows:
                    try:
                        created, _, _ = validate_row(row)
                    except RowError as exc:
                        quarantined.append((row["id"], exc.reason, exc.column))
                    else:
                        if created < cutoff:
                            expired.append((row["id"],))
                timestamp = outbox_time()
                self._conn.executemany(
                    "INSERT INTO outbox_quarantine(delivery_id,reason,field,quarantined_at) VALUES(?,?,?,?)",
                    [
                        (i, reason, column, timestamp)
                        for i, reason, column in quarantined
                    ],
                )
                self._conn.executemany(
                    "DELETE FROM delivery_outbox WHERE id=?", expired
                )
                self._conn.commit()
                for row_id, reason, column in quarantined:
                    log.error(
                        "Outbox quarantined id=%s reason=%s field=%s",
                        row_id,
                        reason,
                        column,
                    )
            except Exception:
                self._conn.rollback()
                raise

    def get_event_cursor(self, server_id: int) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT state,identity_json,boot_id,resume_floor FROM event_cursors WHERE server_id=?",
                (server_id,),
            ).fetchone()
            if row is None:
                return {
                    "state": "unverified",
                    "identity": None,
                    "boot_id": None,
                    "resume_floor": None,
                }
            try:
                identity = strict_json(row[1]) if row[1] else None
                if identity is not None and (
                    not isinstance(identity, dict)
                    or set(identity) != {"server_id", "stream_id"}
                    or not all(isinstance(v, str) and v for v in identity.values())
                ):
                    raise ValueError("invalid identity")
                if row[0] not in ("fresh", "unverified", "bound") or (
                    row[0] == "bound" and identity is None
                ):
                    raise ValueError("invalid state")
                if identity is not None:
                    StateRecord.parse(
                        {
                            "version": 2,
                            **identity,
                            "lineage_origin": "legacy_migration",
                            "next_event_id": 1,
                        }
                    )
                if row[2] is None:
                    if row[3] is not None:
                        raise ValueError("incomplete boot evidence")
                else:
                    floor = integer(row[3], 0, (1 << 63) - 1)
                    parse_cursor(
                        {
                            "version": 1,
                            "durable": True,
                            **(identity or {}),
                            "boot_id": row[2],
                            "resume_floor": floor,
                            "offered_highwater": floor,
                            "next_event_id": floor + 1,
                        }
                    )
            except (ValueError, TypeError) as exc:
                raise StateError("cursor_store_invalid") from exc
            return {
                "state": row[0],
                "identity": identity,
                "boot_id": row[2],
                "resume_floor": row[3],
            }

    def read_acks(self) -> dict[int, int]:
        raw = self.get_config("last_event_ids", "")
        if not raw:
            return {}
        try:
            value = strict_json(raw)
            if not isinstance(value, dict):
                raise ValueError("invalid ack map")
            result = {}
            for key, number in value.items():
                if (
                    not isinstance(key, str)
                    or not key.isdecimal()
                    or str(int(key)) != key
                    or int(key) < 1
                ):
                    raise ValueError("invalid ack key")
                result[int(key)] = integer(number, -1, (1 << 63) - 1)
            return result
        except (ValueError, TypeError) as exc:
            raise StateError("cursor_store_invalid") from exc

    def event_floor(self, server_id: int, acks: dict[int, int]) -> int:
        with self._lock:
            floor = integer(acks.get(server_id, -1), -1, (1 << 63) - 1)
            maximum = self._conn.execute(
                "SELECT MAX(event_id), MIN(event_id) FROM events WHERE server_id=?",
                (server_id,),
            ).fetchone()
            if maximum[0] is not None:
                integer(maximum[1])
                floor = max(floor, integer(maximum[0], 1, (1 << 63) - 1))
            prefix = f"{server_id}:"
            for (key,) in self._conn.execute(
                "SELECT dedupe_key FROM delivery_outbox WHERE dedupe_key LIKE ?",
                (prefix + "%",),
            ):
                suffix = key[len(prefix) :]
                if not suffix.isdecimal() or str(int(suffix)) != suffix:
                    raise StateError("cursor_store_invalid")
                floor = max(floor, integer(int(suffix), 1, (1 << 63) - 1))
            consumed = self._conn.execute(
                "SELECT stream_id,highwater FROM upstream_consumed WHERE server_id=?",
                (server_id,),
            ).fetchone()
            if consumed:
                binding = self.get_event_cursor(server_id)
                if (
                    not binding["identity"]
                    or binding["identity"]["stream_id"] != consumed[0]
                ):
                    raise StateError("cursor_store_invalid")
                floor = max(floor, integer(consumed[1], 0, (1 << 63) - 1))
            return floor

    def commit_event_cursor(
        self,
        server_id: int,
        binding: dict,
        acks: dict[int, int],
        *,
        require_disabled: bool = False,
        diagnostic: str | None = None,
    ) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                server = self.get_server(server_id)
                if server is None or (require_disabled and server["enabled"]):
                    raise StateError("cursor_adoption_requires_disabled_server")
                if diagnostic is not None:
                    self._conn.execute(
                        "INSERT INTO config(key,value) VALUES(?,?)",
                        ("last_event_ids.fault-" + secrets.token_hex(16), diagnostic),
                    )
                self._conn.execute(
                    "INSERT INTO event_cursors(server_id,state,identity_json,boot_id,resume_floor) VALUES(?,?,?,?,?) ON CONFLICT(server_id) DO UPDATE SET state=excluded.state,identity_json=excluded.identity_json,boot_id=excluded.boot_id,resume_floor=excluded.resume_floor",
                    (
                        server_id,
                        binding["state"],
                        json.dumps(binding["identity"]),
                        binding.get("boot_id"),
                        binding.get("resume_floor"),
                    ),
                )
                self._conn.execute(
                    "INSERT INTO config(key,value) VALUES('last_event_ids',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(acks),),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
