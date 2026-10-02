"""Real legacy schema, isolated data, and offline outbox recovery regressions."""

from __future__ import annotations

import ast
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from taskpaw_v3.hub.server import outbox_migration as migration
from taskpaw_v3.hub.server import poller as poller_module
from taskpaw_v3.hub.server import store as store_module
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore


def legacy_db(
    path: Path,
    *,
    timestamp: str = "2026-10-01T12:00:00",
    payload: str = '{"text":"fake"}',
) -> None:
    # Extract the actual frozen writer's DDL without importing its GUI/runtime.
    tree = ast.parse(
        (Path(__file__).resolve().parents[2] / "taskpaw_hub.py").read_text()
    )
    ddl = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "CREATE TABLE IF NOT EXISTS delivery_outbox" in n.value
    )
    with sqlite3.connect(path) as conn:
        conn.execute(ddl)
        conn.execute(
            "INSERT INTO delivery_outbox(server_name,payload_json,kind,delivery_state,attempts,next_attempt_at,created_at) VALUES(?,?,'event','failed',3,?,?)",
            ("legacy", payload, timestamp, timestamp),
        )


def test_real_v2_naive_rows_do_not_stop_two_agent_poll_cycles(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    legacy_db(path)
    with_store = HubStore(path)
    try:
        with_store.add_server("one", "127.0.0.1")
        with_store.add_server("two", "127.0.0.2")
        poller = Poller(with_store, "http://invalid", lambda: True, lambda: "fake")
        sampled = []
        monkeypatch.setattr(
            poller,
            "fetch_status",
            lambda server: (sampled.append(server["name"]) or True, "{}"),
        )
        monkeypatch.setattr(poller, "fetch_events", lambda server: [])
        for _ in range(2):
            poller.poll_once()
        assert sampled == ["one", "two", "one", "two"]
        assert with_store._conn.execute(
            "SELECT attempts,created_at FROM delivery_outbox"
        ).fetchall() == [(3, "2026-10-01T12:00:00")]
        assert with_store._conn.execute(
            "SELECT reason FROM outbox_quarantine"
        ).fetchall() == [("source_timezone_required",)]
    finally:
        with_store.close()


def test_real_v2_bad_json_is_preserved_instead_of_deleted(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    legacy_db(
        path,
        timestamp=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        payload="broken fake json",
    )
    store = HubStore(path)
    try:
        poller = Poller(store, "http://invalid", lambda: True, lambda: "fake")
        poller.poll_once()
        assert store._conn.execute(
            "SELECT payload_json,attempts FROM delivery_outbox"
        ).fetchall() == [("broken fake json", 3)]
        assert store._conn.execute(
            "SELECT reason FROM outbox_quarantine"
        ).fetchall() == [("invalid_payload",)]
    finally:
        store.close()


# Portable TZif v1: synthetic zone changes by 90 minutes, including fold/gap.
# No host zone database or tzset: these DST cases execute on Windows too.
def synthetic_zone():
    import io
    import struct
    from zoneinfo import ZoneInfo

    transitions = [
        int(datetime(2026, 3, 8, 7, tzinfo=timezone.utc).timestamp()),
        int(datetime(2026, 11, 1, 6, tzinfo=timezone.utc).timestamp()),
    ]
    data = b"TZif\0" + b"\0" * 15 + struct.pack(">6I", 0, 0, 0, 2, 2, 8)
    data += struct.pack(">2i", *transitions) + b"\x01\x00"
    data += (
        struct.pack(">iBB", -18000, 0, 0)
        + struct.pack(">iBB", -12600, 1, 4)
        + b"STD\0DST\0"
    )
    return ZoneInfo.from_file(io.BytesIO(data)), data


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-01-02T12:00:00Z", "2026-01-02T12:00:00.000000+00:00"),
        ("2026-01-02 12:00:00.123456+05:45", "2026-01-02T06:15:00.123456+00:00"),
        ("2026-01-02T12:00:00.1-03:30", "2026-01-02T15:30:00.100000+00:00"),
    ],
)
def test_aware_instant_precision(value, expected):
    assert (
        migration.normalize_time(value, synthetic_zone()[0], "created_at") == expected
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"2026-01-01T12:00:00",
        "",
        "2026-01-01",
        "2026-02-30T12:00:00",
        "2026-01-01T12:00:00.1234567Z",
        "2026-01-01T25:00:00Z",
    ],
)
def test_invalid_time_is_explicit(value):
    with pytest.raises(migration.RowError, match="invalid_time"):
        migration.normalize_time(value, None, "created_at")


