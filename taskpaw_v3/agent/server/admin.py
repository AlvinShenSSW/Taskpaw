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

import ipaddress
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


def _bind_is_wildcard(host: str) -> bool:
    """All-interfaces bind? True for 0.0.0.0, :: and every IPv6 spelling of the
    unspecified address (e.g. 0:0:0:0:0:0:0:0), so the exposure guard can't be
    bypassed by an alternate spelling (Codex #43)."""
    if host in ("", "*"):
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False  # a hostname, not an IP literal — the loopback check handles it


def _bind_is_loopback(host: str) -> bool:
    """On-host only? True for localhost and any loopback IP (127.0.0.0/8, ::1)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
        # The DESIRED editable scalars (what's persisted / will apply on the next
        # restart). The running `_config` keeps its BOOT values for non-live fields
        # (sockets, the EventQueue machine tag, supervisor membership are bound at
        # startup and can't be re-applied live), so `/status` never advertises a
        # machine the events don't use (Codex #43). Config edits accumulate here so
        # a pending change isn't lost by a later save, and restart_required is the
        # difference between desired and running.
        self._desired = {f: getattr(config, f) for f in self._EDITABLE_CONFIG}

    # ── internals ──────────────────────────────────────────────────────────
    def _save(self, desired: dict) -> None:
        """Write the running monitors PLUS the given DESIRED editable scalars. The
        running _config holds BOOT values for non-live fields, so writing it
        directly would revert a pending (restart-required) config edit (Codex #43
        r6). Takes `desired` explicitly so update_config can persist a candidate
        BEFORE committing it to self._desired (atomic on failure, r7)."""
        if self._path is None:
            return
        save_yaml(AgentConfig(**{**self._config.model_dump(), **desired}), self._path)

    def _persist(self) -> None:
        self._save(self._desired)

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
            # Default-enabled UNLESS the plugin wants a manual start: managed Lada
            # LAUNCHES lada-cli on start, so (V2 parity) add it STOPPED and let the
            # operator click Start — adding the monitor must not kick off video
            # processing unbidden. An explicit `enabled` in the request still wins.
            plugin, cfg = self._validated_config(added)
            default_enabled = not plugin.manual_start(cfg)
            added["enabled"] = _as_bool(spec.get("enabled", default_enabled))
            new_list[-1] = added
            # Register live BEFORE persisting, so a config that can't actually run
            # is never written to agent.yaml (Codex). add_monitor already
            # validated, but registering can still surface a real failure.
            if added["enabled"] and self._sup is not None:
                self._sup.register(plugin, cfg, instance_id=monitor_name(added))
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
                # Validate BEFORE registering/persisting: a monitor whose stored
                # config no longer validates (e.g. a plugin schema change while it
                # sat disabled) must not be persisted enabled and then fail the
                # next boot (Codex).
                plugin, cfg = self._validated_config(m)
                if self._sup is not None and not self._sup.has(iid):
                    self._sup.register(plugin, cfg, instance_id=iid)   # launches now
                # A manual-start monitor (managed Lada LAUNCHES lada-cli) is a
                # per-SESSION runtime toggle: Start launches it now but enabled
                # stays false, so it does NOT auto-start on the next boot — the
                # operator clicks Start each session (#70). Every other monitor
                # persists enabled:true and auto-starts at boot.
                if not plugin.manual_start(cfg):
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

    # Editable top-level agent settings (NOT monitors — that's the monitor API —
    # and NOT server_id, the stable identity used for Hub grouping).
    _EDITABLE_CONFIG = (
        "machine", "bind_host", "bind_port", "control_host", "control_port",
        "api_token", "host_metrics",
    )
    # Editable fields that are NOT live-safe: changing them needs a restart (only
    # api_token is read per-request). Used for the restart-required baseline.
    _NON_LIVE_CONFIG = tuple(f for f in _EDITABLE_CONFIG if f != "api_token")

    def config_view(self) -> dict[str, Any]:
        """The config to SHOW in the editor: current monitors + everything from the
        running config, but the editable scalars come from the DESIRED (pending)
        state so the form reflects unsaved-since-restart edits and doesn't send
        stale running values back (which would revert a pending change). The token
        is masked by the route, not here."""
        with self._lock:
            return {**self._config.model_dump(), **self._desired}

    def update_config(self, patch: dict) -> dict[str, Any]:
        """Edit top-level agent config (machine/ports/token) from the Settings UI
        (#43). Validates the merged result and persists agent.yaml atomically. Only
        api_token is live (read per request); machine/ports/host/host_metrics are
        bound at startup, so they persist for the next restart and the call returns
        restart_required while they differ from the running config."""
        if not isinstance(patch, dict):
            raise ValueError("config patch must be an object")
        with self._lock:
            p = dict(patch)
            # The GET masks the token as "***"; an unchanged/blank token in the
            # patch must NOT clobber the real one.
            if str(p.get("api_token", "")).strip() in ("", "***"):
                p.pop("api_token", None)
            # Merge over the DESIRED scalars (so a pending edit isn't lost by a
            # later save) on top of the running config's monitors (always current).
            editables = {**self._desired, **{k: v for k, v in p.items() if k in self._EDITABLE_CONFIG}}
            merged = {**self._config.model_dump(), **editables}
            validated = AgentConfig(**merged)        # full validation (ports/loopback/blank)
            # Network-exposure guard (constitution: no public/WAN exposure). The
            # network API binds bind_host after restart; from the UI we refuse a
            # wildcard/all-interfaces bind outright, and require a token for any
            # non-loopback bind — else /status and /events would be reachable
            # off-host unauthenticated (Codex #43 P1). Raised BEFORE save → reject
            # leaves config + disk untouched. Normalize via ipaddress so every
            # spelling is caught (e.g. `0:0:0:0:0:0:0:0` == `::`, `127.0.0.2` is
            # still loopback) — not a brittle exact-string set (Codex #43 r3).
            bh = validated.bind_host.strip().strip("[]")
            if _bind_is_wildcard(bh):
                raise ValueError(
                    f"refusing to bind the network API to all interfaces ({bh!r}) "
                    f"from the UI — use 127.0.0.1 or a specific LAN address.")
            if not _bind_is_loopback(bh) and not validated.api_token.strip():
                raise ValueError(
                    f"binding the network API to a non-loopback address ({bh}) "
                    f"requires an API token, or /status and /events would be "
                    f"reachable off-host without auth. Set a token first, or keep "
                    f"bind_host on 127.0.0.1.")
            # A restart is needed if any non-live DESIRED field differs from what
            # the agent is actually RUNNING (the still-at-boot _config) — this
            # stays True across saves until the agent restarts (Codex #43).
            restart_required = any(
                getattr(validated, f) != getattr(self._config, f) for f in self._NON_LIVE_CONFIG
            )
            # Persist the candidate desired scalars FIRST; commit to self._desired
            # (and the live token) ONLY if the write succeeds, so a failed save
            # leaves config + disk untouched — atomic (Codex #43 r7).
            new_desired = {f: getattr(validated, f) for f in self._EDITABLE_CONFIG}
            self._save(new_desired)
            self._desired = new_desired
            # Live-apply ONLY the live-safe field: api_token (token_ok reads it per
            # request). Non-live fields stay at their BOOT values in the running
            # _config, so /status & events stay consistent until the restart that
            # restart_required asks for (Codex #43).
            self._config.api_token = validated.api_token
            return {"ok": True, "restart_required": restart_required}

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
