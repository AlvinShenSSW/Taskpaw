"""Real SQLite lifecycle tests; all databases and backups are owned tmp_path files."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from taskpaw_v3.workflow import schema

STAMP = "2026-10-19T00:00:00+00:00"


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO principals(principal_id,role,body_json) VALUES(?,?,?)",
        ("operator", "operator", "{}"),
    )
    conn.execute(
        "INSERT INTO credentials(credential_id,principal_id,secret_digest,generation,"
        "issued_at,expires_at,body_json) VALUES(?,?,?,?,?,?,?)",
        (
            "credential",
            "operator",
            "a" * 64,
            1,
            STAMP,
            "2027-01-01T00:00:00+00:00",
            "{}",
        ),
    )
    conn.execute(
        "INSERT INTO authority(singleton,authority_id,epoch,activation_state,"
        "max_observed_at,lease_seconds,operator_credential_id,tls_binding_json,body_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (1, "authority", 1, "active", STAMP, 60, "credential", "{}", "{}"),
    )


def _database(path: Path) -> sqlite3.Connection:
    conn = schema.connect_new(path)
    schema.configure_connection(conn)
    conn.execute("BEGIN IMMEDIATE")
    schema.initialize_schema(conn)
    _bootstrap(conn)
    conn.commit()
    return conn


def test_create_is_exclusive_and_open_never_creates(tmp_path):
    path = tmp_path / "workflow.db"
    with pytest.raises(schema.SchemaError, match="database_not_found"):
        schema.connect_existing(path)
    assert not path.exists()
    path.write_bytes(b"do not replace")
    with pytest.raises(schema.SchemaError, match="database_exists"):
        schema.connect_new(path)
    assert path.read_bytes() == b"do not replace"


def test_initialization_and_bootstrap_are_one_caller_owned_transaction(tmp_path):
    path = tmp_path / "workflow.db"
    conn = schema.connect_new(path)
    try:
        schema.configure_connection(conn)
        with pytest.raises(schema.SchemaError, match="transaction_required"):
            schema.initialize_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        schema.initialize_schema(conn)
        assert conn.in_transaction
        with pytest.raises(schema.SchemaError, match="invalid_authority"):
            schema.validate_schema(conn)
        _bootstrap(conn)
        schema.validate_schema(conn)
        conn.rollback()
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()
    with pytest.raises(schema.SchemaError):
        schema.connect_existing(path)


def test_reopen_validates_then_configures_wal_full_and_foreign_keys(tmp_path):
    path = tmp_path / "workflow.db"
    _database(path).close()
    conn = schema.connect_existing(path)
    try:
        schema.configure_connection(conn)
        schema.validate_schema(conn)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert 0 < conn.execute("PRAGMA busy_timeout").fetchone()[0] <= 5000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            conn.execute("PRAGMA application_id").fetchone()[0] == schema.APPLICATION_ID
        )
    finally:
        conn.close()


@pytest.mark.parametrize("contents", [b"", b"not sqlite\x00broken"])
def test_empty_or_corrupt_database_is_preserved_and_rejected(tmp_path, contents):
    path = tmp_path / "workflow.db"
    path.write_bytes(contents)
    with pytest.raises(schema.SchemaError):
        schema.connect_existing(path)
    assert path.read_bytes() == contents
    assert not path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "PRAGMA application_id=42",
        "PRAGMA user_version=2",
        "PRAGMA user_version=0",
        "DROP TABLE cleanup_items",
        "ALTER TABLE films ADD COLUMN unexpected TEXT",
        "DROP INDEX one_unresolved_assignment_per_job",
        "DELETE FROM authority",
    ],
)
def test_unknown_or_partial_database_is_not_repaired(tmp_path, mutation):
    path = tmp_path / "workflow.db"
    conn = _database(path)
    conn.execute(mutation)
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with pytest.raises(schema.SchemaError):
        schema.connect_existing(path)
    assert path.read_bytes() == before


def test_foreign_database_keeps_delete_journal_and_its_schema(tmp_path):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE films(film_id TEXT)")
    before = path.read_bytes()
    with pytest.raises(schema.SchemaError, match="wrong_application_id"):
        schema.connect_existing(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_same_index_name_with_weaker_predicate_is_rejected(tmp_path):
    path = tmp_path / "workflow.db"
    conn = _database(path)
    conn.execute("DROP INDEX one_unresolved_assignment_per_job")
    conn.execute(
        "CREATE UNIQUE INDEX one_unresolved_assignment_per_job "
        "ON assignments(job_id) WHERE state='running'"
    )
    conn.commit()
    conn.close()
    with pytest.raises(schema.SchemaError, match="invalid_schema"):
        schema.connect_existing(path)


def test_foreign_key_data_and_bootstrap_identity_are_validated(tmp_path):
    path = tmp_path / "workflow.db"
    conn = _database(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE credentials SET principal_id='missing'")
    conn.commit()
    with pytest.raises(schema.SchemaError):
        schema.validate_schema(conn)
    conn.execute("UPDATE credentials SET principal_id='operator'")
    conn.execute("UPDATE principals SET role='observer'")
    conn.commit()
    with pytest.raises(schema.SchemaError, match="invalid_authority"):
        schema.validate_schema(conn)
    conn.close()


@pytest.mark.parametrize("point", ["ddl", "version", "commit"])
def test_creation_faults_leave_no_committed_schema(tmp_path, monkeypatch, point):
    real_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if point == "ddl" and sql.lstrip().startswith("CREATE TABLE attempts"):
                raise sqlite3.OperationalError("injected DDL failure")
            if point == "version" and sql.startswith("PRAGMA user_version="):
                raise sqlite3.OperationalError("injected version failure")
            return super().execute(sql, *args, **kwargs)

        def commit(self):
            if point == "commit":
                raise sqlite3.OperationalError("injected commit failure")
            return super().commit()

    def connect(*args, **kwargs):
        kwargs["factory"] = FailingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(schema.sqlite3, "connect", connect)
    path = tmp_path / "workflow.db"
    conn = schema.connect_new(path)
    try:
        schema.configure_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises((schema.SchemaError, sqlite3.OperationalError)):
            schema.initialize_schema(conn)
            _bootstrap(conn)
            conn.commit()
        conn.rollback()
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        conn.close()


def test_schema_rejects_reinitializing_any_nonempty_database(tmp_path):
    conn = _database(tmp_path / "workflow.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(schema.SchemaError, match="database_not_empty"):
            schema.initialize_schema(conn)
        conn.rollback()
        schema.validate_schema(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("committed", [False, True])
def test_process_exit_before_or_after_commit_is_atomic(tmp_path, committed):
    path = tmp_path / "workflow.db"
    script = """
