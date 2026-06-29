"""Hub store + poller: at-least-once ordering, 404 fallback, outbox drain/DLQ."""

from __future__ import annotations

import json

import pytest

from taskpaw_v3.hub.server import poller as poller_mod
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore


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


def test_recent_events_filters_and_orders(tmp_path):
    # The dashboard event log (#44): newest first, joins the server name, filters
    # by server id and/or level.
    s = _store(tmp_path)
    try:
        a = s.add_server("moomoo", "127.0.0.1", 5680)
        b = s.add_server("pig", "127.0.0.1", 5681)
        s.store_event(a, {"id": 1, "monitor": "lada", "message": "older", "level": "info"})
        s.store_event(a, {"id": 2, "monitor": "lada", "message": "boom", "level": "alert"})
        s.store_event(b, {"id": 1, "monitor": "comfy", "message": "hi", "level": "info"})
        allev = s.recent_events()
        assert len(allev) == 3 and {e["server"] for e in allev} == {"moomoo", "pig"}
        assert "server" in allev[0]                          # name joined
        # filter by server
        assert {e["message"] for e in s.recent_events(server_id=a)} == {"older", "boom"}
        # filter by level
        lvl = s.recent_events(level="alert")
        assert [e["message"] for e in lvl] == ["boom"]
        # limit
        assert len(s.recent_events(limit=1)) == 1
    finally:
        s.close()


