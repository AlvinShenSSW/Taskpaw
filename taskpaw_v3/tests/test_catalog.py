"""Selectable-monitor catalog + config-edit helpers + /control/plugins."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.catalog import (
    add_monitor,
    has_monitor,
    plugin_catalog,
    preset_catalog,
    remove_monitor,
)
from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.core.config import AgentConfig


# ── catalog ──────────────────────────────────────────────────────────────--
def test_plugin_catalog_lists_all_with_schema():
    cat = plugin_catalog()
    by_id = {p["type_id"]: p for p in cat}
    # every built-in selectable service is offered
    assert {"host_metrics", "process", "comfyui", "folder", "custom_cmd",
            "state_file", "heartbeat", "tcp_check"} <= set(by_id)
    # each entry carries a form schema the UI can render + the four-piece bits
    for p in cat:
        assert "properties" in p["json_schema"]
        assert "category" in p and "display_name" in p
        assert "config_version" in p and "system" in p
    # host_metrics is flagged system (auto-injected) so the UI won't offer a dup
    assert by_id["host_metrics"]["system"] is True
    assert by_id["comfyui"]["system"] is False


def test_preset_catalog_has_moomoo_as_an_option():
    presets = {p["id"]: p for p in preset_catalog()}
    assert "moomoo" in presets
    mon = presets["moomoo"]["monitors"]
    assert len(mon) == 4                      # moomoo is just a selectable bundle
    assert {m["type_id"] for m in mon} == {"process", "tcp_check", "heartbeat"}


# ── config-edit helpers ──────────────────────────────────────────────────--
def test_add_monitor_validates_and_appends():
    out = add_monitor([], {"type_id": "tcp_check",
                           "config": {"name": "opend", "port": 11111}})
    assert len(out) == 1 and out[0]["type_id"] == "tcp_check"
    # canonical {type_id, name, config} shape (matches migration/examples)
    assert out[0]["name"] == "opend" and out[0]["config"]["name"] == "opend"
    assert set(out[0]) == {"type_id", "name", "config"}
    assert has_monitor(out, "opend")


def test_add_monitor_rejects_non_object_config():
    with pytest.raises(ValueError):
        add_monitor([], {"type_id": "tcp_check", "config": [1, 2]})   # not a dict


def test_add_monitor_rejects_unknown_type():
    with pytest.raises(ValueError):
        add_monitor([], {"type_id": "nope", "config": {"name": "x"}})


def test_add_monitor_rejects_invalid_config():
    with pytest.raises(ValueError):
        add_monitor([], {"type_id": "tcp_check", "config": {"name": "x"}})  # no port


def test_add_monitor_rejects_duplicate_name():
    base = add_monitor([], {"type_id": "tcp_check", "config": {"name": "a", "port": 1}})
    with pytest.raises(ValueError):
        add_monitor(base, {"type_id": "tcp_check", "config": {"name": "a", "port": 2}})


def test_add_monitor_is_pure():
    src: list = []
    add_monitor(src, {"type_id": "tcp_check", "config": {"name": "a", "port": 1}})
    assert src == []                          # original untouched


def test_remove_monitor():
    base = add_monitor([], {"type_id": "tcp_check", "config": {"name": "a", "port": 1}})
    assert remove_monitor(base, "a") == []
    with pytest.raises(ValueError):
        remove_monitor(base, "missing")


def test_remove_monitor_raises_on_duplicates_not_silent_wipe():
    # duplicate names shouldn't exist via add_monitor, but a hand-edited/migrated
    # config could carry them — removing both silently would be data loss (Kimi).
    dupes = [
        {"type_id": "tcp_check", "config": {"name": "a", "port": 1}},
        {"type_id": "tcp_check", "config": {"name": "a", "port": 2}},
    ]
    with pytest.raises(ValueError, match="multiple"):
        remove_monitor(dupes, "a")


def test_add_monitor_non_string_type_id_raises_valueerror():
    # malformed JSON could give a list/None type_id — must be ValueError, not
    # TypeError from reg.has() (Kimi).
    with pytest.raises(ValueError):
        add_monitor([], {"type_id": ["tcp_check"], "config": {"name": "x"}})
    with pytest.raises(ValueError):
        add_monitor([], {"config": {"name": "x"}})  # missing type_id


def test_name_at_top_level_supported():
    out = add_monitor([], {"type_id": "tcp_check", "name": "top",
                           "config": {"port": 11111}})
    assert has_monitor(out, "top")


# ── /control/plugins endpoint ────────────────────────────────────────────--
def test_network_status_resolves_name_from_config():
    # monitors are stored as {type_id, config:{name}} — the /status fallback must
    # report the real name, not null (Hub reads this) (Kimi).
    from taskpaw_v3.core.protocol import EventQueue

    cfg = AgentConfig(server_id="a", machine="m", host_metrics=False, monitors=[
        {"type_id": "tcp_check", "config": {"name": "opend", "port": 11111}},
    ])
    client = TestClient(create_network_app(cfg, EventQueue(machine="m")))
    r = client.get("/status")
    assert r.status_code == 200
    mons = r.json()["monitors"]
    assert mons[0]["name"] == "opend" and mons[0]["type_id"] == "tcp_check"


def test_control_plugins_endpoint():
    cfg = AgentConfig(server_id="a", machine="m")
    client = TestClient(create_control_app(cfg))
    r = client.get("/control/plugins")
    assert r.status_code == 200
    body = r.json()
    ids = {p["type_id"] for p in body["plugins"]}
    assert "host_metrics" in ids and "comfyui" in ids
    assert any(p["id"] == "moomoo" for p in body["presets"])
