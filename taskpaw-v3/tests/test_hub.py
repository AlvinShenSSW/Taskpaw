"""Hub store + poller: at-least-once ordering, 404 fallback, outbox drain/DLQ."""

from __future__ import annotations

import json

import pytest

from hub.server import poller as poller_mod
from hub.server.poller import Poller
from hub.server.store import HubStore


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _store(tmp_path):
    return HubStore(tmp_path / "hub.db")


def test_store_event_idempotent(tmp_path):
    s = _store(tmp_path)
    try:
        sid = s.add_server("agent", "127.0.0.1", 5680)
        ev = {"id": 3, "monitor": "m", "message": "x"}
        s.store_event(sid, ev)
        s.store_event(sid, ev)  # at-least-once re-store → no dup
        n = s._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert n == 1
    finally:
        s.close()


def test_poller_stores_enqueues_then_advances_ack(tmp_path, monkeypatch):
    s = _store(tmp_path)
    try:
        sid = s.add_server("agent", "127.0.0.1", 5680)
        s.set_config("openclaw_token", "tok")
        p = Poller(s, "http://oc/hook", get_active=lambda: True, get_token=lambda: "tok")
        p.last_event_ids = {sid: 2}

        seen = []
        sent = []

        def fake_urlopen(req, timeout):
            if "/events" in req.full_url:
                seen.append(req.full_url)
                return FakeResp({"events": [{"id": 2, "message": "seen"},
                                            {"id": 3, "message": "new"},
                                            {"id": 4, "message": "newer"}]})
            sent.append(json.loads(req.data.decode()))
            return FakeResp({"ok": True})

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", fake_urlopen)
        p.poll_once()

        assert seen == ["http://127.0.0.1:5680/events?ack=2"]
        assert p.last_event_ids[sid] == 4
        assert json.loads(s.get_config("last_event_ids")) == {str(sid): 4}
        msgs = [r[0] for r in s._conn.execute("SELECT message FROM events ORDER BY event_id").fetchall()]
        assert msgs == ["new", "newer"]
        assert sent == [{"text": "TaskPaw Event | agent: new"},
                        {"text": "TaskPaw Event | agent: newer"}]
        assert s._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
    finally:
        s.close()


def test_poller_404_fallback_for_legacy_agent(tmp_path, monkeypatch):
    import urllib.error

    s = _store(tmp_path)
    try:
        sid = s.add_server("agent", "127.0.0.1", 5680)
        p = Poller(s, "http://oc/hook", get_active=lambda: False, get_token=lambda: "")
        p.last_event_ids = {sid: 2}
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req.full_url)
            if "ack=" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", None, None)
            return FakeResp({"events": [{"id": 3, "message": "new"}]})

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", fake_urlopen)
        evs = p.fetch_events(s.list_servers()[0])
        assert [e["id"] for e in evs] == [3]
        assert seen == ["http://127.0.0.1:5680/events?ack=2",
                        "http://127.0.0.1:5680/events"]
    finally:
        s.close()


def test_poller_disabled_stores_without_outbox(tmp_path, monkeypatch):
    s = _store(tmp_path)
    try:
        sid = s.add_server("agent", "127.0.0.1", 5680)
        p = Poller(s, "http://oc/hook", get_active=lambda: False, get_token=lambda: "")
        p.last_event_ids = {sid: 2}

        def fake_urlopen(req, timeout):
            return FakeResp({"events": [{"id": 3, "message": "new"}]})

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", fake_urlopen)
        p.poll_once()
        assert s._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert s._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0
        assert json.loads(s.get_config("last_event_ids")) == {str(sid): 3}
    finally:
        s.close()


def test_outbox_dead_letters_once(tmp_path, monkeypatch):
    import urllib.error
    from datetime import datetime, timedelta

    s = _store(tmp_path)
    try:
        s.set_config("openclaw_token", "tok")
        s.enqueue_delivery(
            "agent", "event", json.dumps({"text": "x"}),
            delivery_state="failed", attempts=9,
            next_attempt_at=datetime.now() - timedelta(seconds=1),
        )
        p = Poller(s, "http://oc/hook", get_active=lambda: True, get_token=lambda: "tok")
        alerts = []
        monkeypatch.setattr(p, "emit_local_alert", alerts.append)

        def fail(req, timeout):
            raise urllib.error.URLError("down")

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", fail)
        p.drain_outbox()
        p.drain_outbox()
        row = s._conn.execute(
            "SELECT delivery_state, attempts FROM delivery_outbox"
        ).fetchone()
        assert row == ("dead_letter", 10)
        assert len(alerts) == 1
    finally:
        s.close()
