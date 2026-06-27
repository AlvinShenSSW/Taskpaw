"""V2 → V3 migrator (#20, design §8). Read-only plan + type mapping."""

from __future__ import annotations

import json

import pytest

from taskpaw_v3.migrate import migrate_config, migrate_state, plan_migration
from taskpaw_v3.monitors.registry import default_registry


def _w(**kw):
    base = {"id": "abc123", "name": "w", "watcher_type": "lada", "enabled": True}
    base.update(kw)
    return base


# ── per-type mapping ─────────────────────────────────────────────────────--
def test_lada_maps_to_output_folder():
    """Lada is a task (process-exit = done); the faithful V3 signal is the
    output folder, not a service-semantics process monitor (Codex #20 r3)."""
    plan = migrate_config({"watchers": [_w(watcher_type="lada", name="Lada",
                                           process_name="lada-cli.exe",
                                           lada_output_folder="/out")]})
    assert not plan.warnings
    m = plan.monitors[0]
    assert m.type_id == "folder"
    assert m.config["path"] == "/out"
    assert m.source_type == "lada"


def test_lada_managed_mode_warns_but_still_maps_output_folder():
    """Managed Lada (lada_cli_path set) → folder monitor on the output folder,
    plus a warning that V3 won't launch lada (Codex #20 P1)."""
    plan = migrate_config({"watchers": [_w(
        watcher_type="lada", name="Lada", process_name="lada-cli",
        lada_cli_path="C:/lada/lada-cli.exe",
        lada_output_folder="D:/out",
        lada_extra_args="--device cuda:1")]})
    assert [m.type_id for m in plan.monitors] == ["folder"]
    assert plan.monitors[0].config["path"] == "D:/out"
    assert any("managed mode" in w.reason for w in plan.warnings)


def test_lada_without_output_folder_skipped_with_warning():
    """No output folder → no faithful V3 signal (process plugin would false-
    alert on completion); skip + warn rather than emit a wrong monitor."""
    plan = migrate_config({"watchers": [_w(
        watcher_type="lada", name="Lada", process_name="lada-cli")]})
    assert not plan.monitors
    assert any("can't be represented" in w.reason for w in plan.warnings)


def test_generic_process_watcher_maps_to_process():
    """V2 watcher_type 'process' must migrate, not fall into unknown-type (Codex #20 r2)."""
    plan = migrate_config({"watchers": [_w(watcher_type="process", name="PM2",
                                           process_name="pm2 God")]})
    m = plan.monitors[0]
    # anchored exact-name match, no cmdline search — preserves V2 semantics (Codex r8)
    assert m.type_id == "process" and m.config["pattern"] == r"^pm2\ God$"
    assert m.config["search_cmdline"] is False
    assert m.config["category_label"] == "service"
    # mapped, but the operator is warned the severity semantics differ (Codex r5)
    assert any("neutral start/exit" in w.reason for w in plan.warnings)


def test_comfyui_maps_host_port_idle():
    plan = migrate_config({"watchers": [_w(watcher_type="comfyui", name="Comfy",
                                           comfyui_host="10.0.0.5", comfyui_port=9000,
                                           idle_confirm_count=4)]})
    m = plan.monitors[0]
    assert m.type_id == "comfyui"
    assert m.config["host"] == "10.0.0.5" and m.config["port"] == 9000
    assert m.config["idle_confirm"] == 4


def test_folder_maps_extensions_and_stable():
    plan = migrate_config({"watchers": [_w(watcher_type="folder", name="DL",
                                           watch_folder="/data/out",
                                           file_extensions="mp4, .mkv ,avi",
                                           stable_seconds=45)]})
    m = plan.monitors[0]
    assert m.type_id == "folder"
    assert m.config["path"] == "/data/out"
    assert m.config["extensions"] == ["mp4", "mkv", "avi"]
    assert m.config["stable_seconds"] == 45.0


def test_custom_cmd_maps_command():
    plan = migrate_config({"watchers": [_w(watcher_type="custom_cmd", name="C",
                                           custom_command="check.sh --all")]})
    m = plan.monitors[0]
    assert m.type_id == "custom_cmd" and m.config["command"] == "check.sh --all"


# ── warnings / skips ─────────────────────────────────────────────────────--
def test_unknown_type_warns_and_skips():
    plan = migrate_config({"watchers": [_w(watcher_type="macsubs", name="subs")]})
    assert not plan.monitors
    assert plan.warnings and "macsubs" in plan.warnings[0].reason


def test_incomplete_watcher_warns():
    plan = migrate_config({"watchers": [_w(watcher_type="folder", name="x", watch_folder="")]})
    assert not plan.monitors and plan.warnings


def test_disabled_flag_carried_but_excluded_from_runtime():
    plan = migrate_config({"watchers": [
        _w(watcher_type="custom_cmd", name="off", custom_command="x", enabled=False),
        _w(watcher_type="custom_cmd", name="on", custom_command="y", enabled=True),
    ]})
    assert plan.monitors[0].enabled is False
    # disabled watcher is in monitors (preview) but NOT in the runnable set
    runtime = plan.to_runtime_monitors()
    names = [m["name"] for m in runtime]
    assert names == ["on"]
    assert all(set(m) == {"type_id", "name", "config"} for m in runtime)
    assert any("disabled in V2" in w.reason for w in plan.warnings)


def test_poll_interval_carried_when_sane():
    plan = migrate_config({"watchers": [_w(watcher_type="custom_cmd", name="c",
                                           custom_command="x", poll_interval=20)]})
    assert plan.monitors[0].config["poll_interval"] == 20.0


def test_machine_name_preserved():
    plan = migrate_config({"machine_name": "BlackGoldPig", "watchers": []})
    assert plan.machine_name == "BlackGoldPig"


# ── migrated configs validate against the real plugins ───────────────────--
def test_migrated_configs_validate_against_plugins():
    reg = default_registry()
    cfg = {"watchers": [
        _w(watcher_type="lada", name="Lada", process_name="lada-cli",
           lada_output_folder="/out"),
        _w(watcher_type="process", name="PM2", process_name="pm2"),
        _w(watcher_type="comfyui", name="Comfy"),
        _w(watcher_type="folder", name="DL", watch_folder="/tmp", file_extensions="mp4"),
        _w(watcher_type="custom_cmd", name="C", custom_command="echo hi"),
    ]}
    plan = migrate_config(cfg)
    assert len(plan.monitors) == 5
    for m in plan.monitors:
        plugin = reg.get(m.type_id)
        validated = plugin.validate_config(m.config)  # raises on bad mapping
        assert validated.name == m.name


# ── state cursor ─────────────────────────────────────────────────────────--
def test_migrate_state_reads_next_event_id():
    assert migrate_state({"next_event_id": 42}) == 42
    assert migrate_state({}) == 1
    assert migrate_state({"next_event_id": 0}) == 1
    assert migrate_state({"next_event_id": "bad"}) == 1


# ── end-to-end from files ────────────────────────────────────────────────--
def test_plan_migration_from_files(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"machine_name": "M", "watchers": [
        _w(watcher_type="folder", name="DL", watch_folder="/data")]}))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"next_event_id": 7}))
    plan = plan_migration(cfg, state)
    assert plan.cursor == 7 and plan.machine_name == "M"
    assert plan.monitors[0].type_id == "folder"


def test_plan_migration_missing_state_is_non_fatal(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"watchers": []}))
    plan = plan_migration(cfg, tmp_path / "nope.json")
    assert plan.cursor == 1


def test_plan_migration_missing_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        plan_migration(tmp_path / "nope.json")