import os, sys
from pathlib import Path
from taskpaw_v3.workflow import schema
from taskpaw_v3.tests.test_workflow_migrations import _bootstrap
conn = schema.connect_new(Path(sys.argv[1]))
schema.configure_connection(conn)
conn.execute('BEGIN IMMEDIATE')
schema.initialize_schema(conn)
_bootstrap(conn)
if sys.argv[2] == 'commit':
    conn.commit()
os._exit(23)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            "commit" if committed else "rollback",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    if committed:
        conn = schema.connect_existing(path)
        try:
            assert (
                conn.execute("SELECT authority_id FROM authority").fetchone()[0]
                == "authority"
            )
        finally:
            conn.close()
    else:
        with pytest.raises(schema.SchemaError):
            schema.connect_existing(path)


def test_backup_contains_committed_wal_but_not_uncommitted_changes(tmp_path):
    path = tmp_path / "workflow.db"
    conn = _database(path)
    try:
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE authority SET epoch=2")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE authority SET epoch=3")
        backup = schema.backup_database(path)
        assert backup != path and backup.is_file()
        snapshot = schema.connect_existing(backup)
        try:
            assert snapshot.execute("SELECT epoch FROM authority").fetchone()[0] == 2
        finally:
            snapshot.close()
        conn.rollback()
        schema.validate_schema(conn)
    finally:
        conn.close()


