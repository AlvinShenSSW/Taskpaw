"""Live monitor administration for the agent control API (#57).

The read-only console (#15) could only *show* monitors; this lets the local UI
**add / remove / update / enable / disable** them — validating against the
plugin, persisting `agent.yaml` atomically, and applying the change to the
running Supervisor with NO agent restart.

Source of truth = the on-disk config (`agent.yaml`). Every mutation: (1) edits
the in-memory `AgentConfig.monitors` via the pure `catalog` helpers, (2) writes
it atomically (`save_yaml` = tmp+fsync+replace), (3) reflects it into the live
Supervisor (register a fresh instance / unregister / reconfigure). Step 2 before
step 3 so a persisted change is never lost if the live-apply errors; all three
are serialized under one lock (loopback control API → low contention).

`enabled` lives at the spec top level ({type_id, name, config, enabled}) — the
pydantic config model forbids extra keys, so it can't go inside `config`.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from taskpaw_v3.agent import catalog
from taskpaw_v3.core.config import AgentConfig, save_yaml
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.monitors.runtime import monitor_name
from taskpaw_v3.monitors.supervisor import Supervisor

log = logging.getLogger("taskpaw.agent.admin")


class MonitorAdmin:
    """Serialized add/remove/update/enable/disable for one agent's monitors."""

    def __init__(
        self,
        config: AgentConfig,
        supervisor: Optional[Supervisor],
        registry: PluginRegistry,
        config_path: Optional[Path] = None,
    ) -> None:
        self._config = config
        self._sup = supervisor
        self._reg = registry
        self._path = config_path
        self._lock = threading.Lock()

    # ── internals ──────────────────────────────────────────────────────────
    def _persist(self) -> None:
        # No path (e.g. dev/interactive without a config file) → in-memory only.
        if self._path is not None:
            save_yaml(self._config, self._path)

    def _find(self, name: str) -> Optional[dict]:
        name = str(name).strip()
        for m in self._config.monitors:
            if monitor_name(m) == name:
                return m
        return None

    def _validated_config(self, spec: dict):
        """Plugin + validated pydantic config for a stored spec dict."""
        plugin = self._reg.get(spec["type_id"])
        cfg = plugin.validate_config(dict(spec.get("config") or {}))
        return plugin, cfg

    def _register_live(self, spec: dict) -> None:
        """Register a fresh instance into the running supervisor (no-op if no
        supervisor). Fresh instance each time avoids reusing a stopped one."""
        if self._sup is None:
            return
        plugin, cfg = self._validated_config(spec)
        self._sup.register(plugin, cfg, instance_id=monitor_name(spec))

    # ── operations (each: mutate config → persist → live-apply) ────────────
    def add(self, spec: dict) -> dict[str, Any]:
        with self._lock:
            # catalog.add_monitor validates against the plugin, rejects unknown
            # type / system plugin / duplicate name, and emits {type_id,name,config}.
            new_list = catalog.add_monitor(self._config.monitors, spec, self._reg)
            added = dict(new_list[-1])
            added["enabled"] = bool(spec.get("enabled", True))
            new_list[-1] = added
            self._config.monitors = new_list
            self._persist()
            if added["enabled"]:
                self._register_live(added)
            return {"ok": True, "monitor": added}

    def remove(self, name: str) -> dict[str, Any]:
        with self._lock:
            iid = str(name).strip()
            # raises ValueError if absent or duplicate (no silent data loss).
            self._config.monitors = catalog.remove_monitor(self._config.monitors, iid)
            self._persist()
            if self._sup is not None and self._sup.has(iid):
                self._sup.unregister(iid)
            return {"ok": True, "removed": iid}

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            m = self._find(name)
            if m is None:
                raise ValueError(f"no monitor named {str(name).strip()!r}")
            m["enabled"] = bool(enabled)
            self._persist()
            iid = monitor_name(m)
            if self._sup is not None:
                if enabled and not self._sup.has(iid):
                    self._register_live(m)
                elif not enabled and self._sup.has(iid):
                    self._sup.unregister(iid)
            return {"ok": True, "name": iid, "enabled": bool(enabled)}

    def update(self, name: str, config: dict) -> dict[str, Any]:
        with self._lock:
            m = self._find(name)
            if m is None:
                raise ValueError(f"no monitor named {str(name).strip()!r}")
            if not isinstance(config, dict):
                raise ValueError("monitor config must be an object")
            plugin = self._reg.get(m["type_id"])
            raw = dict(config)
            iid = monitor_name(m)
            # Keep the stable id: a config update must not rename the monitor
            # (that would break Hub grouping); force the existing name.
            raw["name"] = iid
            cfg = plugin.validate_config(raw)        # authoritative validation
            m["config"] = cfg.model_dump()
            self._persist()
            if self._sup is not None and self._sup.has(iid):
                self._sup.reconfigure(iid, cfg)
            return {"ok": True, "name": iid}

    # ── command dispatch (wired as create_control_app's on_command) ────────
    def handle(self, command: str, body: dict) -> dict[str, Any]:
        """Map a {command, ...} control message to an operation. Validation
        errors come back as {"ok": false, "error": ...} (the REST routes turn
        them into 4xx; /control/command returns them as-is)."""
        try:
            if command == "add_monitor":
                return self.add(dict(body.get("monitor") or body))
            if command == "remove_monitor":
                return self.remove(body.get("name", ""))
            if command in ("enable_monitor", "start_monitor"):
                return self.set_enabled(body.get("name", ""), True)
            if command in ("disable_monitor", "stop_monitor"):
                return self.set_enabled(body.get("name", ""), False)
            if command == "update_monitor":
                return self.update(body.get("name", ""), body.get("config") or {})
            return {"ok": False, "error": f"unknown command: {command!r}"}
        except (ValueError, KeyError) as e:
            return {"ok": False, "error": str(e)}