def test_portable_dst_roundtrip_and_fixed_offset_distinction():
    zone, _ = synthetic_zone()
    assert (
        migration.normalize_time("2026-01-02T12:00:00", zone, "created_at")
        == "2026-01-02T17:00:00.000000+00:00"
    )
    assert (
        migration.normalize_time("2026-06-02T12:00:00", zone, "created_at")
        == "2026-06-02T15:30:00.000000+00:00"
    )
    with pytest.raises(migration.RowError, match="ambiguous_time"):
        migration.normalize_time("2026-11-01T01:30:00", zone, "created_at")
    with pytest.raises(migration.RowError, match="nonexistent_time"):
        migration.normalize_time("2026-03-08T02:30:00", zone, "created_at")
    fixed = timezone(timedelta(hours=-3, minutes=-30))
    assert migration.normalize_time(
        "2026-01-02T12:00:00", fixed, "created_at"
    ) != migration.normalize_time("2026-01-02T12:00:00", zone, "created_at")


def test_zone_sources_are_explicit_and_unavailable_never_guessed(tmp_path, monkeypatch):
    assert migration.source_timezone() == (None, None)
    assert migration.source_timezone("UTC")[0] is timezone.utc
    assert migration.source_timezone("-03:30")[0].utcoffset(None) == timedelta(
        hours=-3, minutes=-30
    )
    with pytest.raises(migration.MigrationError, match="invalid_timezone_source"):
        migration.source_timezone("+24:00")
    from zoneinfo import ZoneInfoNotFoundError

    monkeypatch.setattr(
        migration,
        "ZoneInfo",
        lambda name: (_ for _ in ()).throw(ZoneInfoNotFoundError()),
    )
    with pytest.raises(migration.MigrationError, match="timezone_data_unavailable"):
        migration.source_timezone("operator-supplied-zone")


def test_tzif_source_and_bad_file(tmp_path):
    path = tmp_path / "synthetic.tzif"
    path.write_bytes(synthetic_zone()[1])
    zone, spec = migration.source_timezone(tzfile=path)
    assert spec.startswith("tzif-sha256:")
    assert migration.normalize_time("2026-06-02T12:00:00", zone, "created_at").endswith(
        "15:30:00.000000+00:00"
    )
    path.write_bytes(b"not tzif")
    with pytest.raises(migration.MigrationError, match="invalid_timezone_file"):
        migration.source_timezone(tzfile=path)


def raw_rows(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT * FROM delivery_outbox ORDER BY id").fetchall()


def test_preview_is_readonly_apply_preserves_values_and_double_start(tmp_path, capsys):
    path = tmp_path / "hub.db"
    legacy_db(path)
    before = path.read_bytes()
    assert migration.main(["--db", str(path), "--legacy-zone", "+05:45"]) == 0
    preview = json_output(capsys)
    assert preview["normalize_ids"] == [1] and not preview["applied"]
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak"))
    assert (
        migration.main(["--db", str(path), "--legacy-zone", "+05:45", "--apply"]) == 0
    )
    applied = json_output(capsys)
    assert applied["applied"] and applied["backup_file"]
    assert raw_rows(tmp_path / applied["backup_file"])[0][5:7] == (3, None)
    with HubStoreContext(path) as store:
        first = store._conn.execute("SELECT * FROM delivery_outbox").fetchone()
        assert first[5] == 3 and first[2] == '{"text":"fake"}'
        assert first[7:9] == ("2026-10-01T06:15:00.000000+00:00",) * 2
        assert store._conn.execute(
            "SELECT COUNT(*) FROM outbox_migrations"
        ).fetchone() == (1,)
    with HubStoreContext(path) as store:
        assert store._conn.execute("SELECT * FROM delivery_outbox").fetchone() == first
    assert migration.main(["--db", str(path), "--apply"]) == 0
    assert not json_output(capsys)["applied"]
    assert len(list(tmp_path.glob("*.bak"))) == 1


def json_output(capsys):
    import json

    return json.loads(capsys.readouterr().out)


@contextmanager
def HubStoreContext(path):
    store = HubStore(path)
    try:
        yield store
    finally:
        store.close()


def test_unknown_timezone_quarantine_retry_only_after_explicit_repair(tmp_path, capsys):
    path = tmp_path / "hub.db"
    legacy_db(path)
    with HubStoreContext(path) as store:
        assert store.due_deliveries(datetime(2026, 10, 2, tzinfo=timezone.utc)) == []
        assert store._conn.execute(
            "SELECT reason FROM outbox_quarantine"
        ).fetchone() == ("source_timezone_required",)
    assert migration.main(["--db", str(path), "--legacy-zone", "UTC", "--apply"]) == 0
    assert not json_output(capsys)["applied"]
    assert (
        migration.main(
            [
                "--db",
                str(path),
                "--legacy-zone",
                "UTC",
                "--retry-quarantined",
                "--apply",
            ]
        )
        == 0
    )
    assert json_output(capsys)["release_ids"] == [1]
    with HubStoreContext(path) as store:
        assert not store._conn.execute("SELECT * FROM outbox_quarantine").fetchall()
        assert store._conn.execute(
            "SELECT attempts FROM delivery_outbox"
        ).fetchone() == (3,)
        assert store._conn.execute(
            "SELECT COUNT(*) FROM outbox_migrations"
        ).fetchone() == (2,)
    assert migration.main(["--db", str(path), "--retry-quarantined", "--apply"]) == 0
    assert not json_output(capsys)["applied"]
    assert len(list(tmp_path.glob("*.bak"))) == 2


def test_bad_second_timestamp_does_not_half_convert_and_manual_dst_repair(
    tmp_path, capsys
):
    path = tmp_path / "hub.db"
    legacy_db(path, timestamp="2026-11-01T01:30:00")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE delivery_outbox SET created_at='2026-01-02T12:00:00'")
        plan = migration.plan_migration(conn, synthetic_zone()[0])
        assert not plan.normalize and plan.quarantine == [
            (1, "ambiguous_time", "next_attempt_at")
        ]
    with HubStoreContext(path):
        pass
    # The operator's edit is outside this tool. Retry snapshot records the
    # already-edited state; it does not promise a pre-edit snapshot.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delivery_outbox SET created_at='2026-01-02T12:00:00-05:00',next_attempt_at='2026-11-01T01:30:00-05:00'"
        )
    assert migration.main(["--db", str(path), "--apply", "--retry-quarantined"]) == 0
    assert json_output(capsys)["release_ids"] == [1]


