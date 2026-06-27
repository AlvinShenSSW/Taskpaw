from __future__ import annotations

import importlib
import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def import_taskpaw(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    sys.modules.pop("taskpaw", None)
    return importlib.import_module("taskpaw")


def import_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("taskpaw_hub", None)
    return importlib.import_module("taskpaw_hub")


def test_agent_event_ids_persist_across_restart(tmp_path, monkeypatch):
    taskpaw = import_taskpaw(tmp_path, monkeypatch)

    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1

    taskpaw.add_event("machine", "monitor", "first")
    assert taskpaw._events_queue[-1]["id"] == 1

    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1
    taskpaw._load_event_state()

    taskpaw.add_event("machine", "monitor", "after restart")
    assert taskpaw._events_queue[-1]["id"] == 2


def test_agent_events_endpoint_payload_shape_and_legacy_clear(tmp_path, monkeypatch):
    taskpaw = import_taskpaw(tmp_path, monkeypatch)
    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1
    taskpaw.add_event("machine", "monitor", "done")

    payload = taskpaw.get_events_payload()
    assert set(payload.keys()) == {"events"}
    assert [event["message"] for event in payload["events"]] == ["done"]

    assert taskpaw.get_events_payload() == {"events": []}


def test_agent_ack_keeps_events_until_confirmed(tmp_path, monkeypatch):
    taskpaw = import_taskpaw(tmp_path, monkeypatch)
    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1
    taskpaw.add_event("machine", "monitor", "first")
    taskpaw.add_event("machine", "monitor", "second")

    first_poll = taskpaw.get_events_payload(ack_id=0)["events"]
    after_crash_poll = taskpaw.get_events_payload(ack_id=0)["events"]

    assert [event["id"] for event in first_poll] == [1, 2]
    assert [event["id"] for event in after_crash_poll] == [1, 2]

    acked_poll = taskpaw.get_events_payload(ack_id=2)["events"]
    assert acked_poll == []
    assert taskpaw._events_queue == []


def test_agent_add_event_optional_fields_are_additive(tmp_path, monkeypatch):
    taskpaw = import_taskpaw(tmp_path, monkeypatch)
    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1

    taskpaw.add_event("machine", "monitor", "legacy")
    assert set(taskpaw._events_queue[-1].keys()) == {
        "id",
        "time",
        "machine",
        "monitor",
        "message",
    }

    payload = {"code": 42}
    taskpaw.add_event(
        "machine",
        "monitor",
        "rich",
        level="alert",
        title="Needs attention",
        data=payload,
    )
    payload["code"] = 99
    rich = taskpaw._events_queue[-1]
    assert rich["level"] == "alert"
    assert rich["title"] == "Needs attention"
    assert rich["data"] == {"code": 42}


def test_agent_event_counter_saved_while_lock_held(tmp_path, monkeypatch):
    taskpaw = import_taskpaw(tmp_path, monkeypatch)
    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1
    saved_next_ids = []

    def fake_save_event_state():
        assert taskpaw._events_lock.locked()
        saved_next_ids.append(taskpaw._next_event_id)

    monkeypatch.setattr(taskpaw, "_save_event_state", fake_save_event_state)

    taskpaw.add_event("machine", "monitor", "locked save")

    assert saved_next_ids == [2]


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_hub_get_new_events_sends_ack_without_advancing(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        server = hub.Server(id=7, name="agent", ip="127.0.0.1", port=5678)
        engine.last_event_ids = {7: 2}
        db.set_config("last_event_ids", json.dumps({"7": 2}))
        seen_urls = []

        def fake_urlopen(req, timeout):
            seen_urls.append(req.full_url)
            return FakeHTTPResponse(
                {
                    "events": [
                        {"id": 1, "message": "old"},
                        {"id": 2, "message": "seen"},
                        {"id": 3, "message": "new"},
                        {"id": 4, "message": "newer"},
                    ]
                }
            )

        monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)

        events = engine.get_new_events(server)

        assert [event["id"] for event in events] == [3, 4]
        assert engine.last_event_ids[7] == 2
        assert json.loads(db.get_config("last_event_ids")) == {"7": 2}
        assert seen_urls == ["http://127.0.0.1:5678/events?ack=2"]
    finally:
        db._conn.close()


def test_hub_get_new_events_falls_back_for_legacy_agent(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        server = hub.Server(id=7, name="agent", ip="127.0.0.1", port=5678)
        engine.last_event_ids = {7: 2}
        seen_urls = []

        def fake_urlopen(req, timeout):
            seen_urls.append(req.full_url)
            if "ack=" in req.full_url:
                raise urllib.error.HTTPError(
                    req.full_url, 404, "not found", hdrs=None, fp=None
                )
            return FakeHTTPResponse({"events": [{"id": 3, "message": "new"}]})

        monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)

        events = engine.get_new_events(server)

        assert [event["id"] for event in events] == [3]
        assert seen_urls == [
            "http://127.0.0.1:5678/events?ack=2",
            "http://127.0.0.1:5678/events",
        ]
    finally:
        db._conn.close()


def test_hub_poll_stores_enqueues_then_advances_ack_and_drains_outbox(
    tmp_path, monkeypatch
):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_enabled", "1")
    db.set_config("openclaw_token", "token")
    db.set_config("report_every_n_polls", "999")
    server_id = db.add_server(
        hub.Server(name="agent", ip="127.0.0.1", port=5678, enabled=True)
    )
    db.set_config("last_event_ids", json.dumps({str(server_id): 2}))
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        engine.last_event_ids = {server_id: 2}
        seen_event_urls = []
        sent_payloads = []

        monkeypatch.setattr(
            engine,
            "poll_server",
            lambda server: {"reachable": True, "last_seen": datetime.now(), "monitors": []},
        )

        def fake_urlopen(req, timeout):
            if req.full_url.startswith("http://127.0.0.1:5678/events"):
                seen_event_urls.append(req.full_url)
                return FakeHTTPResponse(
                    {
                        "events": [
                            {"id": 2, "message": "seen"},
                            {"id": 3, "message": "new"},
                            {"id": 4, "message": "newer"},
                        ]
                    }
                )
            if req.full_url == "http://127.0.0.1:18789/hooks/wake":
                sent_payloads.append(json.loads(req.data.decode("utf-8")))
                return FakeHTTPResponse({"ok": True})
            raise AssertionError(req.full_url)

        monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)

        engine.poll_all_servers()

        assert seen_event_urls == [f"http://127.0.0.1:5678/events?ack=2"]
        assert engine.last_event_ids[server_id] == 4
        assert json.loads(db.get_config("last_event_ids")) == {str(server_id): 4}
        rows = db._conn.execute(
            "SELECT message FROM events ORDER BY id"
        ).fetchall()
        assert [row[0] for row in rows] == ["new", "newer"]
        assert sent_payloads == [
            {"text": "TaskPaw Event | agent: new"},
            {"text": "TaskPaw Event | agent: newer"},
        ]
        outbox_count = db._conn.execute(
            "SELECT COUNT(*) FROM delivery_outbox"
        ).fetchone()[0]
        assert outbox_count == 0
    finally:
        db._conn.close()


