"""Upgrade a real T01 public-API fixture without rewriting accepted facts."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from taskpaw_v3.tests.test_workflow_store import Clock
from taskpaw_v3.workflow import schema, schema_intake
from taskpaw_v3.workflow.auth import authenticate
from taskpaw_v3.workflow.store import WorkflowStore

FIXTURES = Path(__file__).parent / "fixtures/workflow"
FIXTURE = FIXTURES / "database-v1-archive-pending-cleanup.sql"
META = json.loads(FIXTURE.with_suffix(".json").read_text(encoding="utf-8"))
INTAKE_TABLES = set(schema_intake.TABLES)
# tables a v1 file gains on its upgrade (v2 intake, v3 U1 plans, v4 pins/routes)
ADDED_TABLES = INTAKE_TABLES | set(schema.V4_TABLES)


def legacy_database(path):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == META["fixture_sha256"]
    with sqlite3.connect(path) as conn:
        conn.executescript(FIXTURE.read_text(encoding="utf-8"))
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def rows(path):
    with sqlite3.connect(path) as conn:
        names = [
            value[0]
            for value in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        return {
            name: sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
            for name in names
            if name not in ADDED_TABLES
        }


def test_upgrade_preserves_all_legacy_rows_and_receipt_bound_public_view(tmp_path):
    path = tmp_path / "workflow.db"
    legacy_database(path)
    before = rows(path)
    with WorkflowStore.open(path, clock=Clock()) as store:
        operator = authenticate(
            store,
            META["operator_credential_id"],
            SecretStr("synthetic-operator-credential-for-tests"),
        )
        assert (
            store.get_film(operator, META["film_id"]).model_dump(mode="json")
            == META["expected_view"]
        )
    assert rows(path) == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert ADDED_TABLES <= {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in INTAKE_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert rows(backups[0]) == before
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    with WorkflowStore.open(path, clock=Clock()):
        pass
    assert list(tmp_path.glob("*.bak")) == backups


def test_backup_failure_prevents_any_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "workflow.db"
    legacy_database(path)
    original = path.read_bytes()

    def fail_backup(*args, **kwargs):
        raise OSError("synthetic backup unavailable")

    monkeypatch.setattr(schema, "_snapshot_database", fail_backup)
    with pytest.raises(schema.SchemaError, match="backup_failed"):
        schema.connect_existing(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize("point", ["ddl", "version", "commit"])
def test_upgrade_failure_rolls_back_schema_version_and_preserves_business_facts(
    tmp_path, monkeypatch, point
):
    path = tmp_path / "workflow.db"
    legacy_database(path)
    before = rows(path)
    real_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if point == "ddl" and sql.startswith("CREATE TABLE intake_snapshots"):
                raise sqlite3.OperationalError("injected intake DDL failure")
            if point == "version" and sql == "PRAGMA user_version=4":
                raise sqlite3.OperationalError("injected version failure")
            return super().execute(sql, *args, **kwargs)

        def commit(self):
            if point == "commit" and self.in_transaction:
                raise sqlite3.OperationalError("injected migration commit failure")
            return super().commit()

    def connect(database, *args, **kwargs):
        if str(database).endswith("?mode=rw"):
            kwargs["factory"] = FailingConnection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(schema.sqlite3, "connect", connect)
    with pytest.raises(schema.SchemaError, match="migration_failed"):
        schema.connect_existing(path)
    assert rows(path) == before
    with real_connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not ADDED_TABLES.intersection(
            row[0] for row in conn.execute("SELECT name FROM sqlite_master")
        )
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1 and rows(backups[0]) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "PRAGMA user_version=5",
        "DROP INDEX source_identity_references",
        "ALTER TABLE films ADD COLUMN extra TEXT",
    ],
)
def test_unknown_or_modified_v1_schema_rejected_before_backup_or_writes(
    tmp_path, monkeypatch, mutation
):
    path = tmp_path / "workflow.db"
    legacy_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute(mutation)
    original = path.read_bytes()

    def forbidden_backup(*args, **kwargs):
        pytest.fail("unrecognized schema must not enter migration")

    monkeypatch.setattr(schema, "_snapshot_database", forbidden_backup)
    with pytest.raises(schema.SchemaError):
        schema.connect_existing(path)
    assert path.read_bytes() == original


def test_v1_wal_commits_are_in_backup_before_upgrade(tmp_path):
    path = tmp_path / "workflow.db"
    legacy_database(path)
    with sqlite3.connect(path) as old:
        old.execute("PRAGMA journal_mode=WAL")
        old.execute("PRAGMA wal_autocheckpoint=0")
        old.execute("UPDATE authority SET epoch=2")
        old.commit()
        conn = schema.connect_existing(path)
        try:
            assert conn.execute("SELECT epoch FROM authority").fetchone()[0] == 2
        finally:
            conn.close()
        backups = list(tmp_path.glob("*.bak"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("SELECT epoch FROM authority").fetchone()[0] == 2
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
