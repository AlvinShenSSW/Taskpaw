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


class _ManualPlugin(_FakePlugin):
    """A plugin that LAUNCHES something on start → added stopped (like managed Lada)."""

    type_id = "manual"

    def manual_start(self, config):
        return True


def _registry() -> PluginRegistry:
    r = PluginRegistry()
    r.register(_FakePlugin())
    return r


def _agent_config(**kw) -> AgentConfig:
    base = dict(server_id="s", machine="m", host_metrics=False)
    base.update(kw)
    return AgentConfig(**base)


def test_tasklog_operator_entries_precede_apply_and_rejections_are_silent(monkeypatch):
    from taskpaw_v3.core.tasklog import get_task_log

    cfg = _agent_config()
    sup = build_supervisor(_registry(), [], EventQueue("m"), "m")
    admin = MonitorAdmin(cfg, sup, _registry())
    observed = []
    for method in ("register", "unregister", "reconfigure"):
        original = getattr(sup, method)

        def observe(*a, _original=original, **kw):
            observed.append(get_task_log().query()["entries"][0]["kind"])
            return _original(*a, **kw)

        monkeypatch.setattr(sup, method, observe)
    admin.add({"type_id": "fake", "config": {"name": "x"}})
    admin.update("x", {"poll_interval": 12})
    admin.set_enabled("x", False)
    admin.set_enabled("x", True)
    admin.remove("x")
    assert observed == [
        "operator.add",
        "operator.update",
        "operator.start",
        "operator.remove",
    ]
    rows = get_task_log().query()["entries"]
    assert next(row for row in rows if row["kind"] == "operator.update")["data"] == {
        "fields": ["poll_interval"]
    }
    assert all(r["task_type"] == "fake" for r in rows)
    for operation in (
        lambda: admin.remove("missing"),
        lambda: admin.set_enabled("missing", True),
        lambda: admin.add({"type_id": "nope"}),
        lambda: admin.update("missing", {}),
    ):
        with pytest.raises(ValueError):
            operation()
    assert get_task_log().query()["entries"] == rows


def test_tasklog_config_and_monitor_updates_never_log_values():
    import json

    from taskpaw_v3.core.tasklog import get_task_log

    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, default_registry())
    admin.add(
        {"type_id": "process", "config": {"name": "p", "pattern": "PLANTED_ARGV"}}
    )
    admin.update("p", {"pattern": "PLANTED_COMMAND"})
    admin.update_config({"api_token": "PLANTED_SECRET", "llm_api_key": "PLANTED_KEY"})
    rows = get_task_log().query()["entries"]
    assert rows[0]["data"] == {"fields": ["api_token", "llm_api_key"]}
    assert "PLANTED" not in json.dumps(rows)


# ── config + persistence layer (supervisor=None) ──────────────────────────
def test_admin_add_persists_and_dedupes(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)

    res = admin.add({"type_id": "fake", "config": {"name": "w1"}})
    assert res["ok"] and res["monitor"]["name"] == "w1"
    assert res["monitor"]["enabled"] is True

    reloaded = load_yaml(AgentConfig, path)  # atomic round-trip
    assert [m["name"] for m in reloaded.monitors] == ["w1"]
    assert reloaded.monitors[0]["enabled"] is True

    with pytest.raises(ValueError):  # duplicate name
        admin.add({"type_id": "fake", "config": {"name": "w1"}})
    with pytest.raises(ValueError):  # unknown type
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
    assert cfg.monitors[0]["config"]["name"] == "w1"  # name is stable
    with pytest.raises(ValueError):
        admin.update("missing", {"poll_interval": 5})


def test_admin_update_merges_partial_config(tmp_path):
    # PATCH: changing only poll_interval must keep the required plugin field
    # (process.pattern) and not reset it to a default (Codex #57a).
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, default_registry(), tmp_path / "a.yaml")
    admin.add(
        {
            "type_id": "process",
            "config": {"name": "p", "pattern": "nginx", "search_cmdline": False},
        }
    )
    admin.update("p", {"poll_interval": 30})  # partial — pattern omitted
    c = cfg.monitors[0]["config"]
    assert c["poll_interval"] == 30
    assert c["pattern"] == "nginx"  # required field preserved
    assert c["search_cmdline"] is False  # optional field not reset


def test_enable_invalid_config_does_not_persist(tmp_path):
    # Enabling a disabled monitor whose stored config is invalid (e.g. a plugin
    # schema change) must fail WITHOUT persisting enabled:true — else the next
    # boot breaks while the monitor still isn't running (Codex #57a).
    reg = default_registry()  # real 'process' (needs pattern)
    cfg = _agent_config(
        monitors=[
            {
                "type_id": "process",
                "name": "bad",
                "config": {"name": "bad"},
                "enabled": False,
            },
        ]
    )
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, reg, path)
    with pytest.raises(ValueError):
        admin.set_enabled("bad", True)
    assert cfg.monitors[0]["enabled"] is False  # not flipped
    assert not path.exists()  # nothing persisted


def test_enabled_must_be_a_real_boolean(tmp_path):
    # "false"/0 etc. must be rejected, not truthy-coerced to enable (Codex #57a).
    cfg = _agent_config()
    reg = _registry()
    admin = MonitorAdmin(cfg, None, reg, tmp_path / "a.yaml")
    with pytest.raises(ValueError):
        admin.add({"type_id": "fake", "config": {"name": "w1"}, "enabled": "false"})
    admin.add({"type_id": "fake", "config": {"name": "w1"}})
    with pytest.raises(ValueError):
        admin.set_enabled("w1", "false")
    # via the PATCH route → 400
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            registry=reg,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )
    r = client.patch(
        "/control/monitors", params={"name": "w1"}, json={"enabled": "false"}
    )
    assert r.status_code == 400
    assert cfg.monitors[0]["enabled"] is True  # untouched


def test_admin_handle_dispatch(tmp_path):
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    assert admin.handle(
        "add_monitor", {"monitor": {"type_id": "fake", "config": {"name": "x"}}}
    )["ok"]
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
    assert cfg.monitors == []  # nothing mutated
    assert not path.exists()  # nothing persisted


def test_control_cors_allows_patch_delete(tmp_path):
    # The desktop UI preflights PATCH/DELETE for monitor edit/remove — CORS must
    # allow them or the requests never reach the handlers (Codex #57a).
    cfg = _agent_config()
    reg = _registry()
    admin = MonitorAdmin(cfg, None, reg, tmp_path / "a.yaml")
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            registry=reg,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )
    r = client.options(
        "/control/monitors",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    allowed = r.headers.get("access-control-allow-methods", "")
    assert "DELETE" in allowed and "PATCH" in allowed


def test_slash_named_monitor_is_manageable(tmp_path):
    # A free-form name with '/' must still be addressable for delete/update
    # (name is a query param, not a path segment) (Codex #57a).
    cfg = _agent_config()
    reg = _registry()
    admin = MonitorAdmin(cfg, None, reg, tmp_path / "a.yaml")
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            registry=reg,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )
    assert (
        client.post(
            "/control/monitors",
            json={"type_id": "fake", "config": {"name": "jobs/foo"}},
        ).status_code
        == 200
    )
    # delete it via the query-param route (path routing couldn't match "jobs/foo")
    r = client.request("DELETE", "/control/monitors", params={"name": "jobs/foo"})
    assert r.status_code == 200 and cfg.monitors == []


def test_patch_config_invalid_does_not_flip_enabled(tmp_path):
    # A combined PATCH with an INVALID config + enabled:false must fail (400)
    # without having toggled/persisted enabled (Codex #57a).
    cfg = _agent_config()
    reg = _registry()
    admin = MonitorAdmin(cfg, None, reg, tmp_path / "a.yaml")
    admin.add({"type_id": "fake", "config": {"name": "w1"}})  # enabled True
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            registry=reg,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )

    r = client.patch(
        "/control/monitors",
        params={"name": "w1"},
        json={"config": {"poll_interval": 0}, "enabled": False},
    )
    assert r.status_code == 400  # poll_interval < 1 → invalid
    assert cfg.monitors[0].get("enabled", True) is True  # enabled untouched


@pytest.mark.parametrize("enabled", ["false", 0, None, []])
def test_rejected_combined_patch_changes_nothing(tmp_path, enabled):
    from taskpaw_v3.core.tasklog import get_task_log

    cfg = _agent_config()
    reg = _registry()
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, reg, path)
    admin.add({"type_id": "fake", "config": {"name": "w1"}})
    before = cfg.model_dump()
    disk = path.read_bytes()
    rows = get_task_log().query()["entries"]
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            registry=reg,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )
    response = client.patch(
        "/control/monitors",
        params={"name": "w1"},
        json={"config": {"poll_interval": 12}, "enabled": enabled},
    )
    assert response.status_code == 400
    assert cfg.model_dump() == before
    assert path.read_bytes() == disk
    assert get_task_log().query()["entries"] == rows


# ── config editing (#43) ───────────────────────────────────────────────────
def test_update_config_token_only_is_live_no_restart(tmp_path):
    # api_token is read per-request (token_ok), so changing ONLY it applies live
    # with no restart (#43).
    cfg = _agent_config(api_token="orig")
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    res = admin.update_config({"api_token": "newtok"})
    assert res["ok"] and res["restart_required"] is False
    assert cfg.api_token == "newtok"  # in place (live)
    assert load_yaml(AgentConfig, path).api_token == "newtok"


def test_update_config_machine_change_persists_but_needs_restart(tmp_path):
    # machine tags the EventQueue + names host_metrics (baked at startup), so the
    # change PERSISTS for the next boot but the running config stays put and the
    # call reports restart_required — no runtime divergence (Codex #43).
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    res = admin.update_config({"machine": "newname"})
    assert res["restart_required"] is True
    assert cfg.machine == "m"  # running unchanged
    assert load_yaml(AgentConfig, path).machine == "newname"  # persisted for next boot


def test_update_config_masked_or_blank_token_is_kept(tmp_path):
    cfg = _agent_config(api_token="secret")
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    admin.update_config({"machine": "m2", "api_token": "***"})  # masked → keep real
    assert cfg.api_token == "secret"  # live token unchanged
    assert load_yaml(AgentConfig, path).machine == "m2"  # machine persisted
    admin.update_config({"api_token": "   "})  # blank → keep real
    assert cfg.api_token == "secret"