def test_backup_failure_preserves_source_and_removes_owned_partial(
    tmp_path, monkeypatch
):
    path = tmp_path / "workflow.db"
    _database(path).close()
    original = path.read_bytes()

    def fail_fsync(_fd):
        raise OSError("injected flush failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(schema.SchemaError, match="backup_failed"):
        schema.backup_database(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.bak"))


def test_validation_preserves_store_row_factory(tmp_path):
    conn = _database(tmp_path / "workflow.db")
    try:
        conn.row_factory = sqlite3.Row
        schema.validate_schema(conn)
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def _film(conn: sqlite3.Connection) -> None:
    # Deliberate relational fixtures, not proof of public API admission.
    conn.execute(
        "INSERT INTO films(film_id,intake_session_id,display_name,policy_revision,created_at,body_json) "
        "VALUES('film','intake','Example',1,?, '{}')",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO manifests(film_id,kind,revision,manifest_hash,created_at,body_json) "
        "VALUES('film','input',1,?,?, '{}')",
        ("b" * 64, STAMP),
    )


def _attempt(conn: sqlite3.Connection, name: str, job: str, number: int) -> None:
    conn.execute(
        "INSERT INTO attempts(attempt_id,job_id,attempt_number,owner_principal_id,boot_id,"
        "epoch,fence,input_hash,status,started_at,body_json) VALUES(?,?,?,'operator',"
        "'boot',1,?,'input-hash','running',?,'{}')",
        (name, job, number, number, STAMP),
    )


def _assignment(conn: sqlite3.Connection, name: str, job: str, attempt: str) -> None:
    conn.execute(
        "INSERT INTO assignments(assignment_id,job_id,attempt_id,owner_principal_id,"
        "boot_id,epoch,fence,physical_resource_id,claimed_at,renewed_at,lease_deadline,"
        "state,body_json) VALUES(?,?,?,'operator','boot',1,1,'gpu',?,?,?,'unknown','{}')",
        (name, job, attempt, STAMP, STAMP, STAMP),
    )


def test_unknown_assignment_and_resource_owner_remain_exclusive(tmp_path):
    conn = _database(tmp_path / "workflow.db")
    try:
        _film(conn)
        conn.execute(
            "INSERT INTO physical_resources(physical_resource_id,device_id,identity_hash,body_json) "
            "VALUES('gpu','device','hardware-identity','{}')"
        )
        for job, stage in [("job-1", "restore"), ("job-2", "asr")]:
            conn.execute(
                "INSERT INTO stages(job_id,film_id,input_revision,input_hash,stage,status,body_json) "
                "VALUES(?,'film',1,'input-hash',?,'running','{}')",
                (job, stage),
            )
        _attempt(conn, "attempt-1", "job-1", 1)
        _attempt(conn, "attempt-2", "job-1", 2)
        _attempt(conn, "attempt-3", "job-2", 1)
        _assignment(conn, "assignment-1", "job-1", "attempt-1")
        with pytest.raises(sqlite3.IntegrityError):
            _assignment(conn, "assignment-2", "job-1", "attempt-2")
        _assignment(conn, "assignment-3", "job-2", "attempt-3")
        conn.execute(
            "INSERT INTO resource_owners(assignment_id,physical_resource_id,attempt_id,"
            "owner_generation,state,body_json) VALUES('assignment-1','gpu','attempt-1',1,'unknown','{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO resource_owners(assignment_id,physical_resource_id,attempt_id,"
                "owner_generation,state,body_json) VALUES('assignment-3','gpu','attempt-3',2,'running','{}')"
            )
        conn.execute(
            "UPDATE resource_owners SET state='resolved',resolved_at=? WHERE assignment_id='assignment-1'",
            (STAMP,),
        )
        conn.execute(
            "INSERT INTO resource_owners(assignment_id,physical_resource_id,attempt_id,"
            "owner_generation,state,body_json) VALUES('assignment-3','gpu','attempt-3',2,'running','{}')"
        )
        assert conn.execute("SELECT count(*) FROM resource_owners").fetchone()[0] == 2
        schema.validate_schema(conn)
    finally:
        conn.close()


def test_context_pointer_and_approval_hash_are_relationally_bound(tmp_path):
    conn = _database(tmp_path / "workflow.db")
    try:
        _film(conn)
        conn.execute(
            "INSERT INTO approval_contexts(film_id,context_revision,context_hash,binding_hash,"
            "state,created_at,body_json) VALUES('film',1,'context','binding','ready',?,'{}')",
            (STAMP,),
        )
        conn.execute(
            "UPDATE films SET current_context_revision=1,context_state='ready'"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE films SET current_context_revision=2")
        conn.execute(
            "INSERT INTO approvals(approval_id,film_id,context_revision,context_hash,binding_hash,"
            "operator_principal_id,epoch,issued_at,expires_at,body_json) "
            "VALUES('approval','film',1,'context','binding','operator',1,?,?,'{}')",
            (STAMP, "2027-01-01T00:00:00+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE approvals SET context_hash='other-context'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM approval_contexts")
        schema.validate_schema(conn)
    finally:
        conn.close()


def test_constraint_removal_with_same_columns_is_rejected(tmp_path):
    path = tmp_path / "workflow.db"
    conn = _database(path)
    # Empty cleanup_items has no dependents. Recreate all columns but weaken its FK.
    conn.execute("DROP TABLE cleanup_items")
    weaker = schema.TABLES["cleanup_items"].replace(
        " REFERENCES operations(operation_id) ON DELETE RESTRICT", ""
    )
    conn.execute(weaker)
    with pytest.raises(schema.SchemaError, match="invalid_schema"):
        schema.validate_schema(conn)
    conn.close()


def test_rw_connection_failure_closes_preflight_and_sanitizes_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "workflow.db"
    _database(path).close()
    real_connect = sqlite3.connect
    opened = []

    def connect(database, *args, **kwargs):
        if database.endswith("?mode=rw"):
            raise sqlite3.OperationalError("private location from native error")
        conn = real_connect(database, *args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(schema.sqlite3, "connect", connect)
    with pytest.raises(schema.SchemaError, match="^database_unavailable$"):
        schema.connect_existing(path)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_resolved_network_location_is_rejected_before_file_creation(
    tmp_path, monkeypatch
):
    def unsafe_open(*args, **kwargs):
        raise AssertionError("must reject before any filesystem open")

    with monkeypatch.context() as patch:
        patch.setattr(
            Path, "resolve", lambda self: Path("//example.invalid/share/workflow.db")
        )
        patch.setattr(schema.os, "open", unsafe_open)
        with pytest.raises(schema.SchemaError, match="local_database_required"):
            schema.connect_new(tmp_path / "workflow.db")
