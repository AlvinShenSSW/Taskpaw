"""Live monitor admin (#57): per-monitor lifecycle + enabled semantics +
atomic persistence + live-apply to the Supervisor."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig, load_yaml
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)
from taskpaw_v3.monitors.registry import PluginRegistry, default_registry
from taskpaw_v3.monitors.runtime import build_supervisor, merge_status


# ── a trivial fake plugin (no psutil / network / files in worker threads) ──
class _FakeConfig(BaseMonitorConfig):
    pass


class _FakeInstance(MonitorInstance):
    def check(self, emit) -> MonitorStatus:
        return MonitorStatus(state="ok", detail="ok")


class _FakePlugin(MonitorPlugin):
    type_id = "fake"
    display_name = "Fake"

    @classmethod
    def config_model(cls):
        return _FakeConfig

    def create(self, instance_id, config):
        return _FakeInstance(instance_id, config)


def _registry() -> PluginRegistry:
    r = PluginRegistry()
    r.register(_FakePlugin())
    return r


def _agent_config(**kw) -> AgentConfig:
    base = dict(server_id="s", machine="m", host_metrics=False)
    base.update(kw)
    return AgentConfig(**base)


# ── config + persistence layer (supervisor=None) ──────────────────────────
def test_admin_add_persists_and_dedupes(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)

    res = admin.add({"type_id": "fake", "config": {"name": "w1"}})
    assert res["ok"] and res["monitor"]["name"] == "w1"
    assert res["monitor"]["enabled"] is True

    reloaded = load_yaml(AgentConfig, path)               # atomic round-trip
    assert [m["name"] for m in reloaded.monitors] == ["w1"]
    assert reloaded.monitors[0]["enabled"] is True

    with pytest.raises(ValueError):                       # duplicate name
        admin.add({"type_id": "fake", "config": {"name": "w1"}})
    with pytest.raises(ValueError):                       # unknown type
        admin.add({"type_id": "nope", "config": {"name": "w2"}})


def test_admin_remove(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    admin.add({"type_id": "fake", "config": {"name": "w1"}})

    admin.remove("w1")
    assert cfg.monitors == []
    assert load_yaml(AgentConfig, path).monitors == []
    with pytest.raises(ValueError):
        admin.remove("missing")


def test_admin_enable_disable_persists(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    admin.add({"type_id": "fake", "config": {"name": "w1"}})

    admin.set_enabled("w1", False)
    assert cfg.monitors[0]["enabled"] is False
    assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is False
    admin.set_enabled("w1", True)
    assert cfg.monitors[0]["enabled"] is True


def test_admin_update_keeps_name(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    admin.add({"type_id": "fake", "config": {"name": "w1", "poll_interval": 10}})

    admin.update("w1", {"poll_interval": 30})
    assert cfg.monitors[0]["config"]["poll_interval"] == 30
    assert cfg.monitors[0]["config"]["name"] == "w1"     # name is stable
    with pytest.raises(ValueError):
        admin.update("missing", {"poll_interval": 5})


def test_admin_update_merges_partial_config(tmp_path):
    # PATCH: changing only poll_interval must keep the required plugin field
    # (process.pattern) and not reset it to a default (Codex #57a).
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, default_registry(), tmp_path / "a.yaml")
    admin.add({"type_id": "process", "config": {"name": "p", "pattern": "nginx",
                                                "search_cmdline": False}})
    admin.update("p", {"poll_interval": 30})         # partial — pattern omitted
    c = cfg.monitors[0]["config"]
    assert c["poll_interval"] == 30
    assert c["pattern"] == "nginx"                    # required field preserved
    assert c["search_cmdline"] is False              # optional field not reset


def test_admin_handle_dispatch(tmp_path):
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    assert admin.handle("add_monitor",
                        {"monitor": {"type_id": "fake", "config": {"name": "x"}}})["ok"]
    assert admin.handle("disable_monitor", {"name": "x"})["enabled"] is False
    assert admin.handle("nope", {})["ok"] is False
    # validation errors surface as {ok:false}, not a raised 500.
    assert admin.handle("remove_monitor", {"name": "missing"})["ok"] is False


def test_admin_add_rejects_auto_monitor_collision(tmp_path):
    # host_metrics auto-injects "<machine>-host" (here "m-host"); a user monitor
    # with that name must be rejected BEFORE persisting — else register() fails
    # after the write, leaving config changed (Codex #57a).
    cfg = _agent_config(host_metrics=True)
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    with pytest.raises(ValueError):
        admin.add({"type_id": "fake", "config": {"name": "m-host"}})
    assert cfg.monitors == []           # nothing mutated
    assert not path.exists()            # nothing persisted


def test_control_cors_allows_patch_delete(tmp_path):
    # The desktop UI preflights PATCH/DELETE for monitor edit/remove — CORS must
    # allow them or the requests never reach the handlers (Codex #57a).
    cfg = _agent_config()
    reg = _registry()
    admin = MonitorAdmin(cfg, None, reg, tmp_path / "a.yaml")
    client = TestClient(create_control_app(cfg, admin=admin, registry=reg))
    r = client.options("/control/monitors/x",
                       headers={"Origin": "http://tauri.localhost",
                                "Access-Control-Request-Method": "DELETE"})
    allowed = r.headers.get("access-control-allow-methods", "")
    assert "DELETE" in allowed and "PATCH" in allowed


def test_patch_config_invalid_does_not_flip_enabled(tmp_path):
    # A combined PATCH with an INVALID config + enabled:false must fail (400)
    # without having toggled/persisted enabled (Codex #57a).
    cfg = _agent_config()
    reg = _registry()
    admin = MonitorAdmin(cfg, None, reg, tmp_path / "a.yaml")
    admin.add({"type_id": "fake", "config": {"name": "w1"}})   # enabled True
    client = TestClient(create_control_app(cfg, admin=admin, registry=reg))

    r = client.patch("/control/monitors/w1",
                     json={"config": {"poll_interval": 0}, "enabled": False})
    assert r.status_code == 400                # poll_interval < 1 → invalid
    assert cfg.monitors[0].get("enabled", True) is True   # enabled untouched


# ── enabled filtering at build time ────────────────────────────────────────
def test_build_supervisor_skips_disabled():
    q = EventQueue(machine="m")
    monitors = [
        {"type_id": "fake", "name": "on", "config": {"name": "on"}},
        {"type_id": "fake", "name": "off", "config": {"name": "off"}, "enabled": False},
    ]
    sup = build_supervisor(_registry(), monitors, q, "m")
    assert sup.has("on") is True
    assert sup.has("off") is False


def test_merge_status_shows_disabled_as_stopped():
    # A disabled monitor must still appear in /status (as stopped) so the console
    # can list + re-enable it (Codex #57a).
    cfg = _agent_config(monitors=[
        {"type_id": "fake", "name": "on", "config": {"name": "on"}},
        {"type_id": "fake", "name": "off", "config": {"name": "off"}, "enabled": False},
    ])
    live = {"on": {"state": "ok", "metrics": {}, "detail": "", "alive": True,
                   "failures": 0, "degraded": False, "dropped": 0}}
    merged = merge_status(cfg, live)
    assert merged["on"]["state"] == "ok" and merged["on"]["enabled"] is True
    assert merged["on"]["type_id"] == "fake"
    assert merged["off"]["state"] == "stopped"
    assert merged["off"]["enabled"] is False and merged["off"]["alive"] is False


# ── supervisor live unregister ────────────────────────────────────────────
def test_supervisor_unregister():
    q = EventQueue(machine="m")
    sup = build_supervisor(
        _registry(), [{"type_id": "fake", "name": "w", "config": {"name": "w"}}], q, "m"
    )
    assert sup.has("w")
    sup.start()
    try:
        sup.unregister("w")
        assert sup.has("w") is False
    finally:
        sup.stop()
    with pytest.raises(KeyError):
        sup.unregister("w")


# ── live-apply: admin drives a running supervisor ─────────────────────────
def test_admin_live_apply(tmp_path):
    q = EventQueue(machine="m")
    reg = _registry()
    sup = build_supervisor(reg, [], q, "m")     # start empty → add the first live
    sup.start()
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, sup, reg, tmp_path / "a.yaml")
    try:
        admin.add({"type_id": "fake", "config": {"name": "w1"}})
        assert sup.has("w1")
        admin.set_enabled("w1", False)
        assert sup.has("w1") is False           # disabled → unregistered live
        admin.set_enabled("w1", True)
        assert sup.has("w1") is True            # re-enabled → re-registered
        admin.remove("w1")
        assert sup.has("w1") is False
    finally:
        sup.stop()