def test_update_config_port_change_requires_restart(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    res = admin.update_config({"control_port": 6000})
    assert res["restart_required"] is True
    assert cfg.control_port == 5681  # running socket unchanged
    assert (
        load_yaml(AgentConfig, path).control_port == 6000
    )  # persisted; applies next boot


def test_config_view_shows_pending_not_running(tmp_path):
    # The editor reads config_view: pending (desired) editable scalars + the
    # CURRENT monitors, so it doesn't send stale running values back (#43).
    cfg = _agent_config(
        monitors=[{"type_id": "fake", "name": "w", "config": {"name": "w"}}]
    )
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    admin.update_config({"control_port": 6000})
    view = admin.config_view()
    assert (
        view["control_port"] == 6000 and cfg.control_port == 5681
    )  # pending vs running
    assert [m["name"] for m in view["monitors"]] == ["w"]  # current monitors


def test_update_config_failed_persist_is_atomic(tmp_path, monkeypatch):
    # If the write fails (disk full / read-only dir), the request must raise AND
    # leave config + pending state untouched — no leaked "pending" edit (Codex r7).
    import taskpaw_v3.agent.server.admin as adminmod

    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(adminmod, "save_yaml", boom)
    with pytest.raises(OSError):
        admin.update_config({"control_port": 6000})
    assert admin.config_view()["control_port"] == 5681  # not leaked as pending
    assert cfg.control_port == 5681


def test_monitor_op_does_not_revert_pending_config_edit(tmp_path):
    # A monitor mutation persists the config too — it must NOT overwrite a pending
    # (restart-required) config edit with the old running values (Codex #43 r6).
    cfg = _agent_config()
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    admin.update_config({"control_port": 6000})  # pending edit
    admin.add({"type_id": "fake", "config": {"name": "w1"}})  # monitor op → _persist
    reloaded = load_yaml(AgentConfig, path)
    assert reloaded.control_port == 6000  # pending edit preserved
    assert [m["config"]["name"] for m in reloaded.monitors] == ["w1"]


def test_update_config_pending_restart_persists_across_saves(tmp_path):
    # A pending restart keeps being reported until the agent actually restarts —
    # comparing against the BOOT baseline, not the already-edited config (Codex r4).
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    assert admin.update_config({"control_port": 6000})["restart_required"] is True
    # a later, unrelated save must STILL flag the pending (un-restarted) port change
    assert admin.update_config({"api_token": "tok"})["restart_required"] is True
    # reverting to the running value clears it (no restart needed)
    assert admin.update_config({"control_port": 5681})["restart_required"] is False


def test_update_config_rejects_invalid(tmp_path):
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    with pytest.raises(ValueError):
        admin.update_config({"control_port": 0})  # invalid port
    with pytest.raises(ValueError):
        admin.update_config({"control_host": "0.0.0.0"})  # control must be loopback
    assert cfg.control_port == 5681 and cfg.control_host == "127.0.0.1"  # unchanged


def test_update_config_blocks_unsafe_network_exposure(tmp_path):
    # No public/WAN exposure: reject a wildcard bind from the UI, and require a
    # token for a non-loopback bind (Codex #43 P1).
    path = tmp_path / "a.yaml"
    cfg = _agent_config(api_token="")  # no token
    admin = MonitorAdmin(cfg, None, _registry(), path)
    with pytest.raises(ValueError, match="all interfaces"):
        admin.update_config({"bind_host": "0.0.0.0"})  # wildcard refused
    with pytest.raises(ValueError, match="requires an api_token"):
        admin.update_config({"bind_host": "192.168.1.50"})  # LAN bind needs a token
    assert cfg.bind_host == "127.0.0.1" and not path.exists()  # nothing persisted
    # Alternate spellings of the unspecified address are also refused — even WITH
    # a token (all-interfaces is never allowed from the UI) (Codex #43 r3).
    for wild in ("::", "0:0:0:0:0:0:0:0", "[::]"):
        with pytest.raises(ValueError, match="all interfaces"):
            admin.update_config({"bind_host": wild, "api_token": "tok"})
    # A public/WAN address is refused even WITH a token — LAN + Bearer only, in
    # lockstep with the Hub guard (#114).
    for pub in ("8.8.8.8", "[2001:4860:4860::8888]"):
        with pytest.raises(ValueError, match="public/WAN"):
            admin.update_config({"bind_host": pub, "api_token": "tok"})
    # LAN bind WITH a token is allowed (Hub-reachable + authenticated); it persists
    # for the next boot while the running bind stays at loopback until restart.
    res = admin.update_config({"bind_host": "192.168.1.50", "api_token": "tok"})
    assert res["ok"] and cfg.bind_host == "127.0.0.1"
    assert load_yaml(AgentConfig, path).bind_host == "192.168.1.50"


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


def test_manual_start_is_session_only_not_persisted():
    # Starting a manual-start monitor (managed Lada) LAUNCHES it for this session
    # but does NOT persist enabled:true — so it stays enabled:false in config and
    # the next agent boot leaves it stopped (the operator starts it each session,
    # #70). A passive monitor persists enabled:true and auto-starts at boot.
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    q = EventQueue(machine="m")
    reg = _registry()
    reg.register(_ManualPlugin())
    sup = build_supervisor(reg, [], q, "m")
    sup.start()
    cfg = _agent_config(
        monitors=[
            {
                "type_id": "manual",
                "name": "j",
                "config": {"name": "j"},
                "enabled": False,
            },
            {"type_id": "fake", "name": "f", "config": {"name": "f"}, "enabled": False},
        ]
    )
    admin = MonitorAdmin(cfg, sup, reg, tmp / "a.yaml")
    try:
        admin.set_enabled("j", True)
        assert sup.has("j") is True  # launched live this session
        assert cfg.monitors[0]["enabled"] is False  # but NOT persisted enabled
        admin.set_enabled("f", True)
        assert (
            sup.has("f") is True and cfg.monitors[1]["enabled"] is True
        )  # passive persists
        admin.set_enabled("j", False)
        assert sup.has("j") is False  # stop unregisters live
    finally:
        sup.stop()


def test_merge_status_shows_disabled_as_stopped():
    # A disabled monitor must still appear in /status (as stopped) so the console
    # can list + re-enable it (Codex #57a).
    cfg = _agent_config(
        monitors=[
            {"type_id": "fake", "name": "on", "config": {"name": "on"}},
            {
                "type_id": "fake",
                "name": "off",
                "config": {"name": "off"},
                "enabled": False,
            },
        ]
    )
    live = {
        "on": {
            "state": "ok",
            "metrics": {},
            "detail": "",
            "alive": True,
            "failures": 0,
            "degraded": False,
            "dropped": 0,
        }
    }
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


def test_enable_monitor_with_toplevel_name_only(tmp_path):
    # YAML shape {type_id, name, config:{...}} with NO config.name must still be
    # enable-able — the validated config gets the resolved name injected, like
    # build_supervisor() does at boot (Codex #57a).
    q = EventQueue(machine="m")
    reg = _registry()
    sup = build_supervisor(reg, [], q, "m")
    sup.start()
    cfg = _agent_config(
        monitors=[
            {"type_id": "fake", "name": "topname", "config": {}, "enabled": False},
        ]
    )
    admin = MonitorAdmin(cfg, sup, reg, tmp_path / "a.yaml")
    try:
        res = admin.set_enabled("topname", True)
        assert res["enabled"] is True
        assert sup.has("topname")  # registered live, no validation error
    finally:
        sup.stop()


# ── manual-start plugins are ADDED STOPPED (V2 parity, #70) ────────────────
def test_manual_start_plugin_added_stopped(tmp_path):
    # A monitor whose plugin wants a manual start (managed Lada launches lada-cli)
    # is added DISABLED + NOT registered, so saving the form doesn't kick off work
    # — the operator clicks Start. Passive monitors still auto-enable, and an
    # explicit enabled:true still wins.
    q = EventQueue(machine="m")
    reg = _registry()
    reg.register(_ManualPlugin())
    sup = build_supervisor(reg, [], q, "m")
    sup.start()
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, sup, reg, tmp_path / "a.yaml")
    try:
        res = admin.add({"type_id": "manual", "config": {"name": "j"}})
        assert res["monitor"]["enabled"] is False
        assert sup.has("j") is False  # not registered → nothing launched
        res2 = admin.add(
            {"type_id": "manual", "config": {"name": "k"}, "enabled": True}
        )
        assert (
            res2["monitor"]["enabled"] is True and sup.has("k") is True
        )  # explicit wins
        res3 = admin.add({"type_id": "fake", "config": {"name": "f"}})
        assert (
            res3["monitor"]["enabled"] is True and sup.has("f") is True
        )  # passive auto-on
    finally:
        sup.stop()


def test_managed_lada_added_stopped_passive_enabled(tmp_path):
    # The real Lada plugin: managed (CLI path) → added stopped; passive → enabled.
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, default_registry(), tmp_path / "a.yaml")
    managed = admin.add(
        {
            "type_id": "lada",
            "config": {
                "name": "L",
                "lada_cli_path": "C:/lada-cli.exe",
                "lada_input_folder": "C:/in",
                "lada_output_folder": "C:/out",
            },
        }
    )
    assert managed["monitor"]["enabled"] is False
    passive = admin.add(
        {"type_id": "lada", "config": {"name": "P", "process_name": "lada-cli"}}
    )
    assert passive["monitor"]["enabled"] is True


# ── live-apply: admin drives a running supervisor ─────────────────────────
def test_admin_live_apply(tmp_path):
    q = EventQueue(machine="m")
    reg = _registry()
    sup = build_supervisor(reg, [], q, "m")  # start empty → add the first live
    sup.start()
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, sup, reg, tmp_path / "a.yaml")
    try:
        admin.add({"type_id": "fake", "config": {"name": "w1"}})
        assert sup.has("w1")
        admin.set_enabled("w1", False)
        assert sup.has("w1") is False  # disabled → unregistered live
        admin.set_enabled("w1", True)
        assert sup.has("w1") is True  # re-enabled → re-registered
        admin.remove("w1")
        assert sup.has("w1") is False
    finally:
        sup.stop()


# ── global LLM API settings (#178) ─────────────────────────────────────────
_LLM_KEY = "sk-ADMINKEY-41d0"


def test_update_config_llm_fields_are_live_no_restart(tmp_path):
    # T-A1: the three LLM fields are live-safe: persisted, applied to the running
    # config, and published to the process-wide holder — no restart_required.
    from taskpaw_v3.core.llm import get_llm_settings

    cfg = _agent_config()
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    res = admin.update_config(
        {
            "llm_api_base": "http://127.0.0.1:11434/v1/",
            "llm_model": " qwen3 ",
            "llm_api_key": _LLM_KEY,
        }
    )
    assert res == {"ok": True, "restart_required": False}
    on_disk = load_yaml(AgentConfig, path)
    assert (on_disk.llm_api_base, on_disk.llm_model, on_disk.llm_api_key) == (
        "http://127.0.0.1:11434/v1",
        "qwen3",
        _LLM_KEY,
    )
    assert (cfg.llm_api_base, cfg.llm_model, cfg.llm_api_key) == (
        "http://127.0.0.1:11434/v1",
        "qwen3",
        _LLM_KEY,
    )
    s = get_llm_settings()
    assert (s.api_base, s.model, s.api_key, s.key_source) == (
        "http://127.0.0.1:11434/v1",
        "qwen3",
        _LLM_KEY,
        "config",
    )
    # A non-live field alongside still reports restart_required (unchanged).
    assert admin.update_config({"machine": "m2", "llm_model": "x"})["restart_required"]


def test_update_config_llm_key_keep_clear_and_env(tmp_path, monkeypatch):
    # T-A2 (D12): blank/*** keeps the stored key; null clears it; an env key is
    # never written back into agent.yaml.
    from taskpaw_v3.core.llm import LLM_KEY_ENV, get_llm_settings

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    for kept in ("***", "  ", "", " *** "):
        admin.update_config({"llm_api_key": kept, "llm_model": "m1"})
        assert cfg.llm_api_key == _LLM_KEY
        assert load_yaml(AgentConfig, path).llm_api_key == _LLM_KEY
    admin.update_config({"llm_api_key": None})
    assert cfg.llm_api_key == ""
    assert load_yaml(AgentConfig, path).llm_api_key == ""
    assert get_llm_settings().key_source == "none"
    # With the env var set, saves report source env but persist only the stored.
    monkeypatch.setenv(LLM_KEY_ENV, "sk-ENVKEY-0000")
    admin.update_config({"llm_model": "m2", "llm_api_key": "***"})
    assert "sk-ENVKEY-0000" not in path.read_text(encoding="utf-8")
    assert load_yaml(AgentConfig, path).llm_api_key == ""
    s = get_llm_settings()
    assert (s.api_key, s.key_source, s.model) == ("sk-ENVKEY-0000", "env", "m2")


def test_update_config_llm_failed_save_leaves_holder(tmp_path, monkeypatch):
    # T-A3: validate → guard → save → commit → live-apply. A failed save leaves
    # the running config AND the holder untouched.
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import get_llm_settings

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    before = get_llm_settings()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(adminmod, "save_yaml", boom)
    with pytest.raises(OSError):
        admin.update_config({"llm_model": "new/model", "llm_api_key": None})
    assert get_llm_settings() is before
    assert (cfg.llm_model, cfg.llm_api_key) == ("grok-4.3", _LLM_KEY)
    assert admin.config_view()["llm_model"] == "grok-4.3"


def test_update_config_rejects_bad_llm_base(tmp_path):
    cfg = _agent_config()
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    with pytest.raises(ValueError):
        admin.update_config({"llm_api_base": "ftp://x"})
    assert not path.exists() and cfg.llm_api_base == "https://api.x.ai/v1"


class _ChatSpy:
    """Stands in for admin.chat: records the call and checks the admin lock is
    NOT held while the (slow, network) request runs (D11)."""

    def __init__(self, admin, result=None, exc=None):
        self.admin = admin
        self.result = result
        self.exc = exc
        self.calls: list = []

    def __call__(self, settings, messages, **kw):
        assert self.admin._lock.acquire(blocking=False), "admin lock held in chat"
        self.admin._lock.release()
        self.calls.append((settings, messages, kw))
        if self.exc is not None:
            raise self.exc
        return self.result


_PROBE_REPLY = '{"1": "你好"}'


def _ok_result(finish_reason="stop", content=_PROBE_REPLY):
    from taskpaw_v3.core.llm import ChatResult

    return ChatResult(content, finish_reason, "served/m", 42)


