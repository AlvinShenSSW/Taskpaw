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
def test_lada_maps_to_lada_plugin():
    """V3 now has a full `lada` plugin (#59) — carry the lada_* fields over,
    no warning, no folder compromise."""
    plan = migrate_config(
        {
            "watchers": [
                _w(
                    watcher_type="lada",
                    name="Lada",
                    process_name="lada-cli.exe",
                    lada_output_folder="/out",
                )
            ]
        }
    )
    assert not plan.warnings
    m = plan.monitors[0]
    assert m.type_id == "lada"
    assert m.config["lada_output_folder"] == "/out"
    assert m.config["process_name"] == "lada-cli.exe"
    assert m.source_type == "lada"


def _managed_lada(**extra):
    base = dict(
        watcher_type="lada",
        name="Lada",
        process_name="lada-cli",
        lada_cli_path="C:/lada/lada-cli.exe",
        lada_input_folder="D:/in",
        lada_output_folder="D:/out",
        lada_extra_args="--device cuda:1",
        lada_gpu_monitor=True,
    )
    base.update(extra)
    return _w(**base)


def test_managed_lada_carries_cli_but_imported_disabled():
    """Managed Lada carries the CLI path + folders + args, but (with V2 auto_start
    off, the default) is imported DISABLED so it doesn't auto-launch lada-cli on
    the first V3 boot (Codex #59 P1)."""
    plan = migrate_config({"watchers": [_managed_lada()]})  # no auto_start → off
    m = plan.monitors[0]
    assert m.type_id == "lada"
    assert m.config["lada_cli_path"] == "C:/lada/lada-cli.exe"
    assert m.config["lada_input_folder"] == "D:/in"
    assert m.config["lada_extra_args"] == "--device cuda:1"
    assert m.config["lada_gpu_monitor"] is True
    assert m.enabled is False  # safety-disabled
    rt = plan.to_runtime_monitors()
    assert len(rt) == 1 and rt[0]["enabled"] is False  # in config, not started
    assert rt[0]["config"]["lada_cli_path"] == "C:/lada/lada-cli.exe"
    assert any("imported DISABLED" in w.reason for w in plan.warnings)


def test_managed_lada_always_imported_disabled_even_with_auto_start():
    """V3 managed Lada is operator-started each session (it launches lada-cli), so
    it's imported DISABLED regardless of V2 auto_start — never auto-starts at boot
    (#70). This also means migration can't carry an enabled folderless managed Lada
    that would fail validation."""
    plan = migrate_config({"auto_start": True, "watchers": [_managed_lada()]})
    assert plan.monitors[0].enabled is False
    rt = plan.to_runtime_monitors()
    assert [m["name"] for m in rt] == ["Lada"] and rt[0]["enabled"] is False
    assert any("imported DISABLED" in w.reason for w in plan.warnings)


def test_lada_passive_no_output_folder_still_maps():
    """No output folder is fine now — a passive lada monitor still maps (process
    detection works without folders)."""
    plan = migrate_config(
        {"watchers": [_w(watcher_type="lada", name="Lada", process_name="lada-cli")]}
    )
    assert not plan.warnings
    assert [m.type_id for m in plan.monitors] == ["lada"]
    assert plan.monitors[0].config["process_name"] == "lada-cli"


def test_comfyui_carries_log_path():
    """V3's comfyui plugin tails comfyui_log_path for error diagnostics (#60)."""
    plan = migrate_config(
        {
            "watchers": [
                _w(
                    watcher_type="comfyui",
                    name="C",
                    comfyui_host="10.0.0.5",
                    comfyui_port=9000,
                    comfyui_log_path="/var/log/comfy.log",
                )
            ]
        }
    )
    m = plan.monitors[0]
    assert m.type_id == "comfyui"
    assert m.config["host"] == "10.0.0.5" and m.config["port"] == 9000
    assert m.config["comfyui_log_path"] == "/var/log/comfy.log"


def test_generic_process_watcher_maps_to_process():
    """V2 watcher_type 'process' must migrate, not fall into unknown-type (Codex #20 r2)."""
    plan = migrate_config(
        {"watchers": [_w(watcher_type="process", name="PM2", process_name="pm2 God")]}
    )
    m = plan.monitors[0]
    # anchored exact-name match, no cmdline search — preserves V2 semantics (Codex r8)
    assert m.type_id == "process" and m.config["pattern"] == r"^pm2\ God$"
    assert m.config["search_cmdline"] is False
    assert m.config["category_label"] == "service"
    # mapped, but the operator is warned the severity semantics differ (Codex r5)
    assert any("neutral start/exit" in w.reason for w in plan.warnings)