@pytest.mark.parametrize(
    "column,value,reason",
    [
        ("created_at", None, "invalid_time"),
        ("created_at", b"fake", "invalid_time"),
        ("next_attempt_at", "2099-02-30T12:00:00", "invalid_time"),
        ("attempts", -1, "invalid_attempts"),
        ("attempts", 1.5, "invalid_attempts"),
        ("attempts", "bad", "invalid_attempts"),
        ("payload_json", "[]", "invalid_payload"),
        ("payload_json", "bad", "invalid_payload"),
        ("kind", "bad", "invalid_kind"),
        ("delivery_state", "bad", "invalid_state"),
    ],
)
def test_bad_rows_all_states_are_retained_and_never_pruned(
    tmp_path, column, value, reason
):
    path = tmp_path / "hub.db"
    # Deliberately unconstrained damaged DB, including invalid NULL values.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE delivery_outbox(id INTEGER PRIMARY KEY,server_name TEXT,payload_json TEXT,kind TEXT,delivery_state TEXT,attempts,last_error TEXT,next_attempt_at,created_at)"
        )
        conn.execute(
            "INSERT INTO delivery_outbox VALUES(1,'fake','{}','event','dead_letter',3,'keep','2000-01-01T00:00:00Z','2000-01-01T00:00:00Z')"
        )
        conn.execute(f"UPDATE delivery_outbox SET {column}=?", (value,))
    before = raw_rows(path)[0]
    with HubStoreContext(path) as store:
        assert store._conn.execute(
            "SELECT reason,field FROM outbox_quarantine"
        ).fetchone() == (reason, column)
        store.prune_dead_letters()
        assert (
            store._conn.execute("SELECT * FROM delivery_outbox").fetchone()[:9]
            == before
        )
    with HubStoreContext(path) as store:
        assert store._conn.execute(
            "SELECT COUNT(*) FROM outbox_migrations"
        ).fetchone() == (1,)