def test_hub_poll_crash_before_ack_persist_refetches_events(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    # Forwarding enabled + token set so the outbox enqueue path (where the crash
    # is injected) actually runs; without enabled+token, events are stored but
    # never enqueued.
    db.set_config("openclaw_enabled", "1")
    db.set_config("openclaw_token", "token")
    db.set_config("report_every_n_polls", "999")
    server_id = db.add_server(
        hub.Server(name="agent", ip="127.0.0.1", port=5678, enabled=True)
    )
    db.set_config("last_event_ids", json.dumps({str(server_id): 2}))
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        engine.last_event_ids = {server_id: 2}
        seen_event_urls = []

        monkeypatch.setattr(
            engine,
            "poll_server",
            lambda server: {"reachable": True, "last_seen": datetime.now(), "monitors": []},
        )

        def fake_urlopen(req, timeout):
            if req.full_url.startswith("http://127.0.0.1:5678/events"):
                seen_event_urls.append(req.full_url)
                return FakeHTTPResponse({"events": [{"id": 3, "message": "new"}]})
            # OpenClaw POST from outbox drain — succeed quietly (at-least-once may
            # re-deliver the row left by the crashed first poll); don't pollute
            # seen_event_urls.
            return FakeHTTPResponse({"ok": True})

        monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)

        original_enqueue = db.enqueue_delivery
        enqueue_calls = 0

        def enqueue_then_crash(*args, **kwargs):
            nonlocal enqueue_calls
            enqueue_calls += 1
            delivery_id = original_enqueue(*args, **kwargs)
            if enqueue_calls == 1:
                raise RuntimeError("crash before ack persist")
            return delivery_id

        monkeypatch.setattr(db, "enqueue_delivery", enqueue_then_crash)

        with pytest.raises(RuntimeError):
            engine.poll_all_servers()

        assert engine.last_event_ids == {server_id: 2}
        assert json.loads(db.get_config("last_event_ids")) == {str(server_id): 2}

        monkeypatch.setattr(db, "enqueue_delivery", original_enqueue)
        engine.poll_all_servers()

        assert seen_event_urls == [
            f"http://127.0.0.1:5678/events?ack=2",
            f"http://127.0.0.1:5678/events?ack=2",
        ]
        assert engine.last_event_ids[server_id] == 3
        assert json.loads(db.get_config("last_event_ids")) == {str(server_id): 3}
    finally:
        db._conn.close()