def test_llm_test_uses_candidate_without_persisting(tmp_path, monkeypatch):
    # T-A4: candidate base/model reach chat() with the real translation probe
    # (#192 G5/H4: strict, json_mode on); nothing persists.
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import get_llm_chain, get_llm_settings
    from taskpaw_v3.monitors.subs.translate import PROBE_MAX_TOKENS, probe_messages

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    holder = get_llm_settings()
    chain = get_llm_chain()
    desired = dict(admin._desired)
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    res = admin.llm_test(
        {"llm_api_base": "http://h:1/v1/", "llm_model": "cand/m", "llm_api_key": ""}
    )
    assert res == {"ok": True, "model": "served/m", "latency_ms": 42}
    ((settings, messages, kw),) = spy.calls
    assert (settings.api_base, settings.model) == ("http://h:1/v1", "cand/m")
    assert (settings.api_key, settings.key_source) == (_LLM_KEY, "config")  # blank
    assert messages == probe_messages()
    assert kw == {
        "max_tokens": PROBE_MAX_TOKENS,
        "json_mode": True,
        "timeout": 20,
        "strict": True,
    }
    # Nothing touched: desired, running config, disk, holders.
    assert admin._desired == desired
    assert (cfg.llm_api_base, cfg.llm_model) == (
        "https://api.x.ai/v1",
        "grok-4.3",
    )
    assert not path.exists()
    assert get_llm_settings() is holder
    assert get_llm_chain() is chain


