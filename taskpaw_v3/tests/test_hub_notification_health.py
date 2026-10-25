"""Local Hub history and delivery evidence must outlive notification settings."""

from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.hub.server.app import HubService, create_hub_app
from taskpaw_v3.hub.server.status_md import render_status_md
from taskpaw_v3.hub.server.store import HubStore


@pytest.mark.parametrize("enabled,token", [(False, ""), (True, "")])
def test_self_health_is_durable_without_delivery(tmp_path, enabled, token):
    store = HubStore(tmp_path / "hub.db")
    service = HubService(
        HubConfig(openclaw_enabled=enabled, openclaw_token=token), store
    )
    assert service.self_supervisor is not None
    service.self_supervisor._sink("hub-host", "alert", "Disk", "disk full")
    store.close()
    store = HubStore(tmp_path / "hub.db")
    try:
        events = store.recent_hub_events()
        assert len(events) == 1
        assert events[0]["server_id"] is None
        assert events[0]["message"] == "disk full"
        assert store.due_deliveries() == []
    finally:
        store.close()


def test_notification_summary_survives_success_and_restart(tmp_path):
    store = HubStore(tmp_path / "hub.db")
    point = datetime.now(timezone.utc)
    first = store.enqueue_delivery("win", "event", '{"text":"hi"}')
    assert first is not None
    health = store.notification_health(now=point + timedelta(seconds=20))
    assert health["pending_count"] == 1
    assert health["oldest_pending_age_seconds"] == pytest.approx(20, abs=1)
    assert health["last_success_at"] is None
    store.mark_delivery_failed(first, 1, "http_auth", point)
    assert store.notification_health()["failed_count"] == 1
    store.complete_delivery(first)
    store.close()
    store = HubStore(tmp_path / "hub.db")
    try:
        health = store.notification_health()
        assert health["pending_count"] == 0
        assert health["oldest_pending_age_seconds"] is None
        assert health["last_success_at"] is not None
        assert health["last_failure_at"] is not None
        second = store.enqueue_delivery("win", "event", '{"text":"again"}')
        store.mark_delivery_dead_letter(second, 10, "attempt cap")
        assert store.notification_health()["dead_letter_count"] == 1
    finally:
        store.close()


def test_success_receipt_and_outbox_removal_roll_back_together(tmp_path):
    store = HubStore(tmp_path / "hub.db")
    try:
        row = store.enqueue_delivery("win", "event", '{"text":"hi"}')
        store._conn.execute(
            "CREATE TRIGGER reject_delete BEFORE DELETE ON delivery_outbox "
            "BEGIN SELECT RAISE(ABORT,'test disk error'); END"
        )
        with pytest.raises(Exception, match="test disk error"):
            store.complete_delivery(row)
        assert len(store.due_deliveries()) == 1
        assert store.notification_health()["last_success_at"] is None
    finally:
        store.close()


def test_read_health_routes_are_additive_and_bearer_guarded(tmp_path):
    from fastapi.testclient import TestClient

    store = HubStore(tmp_path / "hub.db")
    try:
        app, _ = create_hub_app(HubConfig(self_monitor=False, api_token="fake"), store)
        store.record_hub_event("hub", "host", "disk full", "alert", False)
        client = TestClient(app)
        assert client.get("/status").status_code == 401
        assert client.get("/hub-events").status_code == 401
        headers = {"Authorization": "Bearer fake"}
        body = client.get("/status", headers=headers).json()
        assert body["notifications"]["state"] == "disabled"
        assert body["notifications"]["pending_count"] == 0
        assert len(client.get("/hub-events", headers=headers).json()["events"]) == 1
    finally:
        store.close()


def test_status_md_shows_success_age_and_delivery_health():
    raw = render_status_md(
        [
            {
                "name": "win",
                "reachable": True,
                "status_json": "{}",
                "status_health": {
                    "state": "ok",
                    "age_seconds": 4,
                    "last_good_at": "2026-10-25T00:00:00+00:00",
                },
            }
        ],
        "2026-10-25 09:00:04",
        notifications={
            "state": "failed",
            "pending_count": 2,
            "oldest_pending_age_seconds": 80,
            "failed_count": 1,
            "dead_letter_count": 0,
            "last_success_at": None,
        },
    )
    assert "## win: ONLINE" in raw
    assert "4s" in raw and "2026-10-25T00:00:00+00:00" in raw
    assert "Notifications: failed" in raw and "pending 2" in raw
    assert "last success never" in raw


def test_documented_sql_uses_current_evidence_not_success_history(tmp_path, capsys):
    from pathlib import Path

    store = HubStore(tmp_path / "hub.db")
    sid = store.add_server("fixture", "127.0.0.1")
    store.record_status(sid, '{"monitors":{}}', None)
    guide = Path("docs/guides/openclaw-integration.md").read_text()
    example = guide.split("```python\n", 1)[1].split("```", 1)[0]
    example = example.replace("~/.taskpaw-hub/hub.db", str(store.db_path))
    try:
        exec(compile(example, "guide-example", "exec"), {})
        assert "current: True" in capsys.readouterr().out
        store.record_status(sid, None, "http_auth")
        exec(compile(example, "guide-example", "exec"), {})
        output = capsys.readouterr().out
        assert "current: False" in output and "reason: http_auth" in output
        assert len(store.latest_statuses()) == 1  # previous good JSON remains
        store._conn.execute(
            "UPDATE upstream_status SET error_code=NULL,last_good_at=?",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),),
        )
        store._conn.commit()
        exec(compile(example, "guide-example", "exec"), {})
        output = capsys.readouterr().out
        assert "current: False" in output and "reason: status_stale" in output
    finally:
        store.close()


def test_local_history_retention_does_not_remove_pending_deliveries(tmp_path):
    store = HubStore(tmp_path / "hub.db")
    try:
        first = store.record_hub_event("hub", "host", "old event", "error", True)
        store._conn.execute(
            "UPDATE hub_events SET received_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), first),
        )
        store._conn.commit()
        store.record_hub_event("hub", "host", "current", "info", False)
        assert [e["message"] for e in store.recent_hub_events()] == ["current"]
        assert len(store.due_deliveries()) == 1
    finally:
        store.close()


@pytest.mark.parametrize("reason", ["delivery_busy", "helper_cancelled"])
def test_sender_capacity_or_cancellation_does_not_consume_retries(tmp_path, reason):
    from taskpaw_v3.hub.server.poller import Poller
    from taskpaw_v3.hub.server.upstream_worker import UpstreamError

    store = HubStore(tmp_path / "hub.db")
    try:
        store.enqueue_delivery("fixture", "event", '{"text":"fake"}')
        before = store.due_deliveries()
        poller = Poller(store, "http://fake.invalid", lambda: True, lambda: "fake")

        def busy(*_):
            raise UpstreamError(reason)

        poller.drain_outbox(send=busy)
        assert store.due_deliveries() == before
        assert store.notification_health()["last_failure_at"] is None
    finally:
        store.close()
