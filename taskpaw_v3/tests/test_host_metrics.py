"""host_metrics plugin + Hub self-monitor (#21)."""

from __future__ import annotations

import sys
import types

import pytest

from taskpaw_v3.monitors.plugins import host_metrics as hm
from taskpaw_v3.monitors.plugins.host_metrics import HostMetricsConfig, HostMetricsPlugin
from taskpaw_v3.monitors.registry import default_registry


def fake_psutil(cpu=10.0, mem=20.0, disk=30.0, sent=0, recv=0,
                mem_total=16 * 1024**3, mem_used=None):
    ns = types.SimpleNamespace
    used = mem_used if mem_used is not None else int(mem_total * mem / 100)
    return ns(
        cpu_percent=lambda interval=None: cpu,
        # available = total - used, since host_metrics derives used from
        # total - available (to match vm.percent).
        virtual_memory=lambda: ns(percent=mem, used=used, total=mem_total,
                                  available=mem_total - used),
        disk_usage=lambda path: ns(percent=disk),
        net_io_counters=lambda: ns(bytes_sent=sent, bytes_recv=recv),
    )


def _inst(monkeypatch, **psutil_kw):
    monkeypatch.setattr(hm, "psutil", fake_psutil(**psutil_kw))
    monkeypatch.setattr(hm, "read_gpu", lambda: None)  # macOS path = n/a
    return HostMetricsPlugin().create("host", HostMetricsConfig(name="host"))


def test_registry_includes_host_metrics():
    assert "host_metrics" in default_registry().types()


def test_metrics_keys_and_gpu_na(monkeypatch):
    inst = _inst(monkeypatch, cpu=12.0, mem=34.0, disk=56.0)
    st = inst.check(lambda *a, **k: None)
    m = st.metrics
    assert set(m) == {"cpu_pct", "mem_pct", "mem_used_mb", "mem_total_mb", "disk_pct",
                      "net_in_bps", "net_out_bps",
                      "gpu_pct", "gpu_mem_used_mb", "gpu_mem_total_mb"}
    assert m["cpu_pct"] == 12.0 and m["mem_pct"] == 34.0 and m["disk_pct"] == 56.0
    # absolute RAM too (for status.md "RAM used/total GB") — 34% of 16 GiB
    assert m["mem_total_mb"] == 16 * 1024 and m["mem_used_mb"] == round(16 * 1024 * 0.34)
    assert m["gpu_pct"] == "n/a" and m["gpu_mem_used_mb"] == "n/a"  # macOS: GPU ignored
    assert st.state == "ok"


def test_cpu_sustained_alert_then_recovery(monkeypatch):
    events = []
    emit = lambda *a, **k: events.append(a)
    monkeypatch.setattr(hm, "read_gpu", lambda: None)

    # cpu over threshold (90) — needs 3 sustained cycles by default
    monkeypatch.setattr(hm, "psutil", fake_psutil(cpu=95.0))
    inst = HostMetricsPlugin().create("host", HostMetricsConfig(name="host"))
    inst.check(emit); inst.check(emit)
    assert not any(e[0] == "alert" for e in events)  # only 2 cycles
    inst.check(emit)  # 3rd → alert
    assert any(e[0] == "alert" and "cpu" in e[1] for e in events)

    # cpu drops → recovery 'done'
    monkeypatch.setattr(hm, "psutil", fake_psutil(cpu=5.0))
    inst.check(emit)
    assert any(e[0] == "done" and "cpu" in e[1] for e in events)


def test_mem_and_disk_immediate_alert(monkeypatch):
    events = []
    monkeypatch.setattr(hm, "read_gpu", lambda: None)
    monkeypatch.setattr(hm, "psutil", fake_psutil(mem=99.0, disk=99.0))
    inst = HostMetricsPlugin().create("host", HostMetricsConfig(name="host"))
    st = inst.check(lambda *a, **k: events.append(a))
    assert any(e[0] == "alert" and "memory" in e[1] for e in events)
    assert any(e[0] == "alert" and "disk" in e[1] for e in events)
    assert st.state == "degraded"  # a valid MonitorStatus state (not the event level "warn")


def test_net_throughput_uses_delta(monkeypatch):
    monkeypatch.setattr(hm, "read_gpu", lambda: None)
    inst = HostMetricsPlugin().create("host", HostMetricsConfig(name="host"))
    monkeypatch.setattr(hm, "psutil", fake_psutil(sent=0, recv=0))
    inst.check(lambda *a, **k: None)  # establishes baseline, net=0
    monkeypatch.setattr(hm, "psutil", fake_psutil(sent=1_000_000, recv=2_000_000))
    st = inst.check(lambda *a, **k: None)
    assert st.metrics["net_out_bps"] > 0 and st.metrics["net_in_bps"] > 0


def test_read_gpu_none_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert hm.read_gpu() is None


# ── Hub self-monitor ────────────────────────────────────────────────────---
def test_hub_status_includes_self_monitor(tmp_path):
    from fastapi.testclient import TestClient
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.hub.server.store import HubStore

    store = HubStore(tmp_path / "hub.db")
    try:
        app, _svc = create_hub_app(HubConfig(machine="hub", self_monitor=True), store)
        r = TestClient(app).get("/status")
        assert r.status_code == 200
        assert "hub-host" in r.json()["self"]  # self-monitor registered
    finally:
        store.close()


