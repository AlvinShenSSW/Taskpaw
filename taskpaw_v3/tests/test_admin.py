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
    client = TestClient(create_control_app(cfg, admin=admin, registry=reg))
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
    client = TestClient(create_control_app(cfg, admin=admin, registry=reg))
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
    client = TestClient(create_control_app(cfg, admin=admin, registry=reg))
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
    client = TestClient(create_control_app(cfg, admin=admin, registry=reg))

    r = client.patch(
        "/control/monitors",
        params={"name": "w1"},
        json={"config": {"poll_interval": 0}, "enabled": False},
    )
    assert r.status_code == 400  # poll_interval < 1 → invalid
    assert cfg.monitors[0].get("enabled", True) is True  # enabled untouched


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
    assert res == {
        "ok": True,
        "model": "served/m",
        "latency_ms": 42,
        "truncated": False,
    }
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
    assert res["truncated"] is False and isinstance(res["latency_ms"], int)
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
        "llm_fallback1_api_base",
        "llm_fallback1_model",
        "llm_fallback1_api_key",
        "llm_fallback2_api_base",
        "llm_fallback2_model",
        "llm_fallback2_api_key",
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
    client = TestClient(create_control_app(cfg, admin=admin))
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
    assert res == {
        "ok": True,
        "model": "served/m",
        "latency_ms": 42,
        "truncated": False,
    }
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
