"""Single-owner workflow transactions; importing this module performs no I/O."""

from __future__ import annotations

import hmac
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError
from typing_extensions import Self

from taskpaw_v3.core.state import FileLease, StateError

from . import schema
from .auth import AuthProof, PairingProof, require_role
from .models import (
    AuthorityBootstrap,
    CommandMeta,
    CommandResult,
    Model,
    WorkflowError,
    canonical_json,
    digest,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Mutation:
    entity_id: str
    version: int
    status: str
    operation_id: str | None = None
    assignment_id: str | None = None
    attempt_id: str | None = None
    context_revision: int | None = None
    context_hash: str | None = None


# Existing admitted effects may still be recorded while recovery blocks new work.
_RECOVERY_COMMANDS = frozenset(
    {
        "revoke_credential",
        "revoke_worker",
        "mark_unknown",
        "resolve_assignment",
        "revoke_approval",
        "finish_attempt",
        "accept_artifact_receipt",
        "accept_archive_receipt",
        "fail_archive",
        "record_cleanup_result",
        "record_boot",
        "record_intake_owner",
        "record_intake_source_notice",
        "begin_intake_root_scan",
        "finish_intake_root_scan",
        "mark_intake_scan_seen",
        "finish_intake_snapshot",
        "cancel_intake_snapshot",
    }
)


class WorkflowCore:
    """All mutations authenticate again inside the serialized SQLite transaction."""

    def __init__(
        self, path: Path, lease: FileLease, conn: sqlite3.Connection, clock: Clock
    ):
        self.path = path
        self._lease = lease
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def _connect(cls, path: Path, clock: Clock | None, *, create: bool) -> Self:
        raw = str(path)
        path = Path(path).resolve()
        if raw.startswith(("\\\\", "//")) or str(path).startswith(("\\\\", "//")):
            raise WorkflowError("local_database_required")
        if not create and not path.is_file():
            raise WorkflowError("database_not_found")
        lease = FileLease(path.with_name(path.name + ".workflow.lock"))
        conn = None
        try:
            lease.acquire()
            conn = schema.connect_new(path) if create else schema.connect_existing(path)
            schema.configure_connection(conn)
            return cls(path, lease, conn, clock or SystemClock())
        except BaseException as exc:
            if conn is not None:
                conn.close()
            lease.close()
            if isinstance(exc, schema.SchemaError):
                raise WorkflowError(exc.reason) from exc
            if isinstance(exc, StateError):
                raise WorkflowError("authority_unavailable") from exc
            raise

    @classmethod
    def create(
        cls, path: Path, bootstrap: AuthorityBootstrap, *, clock: Clock | None = None
    ) -> Self:
        bootstrap = AuthorityBootstrap.model_validate(bootstrap)
        store = cls._connect(path, clock, create=True)
        try:
            now = store._wall_time()
            if bootstrap.operator_expires_at <= now:
                raise WorkflowError("invalid_request")
            store._conn.execute("BEGIN IMMEDIATE")
            schema.initialize_schema(store._conn)
            store._insert(
                "principals",
                principal_id=bootstrap.operator_principal_id,
                role="operator",
                body_json="{}",
            )
            store._insert(
                "credentials",
                credential_id=bootstrap.operator_credential_id,
                principal_id=bootstrap.operator_principal_id,
                secret_digest=bootstrap.operator_secret_digest,
                generation=1,
                issued_at=timestamp(now),
                expires_at=timestamp(bootstrap.operator_expires_at),
                body_json="{}",
            )
            store._insert(
                "authority",
                singleton=1,
                authority_id=bootstrap.authority_id,
                epoch=bootstrap.epoch,
                activation_state="active",
                max_observed_at=timestamp(now),
                lease_seconds=bootstrap.lease_seconds,
                operator_credential_id=bootstrap.operator_credential_id,
                tls_binding_json=canonical_json(bootstrap.tls),
                body_json="{}",
            )
            store._conn.commit()
            schema.validate_schema(store._conn)
            return store
        except BaseException as exc:
            store._conn.rollback()
            store.close()
            if isinstance(exc, schema.SchemaError):
                raise WorkflowError(exc.reason) from exc
            raise

    @classmethod
    def open(cls, path: Path, *, clock: Clock | None = None) -> Self:
        return cls._connect(path, clock, create=False)

    def close(self) -> None:
        # Closing waits for the transaction owner; never releases its OS lease early.
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
                self._lease.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise WorkflowError("authority_unavailable")

    def _wall_time(self) -> datetime:
        value = self._clock.now()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise WorkflowError("authority_unavailable")
        return value.astimezone(timezone.utc)

    def _observe_time(self) -> datetime:
        """Persist high-water even if the subsequent request fails authentication."""
        self._ensure_open()
        now = self._wall_time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._one("SELECT * FROM authority WHERE singleton=1")
            high = parse_time(row["max_observed_at"])
            if now < high:
                self._conn.execute(
                    "UPDATE authority SET activation_state='recovery_required' WHERE singleton=1"
                )
            elif now > high:
                self._conn.execute(
                    "UPDATE authority SET max_observed_at=? WHERE singleton=1",
                    (timestamp(now),),
                )
            self._conn.commit()
            return max(now, high)
        except BaseException:
            self._conn.rollback()
            raise

    def _one(
        self, sql: str, args: tuple[Any, ...] = (), *, code: str = "not_found"
    ) -> sqlite3.Row:
        row = self._conn.execute(sql, args).fetchone()
        if row is None:
            raise WorkflowError(code)
        return row

    def _insert(self, table: str, **values: Any) -> None:
        if table not in schema.TABLES or any(not key.isidentifier() for key in values):
            raise ValueError("invalid_internal_table")
        columns = ",".join(values)
        marks = ",".join("?" for _ in values)
        self._conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
        )

    def _update(
        self,
        table: str,
        key_field: str,
        key_value: str,
        expected_version: int,
        **values: Any,
    ) -> int:
        if (
            table not in schema.TABLES
            or not key_field.isidentifier()
            or any(not key.isidentifier() for key in values)
        ):
            raise ValueError("invalid_internal_table")
        if "version" in values:
            raise ValueError("version_owned_by_cas")
        changes = ",".join([*(f"{key}=?" for key in values), "version=version+1"])
        result = self._conn.execute(
            f"UPDATE {table} SET {changes} WHERE {key_field}=? AND version=?",
            (*values.values(), key_value, expected_version),
        )
        if result.rowcount != 1:
            raise WorkflowError("cas_conflict")
        return expected_version + 1

    def _credential(
        self, credential_id: str, secret_digest: str, now: datetime
    ) -> sqlite3.Row:
        row = self._one(
            "SELECT p.*,c.credential_id,c.secret_digest,c.generation,c.expires_at,c.revoked_at "
            "FROM credentials c JOIN principals p ON p.principal_id=c.principal_id WHERE credential_id=?",
            (credential_id,),
            code="unauthorized",
        )
        if (
            not hmac.compare_digest(row["secret_digest"], secret_digest)
            or row["revoked_at"] is not None
            or parse_time(row["expires_at"]) <= now
        ):
            raise WorkflowError("unauthorized")
        if row["worker_id"]:
            worker = self._one(
                "SELECT * FROM workers WHERE worker_id=?",
                (row["worker_id"],),
                code="unauthorized",
            )
            if (
                worker["admission_state"] == "revoked"
                or worker["enrollment_state"] == "retired"
            ):
                raise WorkflowError("unauthorized")
        return row

    def _authenticate(self, credential_id: str, secret_digest: str) -> AuthProof:
        with self._lock:
            now = self._observe_time()
            row = self._credential(credential_id, secret_digest, now)
            authority = self._one("SELECT * FROM authority WHERE singleton=1")
            return AuthProof(
                credential_id,
                row["principal_id"],
                row["generation"],
                authority["authority_id"],
                secret_digest,
            )

    def _authenticate_pairing(
        self, pairing_id: str, ticket_digest: str, new_secret_digest: str
    ) -> PairingProof:
        with self._lock:
            now = self._observe_time()
            row = self._one(
                "SELECT * FROM pairings WHERE pairing_id=?",
                (pairing_id,),
                code="unauthorized",
            )
            if (
                row["consumed_at"] is not None
                or parse_time(row["expires_at"]) <= now
                or not hmac.compare_digest(row["ticket_digest"], ticket_digest)
            ):
                raise WorkflowError("unauthorized")
            authority = self._one("SELECT * FROM authority WHERE singleton=1")
            return PairingProof(
                pairing_id, authority["authority_id"], ticket_digest, new_secret_digest
            )

    def _check_auth(self, auth: AuthProof, now: datetime) -> sqlite3.Row:
        if not isinstance(auth, AuthProof):
            raise WorkflowError("unauthorized")
        row = self._credential(auth.credential_id, auth.secret_digest, now)
        authority = self._one("SELECT * FROM authority WHERE singleton=1")
        if (
            row["principal_id"] != auth.principal_id
            or row["generation"] != auth.generation
            or authority["authority_id"] != auth.authority_id
        ):
            raise WorkflowError("unauthorized")
        return row

    def _pairing_principal(
        self, auth: PairingProof, command: Model, now: datetime
    ) -> sqlite3.Row:
        raise WorkflowError("unauthorized")

    def _execute(
        self,
        auth: AuthProof | PairingProof,
        meta: CommandMeta,
        command: Model,
        kind: str,
        handler: Callable[[sqlite3.Row, datetime], Mutation],
    ) -> CommandResult:
        try:
            meta = CommandMeta.model_validate(meta)
            # Callers use frozen, deeply typed values. Revalidate even model_copy/model_construct.
            command = type(command).model_validate(command)
            request_digest = digest(
                {
                    "authority_id": meta.authority_id,
                    "epoch": meta.authority_epoch,
                    "command": command,
                }
            )
        except (ValidationError, ValueError) as exc:
            raise WorkflowError("invalid_request") from exc
        with self._lock:
            now = self._observe_time()
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if kind == "complete_pairing" and isinstance(auth, PairingProof):
                    principal = self._pairing_principal(auth, command, now)
                elif isinstance(auth, AuthProof):
                    principal = self._check_auth(auth, now)
                else:
                    raise WorkflowError("unauthorized")
                require_role(principal["role"], kind)
                authority = self._one("SELECT * FROM authority WHERE singleton=1")
                if (
                    authority["authority_id"] != meta.authority_id
                    or authority["epoch"] != meta.authority_epoch
                ):
                    raise WorkflowError("stale_attempt")
                old = self._conn.execute(
                    "SELECT * FROM request_results WHERE principal_id=? AND command_kind=? AND idempotency_key=?",
                    (principal["principal_id"], kind, meta.idempotency_key),
                ).fetchone()
                if old is not None:
                    if not hmac.compare_digest(old["request_digest"], request_digest):
                        raise WorkflowError("idempotency_conflict")
                    result = CommandResult.model_validate_json(old["result_json"])
                    self._conn.commit()
                    return result
                if (
                    authority["activation_state"] != "active"
                    and kind not in _RECOVERY_COMMANDS
                ):
                    raise WorkflowError("authority_unavailable")
                mutation = handler(principal, now)
                result_id = str(uuid4())
                self._insert(
                    "audit",
                    event_id=str(uuid4()),
                    authority_id=meta.authority_id,
                    epoch=meta.authority_epoch,
                    principal_id=principal["principal_id"],
                    command_kind=kind,
                    request_id=meta.request_id,
                    entity_id=mutation.entity_id,
                    created_at=timestamp(now),
                    body_json=canonical_json({"result_id": result_id}),
                )
                audit_id = self._one("SELECT last_insert_rowid()")[0]
                result = CommandResult(
                    request_id=meta.request_id,
                    result_id=result_id,
                    audit_id=audit_id,
                    **asdict(mutation),
                )
                self._insert(
                    "request_results",
                    principal_id=principal["principal_id"],
                    command_kind=kind,
                    idempotency_key=meta.idempotency_key,
                    request_id=meta.request_id,
                    request_digest=request_digest,
                    result_id=result_id,
                    audit_seq=audit_id,
                    created_at=timestamp(now),
                    result_json=canonical_json(result),
                )
                self._conn.commit()
                return result
            except BaseException as exc:
                self._conn.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise WorkflowError("cas_conflict") from exc
                if isinstance(exc, sqlite3.Error):
                    raise WorkflowError("authority_unavailable") from exc
                raise
