"""Verified Agent event lineage, durable reservations and offline recovery."""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_EVENT_ID = (1 << 63) - 1
_HEX = re.compile(r"^[0-9a-f]{32}$")


def new_lineage_id() -> str:
    """Return a 128-bit CSPRNG identity for a lineage or runtime boot."""
    return secrets.token_hex(16)


class StateError(ValueError):
    def __init__(self, reason: str, backups: tuple[Path, ...] = ()) -> None:
        self.reason = reason
        self.backups = backups
        super().__init__(reason)


class FileLease:
    """Stable OS lock, shared by runtime and offline writers. Never unlink it."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._file: Any = None

    def acquire(self) -> FileLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            self._file = os.fdopen(fd, "r+b", buffering=0)
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    self._file.write(b"\0")
                self._file.seek(0)
                getattr(msvcrt, "locking")(
                    self._file.fileno(), getattr(msvcrt, "LK_NBLCK"), 1
                )
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise StateError("lease_held_or_unavailable") from exc
        return self

    def close(self) -> None:
        if self._file is not None:
            self._file.close()  # OS unlock also occurs on process death
            self._file = None

    def __enter__(self) -> FileLease:
        return self.acquire()

    def __exit__(self, *args: object) -> None:
        self.close()


def state_paths(path: Path) -> tuple[Path, Path, Path]:
    path = Path(path)
    return (
        path,
        path.with_name(path.stem + ".highwater.json"),
        path.with_suffix(".lock"),
    )


def db_lease_path(db: Path) -> Path:
    db = Path(db).resolve()
    return db.with_name(db.name + ".event-cursor.lock")


def integer(value: object, minimum: int = 1, maximum: int = MAX_EVENT_ID + 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise StateError("invalid_counter")
    return value


def strict_json(raw: str) -> Any:
    """Reject ambiguous duplicate keys in durable recovery evidence."""

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict = {}
        for key, value in items:
            if key in result:
                raise StateError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=pairs)


@dataclass(frozen=True)
class StateRecord:
    version: int
    server_id: str
    stream_id: str
    lineage_origin: str
    next_event_id: int

    @classmethod
    def parse(cls, value: object) -> StateRecord:
        if not isinstance(value, dict):
            raise StateError("corrupt_state")
        if set(value) == {"next_event_id"}:
            integer(value["next_event_id"])
            raise StateError("migration_required")
        if set(value) != {
            "version",
            "server_id",
            "stream_id",
            "lineage_origin",
            "next_event_id",
        }:
            raise StateError("corrupt_state")
        if type(value["version"]) is not int or value["version"] != 2:
            raise StateError("unsupported_state_version")
        sid, stream = value["server_id"], value["stream_id"]
        if (
            not isinstance(sid, str)
            or not sid.strip()
            or not isinstance(stream, str)
            or not _HEX.fullmatch(stream)
        ):
            raise StateError("corrupt_state")
        if value["lineage_origin"] not in ("new_pairing", "legacy_migration"):
            raise StateError("corrupt_state")
        return cls(
            2, sid, stream, value["lineage_origin"], integer(value["next_event_id"])
        )


def read_record(path: Path) -> StateRecord:
    try:
        return StateRecord.parse(strict_json(Path(path).read_text(encoding="utf-8")))
    except FileNotFoundError as exc:
        raise StateError("initialization_required") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError("corrupt_state") from exc


def _sync_dir(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def backup_sources(path: Path) -> tuple[Path, ...]:
    backups = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    try:
        for source in state_paths(path)[:2]:
            if not source.exists():
                continue
            raw = source.read_bytes()
            backup = source.with_name(
                f"{source.name}.fault-{stamp}-{secrets.token_hex(8)}"
            )
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            _sync_dir(backup.parent)
            backups.append(backup)
    except OSError as exc:
        raise StateError("backup_failed", tuple(backups)) from exc
    return tuple(backups)


def write_pair(path: Path, record: StateRecord) -> None:
    primary, anchor, _ = state_paths(path)
    atomic_json(anchor, asdict(record))
    atomic_json(primary, asdict(record))


def initialize_state(
    path: Path, server_id: str, next_event_id: int = 1, *, origin: str = "new_pairing"
) -> StateRecord:
    """Explicit low-level initialization for an admitted pairing; never a load fallback."""
    with FileLease(state_paths(path)[2]):
        if any(p.exists() for p in state_paths(path)[:2]):
            raise StateError("state_already_exists")
        record = StateRecord(
            2, server_id, new_lineage_id(), origin, integer(next_event_id)
        )
        StateRecord.parse(asdict(record))
        write_pair(path, record)
        return record


class StateSession:
    def __init__(self, path: Path, record: StateRecord, lease: FileLease):
        self.path, self.record, self.lease = Path(path), record, lease
        self.boot_id = new_lineage_id()
        self.resume_floor = record.next_event_id - 1
        self._pending: StateRecord | None = None
        # Never hold this lock across filesystem I/O: close must remain bounded.
        self._lifecycle_lock = threading.Lock()
        self._closing = False
        self._reservation_active = False

    @classmethod
    def open(cls, path: Path, server_id: str | None = None) -> StateSession:
        lease = FileLease(state_paths(path)[2]).acquire()
        try:
            primary, anchor, _ = state_paths(path)
            a, b = read_record(primary), read_record(anchor)
            if a != b:
                raise StateError("state_records_disagree")
            if server_id is not None and a.server_id != server_id:
                raise StateError("state_identity_mismatch")
            if a.next_event_id > MAX_EVENT_ID:
                raise StateError("counter_exhausted")
            return cls(path, a, lease)
        except StateError as exc:
            try:
                backups = backup_sources(path)
            finally:
                lease.close()
            raise StateError(exc.reason, backups) from exc
        except BaseException:
            lease.close()
            raise

    def reserve(self, next_id: int) -> None:
        with self._lifecycle_lock:
            if self._closing or self.lease._file is None:
                raise StateError("state_session_closed")
            if self._reservation_active:
                raise StateError("reservation_in_progress")
            self._reservation_active = True
        try:
            integer(next_id)
            if next_id != self.record.next_event_id + 1:
                raise StateError("invalid_reservation")
            target = replace(self.record, next_event_id=next_id)
            primary, anchor, _ = state_paths(self.path)
            a, b = read_record(primary), read_record(anchor)
            allowed = (
                (self.record, target) if self._pending == target else (self.record,)
            )
            if a not in allowed or b not in allowed or (a == target and b != target):
                raise StateError("state_changed_during_boot")
            self._pending = target
            write_pair(self.path, target)
            self.record, self._pending = target, None
        finally:
            with self._lifecycle_lock:
                self._reservation_active = False
                if self._closing:
                    self.lease.close()

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
            if not self._reservation_active:
                self.lease.close()


def load_next_id(path: Path) -> int:
    session = StateSession.open(path)
    try:
        return session.record.next_event_id
    finally:
        session.close()


def save_next_id(path: Path, next_id: int) -> None:
    session = StateSession.open(path)
    try:
        session.reserve(next_id)
    finally:
        session.close()


def parse_cursor(value: object) -> dict:
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value.get("version") != 1
        or value.get("durable") is not True
    ):
        raise StateError("legacy_cursor_unverifiable")
    for key in ("stream_id", "boot_id"):
        if not isinstance(value.get(key), str) or not _HEX.fullmatch(value[key]):
            raise StateError("invalid_cursor")
    if not isinstance(value.get("server_id"), str) or not value["server_id"].strip():
        raise StateError("invalid_cursor")
    floor = integer(value.get("resume_floor"), 0, MAX_EVENT_ID)
    offered = integer(value.get("offered_highwater"), 0, MAX_EVENT_ID)
    nxt = integer(value.get("next_event_id"))
    if not floor <= offered < nxt:
        raise StateError("invalid_cursor")
    return {
        key: value[key]
        for key in (
            "version",
            "durable",
            "server_id",
            "stream_id",
            "boot_id",
            "resume_floor",
            "offered_highwater",
            "next_event_id",
        )
    }
