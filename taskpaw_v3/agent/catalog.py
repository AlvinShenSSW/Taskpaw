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
from taskpaw_v3.monitors.runtime import monitor_name

# host_metrics is auto-injected per agent (design §5b) — flag it `system` so the
# UI shows it as always-on and doesn't offer a redundant second one (Kimi).
_SYSTEM_TYPES = {"host_metrics"}


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
            "config_version": p.config_version,
            "system": p.type_id in _SYSTEM_TYPES,
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


def has_monitor(monitors: list[dict], name: str) -> bool:
    return any(monitor_name(m) == name for m in monitors)


def add_monitor(monitors: list[dict], spec: dict,
                registry: Optional[PluginRegistry] = None) -> list[dict]:
    """Return a new list with `spec` appended, after validating it against its
    plugin. Emits the canonical {type_id, name, config} shape (matches migration
    / examples / source-of-truth — Kimi). Raises ValueError on unknown type,
    non-object config, invalid config, or a duplicate monitor name."""
    reg = registry or default_registry()
    type_id = spec.get("type_id")
    # Validate it's a str BEFORE reg.has() — a non-hashable type_id (e.g. a list
    # from malformed JSON) would otherwise raise TypeError, not ValueError (Kimi).
    if not isinstance(type_id, str) or not type_id or not reg.has(type_id):
        raise ValueError(f"unknown monitor type_id: {type_id!r}")
    raw_in = spec.get("config")
    if raw_in is not None and not isinstance(raw_in, dict):
        raise ValueError("monitor config must be an object")  # not list/null (Kimi)
    raw = dict(raw_in or {})
    top = spec.get("name")
    if top is not None and "name" in raw and top != raw["name"]:
        # Catch operator confusion instead of silently preferring config.name (Kimi).
        raise ValueError(f"conflicting names: top-level {top!r} vs config {raw['name']!r}")
    if "name" not in raw and top:
        raw["name"] = top
    cfg = reg.get(type_id).validate_config(raw)   # authoritative validation
    name = cfg.name
    if has_monitor(monitors, name):
        raise ValueError(f"a monitor named {name!r} already exists")
    return [*monitors, {"type_id": type_id, "name": name, "config": cfg.model_dump()}]


def remove_monitor(monitors: list[dict], name: str) -> list[dict]:
    """Return a new list without the monitor named `name`. Raises if absent, or
    if more than one matches — removing several at once (from hand-edited YAML /
    migration that slipped a duplicate in) would be silent data loss (Kimi)."""
    matches = [i for i, m in enumerate(monitors) if monitor_name(m) == name]
    if not matches:
        raise ValueError(f"no monitor named {name!r}")
    if len(matches) > 1:
        raise ValueError(f"multiple monitors named {name!r}; resolve the duplicate first")
    i = matches[0]
    return monitors[:i] + monitors[i + 1:]