def test_llm_test_key_resolution(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLM_KEY_ENV

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    admin.llm_test({"llm_api_key": "***"})  # masked → the stored key
    admin.llm_test({"llm_api_key": None})  # null = absent here (no clear)
    admin.llm_test({"llm_api_key": " sk-typed \r\n"})  # a typed candidate, stripped
    monkeypatch.setenv(LLM_KEY_ENV, "sk-ENVKEY-1111")
    admin.llm_test({})  # env first
    assert [(c[0].api_key, c[0].key_source) for c in spy.calls] == [
        (_LLM_KEY, "config"),
        (_LLM_KEY, "config"),
        ("sk-typed", "config"),
        ("sk-ENVKEY-1111", "env"),
    ]
    assert cfg.llm_api_key == _LLM_KEY  # a typed candidate is never saved


def test_llm_test_errors_never_carry_exception_text(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLMError

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    monkeypatch.setattr(
        adminmod,
        "chat",
        _ChatSpy(admin, exc=LLMError("auth", "authentication failed", 401)),
    )
    res = admin.llm_test({})
    assert res == {"ok": False, "error": "auth: authentication failed (HTTP 401)"}
    monkeypatch.setattr(
        adminmod, "chat", _ChatSpy(admin, exc=LLMError("bad_response", "HTTP 500", 500))
    )
    assert admin.llm_test({}) == {"ok": False, "error": "bad_response: HTTP 500"}
    monkeypatch.setattr(
        adminmod, "chat", _ChatSpy(admin, exc=LLMError("network", "timeout"))
    )
    assert admin.llm_test({}) == {"ok": False, "error": "network: timeout"}
    monkeypatch.setattr(
        adminmod, "chat", _ChatSpy(admin, exc=RuntimeError(f"boom {_LLM_KEY}"))
    )
    res = admin.llm_test({})
    assert res == {"ok": False, "error": "unexpected error: RuntimeError"}
    assert _LLM_KEY not in str(res)


class _BodyOpener:
    """For the REAL chat(): `.open()` answers each request with the next canned
    envelope (hermetic — no socket) and records the request payloads."""

    def __init__(self, *bodies: dict):
        self.bodies = list(bodies)
        self.payloads: list = []

    def open(self, request, timeout=None):
        import io
        import json

        self.payloads.append(json.loads(request.data))
        return io.BytesIO(json.dumps(self.bodies.pop(0)).encode("utf-8"))


def _envelope(content, finish_reason="stop"):
    return {
        "model": "served/m",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }


def _real_chat_with(monkeypatch, opener):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import chat as real_chat

    monkeypatch.setattr(
        adminmod,
        "chat",
        lambda settings, messages, **kw: real_chat(
            settings, messages, opener=opener, **kw
        ),
    )


def test_llm_test_truncated_reply_fails_like_the_engine_probe(tmp_path, monkeypatch):
    # #192 H4: the Test uses the engine's OK rule — a length-truncated reply is a
    # failure there (strict), so it is one here too (was "ok, truncated").
    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    _real_chat_with(monkeypatch, _BodyOpener(_envelope(_PROBE_REPLY, "length")))
    assert admin.llm_test({}) == {
        "ok": False,
        "error": "bad_response: finish_reason=length",
    }


def test_llm_test_real_chat_probe_round_trip(tmp_path, monkeypatch):
    # The real chat() sends the probe: the system prompt + the こんにちは cue,
    # json_mode on; a valid reply is OK with the served model.
    from taskpaw_v3.monitors.subs.translate import SYSTEM_PROMPT

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    opener = _BodyOpener(_envelope(_PROBE_REPLY))
    _real_chat_with(monkeypatch, opener)
    res = admin.llm_test({"llm_api_key": _LLM_KEY})
    assert res["ok"] is True and res["model"] == "served/m"
    assert set(res) == {"ok", "model", "latency_ms"}  # IR3: no `truncated`
    assert isinstance(res["latency_ms"], int)
    ((payload),) = opener.payloads
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert "こんにちは" in payload["messages"][1]["content"]


def test_llm_test_400_retries_once_without_json_mode(tmp_path, monkeypatch):
    # H4/AC6: HTTP 400 on the json_mode request → ONE retry without json_mode.
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLMError

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    outcomes: list = [LLMError("bad_response", "HTTP 400", 400), _ok_result()]
    calls: list = []

    def fake(settings, messages, **kw):
        calls.append(kw["json_mode"])
        got = outcomes.pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(adminmod, "chat", fake)
    res = admin.llm_test({})
    assert res["ok"] is True and calls == [True, False]
    # 400 again without json_mode → that error, still exactly two requests.
    outcomes[:] = [
        LLMError("bad_response", "HTTP 400", 400),
        LLMError("bad_response", "HTTP 400", 400),
    ]
    calls.clear()
    assert admin.llm_test({}) == {"ok": False, "error": "bad_response: HTTP 400"}
    assert calls == [True, False]


@pytest.mark.parametrize(
    "exc",
    [
        ("auth", "authentication failed", 401),
        ("bad_response", "HTTP 402", 402),
        ("bad_response", "HTTP 404", 404),
        ("rate_limit", "rate limited", 429),
        ("refusal", "empty reply", None),
    ],
)
def test_llm_test_other_failures_are_not_retried(tmp_path, monkeypatch, exc):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLMError

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    spy = _ChatSpy(admin, exc=LLMError(*exc))
    monkeypatch.setattr(adminmod, "chat", spy)
    res = admin.llm_test({})
    assert res["ok"] is False and res["error"].startswith(f"{exc[0]}: {exc[1]}")
    assert len(spy.calls) == 1


@pytest.mark.parametrize(
    "content",
    ["OK", '{"2": "你好"}', '{"1": ""}', '{"1": "a", "1": "b"}', "PROBEMARKER-9d9d"],
)
def test_llm_test_ok_only_when_the_reply_is_a_valid_translation(
    tmp_path, monkeypatch, content
):
    # H4: an envelope-OK reply that fails the translator's _validate is NOT ok.
    import taskpaw_v3.agent.server.admin as adminmod

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    monkeypatch.setattr(
        adminmod, "chat", _ChatSpy(admin, result=_ok_result(content=content))
    )
    res = admin.llm_test({})
    assert res["ok"] is False and res["error"].startswith("invalid: ")
    assert content not in res["error"]  # the reply text is never echoed


def test_llm_test_bad_base_is_value_error_without_key(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    with pytest.raises(ValueError) as ei:
        admin.llm_test({"llm_api_base": "ftp://x", "llm_api_key": _LLM_KEY})
    assert "llm_api_base" in str(ei.value) and _LLM_KEY not in str(ei.value)
    with pytest.raises(ValueError) as ei:
        admin.llm_test({"llm_api_key": 12345})  # wrong type: value not echoed
    assert "12345" not in str(ei.value)
    assert spy.calls == []


def test_handle_llm_test_dispatches(tmp_path, monkeypatch):
    # T-A5: the /control/command path reaches llm_test, with or without a
    # `candidate` envelope; validation errors come back as {ok: false}.
    import taskpaw_v3.agent.server.admin as adminmod

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    assert admin.handle("llm_test", {"candidate": {"llm_model": "a/b"}})["ok"] is True
    assert admin.handle("llm_test", {"command": "llm_test", "llm_model": "c/d"})["ok"]
    assert [c[0].model for c in spy.calls] == ["a/b", "c/d"]
    res = admin.handle("llm_test", {"llm_api_base": "ftp://x"})
    assert res["ok"] is False and "llm_api_base" in res["error"]
    res = admin.handle("llm_test", {"candidate": ["not", "an", "object"]})
    assert res == {"ok": False, "error": "llm test candidate must be an object"}
    assert len(spy.calls) == 2


def test_live_and_non_live_config_partition():
    # #178/#192: exactly the token + the LLM fields (three per slot + the
    # failover switch) are live; the rest (the restart-required baseline) is
    # unchanged.
    assert set(MonitorAdmin._LIVE_CONFIG) <= set(MonitorAdmin._EDITABLE_CONFIG)
    assert set(MonitorAdmin._LIVE_CONFIG) == {
        "api_token",
        "llm_api_base",
        "llm_model",
        "llm_api_key",
        "llm_thinking_off",
        "llm_fallback1_api_base",
        "llm_fallback1_model",
        "llm_fallback1_api_key",
        "llm_fallback1_thinking_off",
        "llm_fallback2_api_base",
        "llm_fallback2_model",
        "llm_fallback2_api_key",
        "llm_fallback2_thinking_off",
        "llm_failover",
    }
    assert MonitorAdmin._NON_LIVE_CONFIG == (
        "machine",
        "bind_host",
        "bind_port",
        "control_host",
        "control_port",
        "host_metrics",
    )


def test_update_config_400_never_echoes_a_secret_input(tmp_path):
    # Codex 外门 #178: PATCH /control/config with a wrongly typed llm_api_key
    # (or api_token) is rejected with 400, and the detail carries the field
    # name but never the value (pydantic input hidden at the model level).
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )
    marker = "sk-super-secret-marker"
    for field in (
        "llm_api_key",
        "llm_fallback1_api_key",
        "llm_fallback2_api_key",
        "api_token",
    ):
        r = client.patch("/control/config", json={field: [marker]})
        assert r.status_code == 400
        assert marker not in r.text and field in r.text
    assert cfg.llm_api_key == "" and cfg.api_token == ""  # nothing applied
    assert cfg.llm_fallback1_api_key == cfg.llm_fallback2_api_key == ""


# ── #190/#192: fallback providers + failover (AC1, AC11 backend) ───────────
_DS_BASE = "https://api.deepseek.com/v1"


def _fb(slot: str, base: str = _DS_BASE, model: str = "deepseek-chat", key=None):
    from taskpaw_v3.core.llm import llm_slot_fields

    fb, fm, fk = llm_slot_fields(slot)
    out = {fb: base, fm: model}
    if key is not None:
        out[fk] = key
    return out


def test_update_config_fallbacks_and_failover_are_live_and_publish_the_chain(
    tmp_path,
):
    from taskpaw_v3.core.llm import get_llm_chain, get_llm_failover

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    res = admin.update_config(
        {
            **_fb("fallback1", base=_DS_BASE + "/", key=" sk-FB1-a1a1 "),
            **_fb("fallback2", base="http://127.0.0.1:11434/v1", model="qwen3"),
            "llm_failover": False,
        }
    )
    assert res == {"ok": True, "restart_required": False}
    on_disk = load_yaml(AgentConfig, path)
    assert (on_disk.llm_fallback1_api_base, on_disk.llm_fallback1_api_key) == (
        _DS_BASE,
        "sk-FB1-a1a1",
    )
    assert (on_disk.llm_fallback2_model, on_disk.llm_failover) == ("qwen3", False)
    assert (cfg.llm_fallback1_model, cfg.llm_failover) == ("deepseek-chat", False)
    assert [(s.api_base, s.model, s.api_key) for s in get_llm_chain()] == [
        ("https://api.x.ai/v1", "grok-4.3", _LLM_KEY),
        (_DS_BASE, "deepseek-chat", "sk-FB1-a1a1"),
        ("http://127.0.0.1:11434/v1", "qwen3", ""),
    ]
    assert get_llm_failover() is False
    admin.update_config({"llm_failover": True, "llm_api_key": None})
    assert get_llm_failover() is True
    assert [s.model for s in get_llm_chain()] == ["deepseek-chat", "qwen3"]


@pytest.mark.parametrize("slot", ["fallback1", "fallback2"])
def test_update_config_fallback_key_keep_clear_and_env(tmp_path, monkeypatch, slot):
    # AC11: blank/*** keeps the stored fallback key; null clears it; the slot's
    # env key is never written back into agent.yaml and wins at resolution.
    from taskpaw_v3.core.llm import LLM_SLOT_KEY_ENV, get_llm_chain, llm_slot_fields

    _fbase, fmodel, fkey = llm_slot_fields(slot)
    stored = f"sk-{slot.upper()}-STORED-9c9c"
    cfg = _agent_config(**_fb(slot, key=stored))
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    for kept in ("***", "  ", "", " *** "):
        admin.update_config({fkey: kept, fmodel: "m1"})
        assert getattr(cfg, fkey) == stored
        assert getattr(load_yaml(AgentConfig, path), fkey) == stored
    assert [(s.model, s.api_key) for s in get_llm_chain()] == [("m1", stored)]
    admin.update_config({fkey: None})
    assert getattr(cfg, fkey) == ""
    assert getattr(load_yaml(AgentConfig, path), fkey) == ""
    assert get_llm_chain() == ()  # keyless non-loopback fallback → unusable
    env_key = f"sk-ENV-{slot.upper()}-0000"
    monkeypatch.setenv(LLM_SLOT_KEY_ENV[slot], env_key)
    admin.update_config({fmodel: "m2", fkey: "***"})
    assert env_key not in path.read_text(encoding="utf-8")
    assert getattr(load_yaml(AgentConfig, path), fkey) == ""
    assert [(s.model, s.api_key, s.key_source) for s in get_llm_chain()] == [
        ("m2", env_key, "env")
    ]


def test_update_config_failed_save_leaves_the_chain_holders(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import get_llm_chain, get_llm_failover

    cfg = _agent_config(llm_api_key=_LLM_KEY)
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    admin.update_config({**_fb("fallback1", key="sk-FB1-b2b2")})
    chain = get_llm_chain()
    assert len(chain) == 2

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(adminmod, "save_yaml", boom)
    with pytest.raises(OSError):
        admin.update_config({"llm_fallback1_api_key": None, "llm_failover": False})
    assert get_llm_chain() is chain and get_llm_failover() is True
    assert (cfg.llm_fallback1_api_key, cfg.llm_failover) == ("sk-FB1-b2b2", True)


@pytest.mark.parametrize("slot", ["fallback1", "fallback2"])
def test_llm_test_probes_the_given_slot(tmp_path, monkeypatch, slot):
    # AC11: Test for a fallback slot uses THAT slot's candidate fields (the
    # primary's are ignored), keeps a blank/*** key, resolves the slot's env key
    # first, and never persists or publishes.
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLM_SLOT_KEY_ENV, get_llm_chain, llm_slot_fields
    from taskpaw_v3.monitors.subs.translate import probe_messages

    fbase, fmodel, fkey = llm_slot_fields(slot)
    stored = f"sk-{slot.upper()}-STORED-7e7e"
    cfg = _agent_config(llm_api_key=_LLM_KEY, **_fb(slot, key=stored))
    path = tmp_path / "a.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    chain = get_llm_chain()
    res = admin.llm_test(
        {
            "llm_api_base": "http://primary-ignored/v1",
            "llm_model": "primary/ignored",
            fbase: "https://mimo.example/v1/",
            fmodel: " mimo ",
            fkey: "***",
        },
        slot,
    )
    assert res == {"ok": True, "model": "served/m", "latency_ms": 42}
    settings, messages, _kw = spy.calls[-1]
    assert (settings.api_base, settings.model) == ("https://mimo.example/v1", "mimo")
    assert (settings.api_key, settings.key_source) == (stored, "config")
    assert messages == probe_messages()
    admin.llm_test({fkey: " sk-typed \r\n"}, slot)  # a typed candidate, stripped
    assert (spy.calls[-1][0].api_key, spy.calls[-1][0].model) == (
        "sk-typed",
        "deepseek-chat",
    )
    monkeypatch.setenv(LLM_SLOT_KEY_ENV[slot], "sk-ENV-SLOT-1212")
    admin.llm_test({}, slot)  # the slot's env key first
    assert (spy.calls[-1][0].api_key, spy.calls[-1][0].key_source) == (
        "sk-ENV-SLOT-1212",
        "env",
    )
    assert getattr(cfg, fkey) == stored and not path.exists()
    assert get_llm_chain() is chain


def test_llm_test_unknown_slot_is_a_value_error(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod

    admin = MonitorAdmin(_agent_config(), None, _registry(), tmp_path / "a.yaml")
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    for bad in ("fallback3", "", None, 1):
        with pytest.raises(ValueError):
            admin.llm_test({}, bad)  # type: ignore[arg-type]
    res = admin.handle("llm_test", {"candidate": {}, "slot": "nope"})
    assert res["ok"] is False and "slot" in res["error"]
    assert spy.calls == []


def test_handle_llm_test_passes_the_slot(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod

    admin = MonitorAdmin(
        _agent_config(**_fb("fallback2", key="sk-FB2-c3c3")),
        None,
        _registry(),
        tmp_path / "a.yaml",
    )
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    admin.handle("llm_test", {"candidate": {}, "slot": "fallback2"})
    admin.handle("llm_test", {"command": "llm_test", "slot": "fallback2"})
    assert [(c[0].model, c[0].api_key) for c in spy.calls] == [
        ("deepseek-chat", "sk-FB2-c3c3"),
        ("deepseek-chat", "sk-FB2-c3c3"),
    ]


def test_llm_test_fallback_errors_never_carry_a_key(tmp_path, monkeypatch):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLMError

    stored = "sk-FB1-STORED-4d4d"
    admin = MonitorAdmin(
        _agent_config(**_fb("fallback1", key=stored)),
        None,
        _registry(),
        tmp_path / "a.yaml",
    )
    with pytest.raises(ValueError) as ei:
        admin.llm_test(
            {"llm_fallback1_api_base": "ftp://x", "llm_fallback1_api_key": stored},
            "fallback1",
        )
    assert "llm_fallback1_api_base" in str(ei.value) and stored not in str(ei.value)
    monkeypatch.setattr(
        adminmod, "chat", _ChatSpy(admin, exc=RuntimeError(f"boom {stored}"))
    )
    res = admin.llm_test({}, "fallback1")
    assert res == {"ok": False, "error": "unexpected error: RuntimeError"}
    monkeypatch.setattr(
        adminmod,
        "chat",
        _ChatSpy(admin, exc=LLMError("auth", "authentication failed", 401)),
    )
    res = admin.llm_test({}, "fallback1")
    assert res == {"ok": False, "error": "auth: authentication failed (HTTP 401)"}
    assert stored not in str(res)


@pytest.mark.parametrize("prefix", ["llm_", "llm_fallback1_", "llm_fallback2_"])
def test_thinking_patch_live_persistence(tmp_path, prefix):
    from taskpaw_v3.core.llm import get_llm_chain

    cfg = _agent_config(
        **{prefix + "api_base": "http://localhost/v1", prefix + "model": "m"}
    )
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    client = TestClient(
        create_control_app(
            cfg,
            admin=admin,
            control_token="test-control-token",
            control_active=lambda: True,
        ),
        headers={"Authorization": "Bearer test-control-token"},
    )
    field = prefix + "thinking_off"
    for value in (True, False, None):
        response = client.patch("/control/config", json={field: value})
        assert response.status_code == 200
        assert response.json()["restart_required"] is False
        assert getattr(load_yaml(AgentConfig, path), field) is value
        assert getattr(cfg, field) is value
        assert get_llm_chain()[0].thinking_off is (value is True)


@pytest.mark.parametrize(
    "prefix,slot",
    [
        ("llm_", "primary"),
        ("llm_fallback1_", "fallback1"),
        ("llm_fallback2_", "fallback2"),
    ],
)
def test_thinking_test_null_is_auto(tmp_path, monkeypatch, prefix, slot):
    import taskpaw_v3.agent.server.admin as adminmod

    field = prefix + "thinking_off"
    cfg = _agent_config(
        **{prefix + "api_base": "https://api.deepseek.com", field: False}
    )
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    spy = _ChatSpy(admin, result=_ok_result())
    monkeypatch.setattr(adminmod, "chat", spy)
    for candidate, expected in (
        ({}, False),
        ({field: None}, True),
        ({field: True}, True),
        ({field: False}, False),
    ):
        assert set(admin.llm_test(candidate, slot)) == {"ok", "model", "latency_ms"}
        assert spy.calls[-1][0].thinking_off is expected
    assert getattr(cfg, field) is False


@pytest.mark.parametrize("success_step", [0, 1, 2, 3])
def test_thinking_test_four_steps_and_attributed_note(
    tmp_path, monkeypatch, success_step
):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLMError

    admin = MonitorAdmin(
        _agent_config(llm_thinking_off=True), None, _registry(), tmp_path / "a.yaml"
    )
    seen = []

    def fake(settings, messages, **kw):
        seen.append((kw["json_mode"], settings.thinking_off))
        if len(seen) <= success_step:
            raise LLMError("bad_response", "HTTP 400", 400)
        return _ok_result()

    monkeypatch.setattr(adminmod, "chat", fake)
    result = admin.llm_test({})
    assert (
        seen
        == [(True, True), (True, False), (False, True), (False, False)][
            : success_step + 1
        ]
    )
    assert result == {
        "ok": True,
        "model": "served/m",
        "latency_ms": 42,
        **({"note": "thinking_unsupported"} if success_step in (1, 3) else {}),
    }


@pytest.mark.parametrize(
    "thinking,second_status,expected",
    [
        (True, None, [(True, True), (True, False)]),
        (True, 422, [(True, True), (True, False)]),
        (False, None, [(True, False)]),
    ],
)
def test_thinking_test_422_only_on_thinking_step(
    tmp_path, monkeypatch, thinking, second_status, expected
):
    import taskpaw_v3.agent.server.admin as adminmod
    from taskpaw_v3.core.llm import LLMError

    admin = MonitorAdmin(
        _agent_config(llm_thinking_off=thinking), None, _registry(), tmp_path / "a.yaml"
    )
    seen = []

    def fake(settings, messages, **kw):
        seen.append((kw["json_mode"], settings.thinking_off))
        if len(seen) == 1 or second_status:
            raise LLMError("bad_response", "HTTP 422", 422)
        return _ok_result()

    monkeypatch.setattr(adminmod, "chat", fake)
    result = admin.llm_test({})
    assert seen == expected
    assert result["ok"] is (thinking and second_status is None)
    if result["ok"]:
        assert result["note"] == "thinking_unsupported"


# R07: failures must describe disk and actual state, not leak an in-place edit.
@pytest.mark.parametrize("operation", ["add", "remove", "start", "stop", "update"])
def test_r07_persistence_failure_keeps_desired_and_reports_actual(
    tmp_path, monkeypatch, operation
):
    import copy

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.core.config import save_yaml

    spec = {
        "type_id": "fake",
        "name": "owned",
        "config": {"name": "owned"},
        "enabled": operation != "start",
    }
    cfg = _agent_config(monitors=[] if operation == "add" else [spec])
    reg = _registry()
    sup = build_supervisor(reg, cfg.monitors, EventQueue("m"), "m")
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    before = copy.deepcopy(cfg.monitors)
    before_bytes = path.read_bytes()
    adm = MonitorAdmin(cfg, sup, reg, path)
    monkeypatch.setattr(
        module,
        "save_yaml",
        lambda *a: (_ for _ in ()).throw(OSError("PLANTED_SECRET_PATH")),
    )
    try:
        if operation == "add":
            result = adm.add(spec)
        elif operation == "remove":
            result = adm.remove("owned")
        elif operation in ("start", "stop"):
            result = adm.set_enabled("owned", operation == "start")
        else:
            result = adm.update("owned", {"poll_interval": 20})
        assert not result["ok"] and result["persistence"] == "failed"
        assert "PLANTED" not in str(result)
        assert cfg.monitors == before and path.read_bytes() == before_bytes
        assert sup.has("owned") is (operation in ("remove", "update"))
        if operation == "stop":
            assert result["runtime"] == "stopped"
    finally:
        sup.stop(timeout=1)


def test_r07_d1_actual_validator_deadline_single_owner_and_independent_stop(
    tmp_path, monkeypatch
):
    import threading
    from pathlib import Path

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.core.config import save_yaml
    from taskpaw_v3.monitors.plugins import dev_activity

    class OwnedDev(_FakePlugin):
        type_id = "dev_activity"

        @classmethod
        def config_model(cls):
            return dev_activity.DevActivityConfig

    reg = PluginRegistry()
    reg.register(OwnedDev())
    spec = {
        "type_id": "dev_activity",
        "name": "owned",
        "enabled": True,
        "config": {"name": "owned", "session_roots": {"codex": ["/r07-virtual"]}},
    }
    cfg = _agent_config(monitors=[spec])
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    before = path.read_bytes()
    sup = build_supervisor(
        _registry(),
        [{"type_id": "fake", "config": {"name": "owned"}}],
        EventQueue("m"),
        "m",
    )
    adm = MonitorAdmin(cfg, sup, reg, path)
    adm._timeout = 0.06  # test wait budget, never a production env bypass
    entered, release = threading.Event(), threading.Event()
    save_entered, save_release = threading.Event(), threading.Event()
    original_save = module.save_yaml
    stop_save = None
    calls, results = [], []

    def metadata(path):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return True

    def gated_save(*args):
        if threading.current_thread().name == "persist-stop-owned":
            save_entered.set()
            assert save_release.wait(3)
        original_save(*args)

    monkeypatch.setattr(module, "save_yaml", gated_save)
    monkeypatch.setattr(dev_activity.os.path, "realpath", lambda p: str(p))
    monkeypatch.setattr(dev_activity, "safe_path", metadata)
    monkeypatch.setattr(Path, "exists", lambda p: False)
    first = threading.Thread(
        target=lambda: results.append(
            adm.patch("owned", {"config": {"poll_interval": 20}, "enabled": False})
        )
    )
    first.start()
    try:
        assert entered.wait(1)
        first.join(0.3)
        assert not first.is_alive(), "combined PATCH exceeded its entry deadline"
        assert results[0]["error_code"] == "validation_timeout"
        assert results[0]["persistence"] == "not_requested"
        assert sup.has("owned") and path.read_bytes() == before
        assert (
            adm.patch("owned", {"config": {"poll_interval": 30}})["outcome"] == "busy"
        )
        assert len(calls) == 1
        owner = adm._owners["owned"]
        stopped = adm.set_enabled("owned", False)
        assert stopped["runtime"] == "stopped" and not sup.has("owned")
        stop_save = adm._stop_records["owned"]
        assert save_entered.wait(1) and stopped["persistence"] == "pending"
        assert not stop_save.done.is_set() and path.read_bytes() == before
        assert adm._owners["owned"] is owner and len(calls) == 1
        release.set()
        owner.thread.join(1)
        assert not owner.thread.is_alive()
        assert cfg.monitors[0]["config"].get("poll_interval", 10) != 20
        save_release.set()
        assert stop_save.done.wait(1)
        stop_save.thread.join(1)
        assert not stop_save.thread.is_alive() and stop_save.validated == "saved"
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is False
        assert not sup.has("owned")
        assert not adm.config_view()["monitor_operations"]
        assert adm.update("owned", {"poll_interval": 30})["ok"]
        assert len(calls) == 2
    finally:
        release.set()
        save_release.set()
        first.join(1)
        stop_save = stop_save or adm._stop_records.get("owned")
        if stop_save is not None and stop_save.thread is not None:
            stop_save.thread.join(1)
        sup.stop(timeout=1)


def test_r07_blocked_save_cannot_block_stop_or_late_start(tmp_path, monkeypatch):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.core.config import save_yaml

    cfg = _agent_config(
        monitors=[
            {
                "type_id": "fake",
                "name": "owned",
                "enabled": False,
                "config": {"name": "owned"},
            }
        ]
    )
    reg = _registry()
    sup = build_supervisor(reg, [], EventQueue("m"), "m")
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=0.05)
    entered, release = threading.Event(), threading.Event()
    original = module.save_yaml

    def stalled(*args):
        entered.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(module, "save_yaml", stalled)
    responses = []
    worker = threading.Thread(
        target=lambda: responses.append(adm.set_enabled("owned", True))
    )
    worker.start()
    try:
        assert entered.wait(1)
        stopped = adm.set_enabled("owned", False)
        assert stopped["runtime"] == "stopped" and stopped["persistence"] == "pending"
        release.set()
        worker.join(1)
        assert not worker.is_alive() and not sup.has("owned")
        assert (
            cfg.monitors[0]["enabled"] is True
        )  # committed old candidate, never started
        assert adm.set_enabled("owned", False)["persistence"] == "saved"
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is False
    finally:
        release.set()
        worker.join(1)
        sup.stop(timeout=1)


@pytest.mark.parametrize("phase", ["write", "flush", "fsync", "replace"])
@pytest.mark.parametrize("operation", ["add", "remove", "start", "stop", "update"])
def test_r07_real_storage_fault_boundaries(tmp_path, monkeypatch, phase, operation):
    import builtins
    import copy
    import os

    from taskpaw_v3.core.config import save_yaml

    path = tmp_path / "owned.yaml"
    spec = {
        "type_id": "fake",
        "name": "owned",
        "config": {"name": "owned"},
        "enabled": operation != "start",
    }
    cfg = _agent_config(monitors=[] if operation == "add" else [spec])
    save_yaml(cfg, path)
    old_bytes = path.read_bytes()
    old_desired = copy.deepcopy(cfg.monitors)
    reg = _registry()
    sup = build_supervisor(reg, cfg.monitors, EventQueue("m"), "m")
    adm = MonitorAdmin(cfg, sup, reg, path)
    original_open = builtins.open

    class FaultFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            self.file.__enter__()
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def write(self, value):
            if phase == "write":
                raise OSError("owned fault")
            return self.file.write(value)

        def flush(self):
            if phase == "flush":
                raise OSError("owned fault")
            return self.file.flush()

        def fileno(self):
            return self.file.fileno()

    def opened(file, *args, **kwargs):
        result = original_open(file, *args, **kwargs)
        return (
            FaultFile(result)
            if str(file) == str(path.with_suffix(".yaml.tmp"))
            else result
        )

    if phase in {"write", "flush"}:
        monkeypatch.setattr(builtins, "open", opened)
    else:
        monkeypatch.setattr(
            os, phase, lambda *a: (_ for _ in ()).throw(OSError("owned fault"))
        )
    try:
        if operation == "add":
            result = adm.add(spec)
        elif operation == "remove":
            result = adm.remove("owned")
        elif operation in {"start", "stop"}:
            result = adm.set_enabled("owned", operation == "start")
        else:
            result = adm.update("owned", {"poll_interval": 20})
        assert result["persistence"] == "failed"
        assert path.read_bytes() == old_bytes and cfg.monitors == old_desired
        assert not path.with_suffix(".yaml.tmp").exists()
        assert sup.has("owned") is (operation in {"remove", "update"})
    finally:
        sup.stop(timeout=1)


def test_r07_start_failure_reaps_plain_owned_child_before_retry(tmp_path):
    import subprocess
    import sys
    import time

    from taskpaw_v3.monitors.supervisor import Supervisor

    class Launch(_FakeInstance):
        child = None

        def start(self, emit):
            self.child = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            raise RuntimeError("PLANTED_START_SECRET")

        def stop(self, timeout=5):
            if self.child is not None:
                self.child.terminate()
                self.child.wait(timeout=max(0.2, timeout))

    class LaunchPlugin(_FakePlugin):
        instances = []

        def create(self, iid, cfg):
            item = Launch(iid, cfg)
            self.instances.append(item)
            return item

    reg = PluginRegistry()
    plugin = LaunchPlugin()
    reg.register(plugin)
    cfg = _agent_config()
    sup = Supervisor(lambda *a: None)
    sup.start()
    adm = MonitorAdmin(cfg, sup, reg, tmp_path / "owned.yaml", operation_timeout=1)
    try:
        result = adm.add({"type_id": "fake", "config": {"name": "owned"}})
        assert result["outcome"] == "persisted_runtime_failed"
        assert result["error_code"] == "start_failed" and "PLANTED" not in str(result)
        until = time.monotonic() + 2
        while sup.has("owned") and time.monotonic() < until:
            time.sleep(0.01)
        assert plugin.instances[0].child.poll() is not None and not sup.has("owned")
        retried = adm.set_enabled("owned", True)
        assert retried["error_code"] == "start_failed"
        assert len(plugin.instances) == 2
        assert plugin.instances[0].child.poll() is not None
    finally:
        sup.stop(timeout=2)
        for item in plugin.instances:
            if item.child is not None and item.child.poll() is None:
                item.child.kill()
                item.child.wait(timeout=2)


def test_r07_remove_retains_failed_cleanup_and_actual_row(tmp_path):
    from taskpaw_v3.monitors.supervisor import Supervisor

    class Broken(_FakeInstance):
        calls = 0

        def stop(self, timeout=5):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("PLANTED_CLEANUP_SECRET")

    class BrokenPlugin(_FakePlugin):
        def create(self, iid, cfg):
            return Broken(iid, cfg)

    plugin = BrokenPlugin()
    reg = PluginRegistry()
    reg.register(plugin)
    cfg = _agent_config()
    sup = Supervisor(lambda *a: None)
    adm = MonitorAdmin(cfg, sup, reg, tmp_path / "owned.yaml", operation_timeout=0.1)
    adm.add({"type_id": "fake", "config": {"name": "owned"}})
    result = adm.remove("owned")
    assert result["runtime"] == "stopping" and result["error_code"] == "cleanup_failed"
    assert cfg.monitors == [] and sup.has("owned")
    row = adm.status_view()["owned"]
    assert (
        row["configured"] is False
        and row["type_id"] == "fake"
        and row["lifecycle"] == "remove_pending"
    )
    assert (
        adm.add({"type_id": "fake", "config": {"name": "owned"}})["outcome"] == "busy"
    )
    assert adm.set_enabled("owned", False)["runtime"] == "stopped"
    assert "owned" not in adm.status_view()
    assert adm.add({"type_id": "fake", "config": {"name": "owned"}})["ok"]
    sup.stop(timeout=1)


@pytest.mark.parametrize("manual,enabled", [(False, True), (True, False), (True, True)])
def test_r07_stop_restart_uses_disk_and_manual_start_preserves_policy(
    tmp_path, monkeypatch, manual, enabled
):
    from taskpaw_v3.agent.server import admin as admin_mod
    from taskpaw_v3.core.config import save_yaml

    reg = _registry()
    reg.register(_ManualPlugin())
    cfg = _agent_config(
        monitors=[
            {
                "type_id": "manual" if manual else "fake",
                "config": {"name": "owned"},
                "enabled": enabled,
            }
        ]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    sup = build_supervisor(reg, cfg.monitors, EventQueue("m"), "m")
    adm = MonitorAdmin(cfg, sup, reg, path)
    if manual:
        assert adm.set_enabled("owned", True)["ok"]
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is enabled
    original = admin_mod.save_yaml
    monkeypatch.setattr(
        admin_mod,
        "save_yaml",
        lambda *a: (_ for _ in ()).throw(OSError("PLANTED_DISK_PATH")),
    )
    result = adm.set_enabled("owned", False)
    assert result["runtime"] == "stopped" and not sup.has("owned")
    saved = load_yaml(AgentConfig, path)
    assert saved.monitors[0]["enabled"] is enabled
    restarted = build_supervisor(reg, saved.monitors, EventQueue("m"), "m")
    assert restarted.has("owned") is enabled
    restarted.stop(timeout=1)
    monkeypatch.setattr(admin_mod, "save_yaml", original)
    assert adm.set_enabled("owned", False)["ok"]
    saved = load_yaml(AgentConfig, path)
    assert saved.monitors[0]["enabled"] is False
    restarted = build_supervisor(reg, saved.monitors, EventQueue("m"), "m")
    assert not restarted.has("owned")
    restarted.stop(timeout=1)
    sup.stop(timeout=1)


def test_r07_combined_valid_disable_save_failure_still_stops_without_validation_on_stop(
    tmp_path, monkeypatch
):
    from taskpaw_v3.agent.server import admin as admin_mod
    from taskpaw_v3.core.config import save_yaml

    reg = _registry()
    cfg = _agent_config(
        monitors=[{"type_id": "fake", "config": {"name": "owned"}, "enabled": True}]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    before = path.read_bytes()
    sup = build_supervisor(reg, cfg.monitors, EventQueue("m"), "m")
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=0.2)
    monkeypatch.setattr(
        admin_mod,
        "save_yaml",
        lambda *a: (_ for _ in ()).throw(OSError("PLANTED_DISK_PATH")),
    )
    result = adm.patch("owned", {"config": {"poll_interval": 13}, "enabled": False})
    assert (
        result["outcome"] == "applied_not_persisted" and result["runtime"] == "stopped"
    )
    assert path.read_bytes() == before and cfg.monitors[0]["config"] == {
        "name": "owned"
    }
    assert not sup.has("owned")
    # The standalone path doesn't touch a now-invalid stored config validator.
    monkeypatch.setattr(
        reg.get("fake"),
        "validate_config",
        lambda *a: (_ for _ in ()).throw(AssertionError("validator entered")),
    )
    assert adm.set_enabled("owned", False)["runtime"] == "stopped"
    sup.stop(timeout=1)


@pytest.mark.parametrize("operation", ["add", "update"])
def test_r07_create_failure_preserves_committed_desired_and_old_actual(
    tmp_path, operation
):
    from taskpaw_v3.monitors.supervisor import Supervisor

    class FaultPlugin(_FakePlugin):
        broken = False
        created = 0

        def create(self, iid, cfg):
            self.created += 1
            if self.broken:
                raise RuntimeError("PLANTED_CREATE_SECRET")
            return super().create(iid, cfg)

    plugin = FaultPlugin()
    reg = PluginRegistry()
    reg.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    adm = MonitorAdmin(cfg, sup, reg, path)
    old = None
    if operation == "update":
        assert adm.add({"type_id": "fake", "config": {"name": "owned"}})["ok"]
        old = sup._monitors["owned"]
    plugin.broken = True
    result = (
        adm.add({"type_id": "fake", "config": {"name": "owned"}})
        if operation == "add"
        else adm.update("owned", {"poll_interval": 13})
    )
    assert result["outcome"] == "persisted_runtime_failed"
    assert result["error_code"] == "create_failed" and "PLANTED" not in str(result)
    assert load_yaml(AgentConfig, path).monitors == cfg.monitors
    if old:
        assert sup._monitors["owned"] is old and not old.stop.is_set()
        assert adm.status_view()["owned"]["config_in_sync"] is False
    else:
        assert not sup.has("owned")
    plugin.broken = False
    retry = (
        adm.set_enabled("owned", True)
        if operation == "add"
        else adm.update("owned", {"poll_interval": 13})
    )
    assert retry["ok"] and adm.status_view()["owned"]["config_in_sync"] is True
    assert plugin.created == (3 if old else 2)
    sup.stop(timeout=1)


@pytest.mark.parametrize("fault", ["create", "init"])
def test_r07_sr002_edit_reconciles_missing_enabled_passive_owner(tmp_path, fault):
    import time

    from taskpaw_v3.monitors.supervisor import Supervisor

    class Owned(_FakeInstance):
        def start(self, emit):
            if plugin.broken and fault == "init":
                raise RuntimeError("PLANTED_INIT_SECRET")
            started.append(self)

        def stop(self, timeout=5):
            stopped.append(self)

    class Plugin(_FakePlugin):
        broken = True
        creates = 0

        def create(self, iid, cfg):
            self.creates += 1
            if self.broken and fault == "create":
                raise RuntimeError("PLANTED_CREATE_SECRET")
            return Owned(iid, cfg)

    started, stopped = [], []
    plugin = Plugin()
    registry = PluginRegistry()
    registry.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    sup.start()
    admin = MonitorAdmin(cfg, sup, registry, path, operation_timeout=1)
    try:
        added = admin.add({"type_id": "fake", "config": {"name": "owned"}})
        assert added["outcome"] == "persisted_runtime_failed"
        until = time.monotonic() + 1
        while sup.has("owned") and time.monotonic() < until:
            time.sleep(0.005)
        assert not sup.has("owned")
        plugin.broken = False
        result = admin.update("owned", {"poll_interval": 13})
        assert result["ok"] and result["runtime"] == "applied"
        assert sup.has("owned") and len(started) == 1 and plugin.creates == 2
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is True
        assert sup.config_matches("owned", cfg.monitors[0]["config"]) is True
        again = admin.update("owned", {"poll_interval": 13})
        assert again["ok"] and sup.has("owned")
        # Healthy exact-config retry must not introduce concurrent ownership;
        # normal reconfigure may retire/recreate according to existing behavior.
        assert all(item is not started[-1] for item in stopped)
    finally:
        sup.stop(1)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_r07_sr003_validator_launch_failure_releases_only_own_reservation(
    tmp_path, monkeypatch, phase
):
    import threading

    from taskpaw_v3.agent.server import admin as admin_mod
    from taskpaw_v3.core.config import save_yaml
    from taskpaw_v3.monitors.supervisor import Supervisor

    cfg = _agent_config(
        monitors=[{"type_id": "fake", "config": {"name": "owned"}, "enabled": False}]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    old = path.read_bytes()
    registry = _registry()
    sup = Supervisor(lambda *a: None)
    admin = MonitorAdmin(cfg, sup, registry, path, operation_timeout=0.05)
    original_thread, original_start = threading.Thread, threading.Thread.start

    def construct(*args, **kwargs):
        if kwargs.get("name") == "validate-owned":
            raise RuntimeError("PLANTED_CONSTRUCTOR_SECRET")
        return original_thread(*args, **kwargs)

    def start(t):
        if t.name == "validate-owned":
            raise RuntimeError("PLANTED_START_SECRET")
        return original_start(t)

    try:
        with monkeypatch.context() as patch:
            if phase == "construct":
                patch.setattr(admin_mod.threading, "Thread", construct)
            else:
                patch.setattr(threading.Thread, "start", start)
            result = admin.update("owned", {"poll_interval": 13})
            assert result["outcome"] == "not_applied"
            assert (
                result["persistence"] == "not_requested"
                and result["runtime"] == "unchanged"
            )
            assert result["error_code"] == "start_failed" and "PLANTED" not in str(
                result
            )
            assert admin.config_view()["monitor_operations"] == {}
            assert path.read_bytes() == old and not sup.has("owned")
        assert admin.set_enabled("owned", False)["runtime"] == "stopped"
        retried = admin.update("owned", {"poll_interval": 13})
        assert retried["ok"] and admin.config_view()["monitor_operations"] == {}
        assert cfg.monitors[0]["config"]["poll_interval"] == 13
        assert not sup.has("owned")
    finally:
        sup.stop(1)


@pytest.mark.parametrize(
    "policy", ["disabled", "manual-false", "manual-true", "stopped-unsaved"]
)
def test_r07_sr002_edit_recovery_preserves_stop_and_manual_policy(
    tmp_path, monkeypatch, policy
):
    from taskpaw_v3.agent.server import admin as admin_mod
    from taskpaw_v3.core.config import save_yaml
    from taskpaw_v3.monitors.supervisor import Supervisor

    reg = _registry()
    reg.register(_ManualPlugin())
    manual = policy.startswith("manual")
    enabled = policy in {"manual-true", "stopped-unsaved"}
    cfg = _agent_config(
        monitors=[
            {
                "type_id": "manual" if manual else "fake",
                "config": {"name": "owned"},
                "enabled": enabled,
            }
        ]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    sup = Supervisor(lambda *a: None)
    admin = MonitorAdmin(cfg, sup, reg, path)
    try:
        if policy == "stopped-unsaved":
            before = path.read_bytes()
            with monkeypatch.context() as patch:
                patch.setattr(
                    admin_mod,
                    "save_yaml",
                    lambda *a: (_ for _ in ()).throw(OSError("owned-save-fault")),
                )
                stopped = admin.set_enabled("owned", False)
                assert (
                    stopped["runtime"] == "stopped"
                    and stopped["persistence"] == "failed"
                )
                assert (
                    path.read_bytes() == before and cfg.monitors[0]["enabled"] is True
                )
            assert "owned" in admin._overrides
        edited = admin.update("owned", {"poll_interval": 13})
        assert edited["ok"] and not sup.has("owned")
        actual_enabled = False if policy == "stopped-unsaved" else enabled
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is actual_enabled
        if policy == "stopped-unsaved":
            assert "owned" in admin._overrides
    finally:
        sup.stop(1)


@pytest.mark.parametrize("second_name", ["owned", "other"])
def test_r07_ir001_add_rechecks_eligibility_after_overlapping_validation(
    tmp_path, second_name
):
    import threading

    entered, release = threading.Event(), threading.Event()

    class Plugin(_FakePlugin):
        creates = 0

        def validate_config(self, raw):
            if threading.current_thread().name == "first-add":
                entered.set()
                assert release.wait(3)
            return super().validate_config(raw)

        def create(self, iid, cfg):
            self.creates += 1
            return super().create(iid, cfg)

    plugin = Plugin()
    registry = PluginRegistry()
    registry.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    admin = MonitorAdmin(cfg, None, registry, path)
    results, errors = [], []

    def first_add():
        try:
            results.append(admin.add({"type_id": "fake", "config": {"name": "owned"}}))
        except ValueError as error:
            errors.append(error)

    worker = threading.Thread(target=first_add, name="first-add")
    rebuilt = None
    worker.start()
    try:
        assert entered.wait(1)
        assert admin.add({"type_id": "fake", "config": {"name": second_name}})["ok"]
        committed = path.read_bytes()
        release.set()
        worker.join(1)
        assert not worker.is_alive()
        if second_name == "owned":
            assert len(errors) == 1 and "already exists" in str(errors[0])
            assert results == [] and path.read_bytes() == committed
        else:
            assert errors == [] and len(results) == 1 and results[0]["ok"]
        saved = load_yaml(AgentConfig, path)
        expected = {"owned", second_name}
        assert len(saved.monitors) == len(expected)
        assert {item["name"] for item in saved.monitors} == expected
        assert saved.monitors == cfg.monitors and plugin.creates == 0
        # The actual next-start consumer must accept the persisted configuration.
        rebuilt = build_supervisor(registry, saved.monitors, EventQueue("m"), "m")
        assert set(rebuilt.snapshot()) == expected and plugin.creates == len(expected)
    finally:
        release.set()
        worker.join(1)
        if rebuilt is not None:
            rebuilt.stop(1)


def test_r07_ir001_add_active_owner_is_busy_and_partial_retry_is_exact(
    tmp_path, monkeypatch
):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.monitors.supervisor import Supervisor

    class Plugin(_FakePlugin):
        broken = True
        creates = 0

        def create(self, iid, cfg):
            self.creates += 1
            if self.broken:
                raise RuntimeError("owned-create-fault")
            return super().create(iid, cfg)

    plugin = Plugin()
    registry = PluginRegistry()
    registry.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    admin = MonitorAdmin(cfg, sup, registry, path)
    entered, release = threading.Event(), threading.Event()
    original = module.save_yaml

    def gated_save(*args):
        entered.set()
        assert release.wait(3)
        original(*args)

    results = []
    spec = {"type_id": "fake", "config": {"name": "owned"}}
    worker = threading.Thread(target=lambda: results.append(admin.add(spec)))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "save_yaml", gated_save)
            worker.start()
            assert entered.wait(1)
            assert admin.add(spec)["outcome"] == "busy" and plugin.creates == 0
            release.set()
            worker.join(1)
            assert not worker.is_alive()
        assert results[0]["outcome"] == "persisted_runtime_failed"
        committed = path.read_bytes()
        with pytest.raises(ValueError, match="already exists"):
            admin.add(
                {"type_id": "fake", "config": {"name": "owned", "poll_interval": 13}}
            )
        assert path.read_bytes() == committed and plugin.creates == 1
        plugin.broken = False
        assert admin.add(spec)["ok"] and plugin.creates == 2
        assert (
            len(cfg.monitors) == 1
            and load_yaml(AgentConfig, path).monitors == cfg.monitors
        )
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(1)
        sup.stop(1)


@pytest.mark.parametrize("second_interval", [10, 13])
def test_r07_ir001_overlapping_add_uses_current_partial_retry_spec(
    tmp_path, second_interval
):
    import threading

    from taskpaw_v3.monitors.supervisor import Supervisor

    entered, release = threading.Event(), threading.Event()

    class Plugin(_FakePlugin):
        broken = True
        creates = 0

        def validate_config(self, raw):
            if threading.current_thread().name == "first-add":
                entered.set()
                assert release.wait(3)
            return super().validate_config(raw)

        def create(self, iid, cfg):
            self.creates += 1
            if self.broken:
                raise RuntimeError("owned-create-fault")
            return super().create(iid, cfg)

    plugin = Plugin()
    registry = PluginRegistry()
    registry.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    admin = MonitorAdmin(cfg, sup, registry, path)
    results, errors = [], []

    def first_add():
        try:
            results.append(admin.add({"type_id": "fake", "config": {"name": "owned"}}))
        except ValueError as error:
            errors.append(error)

    worker = threading.Thread(target=first_add, name="first-add")
    worker.start()
    try:
        assert entered.wait(1)
        partial = admin.add(
            {
                "type_id": "fake",
                "config": {"name": "owned", "poll_interval": second_interval},
            }
        )
        assert partial["outcome"] == "persisted_runtime_failed"
        committed = path.read_bytes()
        plugin.broken = False
        release.set()
        worker.join(1)
        assert not worker.is_alive() and len(cfg.monitors) == 1
        if second_interval == 10:
            assert errors == [] and results[0]["ok"] and plugin.creates == 2
            assert sup.has("owned")
        else:
            assert len(errors) == 1 and "already exists" in str(errors[0])
            assert results == [] and plugin.creates == 1 and not sup.has("owned")
            assert path.read_bytes() == committed
        assert load_yaml(AgentConfig, path).monitors == cfg.monitors
    finally:
        release.set()
        worker.join(1)
        sup.stop(1)


@pytest.mark.parametrize("phase", ["construct", "start"])
def test_r07_ir002_stop_writer_launch_failure_releases_unstarted_owner(
    tmp_path, monkeypatch, phase
):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.core.config import save_yaml

    registry = _registry()
    cfg = _agent_config(
        monitors=[{"type_id": "fake", "config": {"name": "owned"}, "enabled": True}]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    before = path.read_bytes()
    sup = build_supervisor(registry, cfg.monitors, EventQueue("m"), "m")
    admin = MonitorAdmin(cfg, sup, registry, path, operation_timeout=0.05)
    original_thread, original_start = threading.Thread, threading.Thread.start

    def construct(*args, **kwargs):
        if kwargs.get("name") == "persist-stop-owned":
            raise MemoryError("PLANTED_CONSTRUCTOR_SECRET")
        return original_thread(*args, **kwargs)

    def start(thread):
        if thread.name == "persist-stop-owned":
            raise RuntimeError("PLANTED_START_SECRET")
        return original_start(thread)

    try:
        with monkeypatch.context() as patch:
            if phase == "construct":
                patch.setattr(module.threading, "Thread", construct)
            else:
                patch.setattr(threading.Thread, "start", start)
            result = admin.set_enabled("owned", False)
        assert result["runtime"] == "stopped" and not sup.has("owned")
        assert (
            result["persistence"] == "failed"
            and result["error_code"] == "persistence_failed"
        )
        assert "PLANTED" not in str(result)
        save = admin._stop_records["owned"]
        assert save.done.is_set() and save.validated == "failed"
        assert "owned" not in admin._stop_saves and not admin._mutation.locked()
        assert admin.status_view()["owned"]["persistence"] == "failed"
        assert path.read_bytes() == before and "owned" in admin._overrides
        assert admin.add(
            {"type_id": "fake", "config": {"name": "other"}, "enabled": False}
        )["ok"]
        assert admin.set_enabled("owned", False)["persistence"] == "saved"
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is False
    finally:
        # Baseline constructor fault has no writer; release only that local leak.
        save = admin._stop_records.get("owned")
        if save is not None and save.thread is None and admin._mutation.locked():
            admin._mutation.release()
        sup.stop(1)


def test_r07_ir002_started_writer_survives_raising_start_wrapper(tmp_path, monkeypatch):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.core.config import save_yaml

    registry = _registry()
    cfg = _agent_config(
        monitors=[
            {"type_id": "fake", "config": {"name": name}, "enabled": True}
            for name in ("owned", "other")
        ]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    before = path.read_bytes()
    sup = build_supervisor(registry, cfg.monitors, EventQueue("m"), "m")
    admin = MonitorAdmin(cfg, sup, registry, path, operation_timeout=0.03)
    entered, release = threading.Event(), threading.Event()
    original_save, original_start = module.save_yaml, threading.Thread.start
    writers = []

    def gated_save(*args):
        writers.append(threading.current_thread())
        entered.set()
        assert release.wait(5)
        original_save(*args)

    def start(thread):
        result = original_start(thread)
        if thread.name == "persist-stop-owned":
            raise RuntimeError("PLANTED_STARTED_SECRET")
        return result

    save = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module, "save_yaml", gated_save)
            patch.setattr(threading.Thread, "start", start)
            result = admin.set_enabled("owned", False)
            assert entered.wait(1)
            save = admin._stop_records["owned"]
            assert result["runtime"] == "stopped" and result["persistence"] == "pending"
            assert "PLANTED" not in str(result)
            assert save.thread.ident is not None and save.thread.is_alive()
            assert admin._stop_saves["owned"] is save and admin._mutation.locked()
            assert not save.done.is_set() and path.read_bytes() == before
            assert admin.set_enabled("owned", False)["persistence"] == "pending"
            assert admin._stop_saves["owned"] is save and len(writers) == 1
            independent = admin.set_enabled("other", False)
            assert (
                independent["runtime"] == "stopped"
                and independent["persistence"] == "pending"
            )
            assert not sup.has("owned") and not sup.has("other")
            assert (
                admin.add({"type_id": "fake", "config": {"name": "third"}})["outcome"]
                == "busy"
            )
            release.set()
            assert save.done.wait(1)
            save.thread.join(1)
        assert save.validated == "saved" and not save.thread.is_alive()
        assert (
            not admin._stop_saves and not admin._mutation.locked() and len(writers) == 1
        )
        saved = load_yaml(AgentConfig, path).monitors
        assert saved[0]["enabled"] is False and saved[1]["enabled"] is True
        # The independent Stop preceded storage admission; its override keeps
        # actual execution stopped while an explicit retry persists that intent.
        assert "other" in admin._overrides and not sup.has("other")
        assert admin.status_view()["owned"]["persistence"] == "saved"
        assert admin.set_enabled("other", False)["persistence"] == "saved"
        assert all(
            item["enabled"] is False for item in load_yaml(AgentConfig, path).monitors
        )
        assert admin.add(
            {"type_id": "fake", "config": {"name": "third"}, "enabled": False}
        )["ok"]
    finally:
        release.set()
        if save is not None and save.thread is not None:
            save.thread.join(1)
        sup.stop(1)


def test_r07_ir002_completed_writer_survives_raising_start_wrapper(
    tmp_path, monkeypatch
):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.core.config import save_yaml

    registry = _registry()
    cfg = _agent_config(
        monitors=[{"type_id": "fake", "config": {"name": "owned"}, "enabled": True}]
    )
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    admin = MonitorAdmin(cfg, None, registry, path)
    original_start = threading.Thread.start

    def start(thread):
        result = original_start(thread)
        if thread.name == "persist-stop-owned":
            thread.join(1)
            assert not thread.is_alive()
            raise RuntimeError("PLANTED_COMPLETED_SECRET")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(module.threading.Thread, "start", start)
        result = admin.set_enabled("owned", False)
    save = admin._stop_records["owned"]
    assert save.thread.ident is not None and not save.thread.is_alive()
    assert save.done.is_set() and save.validated == "saved"
    assert result["persistence"] == "saved" and "PLANTED" not in str(result)
    assert not admin._stop_saves and not admin._mutation.locked()
    assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is False


@pytest.fixture
def r07_rejected_validation(monkeypatch):
    """A real rejected Edit, retaining its live validator until the caller exits."""
    import threading
    from contextlib import contextmanager

    @contextmanager
    def reject(admin, plugin, kind="timeout"):
        entered, release = threading.Event(), threading.Event()
        responses = []
        original = plugin.validate_config
        owner = None

        def validate(raw):
            entered.set()
            assert release.wait(5)
            return original(raw)

        def edit():
            responses.append(admin.update("owned", {"poll_interval": 23}))

        with monkeypatch.context() as patch:
            patch.setattr(plugin, "validate_config", validate)
            if kind == "launch":
                original_start = threading.Thread.start

                def start(thread):
                    if thread.name == "validate-owned":
                        raise RuntimeError("PLANTED_VALIDATOR_SECRET")
                    return original_start(thread)

                patch.setattr(threading.Thread, "start", start)
            caller = threading.Thread(target=edit)
            caller.start()
            try:
                if kind != "launch":
                    assert entered.wait(2)
                    owner = admin._owners["owned"]
                    if kind == "cancel":
                        # A genuine concurrent settings commit invalidates this
                        # validator's revision without changing monitor state.
                        assert admin.update_config({"machine": "m"})["ok"]
                        release.set()
                caller.join(2)
                assert not caller.is_alive() and len(responses) == 1
                result = responses[0]
                assert result["outcome"] == "not_applied" and not result["ok"]
                assert result["persistence"] == "not_requested"
                assert result["runtime"] == "unchanged"
                assert (
                    result["error_code"]
                    == {
                        "timeout": "validation_timeout",
                        "cancel": "validation_cancelled",
                        "launch": "start_failed",
                    }[kind]
                )
                assert "PLANTED" not in str(result)
                yield result
            finally:
                release.set()
                caller.join(2)
                assert not caller.is_alive()
                if owner is not None and owner.thread is not None:
                    owner.thread.join(2)
                    assert not owner.thread.is_alive()
        assert not admin.config_view()["monitor_operations"]

    return reject


def test_r07_or001_add_reports_published_start_failure_before_retirement(
    tmp_path, monkeypatch
):
    import threading

    from taskpaw_v3.monitors.supervisor import Supervisor

    entered, release = threading.Event(), threading.Event()
    stopped, checked = [], []

    class Owned(_FakeInstance):
        def start(self, emit):
            raise RuntimeError("PLANTED_START_SECRET")

        def check(self, emit):
            checked.append(self)
            return super().check(emit)

        def stop(self, timeout=5):
            stopped.append(self)

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg)

    reg = PluginRegistry()
    reg.register(Plugin())
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=1)
    original_retire = sup._retire
    owners = []

    def before_retire(iid, managed, deadline):
        if threading.current_thread() is managed.thread:
            owners.append(managed)
            entered.set()
            assert release.wait(5)
        return original_retire(iid, managed, deadline)

    monkeypatch.setattr(sup, "_retire", before_retire)
    try:
        sup.start()
        result = adm.add({"type_id": "fake", "config": {"name": "owned"}})
        assert entered.wait(2)
        managed = owners[0]
        assert managed.initialized.is_set() and not managed.retiring
        assert managed.init_error == "start_failed" and checked == []
        assert result["outcome"] == "persisted_runtime_failed" and not result["ok"]
        assert result["persistence"] == "saved" and result["runtime"] == "failed"
        assert result["error_code"] == "start_failed" and "PLANTED" not in str(result)
        assert load_yaml(AgentConfig, path).monitors == cfg.monitors
        assert adm.status_view()["owned"]["runtime_error_code"] == "start_failed"
        release.set()
        managed.thread.join(2)
        assert not managed.thread.is_alive()
        assert sup.stop_result("owned", 2)["complete"]
        managed.cleanup_thread.join(2)
        assert stopped == [managed.instance] and not sup.has("owned")
        row = adm.status_view()["owned"]
        assert (
            row["lifecycle"] == "stopped"
            and row["runtime_error_code"] == "start_failed"
        )
    finally:
        release.set()
        sup.stop(2)
        for managed in owners:
            managed.thread.join(2)
            if managed.cleanup_thread is not None:
                managed.cleanup_thread.join(2)


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("rejection", ["timeout", "cancel", "launch"])
def test_r07_or002_validation_rejection_is_response_only_for_healthy_runtime(
    tmp_path, cached, rejection, r07_rejected_validation
):
    import copy

    from taskpaw_v3.core.config import save_yaml

    spec = {"type_id": "fake", "config": {"name": "owned"}, "enabled": True}
    cfg = _agent_config(monitors=[] if cached else [spec])
    reg = _registry()
    sup = build_supervisor(reg, cfg.monitors, EventQueue("m"), "m")
    path = tmp_path / "owned.yaml"
    save_yaml(cfg, path)
    adm = MonitorAdmin(
        cfg, sup, reg, path, operation_timeout=2 if rejection == "cancel" else 0.1
    )
    try:
        sup.start()
        if cached:
            assert adm.add(spec)["ok"]
        managed = sup._monitors["owned"]
        assert managed.initialized.wait(2)
        before = copy.deepcopy(cfg.monitors)
        disk = path.read_bytes()
        initial = adm.status_view()["owned"]
        assert not initial.get("runtime_error_code") and managed.thread.is_alive()
        with r07_rejected_validation(adm, reg.get("fake"), rejection):
            row = adm.status_view()["owned"]
            assert not row.get("runtime_error_code")
            assert row.get("persistence") == initial.get("persistence")
            assert cfg.monitors == before and path.read_bytes() == disk
            assert sup._monitors["owned"] is managed and managed.thread.is_alive()
            if rejection == "timeout":
                operation = adm.config_view()["monitor_operations"]["owned"]
                assert (
                    operation["stage"] == "validation_expired"
                    and not operation["retryable"]
                )
        assert not adm.status_view()["owned"].get("runtime_error_code")
    finally:
        sup.stop(2)


@pytest.mark.parametrize("fault", ["create", "start"])
def test_r07_or002_rejected_edit_preserves_pruned_failure_and_exact_add_retry(
    tmp_path, monkeypatch, fault, r07_rejected_validation
):
    from taskpaw_v3.monitors.supervisor import Supervisor

    class Owned(_FakeInstance):
        def start(self, emit):
            if plugin.broken and fault == "start":
                raise RuntimeError("PLANTED_START_SECRET")

    class Plugin(_FakePlugin):
        broken = True
        creates = 0

        def create(self, iid, cfg):
            self.creates += 1
            if self.broken and fault == "create":
                raise RuntimeError("PLANTED_CREATE_SECRET")
            return Owned(iid, cfg)

    reg = PluginRegistry()
    plugin = Plugin()
    reg.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=0.1)
    original_activation = sup.activation_result

    def after_retirement(iid, timeout=0):
        # Reach the real pruned-owner consumer independently of the OR001 race.
        if plugin.broken:
            assert sup.stop_result(iid, 2)["complete"]
        return original_activation(iid, timeout)

    monkeypatch.setattr(sup, "activation_result", after_retirement)
    try:
        sup.start()
        result = adm.add({"type_id": "fake", "config": {"name": "owned"}})
        code = "create_failed" if fault == "create" else "start_failed"
        assert result["outcome"] == "persisted_runtime_failed"
        assert result["error_code"] == code and not sup.has("owned")
        before = path.read_bytes()
        with r07_rejected_validation(adm, plugin):
            row = adm.status_view()["owned"]
            assert row["runtime_error_code"] == code and row["persistence"] == "saved"
            assert row["lifecycle"] == "stopped" and path.read_bytes() == before
        plugin.broken = False
        # Current normalized desired spec is the only allowed duplicate Add retry.
        retried = adm.add(dict(cfg.monitors[0]))
        assert retried["ok"] and plugin.creates == 2
        assert len(load_yaml(AgentConfig, path).monitors) == 1
        assert adm.status_view()["owned"]["runtime_error_code"] is None
        assert sup.config_matches("owned", cfg.monitors[0]["config"])
    finally:
        sup.stop(2)


@pytest.mark.parametrize("live_fault", ["start", "cleanup"])
@pytest.mark.parametrize("save_failed", [False, True])
def test_r07_or002_live_fault_precedes_cached_action_error(
    tmp_path, monkeypatch, live_fault, save_failed
):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.monitors.supervisor import Supervisor

    entered, release = threading.Event(), threading.Event()

    class Owned(_FakeInstance):
        stops = 0

        def start(self, emit):
            if live_fault == "start":
                raise RuntimeError("PLANTED_INIT_SECRET")

        def stop(self, timeout=5):
            self.stops += 1
            if live_fault == "cleanup" and self.stops == 1:
                raise RuntimeError("PLANTED_CLEANUP_SECRET")

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg)

    reg = PluginRegistry()
    reg.register(Plugin())
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=1)
    original_retire = sup._retire
    managed = None

    def before_retire(iid, item, deadline):
        if live_fault == "start" and threading.current_thread() is item.thread:
            entered.set()
            assert release.wait(5)
        return original_retire(iid, item, deadline)

    monkeypatch.setattr(sup, "_retire", before_retire)
    try:
        # A real Add before Supervisor.start leaves a legitimate applied cache.
        assert adm.add({"type_id": "fake", "config": {"name": "owned"}})["ok"]
        managed = sup._monitors["owned"]
        before = path.read_bytes()
        if save_failed:
            with monkeypatch.context() as patch:
                patch.setattr(
                    module,
                    "save_yaml",
                    lambda *a: (_ for _ in ()).throw(OSError("PLANTED_SAVE_SECRET")),
                )
                result = adm.update("owned", {"poll_interval": 20})
            assert (
                result["persistence"] == "failed" and result["runtime"] == "unchanged"
            )
            assert result["error_code"] == "persistence_failed"
        if live_fault == "start":
            sup.start()
            assert entered.wait(2)
        else:
            sup.request_stop("owned", 1)
            assert managed.cleanup_done.wait(2)
            managed.cleanup_thread.join(2)
            assert not managed.cleanup_thread.is_alive()
        expected = "start_failed" if live_fault == "start" else "cleanup_failed"
        assert sup.snapshot()["owned"]["runtime_error_code"] == expected
        row = adm.status_view()["owned"]
        assert row["runtime_error_code"] == expected
        assert row["persistence"] == ("failed" if save_failed else "saved")
        assert path.read_bytes() == before and sup._monitors["owned"] is managed
        assert "PLANTED" not in str(row)
    finally:
        release.set()
        sup.stop(2)
        if managed is not None:
            if managed.thread is not None:
                managed.thread.join(2)
            if managed.cleanup_thread is not None:
                managed.cleanup_thread.join(2)


@pytest.mark.parametrize("persistence", ["saved", "failed", "pending"])
def test_r07_or002_stop_persistence_and_late_retirement_are_preserved(
    tmp_path, monkeypatch, persistence
):
    import threading

    from taskpaw_v3.agent.server import admin as module
    from taskpaw_v3.monitors.supervisor import Supervisor

    cleanup_entered, cleanup_release = threading.Event(), threading.Event()
    save_entered, save_release = threading.Event(), threading.Event()

    class Owned(_FakeInstance):
        def stop(self, timeout=5):
            cleanup_entered.set()
            assert cleanup_release.wait(5)

    class Plugin(_FakePlugin):
        def create(self, iid, cfg):
            return Owned(iid, cfg)

    reg = PluginRegistry()
    reg.register(Plugin())
    cfg = _agent_config()
    path = tmp_path / "owned.yaml"
    sup = Supervisor(lambda *a: None)
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=0.1)
    save = None
    managed = None
    original_save = module.save_yaml

    def persist(*args):
        if persistence == "pending":
            save_entered.set()
            assert save_release.wait(5)
        if persistence == "failed":
            raise OSError("PLANTED_SAVE_SECRET")
        original_save(*args)

    try:
        assert adm.add({"type_id": "fake", "config": {"name": "owned"}})["ok"]
        managed = sup._monitors["owned"]
        before = path.read_bytes()
        monkeypatch.setattr(module, "save_yaml", persist)
        result = adm.set_enabled("owned", False)
        save = adm._stop_records["owned"]
        assert cleanup_entered.wait(2)
        assert (
            result["runtime"] == "stopping" and result["error_code"] == "stop_timeout"
        )
        assert result["persistence"] == persistence
        if persistence == "pending":
            assert save_entered.is_set() and not save.done.is_set()
        row = adm.status_view()["owned"]
        assert (
            row["runtime_error_code"]
            == ("persistence_failed" if persistence == "failed" else "stop_timeout")
            and row["persistence"] == persistence
        )
        if persistence != "saved":
            assert path.read_bytes() == before
        cleanup_release.set()
        assert sup.stop_result("owned", 2)["complete"]
        managed.cleanup_thread.join(2)
        assert not managed.cleanup_thread.is_alive() and not sup.has("owned")
        row = adm.status_view()["owned"]
        assert row["lifecycle"] == "stopped" and row["persistence"] == persistence
        assert row["runtime_error_code"] == (
            "persistence_failed" if persistence == "failed" else None
        )
        save_release.set()
        assert save.done.wait(2)
        save.thread.join(2)
        assert not save.thread.is_alive()
        row = adm.status_view()["owned"]
        assert row["persistence"] == ("failed" if persistence == "failed" else "saved")
        assert load_yaml(AgentConfig, path).monitors[0]["enabled"] is (
            persistence == "failed"
        )
    finally:
        cleanup_release.set()
        save_release.set()
        if save is not None and save.thread is not None:
            save.thread.join(2)
        sup.stop(2)
        if managed is not None and managed.cleanup_thread is not None:
            managed.cleanup_thread.join(2)


def test_i216_r1_folder_update_timeout_never_resumes_retired_owner(tmp_path):
    import threading

    from taskpaw_v3.monitors.plugins.folder import FolderInstance, FolderPlugin
    from taskpaw_v3.monitors.supervisor import Supervisor

    watched = tmp_path / "watched"
    watched.mkdir()
    edge_entered, edge_release = threading.Event(), threading.Event()
    prepared_cleaned = threading.Event()
    created, events = [], []

    class TrackedFolder(FolderInstance):
        starts = 0
        checks = 0
        stops = 0

        def start(self, emit):
            self.starts += 1
            super().start(emit)
            if self is created[0]:
                (watched / "new.txt").write_text("completed", encoding="utf-8")

        def check(self, emit):
            self.checks += 1
            if self is created[0]:
                super().check(emit)  # Discover the file after the real start baseline.

                def completing(*a, **k):
                    edge_entered.set()  # Real Folder has marked its record completed.
                    assert edge_release.wait(3)
                    emit(*a, **k)

                return super().check(completing)
            return super().check(emit)

        def stop(self, timeout=5):
            self.stops += 1
            super().stop(timeout)
            if self is not created[0]:
                prepared_cleaned.set()

    class TrackedPlugin(FolderPlugin):
        def create(self, iid, cfg):
            inst = TrackedFolder(iid, cfg)
            created.append(inst)
            return inst

    plugin = TrackedPlugin()
    reg = PluginRegistry()
    reg.register(plugin)
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    sup = Supervisor(lambda *a: events.append(a))
    adm = MonitorAdmin(cfg, sup, reg, path, operation_timeout=0.2)
    try:
        assert adm.add(
            {
                "type_id": "folder",
                "config": {
                    "name": "owned",
                    "path": str(watched),
                    "stable_seconds": 0,
                    "poll_interval": 1,
                },
            }
        )["ok"]
        sup.start()
        assert edge_entered.wait(1)
        old = sup._monitors["owned"]
        stale_emit = sup._emitter_for("owned", old)
        assert old.instance._files["new.txt"][2] is True
        result = adm.update("owned", {"poll_interval": 2})
        assert (
            result["persistence"],
            result["runtime"],
            result["outcome"],
            result["error_code"],
        ) == ("saved", "stopping", "stop_incomplete", "stop_timeout")
        assert load_yaml(AgentConfig, path).monitors[0]["config"]["poll_interval"] == 2
        assert sup._monitors["owned"] is old and old.stop.is_set()
        assert old.thread.is_alive()
        assert adm.status_view()["owned"]["lifecycle"] == "stopping"
        assert prepared_cleaned.wait(1)
        assert len(created) == 2 and created[1].starts == 0 and created[1].stops == 1
        assert (
            adm.update("owned", {"poll_interval": 3})["error_code"] == "operation_busy"
        )
        assert len(created) == 2
        edge_release.set()
        old.thread.join(1)
        assert not old.thread.is_alive() and old.stop.is_set()
        assert old.instance.checks == 1 and events == []
        assert sup.stop_result("owned", 1)["complete"]
        assert adm.set_enabled("owned", True)["ok"]
        current = sup._monitors["owned"]
        assert current is not old and len(created) == 3
        stale_emit("done", "old", "old")
        assert events == []
        sup._emitter_for("owned", current)("done", "current", "current")
        assert len(events) == 1 and events[0][2] == "current"
    finally:
        edge_release.set()
        sup.stop(1)
