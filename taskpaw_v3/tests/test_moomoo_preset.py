"""moomoo four-life-signs preset + supervisor↔queue runtime wiring (#18)."""

from __future__ import annotations

import pytest

from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.presets.moomoo import moomoo_preset
from taskpaw_v3.monitors.registry import default_registry
from taskpaw_v3.monitors.runtime import build_supervisor, make_queue_sink


def test_preset_has_four_signs_with_expected_types():
    specs = moomoo_preset()
    by_name = {s["config"]["name"]: s for s in specs}
    assert len(specs) == 4
    assert by_name["moomoo-pm2-daemon"]["type_id"] == "process"
    assert by_name["moomoo-orchestrator"]["type_id"] == "process"
    assert by_name["moomoo-opend"]["type_id"] == "tcp_check"
    assert by_name["moomoo-orchestrator-heartbeat"]["type_id"] == "heartbeat"


def test_preset_defaults_match_recon():
    specs = {s["config"]["name"]: s["config"] for s in moomoo_preset()}
    assert specs["moomoo-opend"]["port"] == 11111
    assert specs["moomoo-opend"]["host"] == "127.0.0.1"
    assert "strategy_orchestrator" in specs["moomoo-orchestrator"]["pattern"]
    assert "God Daemon" in specs["moomoo-pm2-daemon"]["pattern"]
    assert specs["moomoo-orchestrator-heartbeat"]["path"].endswith(
        "moomoo/runtime/orchestrator_heartbeat.json"
    )


def test_preset_overrides_apply():
    specs = {s["config"]["name"]: s["config"]
             for s in moomoo_preset(opend_port=22222, heartbeat_path="/tmp/hb.json", grace_seconds=900)}
    assert specs["moomoo-opend"]["port"] == 22222
    assert specs["moomoo-orchestrator-heartbeat"]["path"] == "/tmp/hb.json"
    assert specs["moomoo-orchestrator-heartbeat"]["grace_seconds"] == 900


def test_every_preset_spec_validates_against_its_plugin():
    reg = default_registry()
    for spec in moomoo_preset():
        plugin = reg.get(spec["type_id"])
        cfg = plugin.validate_config(spec["config"])  # must not raise
        assert cfg.name == spec["config"]["name"]


def test_build_supervisor_registers_all_monitors():
    reg = default_registry()
    q = EventQueue("moomoo")
    sup = build_supervisor(reg, moomoo_preset(heartbeat_path="/tmp/x.json"), q, "moomoo")
    # Not started → just verify all four are registered.
    snap = sup.snapshot()
    assert set(snap) == {
        "moomoo-pm2-daemon", "moomoo-orchestrator", "moomoo-opend",
        "moomoo-orchestrator-heartbeat",
    }


def test_build_supervisor_rejects_unknown_type():
    q = EventQueue("m")
    with pytest.raises(ValueError):
        build_supervisor(default_registry(), [{"type_id": "nope", "config": {"name": "x"}}], q, "m")


def test_queue_sink_adapter_enqueues_with_level_and_title():
    q = EventQueue("moomoo")
    sink = make_queue_sink(q, "moomoo")
    sink("alert", "moomoo-opend down", "127.0.0.1:11111 not accepting connections")
    events = q.payload(ack_id=0)["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["level"] == "alert"
    assert ev["title"] == "moomoo-opend down"
    assert ev["monitor"] == "moomoo-opend down"
    assert "11111" in ev["message"]


def test_queue_sink_drops_unknown_level_gracefully():
    q = EventQueue("m")
    make_queue_sink(q, "m")("weird", "t", "m")  # invalid level → omitted, not crash
    ev = q.payload(ack_id=0)["events"][0]
    assert "level" not in ev  # add_event only includes level when valid/provided
