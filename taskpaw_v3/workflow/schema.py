"""Versioned workflow SQLite format; the store owns leases and transactions.

Known v1 and v2 databases receive a verified backup before the atomic upgrade to
v3 (v2: intake tables; v3: the U1 film plans). Unknown or damaged databases are
never repaired or automatically restored.
The caller must hold its FileLease before connecting, and retain it until close.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from taskpaw_v3.hub.server.outbox_migration import (
    MigrationError,
)
from taskpaw_v3.hub.server.outbox_migration import (
    backup_database as _snapshot_database,
)

from . import schema_intake

APPLICATION_ID = 0x54505746  # TPWF, separate from the monitoring database.
SCHEMA_VERSION = 3
SUPPORTED_VERSIONS = (1, 2, SCHEMA_VERSION)
# U04: the tier a v3 upgrade records for restores that already ran. Every
# workflow LTX preset's `standard` profile restores with this tier.
BACKFILL_LTX_MODEL = "standard"
BUSY_TIMEOUT_MS = 5000


class SchemaError(ValueError):
    """A stable, path-free reason; the store maps this to its public error type."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# Literal, versioned DDL also serves as the exact format allowlist on reopen.
# NO ACTION/RESTRICT is intentional: historical identities cannot cascade away.
V1_TABLES = {
    "authority": """CREATE TABLE authority (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        authority_id TEXT NOT NULL UNIQUE,
        epoch INTEGER NOT NULL CHECK(epoch>0),
        activation_state TEXT NOT NULL CHECK(activation_state IN ('inactive','active','recovery_required')),
        max_observed_at TEXT NOT NULL,
        lease_seconds INTEGER NOT NULL CHECK(lease_seconds BETWEEN 1 AND 86400),
        operator_credential_id TEXT NOT NULL REFERENCES credentials(credential_id) ON DELETE RESTRICT,
        tls_binding_json TEXT NOT NULL CHECK(json_valid(tls_binding_json)),
        next_fence INTEGER NOT NULL DEFAULT 1 CHECK(next_fence>0),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "principals": """CREATE TABLE principals (
        principal_id TEXT PRIMARY KEY NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('operator','compute_worker','intake_adapter','receiver_adapter','scrape_adapter','archive_adapter','cleanup_adapter','observer')),
        worker_id TEXT REFERENCES workers(worker_id) ON DELETE RESTRICT,
        component_id TEXT,
        boot_id TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "workers": """CREATE TABLE workers (
        worker_id TEXT PRIMARY KEY NOT NULL,
        device_id TEXT NOT NULL,
        monitoring_reference TEXT,
        name TEXT NOT NULL,
        address TEXT NOT NULL,
        port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
        network_revision INTEGER NOT NULL CHECK(network_revision>0),
        participation INTEGER NOT NULL DEFAULT 0 CHECK(participation IN (0,1)),
        role TEXT NOT NULL CHECK(role IN ('compute_worker','observer')),
        authorization_revision INTEGER NOT NULL CHECK(authorization_revision>0),
        enrollment_state TEXT NOT NULL CHECK(enrollment_state IN ('pending_pairing','pending_validation','verified','retired')),
        admission_state TEXT NOT NULL CHECK(admission_state IN ('disabled','enabled','draining','revoked')),
        current_boot_id TEXT,
        approved_capability_revision INTEGER CHECK(approved_capability_revision>0),
        retired_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "credentials": """CREATE TABLE credentials (
        credential_id TEXT PRIMARY KEY NOT NULL,
        principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        secret_digest TEXT NOT NULL CHECK(length(secret_digest)=64 AND secret_digest NOT GLOB '*[^0-9a-f]*'),
        generation INTEGER NOT NULL CHECK(generation>0),
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        UNIQUE(principal_id,generation)
    )""",
    "pairings": """CREATE TABLE pairings (
        pairing_id TEXT PRIMARY KEY NOT NULL,
        worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE RESTRICT,
        device_id TEXT NOT NULL,
        ticket_digest TEXT NOT NULL CHECK(length(ticket_digest)=64 AND ticket_digest NOT GLOB '*[^0-9a-f]*'),
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        credential_id TEXT REFERENCES credentials(credential_id) ON DELETE RESTRICT,
        request_digest TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        CHECK((consumed_at IS NULL AND credential_id IS NULL AND request_digest IS NULL)
           OR (consumed_at IS NOT NULL AND credential_id IS NOT NULL AND request_digest IS NOT NULL))
    )""",
    "worker_boots": """CREATE TABLE worker_boots (
        worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE RESTRICT,
        boot_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        reconciled INTEGER NOT NULL DEFAULT 0 CHECK(reconciled IN (0,1)),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(worker_id,boot_id)
    )""",
    "physical_resources": """CREATE TABLE physical_resources (
        physical_resource_id TEXT PRIMARY KEY NOT NULL,
        device_id TEXT NOT NULL,
        identity_hash TEXT NOT NULL UNIQUE,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "capabilities": """CREATE TABLE capabilities (
        worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('declared','probed','approved')),
        revision INTEGER NOT NULL CHECK(revision>0),
        probe_revision INTEGER CHECK(probe_revision>0),
        physical_resource_id TEXT REFERENCES physical_resources(physical_resource_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(worker_id,kind,revision),
        CHECK(kind!='approved' OR probe_revision IS NOT NULL)
    )""",
    "films": """CREATE TABLE films (
        film_id TEXT PRIMARY KEY NOT NULL,
        intake_session_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        policy_revision INTEGER NOT NULL CHECK(policy_revision>0),
        current_input_revision INTEGER CHECK(current_input_revision>0),
        current_final_revision INTEGER CHECK(current_final_revision>0),
        current_context_revision INTEGER CHECK(current_context_revision>0),
        context_state TEXT NOT NULL DEFAULT 'invalidated' CHECK(context_state IN ('ready','blocked','invalidated')),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        FOREIGN KEY(film_id,current_context_revision)
            REFERENCES approval_contexts(film_id,context_revision) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    )""",
    "manifests": """CREATE TABLE manifests (
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('input','final')),
        revision INTEGER NOT NULL CHECK(revision>0),
        manifest_hash TEXT NOT NULL CHECK(length(manifest_hash)=64 AND manifest_hash NOT GLOB '*[^0-9a-f]*'),
        input_revision INTEGER CHECK(input_revision>0),
        input_hash TEXT,
        attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(film_id,kind,revision)
    )""",
    "manifest_items": """CREATE TABLE manifest_items (
        film_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        revision INTEGER NOT NULL,
        item_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('main_video','source_subtitle','generated_subtitle','nfo','poster','fanart','thumb','folder_cover','attachment')),
        relative_path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        source_root_id TEXT NOT NULL,
        native_identity_key TEXT,
        parent_item_id TEXT,
        video_item_id TEXT,
        volume_ordinal INTEGER CHECK(volume_ordinal>0),
        language TEXT,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(film_id,kind,revision,item_id),
        UNIQUE(film_id,kind,revision,path_key),
        FOREIGN KEY(film_id,kind,revision) REFERENCES manifests(film_id,kind,revision) ON DELETE RESTRICT,
        FOREIGN KEY(film_id,kind,revision,parent_item_id)
            REFERENCES manifest_items(film_id,kind,revision,item_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(film_id,kind,revision,video_item_id)
            REFERENCES manifest_items(film_id,kind,revision,item_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
    )""",
    "source_refs": """CREATE TABLE source_refs (
        film_id TEXT NOT NULL,
        manifest_kind TEXT NOT NULL DEFAULT 'input' CHECK(manifest_kind='input'),
        input_revision INTEGER NOT NULL,
        item_id TEXT NOT NULL,
        source_root_id TEXT NOT NULL,
        native_identity_key TEXT,
        retained INTEGER NOT NULL DEFAULT 1 CHECK(retained IN (0,1)),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(film_id,input_revision,item_id),
        FOREIGN KEY(film_id,manifest_kind,input_revision,item_id)
            REFERENCES manifest_items(film_id,kind,revision,item_id) ON DELETE RESTRICT
    )""",
    "stages": """CREATE TABLE stages (
        job_id TEXT PRIMARY KEY NOT NULL,
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        input_revision INTEGER NOT NULL CHECK(input_revision>0),
        input_hash TEXT NOT NULL,
        stage TEXT NOT NULL CHECK(stage IN ('intake','restore','asr','translate','return','scrape','archive','cleanup_input','cleanup_workcopy')),
        status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','unknown','blocked','skipped')),
        current_attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        UNIQUE(film_id,input_revision,stage)
    )""",
    "attempts": """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL,
        job_id TEXT NOT NULL REFERENCES stages(job_id) ON DELETE RESTRICT,
        attempt_number INTEGER NOT NULL CHECK(attempt_number>0),
        owner_principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        worker_id TEXT REFERENCES workers(worker_id) ON DELETE RESTRICT,
        boot_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK(epoch>0),
        fence INTEGER NOT NULL CHECK(fence>0),
        input_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','unknown','blocked')),
        operation_id TEXT REFERENCES operations(operation_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        UNIQUE(job_id,attempt_number)
    )""",
    "assignments": """CREATE TABLE assignments (
        assignment_id TEXT PRIMARY KEY NOT NULL,
        job_id TEXT NOT NULL REFERENCES stages(job_id) ON DELETE RESTRICT,
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
        owner_principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        worker_id TEXT REFERENCES workers(worker_id) ON DELETE RESTRICT,
        boot_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK(epoch>0),
        fence INTEGER NOT NULL CHECK(fence>0),
        physical_resource_id TEXT REFERENCES physical_resources(physical_resource_id) ON DELETE RESTRICT,
        claimed_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        lease_deadline TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('running','unknown','resolved')),
        resolved_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        CHECK((state='resolved')=(resolved_at IS NOT NULL))
    )""",
    "resource_owners": """CREATE TABLE resource_owners (
        assignment_id TEXT PRIMARY KEY NOT NULL REFERENCES assignments(assignment_id) ON DELETE RESTRICT,
        physical_resource_id TEXT NOT NULL REFERENCES physical_resources(physical_resource_id) ON DELETE RESTRICT,
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
        owner_generation INTEGER NOT NULL CHECK(owner_generation>0),
        state TEXT NOT NULL CHECK(state IN ('reserved','running','unknown','resolved')),
        resolved_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        CHECK((state='resolved')=(resolved_at IS NOT NULL))
    )""",
    "approval_contexts": """CREATE TABLE approval_contexts (
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        context_revision INTEGER NOT NULL CHECK(context_revision>0),
        context_hash TEXT NOT NULL,
        binding_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('ready','blocked','invalidated')),
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(film_id,context_revision),
        UNIQUE(film_id,context_revision,context_hash,binding_hash)
    )""",
    "approvals": """CREATE TABLE approvals (
        approval_id TEXT PRIMARY KEY NOT NULL,
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        context_revision INTEGER NOT NULL,
        context_hash TEXT NOT NULL,
        binding_hash TEXT NOT NULL,
        operator_principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        epoch INTEGER NOT NULL CHECK(epoch>0),
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        FOREIGN KEY(film_id,context_revision,context_hash,binding_hash)
            REFERENCES approval_contexts(film_id,context_revision,context_hash,binding_hash) ON DELETE RESTRICT
    )""",
    "operations": """CREATE TABLE operations (
        operation_id TEXT PRIMARY KEY NOT NULL,
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        kind TEXT NOT NULL CHECK(kind IN ('restore','asr','translate','return','scrape','archive','cleanup_input','cleanup_workcopy')),
        job_id TEXT REFERENCES stages(job_id) ON DELETE RESTRICT,
        attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
        approval_id TEXT REFERENCES approvals(approval_id) ON DELETE RESTRICT,
        owner_principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        binding_hash TEXT,
        scope TEXT CHECK(scope IN ('input','workcopy')),
        status TEXT NOT NULL CHECK(status IN ('admitted','running','unknown','verified','failed','resolved')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        resolved_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "receipts": """CREATE TABLE receipts (
        receipt_id TEXT PRIMARY KEY NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('artifact','archive')),
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE RESTRICT,
        attempt_id TEXT REFERENCES attempts(attempt_id) ON DELETE RESTRICT,
        approval_id TEXT REFERENCES approvals(approval_id) ON DELETE RESTRICT,
        producer_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        manifest_hash TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        UNIQUE(operation_id,producer_id,kind)
    )""",
    "cleanup_items": """CREATE TABLE cleanup_items (
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE RESTRICT,
        item_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','deleted','already_absent','blocked_changed','blocked_writing','blocked_identity','failed','unknown')),
        updated_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        PRIMARY KEY(operation_id,item_id)
    )""",
    "audit": """CREATE TABLE audit (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        authority_id TEXT NOT NULL REFERENCES authority(authority_id) ON DELETE RESTRICT,
        epoch INTEGER NOT NULL CHECK(epoch>0),
        principal_id TEXT REFERENCES principals(principal_id) ON DELETE RESTRICT,
        command_kind TEXT NOT NULL,
        request_id TEXT,
        entity_id TEXT,
        created_at TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""",
    "request_results": """CREATE TABLE request_results (
        principal_id TEXT NOT NULL REFERENCES principals(principal_id) ON DELETE RESTRICT,
        command_kind TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        result_id TEXT NOT NULL UNIQUE,
        audit_seq INTEGER NOT NULL REFERENCES audit(seq) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        result_json TEXT NOT NULL CHECK(json_valid(result_json)),
        PRIMARY KEY(principal_id,command_kind,idempotency_key)
    )""",
}

V1_INDEXES = {
    "one_unresolved_assignment_per_job": """CREATE UNIQUE INDEX one_unresolved_assignment_per_job
        ON assignments(job_id) WHERE resolved_at IS NULL""",
    "one_active_owner_per_resource": """CREATE UNIQUE INDEX one_active_owner_per_resource
        ON resource_owners(physical_resource_id) WHERE resolved_at IS NULL""",
    "one_unresolved_external_operation": """CREATE UNIQUE INDEX one_unresolved_external_operation
        ON operations(film_id,kind) WHERE resolved_at IS NULL
        AND kind IN ('archive','cleanup_input','cleanup_workcopy')""",
    "source_identity_references": """CREATE INDEX source_identity_references
        ON source_refs(source_root_id,native_identity_key,retained)""",
    "assignment_owner": """CREATE INDEX assignment_owner
        ON assignments(owner_principal_id,worker_id,boot_id)""",
}

# Frozen v2 format: v1 plus the intake tables. Never edited; v3 only adds.
V2_TABLES = {**V1_TABLES, **schema_intake.TABLES}
V2_INDEXES = {**V1_INDEXES, **schema_intake.INDEXES}

# U1 P1': one confirmed plan per (film, input revision). A new input revision
# has no row, so the film is unplanned again; a row is never updated. A NULL
# tier is a film whose inputs are all restored already (needs no GPU).
# `backfilled` marks the v3 upgrade's own default-tier rows (U04): they are no
# operator choice, so the recommendation never learns from them. `actors` is
# the film's actor list (JSON, NFKC-normalised and trimmed) at confirm time: the
# recommendation must outlive the film's lookup cache, which cleanup removes.
V3_TABLES = {
    "film_plans": """CREATE TABLE film_plans (
        film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
        manifest_kind TEXT NOT NULL DEFAULT 'input' CHECK(manifest_kind='input'),
        input_revision INTEGER NOT NULL CHECK(input_revision>0),
        ltx_model TEXT CHECK(ltx_model IN ('standard','good','high')),
        machine TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE RESTRICT,
        confirmed_at TEXT NOT NULL,
        backfilled INTEGER NOT NULL DEFAULT 0 CHECK(backfilled IN (0,1)),
        actors TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY(film_id,input_revision),
        FOREIGN KEY(film_id,manifest_kind,input_revision)
            REFERENCES manifests(film_id,kind,revision) ON DELETE RESTRICT
    )""",
}
V3_INDEXES: dict[str, str] = {}

TABLES = {**V2_TABLES, **V3_TABLES}
INDEXES = {**V2_INDEXES, **V3_INDEXES}
_FORMATS = {
    1: (V1_TABLES, V1_INDEXES),
    2: (V2_TABLES, V2_INDEXES),
    SCHEMA_VERSION: (TABLES, INDEXES),
}


def _path(path: Path) -> Path:
    # Deployment must also reject mapped network drives in its controlled-data
    # directory policy; this low-level module rejects explicit UNC/network paths.
    if str(path).startswith(("\\\\", "//")):
        raise SchemaError("local_database_required")
    resolved = Path(path).resolve()
    if str(resolved).startswith(("\\\\", "//")):
        raise SchemaError("local_database_required")
    return resolved


def _connect(path: Path, mode: str) -> sqlite3.Connection:
    try:
        return sqlite3.connect(
            path.as_uri() + "?mode=" + mode,
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
    except (sqlite3.Error, OSError) as exc:
        raise SchemaError("database_unavailable") from exc


def connect_new(path: Path) -> sqlite3.Connection:
    """Reserve a new file without overwriting; failed initialization stays explicit."""
    path = _path(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        raise SchemaError("database_exists") from exc
    except OSError as exc:
        raise SchemaError("database_unavailable") from exc
    os.close(fd)
    try:
        return _connect(path, "rw")
    except (sqlite3.Error, OSError) as exc:
        raise SchemaError("database_unavailable") from exc


def connect_existing(path: Path) -> sqlite3.Connection:
    """Read-only preflight precedes RW connection and all persistent PRAGMAs."""
    path = _path(path)
    if not path.is_file():
        raise SchemaError("database_not_found")
    if path.stat().st_size == 0:
        raise SchemaError("empty_database")
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path, "ro")
        version = _supported_version(conn)
        validate_schema(conn, version=version)
        conn.close()
        conn = None
        if version != SCHEMA_VERSION:
            _backup_version(path, version=version, deadline_seconds=5.0)
        conn = _connect(path, "rw")
        validate_schema(conn, version=version)
        if version != SCHEMA_VERSION:
            configure_connection(conn)
            _migrate(conn, version)
        return conn
    except BaseException:
        if conn is not None:
            conn.close()
        raise


def configure_connection(conn: sqlite3.Connection) -> None:
    """Configure outside a transaction, after existing-database validation."""
    if conn.in_transaction:
        raise SchemaError("unexpected_transaction")
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if (
            mode is None
            or mode[0] != "wal"
            or conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
        ):
            raise SchemaError("configuration_failed")
    except sqlite3.Error as exc:
        raise SchemaError("configuration_failed") from exc


def initialize_schema(conn: sqlite3.Connection) -> None:
    """DDL and version markers only; caller inserts bootstrap then commits."""
    if not conn.in_transaction:
        raise SchemaError("transaction_required")
    try:
        if (
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            or conn.execute("PRAGMA application_id").fetchone()[0]
            or conn.execute("PRAGMA user_version").fetchone()[0]
        ):
            raise SchemaError("database_not_empty")
        for statement in (*TABLES.values(), *INDEXES.values()):
            conn.execute(statement)
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    except sqlite3.Error as exc:
        raise SchemaError("initialization_failed") from exc


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.split())


def _supported_version(conn: sqlite3.Connection) -> int:
    try:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise SchemaError("wrong_application_id")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in SUPPORTED_VERSIONS:
            raise SchemaError("unsupported_schema_version")
        return int(version)
    except sqlite3.Error as exc:
        raise SchemaError("invalid_database") from exc


def validate_schema(conn: sqlite3.Connection, *, version: int = SCHEMA_VERSION) -> None:
    """Validate exact versioned columns, CHECK/FK clauses, index predicates and data.

    Comparing the complete versioned DDL rejects a same-named weaker index or a
    recreated table missing its FK/CHECK constraints. No speculative repair or
    general-purpose migration inference is performed.
    """
    try:
        if conn.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise SchemaError("wrong_application_id")
        if (
            version not in SUPPORTED_VERSIONS
            or conn.execute("PRAGMA user_version").fetchone()[0] != version
        ):
            raise SchemaError("unsupported_schema_version")
        rows = conn.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        actual = {(row[0], row[1]): _normalized_sql(row[2] or "") for row in rows}
        tables, indexes = _FORMATS[version]
        expected = {
            **{("table", name): _normalized_sql(sql) for name, sql in tables.items()},
            **{("index", name): _normalized_sql(sql) for name, sql in indexes.items()},
        }
        if actual != expected:
            raise SchemaError("invalid_schema")
        if [tuple(row) for row in conn.execute("PRAGMA integrity_check")] != [("ok",)]:
            raise SchemaError("integrity_check_failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SchemaError("foreign_key_check_failed")
        authority = conn.execute(
            "SELECT a.singleton,p.role,p.worker_id FROM authority a "
            "JOIN credentials c ON c.credential_id=a.operator_credential_id "
            "JOIN principals p ON p.principal_id=c.principal_id"
        ).fetchall()
        if [tuple(row) for row in authority] != [(1, "operator", None)]:
            raise SchemaError("invalid_authority")
        if (
            conn.execute(
                "SELECT count(*) FROM principals WHERE role='operator'"
            ).fetchone()[0]
            != 1
        ):
            raise SchemaError("invalid_authority")
        if conn.execute(
            "SELECT 1 FROM films f WHERE "
            "(f.current_input_revision IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM manifests m WHERE m.film_id=f.film_id AND m.kind='input' "
            "AND m.revision=f.current_input_revision)) OR "
            "(f.current_final_revision IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM manifests m WHERE m.film_id=f.film_id AND m.kind='final' "
            "AND m.revision=f.current_final_revision)) OR "
            "(f.context_state='ready' AND f.current_context_revision IS NULL) LIMIT 1"
        ).fetchone():
            raise SchemaError("invalid_current_pointer")
    except sqlite3.Error as exc:
        raise SchemaError("invalid_database") from exc


def _backfill_plans(conn: sqlite3.Connection) -> None:
    """U04 (operator decision IR02): a finished restore keeps its film past 待处理.

    Only a current-revision restore that already succeeded gets a default-tier
    plan (its last attempt's worker is the machine), marked `backfilled`. Every
    other restore — never claimed, interrupted, failed or retrying, even with
    attempts — stays unplanned: once pending it is in 待处理, where the operator
    picks its tier, and nothing claims it before.
    """
    conn.execute(
        "INSERT INTO film_plans "
        "(film_id,input_revision,ltx_model,machine,confirmed_at,backfilled) "
        "SELECT s.film_id,s.input_revision,?,a.worker_id,a.started_at,1 FROM stages s "
        "JOIN films f ON f.film_id=s.film_id AND f.current_input_revision=s.input_revision "
        "JOIN attempts a ON a.attempt_id=(SELECT l.attempt_id FROM attempts l "
        "WHERE l.job_id=s.job_id AND l.worker_id IS NOT NULL "
        "ORDER BY l.attempt_number DESC LIMIT 1) "
        "WHERE s.stage='restore' AND s.status='succeeded'",
        (BACKFILL_LTX_MODEL,),
    )


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """One transaction from a supported older version to SCHEMA_VERSION."""
    if conn.in_transaction:
        raise SchemaError("unexpected_transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        validate_schema(conn, version=version)
        added = []
        if version < 2:
            added += [*schema_intake.TABLES.values(), *schema_intake.INDEXES.values()]
        added += [*V3_TABLES.values(), *V3_INDEXES.values()]
        for statement in added:
            conn.execute(statement)
        _backfill_plans(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        validate_schema(conn)
        conn.commit()
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc, sqlite3.Error):
            raise SchemaError("migration_failed") from exc
        raise


def _backup_version(path: Path, *, version: int, deadline_seconds: float) -> Path:
    try:
        backup = _snapshot_database(path, deadline_seconds=deadline_seconds)
        verification = _connect(backup, "ro")
        try:
            validate_schema(verification, version=version)
        finally:
            verification.close()
        return backup
    except (MigrationError, OSError, sqlite3.Error, SchemaError) as exc:
        raise SchemaError("backup_failed") from exc


def backup_database(path: Path, *, deadline_seconds: float = 5.0) -> Path:
    """Snapshot committed WAL using the existing tested backup primitive.

    The primitive's legacy ``.outbox-v1-*.bak`` filename is retained; its snapshot
    operation copies the entire database, not just monitoring/outbox tables.
    This operation does not migrate, restore or activate either supported version.
    """
    path = _path(path)
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect(path, "ro")
        version = _supported_version(conn)
        validate_schema(conn, version=version)
        conn.close()
        conn = None
        return _backup_version(path, version=version, deadline_seconds=deadline_seconds)
    except (MigrationError, OSError, sqlite3.Error) as exc:
        raise SchemaError("backup_failed") from exc
    finally:
        if conn is not None:
            conn.close()