def test_complete_backup_includes_committed_wal_and_restores_all_tables(tmp_path):
    path = tmp_path / "hub.db"
    legacy_db(path)
    writer = sqlite3.connect(path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE private_config(value TEXT)")
        writer.execute("INSERT INTO private_config VALUES('fake private data')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        backup = migration.backup_database(path)
        with sqlite3.connect(backup) as snapshot:
            assert snapshot.execute("SELECT * FROM private_config").fetchall() == [
                ("fake private data",)
            ]
            assert snapshot.execute(
                "SELECT attempts FROM delivery_outbox"
            ).fetchall() == [(3,)]
            assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
        writer.rollback()
        # Restore only an isolated copy, with no active sidecars/connections.
        restored = tmp_path / "restored.db"
        restored.write_bytes(backup.read_bytes())
        assert raw_rows(restored) == raw_rows(path)
        if os.name != "nt":
            assert backup.stat().st_mode & 0o777 == 0o600
    finally:
        writer.close()


@pytest.mark.parametrize("fault", ["deadline", "fsync", "rename", "collision"])
def test_backup_failures_cleanup_and_never_overwrite_existing(
    tmp_path, monkeypatch, fault
):
    path = tmp_path / "hub.db"
    legacy_db(path)
    before = raw_rows(path)
    if fault == "fsync":
        monkeypatch.setattr(
            migration.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fake"))
        )
    elif fault == "rename":
        monkeypatch.setattr(
            migration.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("fake"))
        )
    elif fault == "collision":

        class FakeID:
            hex = "collision"

        monkeypatch.setattr(migration.uuid, "uuid4", lambda: FakeID())
        (tmp_path / "hub.db.outbox-v1-collision.bak").write_bytes(
            b"existing fake backup"
        )
    with pytest.raises(migration.MigrationError):
        migration.backup_database(
            path, deadline_seconds=-1 if fault == "deadline" else 5
        )
    assert raw_rows(path) == before
    assert not list(tmp_path.glob("*.tmp"))
    if fault == "collision":
        assert (
            tmp_path / "hub.db.outbox-v1-collision.bak"
        ).read_bytes() == b"existing fake backup"
    else:
        assert not list(tmp_path.glob("*.bak"))


@pytest.mark.parametrize(
    "stage", ["schema", "update", "quarantine", "version", "commit", "backup"]
)
def test_failed_initial_migration_is_atomic_and_closes_connection(
    tmp_path, monkeypatch, stage
):
    path = tmp_path / "hub.db"
    legacy_db(
        path,
        timestamp="2026-01-01T12:00:00+02:00"
        if stage != "quarantine"
        else "2026-01-01T12:00:00",
    )
    original = raw_rows(path)
    opened = []
    connect = sqlite3.connect

    class FaultConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if stage == "version" and sql.startswith("INSERT INTO outbox_migrations"):
                raise sqlite3.OperationalError("fake version fault")
            return super().execute(sql, parameters)

        def executemany(self, sql, parameters):
            result = super().executemany(sql, parameters)
            if (stage == "update" and sql.startswith("UPDATE delivery_outbox")) or (
                stage == "quarantine"
                and sql.startswith("INSERT INTO outbox_quarantine")
            ):
                raise sqlite3.OperationalError("fake row fault")
            return result

        def commit(self):
            if stage == "commit":
                raise sqlite3.OperationalError("fake commit fault")
            return super().commit()

    def capture(*args, **kwargs):
        # Only the store connection; backup's independent readers remain real.
        if not opened:
            kwargs["factory"] = FaultConnection
            conn = connect(*args, **kwargs)
            opened.append(conn)
            return conn
        return connect(*args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", capture)
    if stage == "schema":
        old = HubStore._migrate

        def migrate_then_fail(self, cursor):
            old(self, cursor)
            raise sqlite3.OperationalError("fake schema fault")

        monkeypatch.setattr(HubStore, "_migrate", migrate_then_fail)
    if stage == "backup":
        monkeypatch.setattr(
            store_module,
            "backup_database",
            lambda path: (_ for _ in ()).throw(
                migration.MigrationError("backup_failed")
            ),
        )
    with pytest.raises((sqlite3.Error, migration.MigrationError)):
        HubStore(path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    assert raw_rows(path) == original
    with connect(path) as conn:
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('outbox_migrations','outbox_quarantine','servers')"
        ).fetchall()
        assert "dedupe_key" not in {
            r[1] for r in conn.execute("PRAGMA table_info(delivery_outbox)")
        }
    assert not list(tmp_path.glob("*.tmp"))
    if stage != "backup":
        assert len(list(tmp_path.glob("*.bak"))) == 1
        assert raw_rows(next(tmp_path.glob("*.bak"))) == original


def test_missing_schema_and_future_version_fail_closed(tmp_path):
    path = tmp_path / "hub.db"
    with closing(sqlite3.connect(path)) as conn:
        with conn:
            conn.execute("CREATE TABLE delivery_outbox(id INTEGER PRIMARY KEY)")
    with pytest.raises(migration.MigrationError, match="invalid_outbox_schema"):
        HubStore(path)
    path.unlink()
    with HubStoreContext(path) as store:
        store._conn.execute("UPDATE outbox_migrations SET version=2")
        store._conn.commit()
    with pytest.raises(migration.MigrationError, match="unsupported_outbox_version"):
        HubStore(path)


def test_runtime_corrupt_row_state_failure_and_send_failure_do_not_stop_healthy_rows(
    tmp_path, monkeypatch
):
    with HubStoreContext(tmp_path / "hub.db") as store:
        now = datetime.now(timezone.utc)
        bad = store.enqueue_delivery("bad", "event", "{}")
        failing = store.enqueue_delivery("failing", "event", '{"text":"failing"}')
        healthy = store.enqueue_delivery("healthy", "event", '{"text":"healthy"}')
        store._conn.execute(
            "UPDATE delivery_outbox SET created_at='bad time' WHERE id=?", (bad,)
        )
        store._conn.commit()
        store.add_server("one", "127.0.0.1")
        store.add_server("two", "127.0.0.2")
        poller = Poller(store, "http://invalid", lambda: True, lambda: "fake")
        order = []
        monkeypatch.setattr(
            poller,
            "fetch_status",
            lambda server: (order.append(server["name"]) or True, "{}"),
        )
        monkeypatch.setattr(poller, "fetch_events", lambda server: [])
        monkeypatch.setattr(poller_module, "_now", lambda: now + timedelta(seconds=1))

        def send(url, token, payload, timeout):
            order.append(payload["text"])
            if payload["text"] == "failing":
                raise TimeoutError("fake timeout")

        monkeypatch.setattr(poller_module, "send_payload", send)
        monkeypatch.setattr(
            store,
            "mark_delivery_failed",
            lambda *args: (_ for _ in ()).throw(
                sqlite3.OperationalError("fake state write")
            ),
        )
        for _ in range(2):
            poller.poll_once()
        assert order == ["one", "two", "failing", "healthy", "one", "two", "failing"]
        assert store._conn.execute(
            "SELECT id FROM delivery_outbox ORDER BY id"
        ).fetchall() == [(bad,), (failing,)]
        assert store._conn.execute(
            "SELECT delivery_id FROM outbox_quarantine"
        ).fetchall() == [(bad,)]
        assert healthy not in [
            r[0] for r in store._conn.execute("SELECT id FROM delivery_outbox")
        ]


@pytest.mark.parametrize("fault", ["query", "quarantine", "delete", "age"])
def test_drain_failure_boundaries_leave_polling_live(tmp_path, monkeypatch, fault):
    with HubStoreContext(tmp_path / "hub.db") as store:
        first = store.enqueue_delivery("first", "event", '{"text":"first"}')
        second = store.enqueue_delivery("second", "event", '{"text":"second"}')
        store.add_server("one", "127.0.0.1")
        store.add_server("two", "127.0.0.2")
        poller = Poller(store, "http://invalid", lambda: True, lambda: "fake")
        sampled, sent = [], []
        monkeypatch.setattr(
            poller,
            "fetch_status",
            lambda server: (sampled.append(server["name"]) or True, "{}"),
        )
        monkeypatch.setattr(poller, "fetch_events", lambda server: [])
        monkeypatch.setattr(
            poller_module,
            "send_payload",
            lambda url, token, payload, timeout: sent.append(payload["text"]),
        )

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("fake store failure")

        if fault == "query":
            monkeypatch.setattr(store, "due_deliveries", fail)
        elif fault == "quarantine":
            store._conn.execute(
                "UPDATE delivery_outbox SET payload_json='bad' WHERE id=?", (first,)
            )
            store._conn.commit()
            monkeypatch.setattr(store, "quarantine_delivery", fail)
        elif fault == "delete":
            delete = store.delete_delivery
            monkeypatch.setattr(
                store, "delete_delivery", lambda i: fail() if i == first else delete(i)
            )
        else:
            validate = poller_module.validate_row

            def fail_age(row):
                if row["id"] == first:
                    # Exercise arithmetic outside validation too: malformed
                    # return represents an unexpected parser/clock failure.
                    return "2026-01-01T00:00:00", "unused", {}
                return validate(row)

            monkeypatch.setattr(poller_module, "validate_row", fail_age)
        for _ in range(2):
            poller.poll_once()
        assert sampled == ["one", "two", "one", "two"]
        if fault != "query":
            assert "second" in sent
            assert (
                store._conn.execute(
                    "SELECT id FROM delivery_outbox WHERE id=?", (second,)
                ).fetchone()
                is None
            )
        assert store._conn.execute(
            "SELECT id FROM delivery_outbox WHERE id=?", (first,)
        ).fetchone() == (first,)


@pytest.mark.parametrize("enabled,token", [(False, "fake"), (True, "")])
def test_disabled_backlog_acks_and_reenable_young_old_quarantine(
    tmp_path, monkeypatch, enabled, token
):
    with HubStoreContext(tmp_path / "hub.db") as store:
        now = datetime.now(timezone.utc)
        young = store.enqueue_delivery("young", "event", '{"text":"young"}', attempts=2)
        old = store.enqueue_delivery("old", "event", '{"text":"old"}', attempts=4)
        bad = store.enqueue_delivery("bad", "event", '{"text":"bad"}')
        store._conn.execute(
            "UPDATE delivery_outbox SET created_at=? WHERE id=?",
            (migration.outbox_time(now - timedelta(days=2)), old),
        )
        store._conn.commit()
        store.quarantine_delivery(bad, "source_timezone_required", "created_at")
        sid = store.add_server("one", "127.0.0.1")
        switch = {"enabled": enabled, "token": token}
        poller = Poller(
            store,
            "http://invalid",
            lambda: switch["enabled"] and bool(switch["token"]),
            lambda: switch["token"],
        )
        sent, alerts = [], []
        monkeypatch.setattr(poller_module, "_now", lambda: now + timedelta(seconds=1))
        monkeypatch.setattr(poller, "fetch_status", lambda server: (True, "{}"))
        monkeypatch.setattr(
            poller, "fetch_events", lambda server: [{"id": 1, "message": "fake"}]
        )
        monkeypatch.setattr(
            poller_module,
            "send_payload",
            lambda url, token, payload, timeout: sent.append(payload),
        )
        monkeypatch.setattr(poller, "emit_local_alert", alerts.append)
        before = store._conn.execute(
            "SELECT * FROM delivery_outbox ORDER BY id"
        ).fetchall()
        poller.poll_once()
        assert (
            not sent
            and before
            == store._conn.execute(
                "SELECT * FROM delivery_outbox ORDER BY id"
            ).fetchall()
        )
        assert poller.last_event_ids[sid] == 1
        assert store._conn.execute("SELECT COUNT(*) FROM events").fetchone() == (1,)
        switch.update(enabled=True, token="fake")
        monkeypatch.setattr(poller, "fetch_events", lambda server: [])
        poller.poll_once()
        poller.poll_once()
        assert sent == [{"text": "young"}] and len(alerts) == 1
        assert store._conn.execute(
            "SELECT delivery_state,attempts FROM delivery_outbox WHERE id=?", (old,)
        ).fetchone() == ("dead_letter", 4)
        assert store._conn.execute(
            "SELECT attempts FROM delivery_outbox WHERE id=?", (bad,)
        ).fetchone() == (0,)
        assert (
            store._conn.execute(
                "SELECT id FROM delivery_outbox WHERE id=?", (young,)
            ).fetchone()
            is None
        )


def test_canonical_due_microseconds_and_formatter_rejects_naive(tmp_path):
    point = datetime(
        2026, 10, 2, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=5, minutes=45))
    )
    with HubStoreContext(tmp_path / "hub.db") as store:
        row_id = store.enqueue_delivery("fake", "event", "{}", next_attempt_at=point)
        assert store.due_deliveries(point - timedelta(microseconds=1)) == []
        assert store.due_deliveries(point)[0]["id"] == row_id
        with pytest.raises(migration.MigrationError, match="aware_time_required"):
            store.enqueue_delivery(
                "fake", "event", "{}", next_attempt_at=point.replace(tzinfo=None)
            )
        assert store._conn.execute(
            "SELECT COUNT(*) FROM delivery_outbox"
        ).fetchone() == (1,)


