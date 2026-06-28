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
from taskpaw_v3.monitors.runtime import canonical_name, effective_monitors, monitor_name
from taskpaw_v3.monitors.supervisor import Supervisor

log = logging.getLogger("taskpaw.agent.admin")


def _as_bool(v: Any) -> bool:
    """Require a real boolean — `bool("false")` is True, so a JSON/form client
    sending `enabled: "false"` must be rejected, not silently treated as on."""
    if not isinstance(v, bool):
        raise ValueError(f"'enabled' must be a boolean, got {type(v).__name__}")
    return v


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
        """Plugin + validated pydantic config for a stored spec dict. Mirrors
        build_supervisor(): inject the resolved name into the raw config when the
        spec uses the top-level `name` shape (no config.name), so a monitor that
        starts fine at boot is also re-enable-able from the control API (Codex)."""
        plugin = self._reg.get(spec["type_id"])
        raw = dict(spec.get("config") or {})
        name = canonical_name(spec)
        if name and "name" not in raw:
            raw["name"] = name
        cfg = plugin.validate_config(raw)
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
            # Reject a name that collides with an EFFECTIVE monitor BEFORE
            # persisting — catalog.add_monitor only checks config.monitors, so the
            # auto-injected host_metrics ("<machine>-host", not in config.monitors
            # but a live instance id) would otherwise pass validation, get written
            # to agent.yaml, and only THEN fail at register() — leaving config
            # changed after a failed request (Codex #57a).
            resolved = canonical_name(spec)   # raises on top/config name conflict
            if resolved and resolved in {monitor_name(m) for m in effective_monitors(self._config)}:
                raise ValueError(f"a monitor named {resolved!r} already exists")
            # catalog.add_monitor validates against the plugin, rejects unknown
            # type / system plugin / duplicate name, and emits {type_id,name,config}.
            new_list = catalog.add_monitor(self._config.monitors, spec, self._reg)
            added = dict(new_list[-1])
            added["enabled"] = _as_bool(spec.get("enabled", True))
            new_list[-1] = added
            # Register live BEFORE persisting, so a config that can't actually run
            # is never written to agent.yaml (Codex). add_monitor already
            # validated, but registering can still surface a real failure.
            if added["enabled"]:
                self._register_live(added)
            self._config.monitors = new_list
            self._persist()
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
        enabled = _as_bool(enabled)
        with self._lock:
            m = self._find(name)
            if m is None:
                raise ValueError(f"no monitor named {str(name).strip()!r}")
            iid = monitor_name(m)
            if enabled:
                # Validate/register BEFORE persisting enabled:true: a monitor
                # whose stored config no longer validates (e.g. a plugin schema
                # change while it sat disabled) must not be persisted enabled and
                # then fail the next boot (Codex). With a supervisor this both
                # validates and registers; without one we at least validate.
                if self._sup is not None:
                    if not self._sup.has(iid):
                        self._register_live(m)
                else:
                    self._validated_config(m)
                m["enabled"] = True
                self._persist()
            else:
                m["enabled"] = False
                self._persist()
                if self._sup is not None and self._sup.has(iid):
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
            iid = monitor_name(m)
            # PATCH semantics: merge the incoming fields OVER the existing config
            # so a partial update (e.g. just poll_interval) keeps required
            # plugin fields (folder.path, tcp_check.port, …) and doesn't reset
            # omitted optional fields to defaults (Codex #57a). Force the stable
            # id: a config update must not rename the monitor (breaks Hub grouping).
            raw = {**(m.get("config") or {}), **config}
            raw["name"] = iid
            cfg = plugin.validate_config(raw)        # authoritative validation
            # Live-apply BEFORE persisting: if reconfigure() fails (e.g. a wedged
            # worker that won't stop in time → RuntimeError), don't leave disk
            # ahead of runtime (Codex). reconfigure rolls back to the old config
            # on failure, so nothing is half-applied.
            if self._sup is not None and self._sup.has(iid):
                self._sup.reconfigure(iid, cfg)
            m["config"] = cfg.model_dump()
            self._persist()
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
