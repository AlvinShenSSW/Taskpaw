"""Wire a Supervisor to an agent EventQueue, and build one from agent config.

The Supervisor's sink signature is (instance_id, level, title, message, data,
dedupe_key); the agent EventQueue.add is (monitor, message, level, title, data).
This adapter bridges them (monitor = the stable instance_id) so monitor events
flow into the same queue the Hub polls.
"""

from __future__ import annotations

from typing import Any, Iterable

from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.monitors.supervisor import Supervisor


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
        if not type_id or not registry.has(type_id):
            raise ValueError(f"unknown monitor type_id: {type_id!r}")
        plugin = registry.get(type_id)
        # Accept both shapes: the V3 source-of-truth / migration shape
        # {type_id, name, config} (name at top level) AND name-inside-config.
        raw = dict(spec.get("config", {}))
        if "name" in spec and "name" not in raw:
            raw["name"] = spec["name"]
        cfg = plugin.validate_config(raw)
        sup.register(plugin, cfg)
    return sup
