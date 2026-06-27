"""Plugin registry — discover plugins by type_id.

New monitor = register a plugin here (or via register()). The agent builds
instances from config `{type_id, name, config}` by looking the plugin up.
"""

from __future__ import annotations

from typing import Iterable

from taskpaw_v3.monitors.base import MonitorPlugin


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, MonitorPlugin] = {}

    def register(self, plugin: MonitorPlugin) -> None:
        if not plugin.type_id:
            raise ValueError("plugin.type_id must be set")
        if plugin.type_id in self._plugins:
            raise ValueError(f"duplicate plugin type_id: {plugin.type_id}")
        self._plugins[plugin.type_id] = plugin

    def get(self, type_id: str) -> MonitorPlugin:
        if type_id not in self._plugins:
            raise KeyError(f"unknown monitor type_id: {type_id}")
        return self._plugins[type_id]

    def has(self, type_id: str) -> bool:
        return type_id in self._plugins

    def types(self) -> Iterable[str]:
        return tuple(self._plugins.keys())


def default_registry() -> PluginRegistry:
    """Registry with the built-in plugins (process / heartbeat / tcp_check)."""
    from taskpaw_v3.monitors.plugins.process import ProcessPlugin
    from taskpaw_v3.monitors.plugins.heartbeat import HeartbeatPlugin
    from taskpaw_v3.monitors.plugins.tcp_check import TcpCheckPlugin

    reg = PluginRegistry()
    reg.register(ProcessPlugin())
    reg.register(HeartbeatPlugin())
    reg.register(TcpCheckPlugin())
    return reg
