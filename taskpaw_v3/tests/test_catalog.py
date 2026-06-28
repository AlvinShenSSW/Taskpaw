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
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig


# ── catalog ──────────────────────────────────────────────────────────────--
def test_plugin_catalog_lists_all_with_schema():
    cat = plugin_catalog()
    by_id = {p["type_id"]: p for p in cat}
    # every built-in selectable service is offered
    assert {"host_metrics", "process", "comfyui", "folder", "custom_cmd",
            "state_file", "heartbeat", "tcp_check"} <= set(by_id)
    # each entry carries a form schema the UI can render
    for p in cat:
        assert "properties" in p["json_schema"]
        assert "category" in p and "display_name" in p


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
    assert out[0]["config"]["name"] == "opend"
    assert has_monitor(out, "opend")


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


def test_name_at_top_level_supported():
    out = add_monitor([], {"type_id": "tcp_check", "name": "top",
                           "config": {"port": 11111}})
    assert has_monitor(out, "top")


# ── /control/plugins endpoint ────────────────────────────────────────────--
def test_control_plugins_endpoint():
    cfg = AgentConfig(server_id="a", machine="m")
    client = TestClient(create_control_app(cfg))
    r = client.get("/control/plugins")
    assert r.status_code == 200
    body = r.json()
    ids = {p["type_id"] for p in body["plugins"]}
    assert "host_metrics" in ids and "comfyui" in ids
    assert any(p["id"] == "moomoo" for p in body["presets"])
