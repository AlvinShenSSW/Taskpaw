"""Wire a Supervisor to an agent EventQueue, and build one from agent config.

The Supervisor's sink signature is (instance_id, level, title, message, data,
dedupe_key); the agent EventQueue.add is (monitor, message, level, title, data).
This adapter bridges them (monitor = the stable instance_id) so monitor events
flow into the same queue the Hub polls.
"""

from __future__ import annotations

from typing import Any, Iterable

from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.monitors.supervisor import Supervisor


def effective_monitors(config: AgentConfig) -> list[dict[str, Any]]:
    """config.monitors plus a default host_metrics self-monitor (§5b: every agent)
    unless one is already configured or host_metrics is disabled. The injected
    name is collision-free against existing (stripped) names so register() can't
    raise a duplicate. Lives here (not launcher) so app.py can use it without a
    circular import (Kimi)."""
    monitors = list(config.monitors)
    if config.host_metrics and not any(m.get("type_id") == "host_metrics" for m in monitors):
        existing = {n for m in monitors if (n := monitor_name(m))}
        # machine/server_id are stripped+non-blank by AgentConfig validators; the
        # `or` is defensive so a blank stem can never produce "-host".
        stem = config.machine.strip() or config.server_id.strip()
        base = f"{stem}-host"
        name, i = base, 1
        while name in existing:
            name, i = f"{base}-{i}", i + 1
        monitors.append({"type_id": "host_metrics", "name": name, "config": {"name": name}})
    return monitors


def monitor_name(spec: dict[str, Any]) -> str:
    """Resolve a monitor's name from either canonical shape — top-level `name`
    ({type_id, name, config}) or name-inside-config. Single source of truth so a
    shape change touches one place. Returns the STRIPPED name so collision /
    duplicate checks on raw specs align with the validated (stripped) name —
    otherwise " foo " and "foo" slip past has_monitor()/effective_monitors() and
    then collide at registration (Kimi). Lenient: never raises (for display)."""
    cfg = spec.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
    return str(cfg.get("name") or spec.get("name") or "").strip()


def canonical_name(spec: dict[str, Any]) -> str:
    """Strict resolver for MUTATION paths (add_monitor / build_supervisor): like
    monitor_name(), but raises if a top-level `name` and a `config.name` are both
    present and differ (after strip) — a hand-edited/migrated spec would otherwise
    start a monitor under an id that mismatches its top-level name, breaking Hub
    grouping (Kimi)."""
    cfg = spec.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
    top, cname = spec.get("name"), cfg.get("name")
    if top is not None and cname is not None and str(top).strip() != str(cname).strip():
        raise ValueError(f"conflicting names: top-level {top!r} vs config {cname!r}")
    return str(cname or top or "").strip()


def make_queue_sink(queue: EventQueue, machine: str):
    """Adapter: Supervisor sink → EventQueue.add (additive level/title/data).

    `monitor` is the STABLE instance id (so Hub history/grouping is consistent
    across a monitor's state changes); `title` carries the display text.
    """
    def sink(instance_id: str, level: str, title: str, message: str,
             data=None, dedupe_key=None) -> None:
        lvl = level if level in {"info", "warn", "alert", "done"} else None
        # Do NOT swallow: if enqueue fails (e.g. the counter persist errors), let
        # it propagate to Supervisor._safe_sink, which logs it and returns failure
        # so the dedupe key is NOT recorded and the keyed alert can retry. Catching
        # here would make a never-queued alert look delivered → permanent suppression.
        queue.add(monitor=instance_id, message=message, level=lvl, title=title, data=data)
    return sink


def build_supervisor(
    registry: PluginRegistry,
    monitors: Iterable[dict[str, Any]],
    queue: EventQueue,
    machine: str,
) -> Supervisor:
    """Build (not start) a Supervisor from a list of {type_id, config} specs.

    Each spec's config is validated by the plugin's pydantic model (server-side
    authority). Unknown type_ids and invalid configs raise — the caller decides
    whether to fail the whole agent or skip the bad monitor.
    """
    sup = Supervisor(sink=make_queue_sink(queue, machine))
    for spec in monitors:
        type_id = spec.get("type_id")
        # str-guard before registry.has() — a malformed YAML type_id (list/null)
        # must raise ValueError, not TypeError that crashes agent startup (Kimi).
        if not isinstance(type_id, str) or not type_id or not registry.has(type_id):
            raise ValueError(f"unknown monitor type_id: {type_id!r}")
        plugin = registry.get(type_id)
        # Accept both shapes: {type_id, name, config} (name at top level) AND
        # name-inside-config. monitor_name() is the shared resolver.
        raw_in = spec.get("config")
        if raw_in is not None and not isinstance(raw_in, dict):
            raise ValueError("monitor config must be an object")  # list/null → clean error (Kimi)
        raw = dict(raw_in or {})
        name = canonical_name(spec)   # raises on a top-level/config name conflict
        if name and "name" not in raw:
            raw["name"] = name
        cfg = plugin.validate_config(raw)
        sup.register(plugin, cfg)
    return sup