def test_hub_events_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.core.config import HubConfig
    s = _store(tmp_path)
    try:
        a = s.add_server("moomoo", "127.0.0.1", 5680)
        s.store_event(a, {"id": 1, "monitor": "lada", "message": "done", "level": "done"})
        app, _svc = create_hub_app(HubConfig(), s)
        r = TestClient(app).get("/events?level=done&limit=50")
        assert r.status_code == 200
        ev = r.json()["events"]
        assert len(ev) == 1 and ev[0]["server"] == "moomoo" and ev[0]["message"] == "done"
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
    from datetime import datetime, timedelta, timezone

    s = _store(tmp_path)
    try:
        s.set_config("openclaw_token", "tok")
        s.enqueue_delivery(
            "agent", "event", json.dumps({"text": "x"}),
            delivery_state="failed", attempts=9,
            next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
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


def test_poller_uses_configured_polling_token(tmp_path):
    """Polling auth header comes from get_polling_token (HubConfig fallback), not
    only a SQLite row — an authed agent must not silently 401."""
    s = _store(tmp_path)
    try:
        p = Poller(s, "http://oc/hook", get_active=lambda: False,
                   get_token=lambda: "", get_polling_token=lambda: "polltok")
        assert p._auth_headers() == {"Authorization": "Bearer polltok"}
        # default (no override) reads the SQLite row
        p2 = Poller(s, "http://oc/hook", get_active=lambda: False, get_token=lambda: "")
        assert p2._auth_headers() == {}
        s.set_config("polling_token", "fromdb")
        assert p2._auth_headers() == {"Authorization": "Bearer fromdb"}
    finally:
        s.close()


def test_outbox_dedupe_key_idempotent(tmp_path):
    s = _store(tmp_path)
    try:
        s.enqueue_delivery("agent", "event", json.dumps({"text": "x"}), dedupe_key="1:5")
        s.enqueue_delivery("agent", "event", json.dumps({"text": "x"}), dedupe_key="1:5")
        n = s._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        assert n == 1  # replay didn't double-enqueue
    finally:
        s.close()


def test_agent_base_url_brackets_ipv6():
    from taskpaw_v3.hub.server.poller import _agent_base_url
    assert _agent_base_url("127.0.0.1", 5680) == "http://127.0.0.1:5680"
    assert _agent_base_url("::1", 5680) == "http://[::1]:5680"


def test_poller_keeps_latest_status_snapshot(tmp_path, monkeypatch):
    """After a poll the poller exposes each server's parsed /status snapshot +
    last_seen via snapshot_statuses() (#96), and keeps the last good snapshot
    (online=False) when the agent later goes unreachable."""
    s = _store(tmp_path)
    try:
        sid = s.add_server("agent", "127.0.0.1", 5680)
        p = Poller(s, "http://oc/hook", get_active=lambda: False, get_token=lambda: "")

        agent_status = {"machine": "box1",
                        "monitors": {"host": {"state": "ok", "metrics": {"cpu": 12}}}}

        def ok_urlopen(req, timeout):
            if "/status" in req.full_url:
                return FakeResp(agent_status)
            return FakeResp({"events": []})

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", ok_urlopen)
        p.poll_once()

        snaps = p.snapshot_statuses()
        assert snaps[sid]["online"] is True
        assert snaps[sid]["snapshot"] == agent_status
        assert snaps[sid]["last_seen"]  # a timestamp was recorded
        first_seen = snaps[sid]["last_seen"]

        # Agent goes down → keep last good snapshot, mark offline.
        def down_urlopen(req, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", down_urlopen)
        p.poll_once()
        snaps = p.snapshot_statuses()
        assert snaps[sid]["online"] is False
        assert snaps[sid]["snapshot"] == agent_status      # last good preserved
        assert snaps[sid]["last_seen"] == first_seen        # not refreshed
    finally:
        s.close()


def test_status_endpoint_attaches_snapshot_and_keeps_contract(tmp_path, monkeypatch):
    """/status augments each server with online/last_seen/snapshot while keeping
    the existing servers/acks/self contract intact (#96 regression guard)."""
    from fastapi.testclient import TestClient
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.core.config import HubConfig

    s = _store(tmp_path)
    try:
        sid = s.add_server("box1", "127.0.0.1", 5680)
        # self_monitor off → deterministic empty `self`, no host probing in tests.
        app, svc = create_hub_app(HubConfig(self_monitor=False), s)

        agent_status = {"machine": "box1", "monitors": {"h": {"state": "ok"}}}

        def ok_urlopen(req, timeout):
            if "/status" in req.full_url:
                return FakeResp(agent_status)
            return FakeResp({"events": []})

        monkeypatch.setattr(poller_mod.urllib.request, "urlopen", ok_urlopen)
        svc.poller.poll_once()

        r = TestClient(app).get("/status")
        assert r.status_code == 200
        body = r.json()
        # Existing contract preserved.
        assert body["machine"] == HubConfig().machine
        assert "acks" in body and "self" in body and body["self"] == {}
        srv = body["servers"][0]
        for key in ("id", "name", "ip", "port", "enabled"):
            assert key in srv
        assert srv["name"] == "box1"
        # New fields (#96).
        assert srv["online"] is True
        assert srv["snapshot"] == agent_status
        assert srv["last_seen"]
        assert srv["id"] == sid

        # Disabling a server after a good poll must force online=False even though
        # the stale snapshot still says reachable (Codex).
        s.set_server_enabled(sid, False)
        srv2 = TestClient(app).get("/status").json()["servers"][0]
        assert srv2["online"] is False
        assert srv2["snapshot"] == agent_status  # last-known reference kept
    finally:
        s.close()


def test_read_api_bearer_gated_when_token_set(tmp_path):
    """With api_token set, /status and /events require a matching Bearer; /ping
    stays open (#106)."""
    from fastapi.testclient import TestClient
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.core.config import HubConfig

    s = _store(tmp_path)
    try:
        app, _svc = create_hub_app(HubConfig(self_monitor=False, api_token="s3cret"), s)
        client = TestClient(app)
        good = {"Authorization": "Bearer s3cret"}

        for path in ("/status", "/events"):
            assert client.get(path).status_code == 401                       # no header
            assert client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
            r = client.get(path, headers=good)
            assert r.status_code == 200
            assert "WWW-Authenticate" in client.get(path).headers

        # /ping is open even with a token configured and no header.
        assert client.get("/ping").status_code == 200
    finally:
        s.close()


def test_read_api_open_when_token_empty(tmp_path):
    """Empty api_token (default) = auth disabled (V2 parity): /status and /events
    answer without a header (#106)."""
    from fastapi.testclient import TestClient
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.core.config import HubConfig

    s = _store(tmp_path)
    try:
        app, _svc = create_hub_app(HubConfig(self_monitor=False), s)  # api_token=""
        client = TestClient(app)
        assert client.get("/status").status_code == 200
        assert client.get("/events").status_code == 200
    finally:
        s.close()
