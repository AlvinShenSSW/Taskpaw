"""V2 → V3 config/state migrator (design §8).

Pure functions, no I/O of its own beyond reading the two JSON paths handed in.
The output is a `MigrationPlan` the caller can preview before writing anything.

Type mapping (V2 `watcher_type` → V3 `type_id`):
    lada       → process    (process_name → pattern; GPU handled by host_metrics)
    comfyui    → comfyui    (host/port/idle_confirm_count)
    folder     → folder     (watch_folder/stable_seconds/file_extensions)
    custom_cmd → custom_cmd (custom_command)

Unknown / unmappable watchers produce a `MigrationWarning` and are skipped rather
than guessed at. Disabled watchers are carried over but flagged `enabled=False`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class MigratedMonitor:
    """One V3 monitor in the `{type_id, name, config}` shape the agent consumes."""

    type_id: str
    name: str
    config: dict[str, Any]
    enabled: bool = True
    source_id: str = ""           # original V2 watcher id, for traceability
    source_type: str = ""         # original V2 watcher_type


@dataclass
class MigrationWarning:
    source_id: str
    source_type: str
    name: str
    reason: str


@dataclass
class MigrationPlan:
    monitors: list[MigratedMonitor] = field(default_factory=list)
    warnings: list[MigrationWarning] = field(default_factory=list)
    cursor: int = 1               # V3 event-id cursor (from V2 next_event_id)
    machine_name: str = ""

    def is_clean(self) -> bool:
        return not self.warnings


def _split_extensions(raw: str) -> list[str]:
    """V2 stores extensions as a comma-separated string; V3 wants a list."""
    if not raw:
        return []
    return [e.strip().lstrip(".") for e in raw.split(",") if e.strip()]


def _carry_common(w: dict, cfg: dict) -> dict:
    """Carry the shared knobs V2 and V3 both have, when present and sane."""
    poll = w.get("poll_interval")
    if isinstance(poll, (int, float)) and poll >= 1:
        cfg["poll_interval"] = float(poll)
    return cfg


# A mapper returns (specs, warnings):
#   specs    — list of (type_id, name, config) — usually one, but Lada may emit
#              both a process observer AND a folder observer for its output.
#   warnings — list of human-readable strings (non-fatal notes).
_Spec = tuple[str, str, dict]


def _map_lada(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    specs: list[_Spec] = []
    warnings: list[str] = []
    proc = (w.get("process_name") or "").strip()
    if not proc:
        return [], [f"lada watcher {name!r} has no process_name to map to a process pattern"]
    # V2 matched the process by literal name; escape it into a safe regex.
    pcfg: dict[str, Any] = {"name": name, "pattern": re.escape(proc), "category_label": "task"}
    specs.append(("process", name, _carry_common(w, pcfg)))

    # V3 observes; it does not launch/manage processes (constitution). A V2 Lada
    # watcher in managed mode (lada_cli_path / extra args / progress capture)
    # can't be reproduced — carry the OUTPUT folder as a completion observer and
    # warn that managed launching is dropped (Codex #20 P1).
    out = (w.get("lada_output_folder") or "").strip()
    if out:
        fcfg: dict[str, Any] = {"name": f"{name} output", "path": out}
        specs.append(("folder", f"{name} output", _carry_common(w, fcfg)))
    if (w.get("lada_cli_path") or "").strip():
        warnings.append(
            f"lada watcher {name!r} ran in managed mode (lada_cli_path set); V3 "
            f"observes only — it is migrated as a process"
            + (" + output-folder monitor" if out else "")
            + ", and will NOT launch lada-cli")
    return specs, warnings


def _map_process(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    """V2 generic process watcher → V3 process monitor (service, not a task)."""
    proc = (w.get("process_name") or "").strip()
    if not proc:
        return [], [f"process watcher {name!r} has no process_name set"]
    cfg: dict[str, Any] = {"name": name, "pattern": re.escape(proc), "category_label": "service"}
    return [("process", name, _carry_common(w, cfg))], []


def _map_comfyui(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    cfg: dict[str, Any] = {
        "name": name,
        "host": w.get("comfyui_host") or "127.0.0.1",
        "port": int(w.get("comfyui_port") or 8188),
    }
    idle = w.get("idle_confirm_count")
    if isinstance(idle, int) and idle >= 1:
        cfg["idle_confirm"] = idle
    return [("comfyui", name, _carry_common(w, cfg))], []


def _map_folder(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    path = (w.get("watch_folder") or "").strip()
    if not path:
        return [], [f"folder watcher {name!r} has no watch_folder set"]
    cfg: dict[str, Any] = {"name": name, "path": path}
    exts = _split_extensions(w.get("file_extensions") or "")
    if exts:
        cfg["extensions"] = exts
    stable = w.get("stable_seconds")
    if isinstance(stable, (int, float)) and stable >= 0:
        cfg["stable_seconds"] = float(stable)
    return [("folder", name, _carry_common(w, cfg))], []


def _map_custom_cmd(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    cmd = (w.get("custom_command") or "").strip()
    if not cmd:
        return [], [f"custom_cmd watcher {name!r} has no custom_command set"]
    return [("custom_cmd", name, _carry_common(w, {"name": name, "command": cmd}))], []


_MAPPERS = {
    "lada": _map_lada,
    "process": _map_process,
    "comfyui": _map_comfyui,
    "folder": _map_folder,
    "custom_cmd": _map_custom_cmd,
}


def migrate_config(config: dict) -> MigrationPlan:
    """Build a `MigrationPlan` from a parsed V2 `config.json` dict."""
    plan = MigrationPlan(machine_name=config.get("machine_name", "") or "")
    for w in config.get("watchers", []) or []:
        wtype = (w.get("watcher_type") or "").strip()
        name = (w.get("name") or "").strip() or wtype or "unnamed"
        sid = w.get("id", "") or ""
        mapper = _MAPPERS.get(wtype)
        if mapper is None:
            plan.warnings.append(MigrationWarning(sid, wtype, name,
                f"no V3 plugin maps watcher_type {wtype!r}"))
            continue
        specs, notes = mapper(w, name)
        for reason in notes:
            plan.warnings.append(MigrationWarning(sid, wtype, name, reason))
        for type_id, mname, cfg in specs:
            plan.monitors.append(MigratedMonitor(
                type_id=type_id, name=mname, config=cfg,
                enabled=bool(w.get("enabled", True)),
                source_id=sid, source_type=wtype,
            ))
    return plan


def migrate_state(state: dict) -> int:
    """V2 `state.json` next_event_id → V3 cursor (never below 1)."""
    try:
        return max(1, int(state.get("next_event_id", 1)))
    except (TypeError, ValueError):
        return 1


def plan_migration(config_path: str | Path,
                   state_path: Optional[str | Path] = None) -> MigrationPlan:
    """Read V2 config.json (+ optional state.json) and return a read-only plan.

    Raises FileNotFoundError if config_path is missing; a missing/invalid
    state.json is non-fatal (cursor defaults to 1).
    """
    cpath = Path(config_path).expanduser()
    with open(cpath, "r", encoding="utf-8") as f:
        config = json.load(f)
    plan = migrate_config(config)

    if state_path is not None:
        spath = Path(state_path).expanduser()
        if spath.exists():
            try:
                with open(spath, "r", encoding="utf-8") as f:
                    plan.cursor = migrate_state(json.load(f))
            except (OSError, json.JSONDecodeError):
                plan.cursor = 1
    return plan
