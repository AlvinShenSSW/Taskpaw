"""Versioned outbox classification and offline recovery; never infer a legacy zone."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = 1
log = logging.getLogger("taskpaw.hub")
_TIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?\Z"
)
_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})\Z")
_REQUIRED = {
    "id",
    "server_name",
    "payload_json",
    "kind",
    "delivery_state",
    "attempts",
    "last_error",
    "next_attempt_at",
    "created_at",
}


class MigrationError(ValueError):
    """A fixed diagnostic code safe to show without stored contents."""


class RowError(MigrationError):
    def __init__(self, reason: str, column: str) -> None:
        self.reason, self.column = reason, column
        super().__init__(reason)


def outbox_time(value: datetime | None = None) -> str:
    value = value if value is not None else datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise MigrationError("aware_time_required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def source_timezone(
    zone: str | None = None, tzfile: Path | None = None
) -> tuple[tzinfo | None, str | None]:
    if zone is not None and tzfile is not None:
        raise MigrationError("conflicting_timezone_sources")
    if tzfile is not None:
        try:
            data = tzfile.read_bytes()
            from io import BytesIO

            return ZoneInfo.from_file(BytesIO(data)), "tzif-sha256:" + hashlib.sha256(
                data
            ).hexdigest()
        except (OSError, ValueError, EOFError):
            raise MigrationError("invalid_timezone_file") from None
    if zone is None:
        return None, None
    if zone == "UTC":
        return timezone.utc, "UTC"
    matched = _OFFSET.fullmatch(zone)
    if matched:
        hours, minutes = int(matched[2]), int(matched[3])
        if hours > 23 or minutes > 59:
            raise MigrationError("invalid_timezone_source")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(delta if matched[1] == "+" else -delta), zone
    try:
        return ZoneInfo(zone), zone
    except ZoneInfoNotFoundError:
        raise MigrationError("timezone_data_unavailable") from None
    except ValueError:
        raise MigrationError("invalid_timezone_source") from None


def normalize_time(value: Any, source: tzinfo | None, column: str) -> str:
    if not isinstance(value, str) or _TIME.fullmatch(value) is None:
        raise RowError("invalid_time", column)
    try:
        # Python 3.10 accepts only three/six fractional digits; explicitly pad
        # the supported precision so 3.10 and newer parsers have the same input.
        text = re.sub(r"\.(\d+)", lambda m: "." + m[1].ljust(6, "0"), value)
        offset = re.search(r"[+-](\d{2}):(\d{2})(?::(\d{2})(?:\.\d{6})?)?$", text)
        if offset and (
            int(offset[1]) > 23 or int(offset[2]) > 59 or int(offset[3] or 0) > 59
        ):
            raise RowError("invalid_time", column)
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
        if parsed.tzinfo is not None:
            return outbox_time(parsed)
        if source is None:
            raise RowError("source_timezone_required", column)
        candidates = set()
        for fold in (0, 1):
            utc = parsed.replace(tzinfo=source, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(source).replace(tzinfo=None) == parsed:
                candidates.add(utc)
        if not candidates:
            raise RowError("nonexistent_time", column)
        if len(candidates) != 1:
            raise RowError("ambiguous_time", column)
        return outbox_time(candidates.pop())
    except (ValueError, OverflowError) as exc:
        if isinstance(exc, RowError):
            raise
        raise RowError("invalid_time", column) from None


def validate_row(
    row: dict[str, Any], source: tzinfo | None = None
) -> tuple[str, str, dict]:
    if type(row["attempts"]) is not int or row["attempts"] < 0:
        raise RowError("invalid_attempts", "attempts")
    if row["kind"] not in ("event", "summary"):
        raise RowError("invalid_kind", "kind")
    if row["delivery_state"] not in ("pending", "failed", "dead_letter"):
        raise RowError("invalid_state", "delivery_state")
    try:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError
    except (TypeError, ValueError, UnicodeDecodeError, RecursionError):
        raise RowError("invalid_payload", "payload_json") from None
    created = normalize_time(row["created_at"], source, "created_at")
    next_at = normalize_time(row["next_attempt_at"], source, "next_attempt_at")
    return created, next_at, payload


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def migration_version(conn: sqlite3.Connection) -> int:
    if "outbox_migrations" not in _tables(conn):
        return 0
    version = conn.execute(
        "SELECT COALESCE(MAX(version),0) FROM outbox_migrations"
    ).fetchone()[0]
    if type(version) is not int or version not in (0, VERSION):
        raise MigrationError("unsupported_outbox_version")
    return version


@dataclass
class Plan:
    version: int
    source_spec: str | None
    normalize: list[tuple[str, str, int]] = field(default_factory=list)
    quarantine: list[tuple[int, str, str]] = field(default_factory=list)
    release: list[int] = field(default_factory=list)
    unresolved: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.version == 0 or bool(
            self.normalize or self.quarantine or self.release
        )

    def report(self) -> dict:
        return {
            "version": self.version,
            "source_spec": self.source_spec,
            "changed": self.changed,
            "normalize_ids": [r[2] for r in self.normalize],
            "quarantine": self.quarantine,
            "release_ids": self.release,
            "unresolved": self.unresolved,
        }


def plan_migration(
    conn: sqlite3.Connection,
    source: tzinfo | None = None,
    source_spec: str | None = None,
    *,
    retry: bool = False,
) -> Plan:
    version = migration_version(conn)
    plan = Plan(version, source_spec)
    tables = _tables(conn)
    if "delivery_outbox" not in tables:
        return plan
    columns = {r[1] for r in conn.execute("PRAGMA table_info(delivery_outbox)")}
    if not _REQUIRED <= columns:
        raise MigrationError("invalid_outbox_schema")
    existing: set[int] = set()
    if "outbox_quarantine" in tables:
        existing = {
            r[0] for r in conn.execute("SELECT delivery_id FROM outbox_quarantine")
        }
    if version == VERSION and not retry:
        if "outbox_quarantine" in tables:
            plan.unresolved = list(
                conn.execute(
                    "SELECT delivery_id,reason,field FROM outbox_quarantine ORDER BY delivery_id"
                )
            )
        return plan
    cur = conn.execute("SELECT * FROM delivery_outbox ORDER BY id")
    names = [d[0] for d in cur.description]
    for values in cur:
        row = dict(zip(names, values))
        row_id = row["id"]
        if version == VERSION and row_id not in existing:
            continue
        if row_id in existing and not retry:
            continue
        try:
            created, next_at, _ = validate_row(row, source)
        except RowError as exc:
            item = (row_id, exc.reason, exc.column)
            if row_id in existing:
                plan.unresolved.append(item)
            else:
                plan.quarantine.append(item)
            continue
        if (created, next_at) != (row["created_at"], row["next_attempt_at"]):
            plan.normalize.append((created, next_at, row_id))
        if row_id in existing:
            plan.release.append(row_id)
    return plan


def create_metadata(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox_migrations(run_id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL, source_spec TEXT, backup_file TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox_quarantine(delivery_id INTEGER PRIMARY KEY REFERENCES delivery_outbox(id) ON DELETE CASCADE, reason TEXT NOT NULL, field TEXT, quarantined_at TEXT NOT NULL)"
    )


def apply_plan(conn: sqlite3.Connection, plan: Plan, backup: Path | None) -> None:
    """Caller owns BEGIN/commit/rollback; helpers never commit implicitly."""
    create_metadata(conn)
    if not plan.changed:
        return
    conn.executemany(
        "UPDATE delivery_outbox SET created_at=?,next_attempt_at=? WHERE id=?",
        plan.normalize,
    )
    timestamp = outbox_time()
    conn.executemany(
        "INSERT INTO outbox_quarantine(delivery_id,reason,field,quarantined_at) VALUES(?,?,?,?)",
        [(i, reason, column, timestamp) for i, reason, column in plan.quarantine],
    )
    conn.executemany(
        "DELETE FROM outbox_quarantine WHERE delivery_id=?",
        [(i,) for i in plan.release],
    )
    conn.execute(
        "INSERT INTO outbox_migrations(version,applied_at,source_spec,backup_file) VALUES(?,?,?,?)",
        (VERSION, timestamp, plan.source_spec, backup.name if backup else None),
    )


def report_quarantine(plan: Plan) -> None:
    for row_id, reason, column in plan.quarantine:
        log.error("Outbox quarantined id=%s reason=%s field=%s", row_id, reason, column)


def backup_database(path: Path, *, deadline_seconds: float = 5.0) -> Path:
    """Complete committed snapshot including WAL, before the caller's first write."""
    started = time.monotonic()
    temporary: Path | None = None
    final: Path | None = None
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    published = False
    final_owned = False
    try:
        fd, name = tempfile.mkstemp(
            prefix=path.name + ".outbox-v1-", suffix=".tmp", dir=path.parent
        )
        temporary = Path(name)
        os.close(fd)
        source = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=min(5.0, deadline_seconds),
        )
        target = sqlite3.connect(temporary)

        def progress(status: int, remaining: int, total: int) -> None:
            if time.monotonic() - started > deadline_seconds:
                raise MigrationError("backup_deadline_exceeded")

        source.backup(target, pages=128, progress=progress, sleep=0.01)
        if target.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise MigrationError("backup_validation_failed")
        target.close()
        target = None
        # Windows fsync uses _commit/FlushFileBuffers, which needs a writable
        # descriptor. The completed, owned snapshot must not be truncated.
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        final = path.with_name(path.name + ".outbox-v1-" + uuid.uuid4().hex + ".bak")
        reserved = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        final_owned = True
        os.close(reserved)
        os.replace(temporary, final)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        published = True
        return final
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError("backup_failed") from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if final is not None and final_owned and not published:
            final.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--legacy-zone")
    group.add_argument("--legacy-tzfile", type=Path)
    parser.add_argument("--apply", action="store_true", help="Hub must be stopped")
    parser.add_argument("--retry-quarantined", action="store_true")
    args = parser.parse_args(argv)
    conn = None
    try:
        source, spec = source_timezone(args.legacy_zone, args.legacy_tzfile)
        if not args.db.is_file():
            raise MigrationError("database_not_found")
        conn = sqlite3.connect(
            args.db.resolve().as_uri() + ("?mode=rw" if args.apply else "?mode=ro"),
            uri=True,
            timeout=5,
        )
        conn.execute("PRAGMA foreign_keys=ON")
        if args.apply:
            conn.execute("BEGIN IMMEDIATE")
        plan = plan_migration(conn, source, spec, retry=args.retry_quarantined)
        backup = None
        if args.apply and plan.changed:
            backup = backup_database(args.db)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(delivery_outbox)")}
            if not columns:
                raise MigrationError("invalid_outbox_schema")
            if "dedupe_key" not in columns:
                conn.execute("ALTER TABLE delivery_outbox ADD COLUMN dedupe_key TEXT")
            if "dead_letter_alerted" not in columns:
                conn.execute(
                    "ALTER TABLE delivery_outbox ADD COLUMN dead_letter_alerted INTEGER NOT NULL DEFAULT 0"
                )
            apply_plan(conn, plan, backup)
            conn.commit()
            report_quarantine(plan)
        report = plan.report()
        report["applied"] = bool(args.apply and plan.changed)
        report["backup_file"] = backup.name if backup else None
        print(json.dumps(report))
        return 0
    except (MigrationError, sqlite3.Error, OSError) as exc:
        if conn is not None:
            conn.rollback()
        print(
            json.dumps(
                {
                    "error": str(exc)
                    if isinstance(exc, MigrationError)
                    else "database_operation_failed"
                }
            )
        )
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
