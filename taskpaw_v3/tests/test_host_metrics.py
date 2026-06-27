"""host_metrics plugin + Hub self-monitor (#21)."""

from __future__ import annotations

import sys
import types

import pytest

from taskpaw_v3.monitors.plugins import host_metrics as hm
from taskpaw_v3.monitors.plugins.host_metrics import HostMetricsConfig, HostMetricsPlugin
from taskpaw_v3.monitors.registry import default_registry


def fake_psutil(cpu=10.0, mem=20.0, disk=30.0, sent=0, recv=0):
    ns = types.SimpleNamespace
    return ns(
        cpu_percent=lambda interval=None: cpu,
        virtual_memory=lambda: ns(percent=mem),
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
    assert set(m) == {"cpu_pct", "mem_pct", "disk_pct", "net_in_bps", "net_out_bps",
                      "gpu_pct", "gpu_mem_used_mb", "gpu_mem_total_mb"}
    assert m["cpu_pct"] == 12.0 and m["mem_pct"] == 34.0 and m["disk_pct"] == 56.0
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