def test_cli_missing_db_invalid_source_and_noop_do_not_create_or_write(
    tmp_path, capsys
):
    path = tmp_path / "absent.db"
    assert migration.main(["--db", str(path)]) == 1
    assert json_output(capsys) == {"error": "database_not_found"}
    assert not path.exists()
    legacy_db(path)
    before = path.read_bytes()
    assert (
        migration.main(["--db", str(path), "--legacy-zone", "+24:00", "--apply"]) == 1
    )
    assert json_output(capsys) == {"error": "invalid_timezone_source"}
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "state,attempts,alerted",
    [("pending", 0, 0), ("failed", 7, 0), ("dead_letter", 10, 1)],
)
def test_normalization_preserves_entire_queue_identity_and_history(
    tmp_path, state, attempts, alerted
):
    path = tmp_path / "hub.db"
    legacy_db(
        path,
        timestamp="2026-01-02T12:00:00.654321-03:30",
        payload=' {"text": "fake", "extra": 1} ',
    )
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE delivery_outbox ADD COLUMN dedupe_key TEXT")
        conn.execute(
            "UPDATE delivery_outbox SET delivery_state=?,attempts=?,last_error='original fake error',dead_letter_alerted=?,dedupe_key='original-key'",
            (state, attempts, alerted),
        )
        conn.execute(
            "CREATE TABLE status_log(id INTEGER PRIMARY KEY,server_id INTEGER,timestamp TEXT,payload_json TEXT)"
        )
        conn.execute("INSERT INTO status_log VALUES(1,1,'2026-01-02 12:00:00','{}')")
    original = raw_rows(path)[0]
    with HubStoreContext(path) as store:
        migrated = store._conn.execute("SELECT * FROM delivery_outbox").fetchone()
        assert migrated[:7] == original[:7] and migrated[9:] == original[9:]
        assert migrated[7:9] == ("2026-01-02T15:30:00.654321+00:00",) * 2
        assert store._conn.execute(
            "SELECT timestamp,status_json FROM status_log"
        ).fetchone() == ("2026-01-02 12:00:00", "{}")
        assert store._conn.execute(
            "SELECT COUNT(*) FROM outbox_quarantine"
        ).fetchone() == (0,)
        if state != "dead_letter":
            assert (
                store.enqueue_delivery("fake", "event", "{}", dedupe_key="original-key")
                is None
            )