def test_hub_self_monitor_can_be_disabled(tmp_path):
    from fastapi.testclient import TestClient
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.hub.server.store import HubStore

    store = HubStore(tmp_path / "hub.db")
    try:
        app, _svc = create_hub_app(HubConfig(machine="hub", self_monitor=False), store)
        r = TestClient(app).get("/status")
        assert r.json()["self"] == {}
    finally:
        store.close()


def test_read_gpu_windows_parses_util_and_vram(monkeypatch):
    import types as _t
    monkeypatch.setattr(sys, "platform", "win32")

    def fake_run(*a, **k):
        return _t.SimpleNamespace(returncode=0, stdout="50, 2048, 8192\n70, 1024, 8192\n")
    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    g = hm.read_gpu()
    assert g["util_pct"] == 60.0           # avg(50,70)
    assert g["mem_used_mb"] == 3072        # 2048+1024 summed
    assert g["mem_total_mb"] == 16384


def test_agent_injects_default_host_metrics():
    from taskpaw_v3.agent.server.launcher import effective_monitors
    from taskpaw_v3.core.config import AgentConfig
    mons = effective_monitors(AgentConfig(server_id="s", machine="dev"))
    hm_specs = [m for m in mons if m["type_id"] == "host_metrics"]
    assert len(hm_specs) == 1 and hm_specs[0]["config"]["name"] == "dev-host"


def test_agent_host_metrics_can_be_disabled():
    from taskpaw_v3.agent.server.launcher import effective_monitors
    from taskpaw_v3.core.config import AgentConfig
    mons = effective_monitors(AgentConfig(server_id="s", machine="dev", host_metrics=False))
    assert not any(m["type_id"] == "host_metrics" for m in mons)


def test_agent_does_not_duplicate_configured_host_metrics():
    from taskpaw_v3.agent.server.launcher import effective_monitors
    from taskpaw_v3.core.config import AgentConfig
    cfg = AgentConfig(server_id="s", machine="dev",
                      monitors=[{"type_id": "host_metrics", "config": {"name": "custom"}}])
    mons = effective_monitors(cfg)
    assert len([m for m in mons if m["type_id"] == "host_metrics"]) == 1


def test_hub_self_monitor_repeat_alerts_not_suppressed(tmp_path):
    """breach→recover→breach must enqueue each incident (no permanent dedupe)."""
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.hub.server.store import HubStore

    store = HubStore(tmp_path / "hub.db")
    store.set_config("openclaw_enabled", "1")
    store.set_config("openclaw_token", "tok")
    try:
        _app, svc = create_hub_app(HubConfig(machine="hub", openclaw_enabled=True,
                                             openclaw_token="tok", self_monitor=True), store)
        sink = svc.self_supervisor._sink
        # same title twice (e.g. cpu high → recovered → cpu high again)
        sink("hub-host", "alert", "hub-host: cpu high", "CPU 95%")
        sink("hub-host", "alert", "hub-host: cpu high", "CPU 96%")
        n = store._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0]
        assert n == 2  # both incidents queued, not deduped away
    finally:
        store.close()


def test_supervisor_snapshot_includes_metrics(monkeypatch):
    """snapshot must surface MonitorStatus.metrics (the actual values), not just
    state/alive — otherwise host_metrics values never reach /status."""
    from taskpaw_v3.monitors.supervisor import Supervisor
    monkeypatch.setattr(hm, "psutil", fake_psutil(cpu=11.0, mem=22.0, disk=33.0))
    monkeypatch.setattr(hm, "read_gpu", lambda: None)
    sup = Supervisor(sink=lambda *a: None)
    plugin = HostMetricsPlugin()
    sup.register(plugin, HostMetricsConfig(name="host"))
    # drive one check synchronously and store the status as _run would
    inst = sup._monitors["host"].instance
    inst._status = inst.check(lambda *a, **k: None)
    snap = sup.snapshot()["host"]
    assert "metrics" in snap and snap["metrics"]["cpu_pct"] == 11.0
    assert snap["metrics"]["mem_pct"] == 22.0


def test_effective_monitors_avoids_name_collision():
    from taskpaw_v3.agent.server.launcher import effective_monitors
    from taskpaw_v3.core.config import AgentConfig
    # an existing monitor already occupies "dev-host"
    cfg = AgentConfig(server_id="s", machine="dev",
                      monitors=[{"type_id": "tcp_check", "config": {"name": "dev-host", "port": 22}}])
    mons = effective_monitors(cfg)
    names = [(m.get("config") or {}).get("name") for m in mons]
    assert names.count("dev-host") == 1            # the existing one
    hm_name = [m["config"]["name"] for m in mons if m["type_id"] == "host_metrics"][0]
    assert hm_name == "dev-host-1"                 # injected one got a free name


def test_host_metrics_recovery_message_not_threshold_claim(monkeypatch):
    monkeypatch.setattr(hm, "read_gpu", lambda: None)
    events = []
    monkeypatch.setattr(hm, "psutil", fake_psutil(mem=99.0))
    inst = HostMetricsPlugin().create("host", HostMetricsConfig(name="host"))
    inst.check(lambda *a, **k: events.append(a))          # breach
    monkeypatch.setattr(hm, "psutil", fake_psutil(mem=40.0))
    inst.check(lambda *a, **k: events.append(a))          # recover
    done = [e for e in events if e[0] == "done" and "memory" in e[1]][0]
    assert "≥" not in done[2] and "back to 40%" in done[2]