def test_hub_write_status_file_writes_atomically(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    monkeypatch.setattr(hub, "STATUS_FILE", tmp_path / "status.md")
    db = hub.DatabaseManager(tmp_path / "hub.db")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        engine.write_status_file(
            {"agent": {"reachable": True, "monitors": [{"name": "Lada", "status": "idle"}]}}
        )
        content = hub.STATUS_FILE.read_text(encoding="utf-8")
        assert "# TaskPaw Hub Status" in content
        assert "## agent: ONLINE" in content
        assert "- Lada: idle" in content
    finally:
        db._conn.close()


def test_hub_failed_delivery_enqueues_outbox(tmp_path, monkeypatch):
    """A failed OpenClaw send is captured in the outbox as a 'failed' row for
    retry (exercises _enqueue_failed_delivery, the shared failure path)."""
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_token", "token")
    try:
        engine = hub.PollingEngine(db, lambda _: None)

        engine._enqueue_failed_delivery(
            "agent",
            "event",
            {"text": "TaskPaw Event | agent: done"},
            urllib.error.URLError("down"),
        )

        rows = db._conn.execute(
            "SELECT server_name, kind, delivery_state, attempts, payload_json FROM delivery_outbox"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0:4] == ("agent", "event", "failed", 1)
        assert json.loads(rows[0][4])["text"] == "TaskPaw Event | agent: done"
    finally:
        db._conn.close()


def test_hub_recovering_openclaw_sink_drains_outbox(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_token", "token")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        db.enqueue_delivery(
            server_name="agent",
            kind="event",
            payload_json=json.dumps({"text": "retry me"}),
            delivery_state="failed",
            attempts=1,
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        sent_payloads = []

        def ok_urlopen(req, timeout):
            sent_payloads.append(json.loads(req.data.decode("utf-8")))
            return FakeHTTPResponse({"ok": True})

        monkeypatch.setattr(hub.urllib.request, "urlopen", ok_urlopen)
        engine.retry_due_deliveries()

        assert sent_payloads == [{"text": "retry me"}]
        count = db._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        assert count == 0
    finally:
        db._conn.close()


def test_hub_retry_drops_malformed_outbox_row_and_continues(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_token", "token")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        db.enqueue_delivery(
            server_name="agent",
            kind="event",
            payload_json="{not json",
            delivery_state="pending",
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        db.enqueue_delivery(
            server_name="agent",
            kind="event",
            payload_json=json.dumps({"text": "still send me"}),
            delivery_state="pending",
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        sent_payloads = []

        def ok_urlopen(req, timeout):
            sent_payloads.append(json.loads(req.data.decode("utf-8")))
            return FakeHTTPResponse({"ok": True})

        monkeypatch.setattr(hub.urllib.request, "urlopen", ok_urlopen)
        engine.retry_due_deliveries()

        assert sent_payloads == [{"text": "still send me"}]
        count = db._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        assert count == 0
    finally:
        db._conn.close()


def test_hub_delivery_outbox_schema_has_due_index_without_timestamp_defaults(
    tmp_path, monkeypatch
):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    try:
        create_sql = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_outbox'"
        ).fetchone()[0]
        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }

        assert "next_attempt_at TEXT NOT NULL DEFAULT" not in create_sql
        assert "created_at TEXT NOT NULL DEFAULT" not in create_sql
        assert "idx_delivery_outbox_due" in indexes
    finally:
        db._conn.close()


def test_hub_outbox_dead_letters_once_after_attempt_cap(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_token", "token")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        db.enqueue_delivery(
            server_name="agent",
            kind="event",
            payload_json=json.dumps({"text": "will die"}),
            delivery_state="failed",
            attempts=9,
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        alerts = []
        monkeypatch.setattr(engine, "emit_local_alert", alerts.append)

        def fail_urlopen(req, timeout):
            raise urllib.error.URLError("still down")

        monkeypatch.setattr(hub.urllib.request, "urlopen", fail_urlopen)

        engine.retry_due_deliveries()
        engine.retry_due_deliveries()

        row = db._conn.execute(
            "SELECT delivery_state, attempts, dead_letter_alerted FROM delivery_outbox"
        ).fetchone()
        assert row == ("dead_letter", 10, 1)
        assert len(alerts) == 1
        assert "dead-lettered" in alerts[0]
    finally:
        db._conn.close()


def test_agent_event_queue_capped(tmp_path, monkeypatch):
    """clear-on-ack retains events until acked; the queue must not grow without
    bound when the Hub never acks. Oldest events are dropped past the cap."""
    taskpaw = import_taskpaw(tmp_path, monkeypatch)
    monkeypatch.setattr(taskpaw, "MAX_EVENTS_QUEUE", 5)
    taskpaw._events_queue.clear()
    taskpaw._next_event_id = 1

    for i in range(8):
        taskpaw.add_event("machine", "monitor", f"evt-{i}")

    # Never acked, but capped at 5 with the oldest dropped.
    assert len(taskpaw._events_queue) == 5
    ids = [e["id"] for e in taskpaw._events_queue]
    assert ids == [4, 5, 6, 7, 8]  # first 3 dropped, ids still monotonic


def test_hub_poll_disabled_openclaw_stores_history_without_outbox(tmp_path, monkeypatch):
    """With OpenClaw forwarding disabled, events are still stored (history) and
    the ack advances, but nothing is enqueued — the outbox must not accumulate
    undeliverable rows."""
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    # openclaw_enabled left unset -> forwarding disabled
    db.set_config("report_every_n_polls", "999")
    server_id = db.add_server(
        hub.Server(name="agent", ip="127.0.0.1", port=5678, enabled=True)
    )
    db.set_config("last_event_ids", json.dumps({str(server_id): 2}))
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        engine.last_event_ids = {server_id: 2}
        monkeypatch.setattr(
            engine,
            "poll_server",
            lambda server: {"reachable": True, "last_seen": datetime.now(), "monitors": []},
        )

        def fake_urlopen(req, timeout):
            if req.full_url.startswith("http://127.0.0.1:5678/events"):
                return FakeHTTPResponse({"events": [{"id": 3, "message": "new"}]})
            raise AssertionError(f"no OpenClaw call expected when disabled: {req.full_url}")

        monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)
        engine.poll_all_servers()

        rows = db._conn.execute("SELECT message FROM events ORDER BY id").fetchall()
        assert [row[0] for row in rows] == ["new"]
        assert json.loads(db.get_config("last_event_ids")) == {str(server_id): 3}
        assert db._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
    finally:
        db._conn.close()


def test_hub_poll_enabled_without_token_does_not_enqueue(tmp_path, monkeypatch):
    """OpenClaw enabled but no token: retry_due_deliveries() no-ops, so enqueuing
    would leak undeliverable rows. Events are stored but the outbox stays empty."""
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_enabled", "1")  # enabled but NO token
    db.set_config("report_every_n_polls", "999")
    server_id = db.add_server(
        hub.Server(name="agent", ip="127.0.0.1", port=5678, enabled=True)
    )
    db.set_config("last_event_ids", json.dumps({str(server_id): 2}))
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        engine.last_event_ids = {server_id: 2}
        monkeypatch.setattr(
            engine,
            "poll_server",
            lambda server: {"reachable": True, "last_seen": datetime.now(), "monitors": []},
        )

        def fake_urlopen(req, timeout):
            if req.full_url.startswith("http://127.0.0.1:5678/events"):
                return FakeHTTPResponse({"events": [{"id": 3, "message": "new"}]})
            raise AssertionError(f"no OpenClaw call expected without token: {req.full_url}")

        monkeypatch.setattr(hub.urllib.request, "urlopen", fake_urlopen)
        engine.poll_all_servers()

        rows = db._conn.execute("SELECT message FROM events ORDER BY id").fetchall()
        assert [row[0] for row in rows] == ["new"]
        assert json.loads(db.get_config("last_event_ids")) == {str(server_id): 3}
        assert db._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
    finally:
        db._conn.close()
