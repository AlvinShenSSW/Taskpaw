"""V2 → V3 config/state migrator (design §8).

Pure functions, no I/O of its own beyond reading the two JSON paths handed in.
The output is a `MigrationPlan` the caller can preview before writing anything.

Type mapping (V2 `watcher_type` → V3 `type_id`):
    lada       → folder     (Lada is a task: process-exit=done; the faithful V3
                             signal is its output folder. No output folder →
                             skip+warn. GPU is covered by host_metrics.)
    process    → process    (generic; process_name → pattern, service-semantics)
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

    def to_runtime_monitors(self) -> list[dict]:
        """Only the ENABLED monitors, in the `{type_id, name, config}` shape the
        agent's build_supervisor() consumes. Disabled V2 watchers stay in
        `monitors` for preview but are excluded here so they never start — the
        supervisor builder does not honor a top-level enabled flag (Codex #20 r4).
        """
        return [
            {"type_id": m.type_id, "name": m.name, "config": m.config}
            for m in self.monitors if m.enabled
        ]


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


# V2's FolderWatcher IGNORES the stored poll_interval and polls every 1 second.
# Carrying the (often-default-10 or stale) V2 value would report completions much
# later than V2 did, especially with small stable_seconds (Codex #20 r9). Pin the
# V2-equivalent cadence instead of carrying the meaningless field.
_V2_FOLDER_POLL = 1.0


# A mapper returns (specs, warnings):
#   specs    — list of (type_id, name, config) — usually one, but Lada may emit
#              both a process observer AND a folder observer for its output.
#   warnings — list of human-readable strings (non-fatal notes).
_Spec = tuple[str, str, dict]


def _map_lada(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    # V2 Lada is a TASK: process running = busy, process EXIT = completion (a
    # success notification), NOT a failure. V3's `process` plugin is service-
    # semantics (down → alert), so mapping Lada→process would turn every normal
    # finish into a false "down" alert (Codex #20 P2 r3). The faithful V3 signal
    # is the OUTPUT folder — a finished file appearing = done. Map to a folder
    # monitor; if there's no output folder we can't represent it, so skip+warn.
    warnings: list[str] = []
    if (w.get("lada_cli_path") or "").strip():
        warnings.append(
            f"lada watcher {name!r} ran in managed mode (lada_cli_path set); V3 "
            f"observes only and will NOT launch lada-cli")
    out = (w.get("lada_output_folder") or "").strip()
    if not out:
        warnings.append(
            f"lada watcher {name!r} has no lada_output_folder; its process-exit "
            f"completion can't be represented by a V3 plugin (the process plugin "
            f"would alert on normal completion) — skipped, configure manually")
        return [], warnings
    fcfg: dict[str, Any] = {"name": name, "path": out, "poll_interval": _V2_FOLDER_POLL}
    return [("folder", name, fcfg)], warnings


def _map_process(w: dict, name: str) -> tuple[list[_Spec], list[str]]:
    """V2 generic process watcher → V3 process monitor.

    Semantics differ and the operator must know (Codex #20 r5): V2's generic
    watcher sent NEUTRAL start/exit notifications and never alerted on absence,
    whereas V3's process plugin treats the process being absent (at startup or on
    exit) as an ALERT and recovery as `done`. We still map it — process is the
    right plugin and skipping would lose the monitor — but warn about the change.
    """
    proc = (w.get("process_name") or "").strip()
    if not proc:
        return [], [f"process watcher {name!r} has no process_name set"]
    # V2 matched on exact process NAME equality (case-insensitive), not substring
    # and not the command line. Anchor the regex and disable cmdline search so an
    # unrelated process merely containing this name can't mask a real outage
    # (the plugin compiles with re.IGNORECASE) (Codex #20 r8).
    cfg: dict[str, Any] = {
        "name": name,
        "pattern": f"^{re.escape(proc)}$",
        "search_cmdline": False,
        "category_label": "service",
    }
    warn = (f"process watcher {name!r}: V2 sent neutral start/exit notifications; "
            f"V3 treats the process being absent as an alert (and recovery as "
            f"done). Review severity, or disable if it was a short-lived task.")
    return [("process", name, _carry_common(w, cfg))], [warn]


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
    # V2-equivalent 1s cadence (see _V2_FOLDER_POLL) — do NOT carry the ignored
    # V2 poll_interval.
    cfg: dict[str, Any] = {"name": name, "path": path, "poll_interval": _V2_FOLDER_POLL}
    exts = _split_extensions(w.get("file_extensions") or "")
    if exts:
        cfg["extensions"] = exts
    stable = w.get("stable_seconds")
    if isinstance(stable, (int, float)) and stable >= 0:
        cfg["stable_seconds"] = float(stable)
    return [("folder", name, cfg)], []


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
        enabled = bool(w.get("enabled", True))
        if specs and not enabled:
            plan.warnings.append(MigrationWarning(sid, wtype, name,
                "watcher was disabled in V2 — migrated for reference but excluded "
                "from the runnable set (to_runtime_monitors)"))
        for type_id, mname, cfg in specs:
            plan.monitors.append(MigratedMonitor(
                type_id=type_id, name=mname, config=cfg,
                enabled=enabled, source_id=sid, source_type=wtype,
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
