"""Hub-owned availability incidents, independent of Agent event delivery."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.hub.server import poller as module
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore


class Clock:
    seconds = 0.0

    def now(self):
        return datetime(2026, 10, 25, tzinfo=timezone.utc) + timedelta(
            seconds=self.seconds
        )


@pytest.fixture
def monitored(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module, "_now", clock.now)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.seconds)
    store = HubStore(tmp_path / "hub.db")
    sid = store.add_server("windows", "127.0.0.1", 1)
    poller = Poller(store, "", lambda: False, lambda: "", seed_fresh_seconds=60)
    monkeypatch.setattr(poller, "fetch_events", lambda *_: [])
    yield store, poller, sid, clock
    poller.stop()
    assert poller.stopped()
    store.close()


def sample(monitored, monkeypatch, seconds, reason=None, *, active=False):
    store, poller, sid, clock = monitored
    clock.seconds = seconds

    def fetch(_server):
        if reason:
            poller._fetch_errors[sid] = reason
            return False, None
        poller._fetch_errors.pop(sid, None)
        return True, json.dumps({"machine": "windows", "monitors": {}})

    monkeypatch.setattr(poller, "fetch_status", fetch)
    poller._poll_server(store.get_server(sid), active=active)


def events(store):
    # Reading the old store's existing history also gives a behavioural RED:
    # current polling records status failures but emits no availability events.
    table = (
        "hub_events"
        if store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='hub_events'"
        ).fetchone()
        else "events"
    )
    return store._conn.execute(
        f"SELECT monitor,message,level FROM {table} ORDER BY id"
    ).fetchall()


def test_debounce_and_one_persisted_offline_recovery(monitored, monkeypatch):
    store, _, _, _ = monitored
    sample(monitored, monkeypatch, 0)
    sample(monitored, monkeypatch, 10, "upstream_failed")
    sample(monitored, monkeypatch, 40)
    assert events(store) == []
    for point in (50, 60, 100):
        sample(monitored, monkeypatch, point, "http_auth")
    assert events(store) == []  # three failures alone do not meet the grace time
    sample(monitored, monkeypatch, 170, "http_auth")
    assert len(events(store)) == 1
    assert events(store)[0][0] == "agent-availability"
    assert "http_auth" in events(store)[0][1]
    assert events(store)[0][2] == "error"
    sample(monitored, monkeypatch, 180, "http_auth")
    assert len(events(store)) == 1
    sample(monitored, monkeypatch, 190)
    sample(monitored, monkeypatch, 200)
    assert [event[2] for event in events(store)] == ["error", "info"]
    assert (
        store._conn.execute("SELECT count(*) FROM delivery_outbox").fetchone()[0] == 0
    )


@pytest.mark.parametrize("reason", ["http_auth", "json_invalid", "upstream_failed"])
def test_initial_failure_grace_preserves_cause(monitored, monkeypatch, reason):
    store, _, _, _ = monitored
    sample(monitored, monkeypatch, 0, reason)
    sample(monitored, monkeypatch, 120, reason)
    assert events(store) == []  # time alone does not invent failed samples
    sample(monitored, monkeypatch, 121, reason)
    assert len(events(store)) == 1
    assert reason in events(store)[0][1]


def test_old_cache_is_not_current_health(monitored, monkeypatch):
    _, poller, sid, clock = monitored
    sample(monitored, monkeypatch, 0)
    clock.seconds = 61
    snap = poller.snapshot_statuses()[sid]
    assert not snap["online"]
    assert snap["status_health"]["state"] == "stale"
    assert snap["status_health"]["age_seconds"] == 61
    assert snap["snapshot"]["machine"] == "windows"
    assert not poller.status_snapshot()[0]["reachable"]


@pytest.mark.parametrize(
    ("wall_seconds", "awake_seconds", "state", "reason", "age"),
    [
        (3601, 1, "stale", "status_stale", 3601),
        (10, 61, "stale", "status_stale", 61),
        (-1, 1, "error", "clock_changed", None),
        (60, 60, "ok", None, 60),
    ],
)
def test_live_cache_freshness_with_independent_clocks(
    monitored, monkeypatch, wall_seconds, awake_seconds, state, reason, age
):
    store, poller, sid, clock = monitored
    sample(monitored, monkeypatch, 0)
    clock.seconds = wall_seconds
    # On macOS monotonic time excludes system sleep. A backwards wall-clock
    # adjustment must also not make a previously expired sample young again.
    monkeypatch.setattr(module.time, "monotonic", lambda: awake_seconds)
    snap = poller.snapshot_statuses()[sid]
    assert snap["status_health"]["state"] == state
    assert snap["status_health"]["error_code"] == reason
    assert snap["status_health"]["age_seconds"] == age
    assert snap["online"] is (state == "ok")
    assert poller.status_snapshot()[0]["reachable"] is (state == "ok")
    assert snap["snapshot"]["machine"] == "windows"
    assert events(store) == []  # Age alone does not invent a failed sample.
    sample(monitored, monkeypatch, wall_seconds + 1)
    assert poller.snapshot_statuses()[sid]["online"]
    assert poller.snapshot_statuses()[sid]["status_health"]["age_seconds"] == 0


def test_restart_old_cache_reports_stale(monitored, monkeypatch):
    store, _, sid, clock = monitored
    sample(monitored, monkeypatch, 0)
    # record_status uses its own real UTC clock; seed it to our controlled time.
    store._conn.execute(
        "UPDATE upstream_status SET last_good_at=? WHERE server_id=?",
        (clock.now().isoformat(timespec="microseconds"), sid),
    )
    store._conn.commit()
    clock.seconds = 61
    restarted = Poller(store, "", lambda: False, lambda: "", seed_fresh_seconds=60)
    try:
        snap = restarted.snapshot_statuses()[sid]
        assert not snap["online"]
        assert snap["status_health"]["state"] == "stale"
    finally:
        restarted.stop()
        assert restarted.stopped()


def test_restart_preserves_incident_without_counting_downtime(monitored, monkeypatch):
    store, poller, sid, clock = monitored
    sample(monitored, monkeypatch, 0, "upstream_failed")
    sample(monitored, monkeypatch, 60, "upstream_failed")
    poller.stop()
    assert poller.stopped()
    path = store.db_path
    store.close()
    store = HubStore(path)
    restarted = Poller(store, "", lambda: False, lambda: "")
    monkeypatch.setattr(restarted, "fetch_events", lambda *_: [])
    resumed = store, restarted, sid, clock
    try:
        sample(resumed, monkeypatch, 10000, "upstream_failed")
        assert events(store) == []
        sample(resumed, monkeypatch, 10060, "upstream_failed")
        assert len(events(store)) == 1
        restarted.stop()
        assert restarted.stopped()
        store.close()
        store = HubStore(path)
        restarted = Poller(store, "", lambda: False, lambda: "")
        monkeypatch.setattr(restarted, "fetch_events", lambda *_: [])
        resumed = store, restarted, sid, clock
        sample(resumed, monkeypatch, 20000, "upstream_failed")
        assert len(events(store)) == 1
        sample(resumed, monkeypatch, 20001)
        sample(resumed, monkeypatch, 20002)
        assert [event[2] for event in events(store)] == ["error", "info"]
    finally:
        restarted.stop()
        assert restarted.stopped()
        store.close()


@pytest.mark.parametrize("setter", ["set_server_enabled", "update_server"])
def test_disable_reenable_between_samples_resets_incident(
    monitored, monkeypatch, setter
):
    store, _, sid, _ = monitored
    for point in (0, 60, 120):
        sample(monitored, monkeypatch, point, "http_auth")
    assert len(events(store)) == 1
    if setter == "set_server_enabled":
        store.set_server_enabled(sid, False)
        store.set_server_enabled(sid, True)
    else:
        store.update_server(sid, enabled=False)
        store.update_server(sid, enabled=True)
    sample(monitored, monkeypatch, 121)
    assert len(events(store)) == 1  # explicit disable is not a recovery
    for point in (122, 123, 124):
        sample(monitored, monkeypatch, point, "upstream_failed")
    assert len(events(store)) == 1
    sample(monitored, monkeypatch, 242, "upstream_failed")
    assert len(events(store)) == 2


def test_disabled_inflight_result_does_not_create_incident(monitored, monkeypatch):
    store, poller, sid, clock = monitored
    for point in (0, 60):
        sample(monitored, monkeypatch, point, "http_auth")
    server = store.get_server(sid)
    store.set_server_enabled(sid, False)
    clock.seconds = 120
    poller._availability.observe(server, False, "http_auth", False)
    assert events(store) == []
    assert not store._conn.execute("SELECT * FROM hub_availability").fetchall()


def test_disabled_pending_grace_cannot_carry_into_reenable(monitored, monkeypatch):
    store, _, sid, _ = monitored
    sample(monitored, monkeypatch, 0, "http_auth")
    sample(monitored, monkeypatch, 119, "http_auth")
    store.set_server_enabled(sid, False)
    store.set_server_enabled(sid, True)
    for point in (120, 121, 122):
        sample(monitored, monkeypatch, point, "http_auth")
    assert events(store) == []
    sample(monitored, monkeypatch, 240, "http_auth")
    assert len(events(store)) == 1


def test_changed_endpoint_drops_previous_incident_and_old_inflight_result(
    monitored, monkeypatch
):
    store, poller, sid, clock = monitored
    for point in (0, 60, 120):
        sample(monitored, monkeypatch, point, "http_auth")
    old_server = store.get_server(sid)
    store.update_server(sid, port=2)
    clock.seconds = 121
    poller._availability.observe(old_server, True, None, False)
    assert len(events(store)) == 1
    sample(monitored, monkeypatch, 122)
    assert len(events(store)) == 1


def test_stop_rejects_further_availability_observations(monitored, monkeypatch):
    store, poller, _, _ = monitored
    sample(monitored, monkeypatch, 0, "http_auth")
    sample(monitored, monkeypatch, 60, "http_auth")
    prior = store._conn.execute("SELECT * FROM hub_availability").fetchall()
    poller.stop()
    assert poller.stopped()
    sample(monitored, monkeypatch, 120, "http_auth")
    assert events(store) == []
    assert store._conn.execute("SELECT * FROM hub_availability").fetchall() == prior


@pytest.mark.parametrize(
    ("table", "operation"),
    [
        ("hub_events", "INSERT"),
        ("delivery_outbox", "INSERT"),
        ("hub_availability", "UPDATE"),
    ],
)
def test_state_event_outbox_commit_or_rollback_together(
    monitored, monkeypatch, table, operation
):
    store, _, sid, _ = monitored
    for point in (0, 60):
        sample(monitored, monkeypatch, point, "json_invalid", active=True)
    previous = store._conn.execute("SELECT * FROM hub_availability").fetchall()
    store._conn.execute(
        f"CREATE TEMP TRIGGER fail_transition BEFORE {operation} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    store._conn.commit()
    sample(monitored, monkeypatch, 120, "json_invalid", active=True)
    assert events(store) == []
    assert store._conn.execute("SELECT * FROM hub_availability").fetchall() == previous
    assert (
        store._conn.execute("SELECT count(*) FROM delivery_outbox").fetchone()[0] == 0
    )
    store._conn.execute("DROP TRIGGER fail_transition")
    store._conn.commit()
    sample(monitored, monkeypatch, 130, "json_invalid", active=True)
    sample(monitored, monkeypatch, 140, "json_invalid", active=True)
    assert len(events(store)) == 1
    assert (
        store._conn.execute("SELECT count(*) FROM delivery_outbox").fetchone()[0] == 1
    )
    assert store._conn.execute(
        "SELECT announced FROM hub_availability WHERE server_id=?", (sid,)
    ).fetchone() == (1,)


def test_missing_cache_timestamp_is_stale():
    health = Poller._health({"status_json": "{}"}, fresh_seconds=60)
    assert health["state"] == "stale"
    assert health["error_code"] == "status_stale"
    assert health["age_seconds"] is None


def test_storage_failure_does_not_claim_agent_outage(monitored, monkeypatch):
    store, _, _, _ = monitored

    def fail(*_):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "record_status", fail)
    for point in (0, 60, 120):
        sample(monitored, monkeypatch, point)
    assert events(store) == []
    assert not store._conn.execute("SELECT * FROM hub_availability").fetchall()
