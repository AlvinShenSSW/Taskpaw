from __future__ import annotations

import importlib
import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path


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

    taskpaw.add_event(
        "machine",
        "monitor",
        "rich",
        level="alert",
        title="Needs attention",
        data={"code": 42},
    )
    rich = taskpaw._events_queue[-1]
    assert rich["level"] == "alert"
    assert rich["title"] == "Needs attention"
    assert rich["data"] == {"code": 42}


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_hub_get_new_events_dedups_advances_and_sends_ack(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    try:
        engine = hub.PollingEngine(db, lambda _: None)
        server = hub.Server(id=7, name="agent", ip="127.0.0.1", port=5678)
        engine.last_event_ids = {7: 2}
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
        assert engine.last_event_ids[7] == 4
        assert json.loads(db.get_config("last_event_ids")) == {"7": 4}
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


def test_hub_failed_openclaw_send_enqueues_outbox(tmp_path, monkeypatch):
    hub = import_hub(tmp_path, monkeypatch)
    db = hub.DatabaseManager(tmp_path / "hub.db")
    db.set_config("openclaw_token", "token")
    try:
        engine = hub.PollingEngine(db, lambda _: None)

        def fail_urlopen(req, timeout):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(hub.urllib.request, "urlopen", fail_urlopen)
        engine.send_event_to_openclaw("agent", {"message": "done"})

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
