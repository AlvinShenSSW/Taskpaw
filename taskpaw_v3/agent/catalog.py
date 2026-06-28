"""Selectable monitoring services + agent config editing (UI-driven selection).

An agent is uniform across machines; what differs is *which monitor services the
operator enables*. This module exposes:

- `plugin_catalog()` — every monitor type the agent can run, with its form schema,
  so the UI can offer them as choices (the "选择开启监听" picker).
- `preset_catalog()` — named bundles of monitors (e.g. the moomoo MQT life-signs),
  i.e. moomoo is just ONE selectable preset, not a machine type.
- pure `add_monitor` / `remove_monitor` / `has_monitor` helpers that edit an
  agent's `monitors` list (validated against the plugin) — the persistence +
  live-apply wiring builds on these.
"""

from __future__ import annotations

from typing import Any, Optional

from taskpaw_v3.monitors.registry import PluginRegistry, default_registry


def plugin_catalog(registry: Optional[PluginRegistry] = None) -> list[dict[str, Any]]:
    """Every selectable monitor type with its UI form schema."""
    reg = registry or default_registry()
    out: list[dict[str, Any]] = []
    for type_id in reg.types():
        p = reg.get(type_id)
        out.append({
            "type_id": p.type_id,
            "display_name": p.display_name or p.type_id,
            "category": p.category,
            "json_schema": p.json_schema(),
            "ui_schema": p.ui_schema(),
        })
    return out


def preset_catalog() -> list[dict[str, Any]]:
    """Named monitor bundles the operator can enable in one click."""
    from taskpaw_v3.monitors.presets.moomoo import moomoo_preset

    return [
        {
            "id": "moomoo",
            "display_name": "moomoo (MQT life-signs)",
            "description": "pm2 daemon, orchestrator, OpenD :11111, heartbeat",
            "monitors": moomoo_preset(),
        },
    ]


def _name_of(spec: dict) -> str:
    return str((spec.get("config") or {}).get("name") or spec.get("name") or "")


def has_monitor(monitors: list[dict], name: str) -> bool:
    return any(_name_of(m) == name for m in monitors)


def add_monitor(monitors: list[dict], spec: dict,
                registry: Optional[PluginRegistry] = None) -> list[dict]:
    """Return a new list with `spec` appended, after validating it against its
    plugin. Raises ValueError on unknown type, invalid config, or a duplicate
    monitor name (names must be unique per agent)."""
    reg = registry or default_registry()
    type_id = spec.get("type_id")
    if not type_id or not reg.has(type_id):
        raise ValueError(f"unknown monitor type_id: {type_id!r}")
    raw = dict(spec.get("config") or {})
    if "name" in spec and "name" not in raw:
        raw["name"] = spec["name"]
    cfg = reg.get(type_id).validate_config(raw)   # authoritative validation
    name = cfg.name
    if has_monitor(monitors, name):
        raise ValueError(f"a monitor named {name!r} already exists")
    return [*monitors, {"type_id": type_id, "config": cfg.model_dump()}]


def remove_monitor(monitors: list[dict], name: str) -> list[dict]:
    """Return a new list without the monitor named `name`. Raises if absent."""
    if not has_monitor(monitors, name):
        raise ValueError(f"no monitor named {name!r}")
    return [m for m in monitors if _name_of(m) != name]