def test_retry_failure_rolls_back_release_and_preserves_pre_retry_snapshot(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "hub.db"
    legacy_db(path)
    with HubStoreContext(path):
        pass
    before = raw_rows(path)
    apply = migration.apply_plan

    def fail_after_apply(conn, plan, backup):
        apply(conn, plan, backup)
        raise sqlite3.OperationalError("fake retry failure")

    monkeypatch.setattr(migration, "apply_plan", fail_after_apply)
    assert (
        migration.main(
            [
                "--db",
                str(path),
                "--apply",
                "--retry-quarantined",
                "--legacy-zone",
                "UTC",
            ]
        )
        == 1
    )
    assert json_output(capsys) == {"error": "database_operation_failed"}
    assert raw_rows(path) == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT reason FROM outbox_quarantine").fetchone() == (
            "source_timezone_required",
        )
        assert conn.execute("SELECT COUNT(*) FROM outbox_migrations").fetchone() == (1,)
    assert len(list(tmp_path.glob("*.bak"))) == 2


def test_backup_native_failure_closes_both_connections(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    legacy_db(path)
    connect = sqlite3.connect
    opened = []

    class FaultReader(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            raise sqlite3.OperationalError("fake read failure")

    def capture(*args, **kwargs):
        if not opened:
            kwargs["factory"] = FaultReader
        conn = connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(migration.sqlite3, "connect", capture)
    with pytest.raises(migration.MigrationError, match="backup_failed"):
        migration.backup_database(path)
    assert len(opened) == 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.bak"))


def test_new_quarantine_logs_once_only_after_committed_upgrade(tmp_path, caplog):
    path = tmp_path / "hub.db"
    legacy_db(path)
    with HubStoreContext(path):
        pass
    with HubStoreContext(path):
        pass
    messages = [
        record.message
        for record in caplog.records
        if "Outbox quarantined" in record.message
    ]
    assert messages == [
        "Outbox quarantined id=1 reason=source_timezone_required field=created_at"
    ]
    assert '"text"' not in " ".join(messages)


def deep_fake_json():
    # 3.12's native decoder permits substantially deeper nesting than 3.10.
    # This isolated ASCII object exceeds both decoders' recursion capacity.
    return '{"fake":' * 16000 + "0" + "}" * 16000


def test_deep_json_legacy_preview_startup_double_start_and_healthy_polling(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "hub.db"
    timestamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    original = deep_fake_json()
    legacy_db(path, timestamp=timestamp, payload=original)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO delivery_outbox(server_name,payload_json,kind,next_attempt_at,created_at) VALUES('healthy','{\"text\":\"healthy\"}','event',?,?)",
            (timestamp, timestamp),
        )
    assert migration.main(["--db", str(path)]) == 0
    assert json_output(capsys)["quarantine"] == [[1, "invalid_payload", "payload_json"]]
    sampled, sent = [], []
    for cycle in range(2):
        with HubStoreContext(path) as store:
            if cycle == 0:
                store.add_server("one", "127.0.0.1")
                store.add_server("two", "127.0.0.2")
            poller = Poller(store, "http://invalid", lambda: True, lambda: "fake")
            monkeypatch.setattr(
                poller,
                "fetch_status",
                lambda server: (sampled.append(server["name"]) or True, "{}"),
            )
            monkeypatch.setattr(poller, "fetch_events", lambda server: [])
            monkeypatch.setattr(
                poller_module,
                "send_payload",
                lambda url, token, payload, timeout: sent.append(payload),
            )
            poller.poll_once()
            assert store._conn.execute(
                "SELECT id,payload_json,attempts,created_at,next_attempt_at FROM delivery_outbox"
            ).fetchall() == [(1, original, 3, timestamp, timestamp)]
            assert store._conn.execute(
                "SELECT reason,field FROM outbox_quarantine"
            ).fetchall() == [("invalid_payload", "payload_json")]
            assert store._conn.execute(
                "SELECT COUNT(*) FROM outbox_migrations"
            ).fetchone() == (1,)
    assert sampled == ["one", "two", "one", "two"]
    assert sent == [{"text": "healthy"}]
    assert migration.main(["--db", str(path), "--apply", "--retry-quarantined"]) == 0
    assert json_output(capsys)["unresolved"] == [[1, "invalid_payload", "payload_json"]]
    assert raw_rows(path)[0][2] == original
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_runtime_deep_json_quarantined_while_healthy_row_continues(
    tmp_path, monkeypatch
):
    with HubStoreContext(tmp_path / "hub.db") as store:
        invalid = store.enqueue_delivery("invalid", "event", deep_fake_json())
        healthy = store.enqueue_delivery("healthy", "event", '{"text":"healthy"}')
        original = store._conn.execute(
            "SELECT * FROM delivery_outbox WHERE id=?", (invalid,)
        ).fetchone()
        sent = []
        poller = Poller(store, "http://invalid", lambda: True, lambda: "fake")
        monkeypatch.setattr(
            poller_module,
            "send_payload",
            lambda url, token, payload, timeout: sent.append(payload),
        )
        poller.poll_once()
        poller.poll_once()
        assert store._conn.execute(
            "SELECT delivery_id,reason FROM outbox_quarantine"
        ).fetchall() == [(invalid, "invalid_payload")]
        assert (
            store._conn.execute(
                "SELECT * FROM delivery_outbox WHERE id=?", (invalid,)
            ).fetchone()
            == original
        )
        assert (
            store._conn.execute(
                "SELECT id FROM delivery_outbox WHERE id=?", (healthy,)
            ).fetchone()
            is None
        )
        assert sent == [{"text": "healthy"}]


@pytest.mark.parametrize(
    "column,value,reason",
    [
        ("created_at", "", "invalid_time"),
        ("next_attempt_at", "bad", "invalid_time"),
        ("payload_json", deep_fake_json(), "invalid_payload"),
        ("attempts", -1, "invalid_attempts"),
    ],
    ids=["created-time", "next-time", "deep-json", "attempts"],
)
def test_runtime_dead_letter_prune_validates_and_preserves_bad_rows_across_restart(
    tmp_path, column, value, reason, caplog
):
    path = tmp_path / "hub.db"
    old_time = migration.outbox_time(datetime.now(timezone.utc) - timedelta(days=8))
    with HubStoreContext(path) as store:
        invalid = store.enqueue_delivery(
            "invalid",
            "event",
            '{"text":"original"}',
            delivery_state="dead_letter",
            attempts=10,
        )
        expired = store.enqueue_delivery(
            "expired", "event", "{}", delivery_state="dead_letter", attempts=10
        )
        young = store.enqueue_delivery(
            "young", "event", "{}", delivery_state="dead_letter", attempts=10
        )
        store._conn.execute(
            "UPDATE delivery_outbox SET created_at=? WHERE id IN (?,?)",
            (old_time, invalid, expired),
        )
        store._conn.execute(
            f"UPDATE delivery_outbox SET {column}=? WHERE id=?", (value, invalid)
        )
        store._conn.commit()
        original = store._conn.execute(
            "SELECT * FROM delivery_outbox WHERE id=?", (invalid,)
        ).fetchone()
        store.prune_dead_letters()
        assert (
            store._conn.execute(
                "SELECT * FROM delivery_outbox WHERE id=?", (invalid,)
            ).fetchone()
            == original
        )
        assert store._conn.execute(
            "SELECT delivery_id,reason,field FROM outbox_quarantine"
        ).fetchall() == [(invalid, reason, column)]
        assert (
            store._conn.execute(
                "SELECT id FROM delivery_outbox WHERE id=?", (expired,)
            ).fetchone()
            is None
        )
        assert store._conn.execute(
            "SELECT id FROM delivery_outbox WHERE id=?", (young,)
        ).fetchone() == (young,)
    with HubStoreContext(path) as store:
        store.prune_dead_letters()
        store.prune_dead_letters()
        assert (
            store._conn.execute(
                "SELECT * FROM delivery_outbox WHERE id=?", (invalid,)
            ).fetchone()
            == original
        )
        assert store._conn.execute(
            "SELECT COUNT(*) FROM outbox_migrations"
        ).fetchone() == (1,)
    assert len([r for r in caplog.records if "Outbox quarantined" in r.message]) == 1


def test_backup_flush_uses_writable_nontruncating_descriptor(tmp_path, monkeypatch):
    path = tmp_path / "hub.db"
    legacy_db(path)
    opened = []
    original_open = Path.open

    def record_open(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        if self.suffix == ".tmp":
            opened.append((stream.writable(), os.fstat(stream.fileno()).st_size))
        return stream

    monkeypatch.setattr(Path, "open", record_open)
    backup = migration.backup_database(path)
    assert len(opened) == 1
    assert opened[0][0] and opened[0][1] > 0
    assert raw_rows(backup) == raw_rows(path)
    with sqlite3.connect(backup) as snapshot:
        assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert not list(tmp_path.glob("*.tmp"))


def test_prune_failure_rolls_back_quarantine_and_valid_deletion(tmp_path, caplog):
    with HubStoreContext(tmp_path / "hub.db") as store:
        invalid = store.enqueue_delivery(
            "invalid", "event", "bad json", delivery_state="dead_letter"
        )
        expired = store.enqueue_delivery(
            "expired", "event", "{}", delivery_state="dead_letter"
        )
        store._conn.execute(
            "UPDATE delivery_outbox SET created_at=?",
            (migration.outbox_time(datetime.now(timezone.utc) - timedelta(days=8)),),
        )
        store._conn.execute(
            "CREATE TRIGGER fail_prune BEFORE DELETE ON delivery_outbox "
            "BEGIN SELECT RAISE(ABORT,'fake prune failure'); END"
        )
        store._conn.commit()
        before = store._conn.execute(
            "SELECT * FROM delivery_outbox ORDER BY id"
        ).fetchall()
        with pytest.raises(sqlite3.IntegrityError, match="fake prune failure"):
            store.prune_dead_letters()
        assert store._conn.execute("SELECT * FROM outbox_quarantine").fetchall() == []
        assert (
            store._conn.execute("SELECT * FROM delivery_outbox ORDER BY id").fetchall()
            == before
        )
        assert not [r for r in caplog.records if "Outbox quarantined" in r.message]
        store._conn.execute("DROP TRIGGER fail_prune")
        store._conn.commit()
        store.prune_dead_letters()
        assert store._conn.execute(
            "SELECT delivery_id FROM outbox_quarantine"
        ).fetchall() == [(invalid,)]
        assert store._conn.execute("SELECT id FROM delivery_outbox").fetchall() == [
            (invalid,)
        ]
        assert expired != invalid
