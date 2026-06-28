"""Hub CLI (python -m taskpaw_v3.hub) + service config-path helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from taskpaw_v3.hub.__main__ import main
from taskpaw_v3.hub.server import service
from taskpaw_v3.hub.server.store import HubStore


def _db(tmp_path) -> str:
    return str(tmp_path / "hub.db")


# ── add / list / enable / disable / remove ───────────────────────────────--
def test_add_and_list(tmp_path, capsys):
    db = _db(tmp_path)
    assert main(["--db", db, "add-server", "--name", "moomoo", "--ip", "192.168.1.50"]) == 0
    assert main(["--db", db, "list-servers"]) == 0
    out = capsys.readouterr().out
    assert "moomoo" in out and "192.168.1.50:5680" in out
    # persisted
    assert HubStore(Path(db)).list_servers()[0]["name"] == "moomoo"


def test_add_disabled_then_enable(tmp_path, capsys):
    db = _db(tmp_path)
    main(["--db", db, "add-server", "--name", "a", "--ip", "10.0.0.1", "--disabled"])
    assert HubStore(Path(db)).list_servers()[0]["enabled"] == 0
    assert main(["--db", db, "enable-server", "--id", "1"]) == 0
    assert HubStore(Path(db)).list_servers()[0]["enabled"] == 1


def test_remove(tmp_path):
    db = _db(tmp_path)
    main(["--db", db, "add-server", "--name", "a", "--ip", "10.0.0.1"])
    assert main(["--db", db, "remove-server", "--id", "1"]) == 0
    assert HubStore(Path(db)).list_servers() == []


def test_remove_missing_id_returns_error(tmp_path):
    db = _db(tmp_path)
    assert main(["--db", db, "remove-server", "--id", "999"]) == 2


def test_duplicate_name_errors(tmp_path, capsys):
    db = _db(tmp_path)
    main(["--db", db, "add-server", "--name", "dup", "--ip", "1.1.1.1"])
    assert main(["--db", db, "add-server", "--name", "dup", "--ip", "2.2.2.2"]) == 2
    assert "duplicate" in capsys.readouterr().err.lower()


def test_custom_port_carried(tmp_path):
    db = _db(tmp_path)
    main(["--db", db, "add-server", "--name", "p", "--ip", "1.1.1.1", "--port", "5999"])
    assert HubStore(Path(db)).list_servers()[0]["port"] == 5999


@pytest.mark.parametrize("bad", ["0", "70000", "-1", "abc"])
def test_add_server_rejects_invalid_port(tmp_path, bad):
    """Out-of-range/non-int ports must be rejected at parse time, not persisted
    (an invalid port makes the agent permanently unpollable) — Codex."""
    db = _db(tmp_path)
    with pytest.raises(SystemExit):   # argparse exits non-zero on bad type
        main(["--db", db, "add-server", "--name", "p", "--ip", "1.1.1.1", "--port", bad])
    assert HubStore(Path(db)).list_servers() == []


# ── run subcommand → missing config is a clean failure, not a crash ───────--
def test_run_missing_config_returns_1(tmp_path, capsys):
    missing = tmp_path / "nope.yaml"
    assert main(["--config", str(missing), "run"]) == 1
    assert "No hub config" in capsys.readouterr().err


def test_run_hard_fails_on_abandoned_legacy_db(tmp_path, capsys):
    # hub.yaml with a REAL config-adjacent hub.db (old default) but a data_dir
    # whose db is absent → must hard-fail, not silently start empty (Kimi).
    cfg = tmp_path / "hub.yaml"
    cfg.write_text(f"machine: h\ndata_dir: {tmp_path / 'newdir'}\n")
    HubStore(tmp_path / "hub.db").add_server("m", "1.1.1.1")   # real legacy db
    rc = main(["--config", str(cfg), "run"])
    assert rc == 1
    assert "older one exists" in capsys.readouterr().err


def test_zero_byte_legacy_is_not_a_conflict(tmp_path):
    from taskpaw_v3.hub.server.service import legacy_db_conflict
    cfg = tmp_path / "hub.yaml"
    (tmp_path / "hub.db").write_bytes(b"")          # junk, not a real db
    assert legacy_db_conflict(cfg, tmp_path / "newdir" / "hub.db") is None


def test_empty_resolved_db_still_conflicts(tmp_path):
    # a management command may have created an empty resolved db; while a real
    # legacy db exists, that's still a conflict so `run` keeps failing (Codex).
    from taskpaw_v3.hub.server.service import legacy_db_conflict
    cfg = tmp_path / "hub.yaml"
    HubStore(tmp_path / "hub.db").add_server("m", "1.1.1.1")   # legacy with data
    resolved = tmp_path / "newdir" / "hub.db"
    HubStore(resolved)                                          # empty resolved (0 servers)
    assert legacy_db_conflict(cfg, resolved) == tmp_path / "hub.db"


def test_resolved_with_servers_is_not_a_conflict(tmp_path):
    from taskpaw_v3.hub.server.service import legacy_db_conflict
    cfg = tmp_path / "hub.yaml"
    HubStore(tmp_path / "hub.db").add_server("old", "1.1.1.1")
    resolved = tmp_path / "newdir" / "hub.db"
    HubStore(resolved).add_server("new", "2.2.2.2")            # resolved has data
    assert legacy_db_conflict(cfg, resolved) is None


def test_store_missing_explicit_config_fails(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--config", str(tmp_path / "nope.yaml"), "list-servers"])
    assert e.value.code == 2
    assert "config not found" in capsys.readouterr().err


# ── platform config paths ────────────────────────────────────────────────--
def test_default_paths(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    p = service.default_config_path()
    assert p.name == "hub.yaml" and "TaskPaw" in str(p)
    assert service.default_db_path(p).name == "hub.db"


# ── store helpers directly ───────────────────────────────────────────────--
def test_store_set_enabled_and_remove_return_flags(tmp_path):
    s = HubStore(tmp_path / "h.db")
    sid = s.add_server("x", "1.2.3.4")
    assert s.set_server_enabled(sid, False) is True
    assert s.set_server_enabled(999, False) is False
    assert s.remove_server(sid) is True
    assert s.remove_server(sid) is False
    s.close()


def test_remove_server_purges_queued_deliveries(tmp_path):
    """delivery_outbox keys on server_name (no FK cascade) — removing a server
    must drop its pending deliveries so a removed agent can't still fire (Codex)."""
    s = HubStore(tmp_path / "h.db")
    sid = s.add_server("moomoo", "1.2.3.4")
    s.enqueue_delivery("moomoo", "event", "{}")
    s.enqueue_delivery("other", "event", "{}")
    assert len(s.due_deliveries()) == 2
    assert s.remove_server(sid) is True
    remaining = s.due_deliveries()
    assert len(remaining) == 1 and remaining[0]["server_name"] == "other"
    s.close()
