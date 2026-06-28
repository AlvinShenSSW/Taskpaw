"""OpenClaw compatibility: status_log + status.md + data_dir paths (#38)."""

from __future__ import annotations

import json

from taskpaw_v3.core.config import HubConfig
from taskpaw_v3.hub.server.status_md import render_status_md, write_status_md
from taskpaw_v3.hub.server.store import HubStore


# ── store: status_log ────────────────────────────────────────────────────--
def test_log_status_and_latest(tmp_path):
    s = HubStore(tmp_path / "hub.db")
    a = s.add_server("moomoo", "192.168.1.50")
    b = s.add_server("pig", "192.168.1.60")
    s.log_status(a, True, json.dumps({"monitors": {"x": {"state": "ok"}}}))
    s.log_status(a, True, json.dumps({"monitors": {"x": {"state": "error"}}}))  # newer
    s.log_status(b, False, None)
    latest = {r["name"]: r for r in s.latest_statuses()}
    assert latest["moomoo"]["reachable"] == 1
    assert "error" in latest["moomoo"]["status_json"]    # the latest row, not the first
    assert latest["pig"]["reachable"] == 0
    s.close()


def test_latest_statuses_includes_never_polled(tmp_path):
    s = HubStore(tmp_path / "hub.db")
    s.add_server("fresh", "10.0.0.1")
    rows = s.latest_statuses()
    assert rows[0]["name"] == "fresh" and rows[0]["status_json"] is None
    s.close()


def test_prune_status_logs(tmp_path):
    s = HubStore(tmp_path / "hub.db")
    a = s.add_server("m", "1.1.1.1")
    # insert an old row directly (bypass the now() insert)
    s._conn.execute(
        "INSERT INTO status_log(server_id, timestamp, reachable, status_json) "
        "VALUES(?, datetime('now','localtime','-30 days'), 1, '{}')", (a,))
    s._conn.commit()
    s.log_status(a, True, "{}")                # a fresh one
    assert s.prune_status_logs(7) == 1         # only the 30-day-old row dropped
    assert len(s.latest_statuses()) == 1
    s.close()


def test_remove_server_cascades_status_log(tmp_path):
    s = HubStore(tmp_path / "hub.db")
    a = s.add_server("m", "1.1.1.1")
    s.log_status(a, True, "{}")
    s.remove_server(a)
    assert s.latest_statuses() == []
    s.close()


# ── status.md rendering (V2 format) ──────────────────────────────────────--
def test_render_status_md_matches_v2_format():
    rows = [
        {"name": "moomoo", "reachable": 1, "timestamp": "t",
         "status_json": json.dumps({"monitors": {"opend": {"state": "ok"},
                                                  "orch": {"state": "error"}}})},
        {"name": "pig", "reachable": 0, "timestamp": "09:00:00", "status_json": None},
    ]
    md = render_status_md(rows, "2026-06-28 09:00:00")
    assert md.startswith("# TaskPaw Hub Status")
    assert "Last updated: 2026-06-28 09:00:00" in md
    assert "## moomoo: ONLINE" in md
    assert "- opend: ok" in md and "- orch: error" in md
    assert "## pig: OFFLINE (last seen 09:00:00)" in md


def test_render_handles_list_shape_and_bad_json():
    rows = [
        {"name": "a", "reachable": 1, "status_json":
            json.dumps({"monitors": [{"name": "m1", "state": "ok"}]})},
        {"name": "b", "reachable": 1, "status_json": "not json"},
        {"name": "c", "reachable": 1, "status_json": None},
    ]
    md = render_status_md(rows, "now")
    assert "- m1: ok" in md          # list shape handled
    assert "## b: ONLINE" in md      # bad json → online, no monitor lines (no crash)
    assert "## c: ONLINE" in md


def test_write_status_md_atomic(tmp_path):
    out = tmp_path / "status.md"
    rows = [{"name": "m", "reachable": 1, "status_json": json.dumps({"monitors": {}})}]
    write_status_md(out, rows, "now")
    assert out.exists() and "## m: ONLINE" in out.read_text()
    assert list(tmp_path.glob(".*.tmp")) == []           # no temp residue
    write_status_md(out, rows, "later")                  # overwrite ok
    assert "later" in out.read_text()


# ── config / paths ───────────────────────────────────────────────────────--
def test_hubconfig_data_dir_default():
    cfg = HubConfig()
    assert cfg.data_dir == "~/.taskpaw-hub"
    assert cfg.write_status_md is True


def test_db_path_for_uses_data_dir():
    from taskpaw_v3.hub.server.service import db_path_for
    cfg = HubConfig(data_dir="/tmp/tp-test")
    assert str(db_path_for(cfg)) == "/tmp/tp-test/hub.db"


# ── poller: fetch_status + poll logs status_log ──────────────────────────--
def _poller(store):
    from taskpaw_v3.hub.server.poller import Poller
    return Poller(store=store, openclaw_url="http://x", get_active=lambda: False,
                  get_token=lambda: "", http_timeout=1)


def test_poll_once_logs_status_for_every_server(tmp_path, monkeypatch):
    s = HubStore(tmp_path / "hub.db")
    sid = s.add_server("moomoo", "127.0.0.1")
    p = _poller(s)
    monkeypatch.setattr(p, "fetch_status",
                        lambda server: (True, json.dumps({"monitors": {"x": {"state": "ok"}}})))
    monkeypatch.setattr(p, "fetch_events", lambda server: [])   # no events
    p.poll_once()
    latest = s.latest_statuses()
    assert latest[0]["name"] == "moomoo" and latest[0]["reachable"] == 1
    assert "ok" in latest[0]["status_json"]               # logged even with no events
    s.close()


def test_fetch_status_unreachable_returns_false(tmp_path, monkeypatch):
    import taskpaw_v3.hub.server.poller as mod
    s = HubStore(tmp_path / "hub.db")
    p = _poller(s)

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    reachable, body = p.fetch_status({"name": "x", "ip": "10.0.0.9", "port": 5680})
    assert reachable is False and body is None
    s.close()
