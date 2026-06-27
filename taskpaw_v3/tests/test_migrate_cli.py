"""V2→V3 migration preview CLI (python -m taskpaw_v3.migrate)."""

from __future__ import annotations

import json

import yaml

from taskpaw_v3.migrate.__main__ import main


def _v2(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "machine_name": "BlackGoldPig",
        "watchers": [
            {"id": "a1", "name": "Lada", "watcher_type": "lada",
             "process_name": "lada-cli", "lada_output_folder": "/out"},
            {"id": "a2", "name": "Comfy", "watcher_type": "comfyui"},
            {"id": "a3", "name": "Off", "watcher_type": "custom_cmd",
             "custom_command": "x", "enabled": False},
            {"id": "a4", "name": "Weird", "watcher_type": "macsubs"},
        ],
    }))
    (tmp_path / "state.json").write_text(json.dumps({"next_event_id": 99}))
    return cfg


def test_cli_text_preview(tmp_path, capsys):
    rc = main([str(_v2(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BlackGoldPig" in out
    assert "cursor : 99" in out
    assert "folder" in out and "comfyui" in out      # mapped monitors
    assert "DISABLED" in out                          # disabled custom_cmd flagged
    assert "macsubs" in out                           # unknown type warned


def test_cli_yaml_block_is_pasteable(tmp_path, capsys):
    rc = main([str(_v2(tmp_path)), "--yaml"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = yaml.safe_load(out)
    assert "monitors" in parsed
    names = [m["name"] for m in parsed["monitors"]]
    assert "Lada" in names and "Comfy" in names       # enabled, mapped
    assert "Off" not in names                          # disabled excluded
    for m in parsed["monitors"]:
        assert set(m) == {"type_id", "name", "config"}


def test_cli_missing_config_errors(tmp_path, capsys):
    rc = main([str(tmp_path / "nope.json")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err