def test_comfyui_maps_host_port_idle():
    plan = migrate_config(
        {
            "watchers": [
                _w(
                    watcher_type="comfyui",
                    name="Comfy",
                    comfyui_host="10.0.0.5",
                    comfyui_port=9000,
                    idle_confirm_count=4,
                )
            ]
        }
    )
    m = plan.monitors[0]
    assert m.type_id == "comfyui"
    assert m.config["host"] == "10.0.0.5" and m.config["port"] == 9000
    assert m.config["idle_confirm"] == 4


def test_folder_maps_extensions_and_stable():
    plan = migrate_config(
        {
            "watchers": [
                _w(
                    watcher_type="folder",
                    name="DL",
                    watch_folder="/data/out",
                    file_extensions="mp4, .mkv ,avi",
                    stable_seconds=45,
                )
            ]
        }
    )
    m = plan.monitors[0]
    assert m.type_id == "folder"
    assert m.config["path"] == "/data/out"
    assert m.config["extensions"] == ["mp4", "mkv", "avi"]
    assert m.config["stable_seconds"] == 45.0
    # V2 FolderWatcher polled every 1s and ignored the stored poll_interval;
    # don't carry a value that would delay completions (Codex r9).
    assert m.config["poll_interval"] == 1.0


def test_custom_cmd_maps_command():
    plan = migrate_config(
        {
            "watchers": [
                _w(watcher_type="custom_cmd", name="C", custom_command="check.sh --all")
            ]
        }
    )
    m = plan.monitors[0]
    assert m.type_id == "custom_cmd" and m.config["command"] == "check.sh --all"


# ── warnings / skips ─────────────────────────────────────────────────────--
def test_unknown_type_warns_and_skips():
    plan = migrate_config({"watchers": [_w(watcher_type="macsubs", name="subs")]})
    assert not plan.monitors
    assert plan.warnings and "macsubs" in plan.warnings[0].reason


def test_incomplete_watcher_warns():
    plan = migrate_config(
        {"watchers": [_w(watcher_type="folder", name="x", watch_folder="")]}
    )
    assert not plan.monitors and plan.warnings


def test_disabled_flag_carried_into_config_as_enabled_false():
    plan = migrate_config(
        {
            "watchers": [
                _w(
                    watcher_type="custom_cmd",
                    name="off",
                    custom_command="x",
                    enabled=False,
                ),
                _w(
                    watcher_type="custom_cmd",
                    name="on",
                    custom_command="y",
                    enabled=True,
                ),
            ]
        }
    )
    assert plan.monitors[0].enabled is False
    # #59: disabled monitors are CARRIED into the config as enabled:false (not
    # dropped) — the supervisor skips them, the console shows them stopped.
    runtime = {m["name"]: m for m in plan.to_runtime_monitors()}
    assert set(runtime) == {"off", "on"}
    assert runtime["off"]["enabled"] is False
    assert runtime["on"]["enabled"] is True
    assert all(
        set(m) == {"type_id", "name", "config", "enabled"} for m in runtime.values()
    )
    assert any("disabled in V2" in w.reason for w in plan.warnings)


def test_poll_interval_carried_when_sane():
    plan = migrate_config(
        {
            "watchers": [
                _w(
                    watcher_type="custom_cmd",
                    name="c",
                    custom_command="x",
                    poll_interval=20,
                )
            ]
        }
    )
    assert plan.monitors[0].config["poll_interval"] == 20.0


def test_machine_name_preserved():
    plan = migrate_config({"machine_name": "BlackGoldPig", "watchers": []})
    assert plan.machine_name == "BlackGoldPig"


# ── migrated configs validate against the real plugins ───────────────────--
def test_migrated_configs_validate_against_plugins():
    reg = default_registry()
    cfg = {
        "watchers": [
            _w(
                watcher_type="lada",
                name="Lada",
                process_name="lada-cli",
                lada_output_folder="/out",
            ),
            _w(watcher_type="process", name="PM2", process_name="pm2"),
            _w(watcher_type="comfyui", name="Comfy"),
            _w(
                watcher_type="folder",
                name="DL",
                watch_folder="/tmp",
                file_extensions="mp4",
            ),
            _w(watcher_type="custom_cmd", name="C", custom_command="echo hi"),
        ]
    }
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
    cfg.write_text(
        json.dumps(
            {
                "machine_name": "M",
                "watchers": [
                    _w(watcher_type="folder", name="DL", watch_folder="/data")
                ],
            }
        )
    )
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
