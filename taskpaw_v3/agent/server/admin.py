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

from pydantic import ValidationError

from taskpaw_v3.agent import catalog
from taskpaw_v3.core.config import AgentConfig, save_yaml
from taskpaw_v3.core.llm import (
    LLM_SLOTS,
    LLMError,
    chat,
    llm_settings_from_config,
    llm_slot_fields,
    set_llm_chain,
    set_llm_settings,
)
from taskpaw_v3.core.tasklog import get_task_log
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.monitors.runtime import canonical_name, effective_monitors, monitor_name
from taskpaw_v3.monitors.subs.translate import (
    PROBE_MAX_TOKENS,
    llm_chain_from_config,
    probe_messages,
    probe_ok,
)
from taskpaw_v3.monitors.supervisor import Supervisor

log = logging.getLogger("taskpaw.agent.admin")


def _as_bool(v: Any) -> bool:
    """Require a real boolean — `bool("false")` is True, so a JSON/form client
    sending `enabled: "false"` must be rejected, not silently treated as on."""
    if not isinstance(v, bool):
        raise ValueError(f"'enabled' must be a boolean, got {type(v).__name__}")
    return v


def _validation_summary(e: ValidationError) -> str:
    """Field + reason only — pydantic's str() echoes the INPUT value, which for
    an LLM candidate may be the API key (D1)."""
    return "; ".join(
        f"{'.'.join(str(x) for x in err.get('loc', ())) or 'config'}: {err.get('msg')}"
        for err in e.errors()
    )


# Shared network-exposure guard (#114) — single source of truth used by the agent
# UI (here), the agent startup, and the Hub, so the rules can't drift.
from taskpaw_v3.core.net import guard_bind_exposure  # noqa: E402


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
            resolved = canonical_name(spec)  # raises on top/config name conflict
            if resolved and resolved in {
                monitor_name(m) for m in effective_monitors(self._config)
            }:
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
            get_task_log().record(
                monitor_name(added), "operator.add", task_type=added["type_id"]
            )
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
            remaining = catalog.remove_monitor(self._config.monitors, iid)
            removed = self._find(iid)
            assert removed is not None  # catalog just validated its existence
            get_task_log().record(iid, "operator.remove", task_type=removed["type_id"])
            self._config.monitors = remaining
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
                get_task_log().record(iid, "operator.start", task_type=m["type_id"])
                if self._sup is not None and not self._sup.has(iid):
                    self._sup.register(plugin, cfg, instance_id=iid)  # launches now
                # A manual-start monitor (managed Lada LAUNCHES lada-cli) is a
                # per-SESSION runtime toggle: Start launches it now but enabled
                # stays false, so it does NOT auto-start on the next boot — the
                # operator clicks Start each session (#70). Every other monitor
                # persists enabled:true and auto-starts at boot.
                if not plugin.manual_start(cfg):
                    m["enabled"] = True
                    self._persist()
            else:
                get_task_log().record(iid, "operator.stop", task_type=m["type_id"])
                m["enabled"] = False
                self._persist()
                if self._sup is not None and self._sup.has(iid):
                    self._sup.unregister(iid)
            return {"ok": True, "name": iid, "enabled": bool(enabled)}

    def patch(self, name: str, body: dict) -> dict[str, Any]:
        if "config" not in body and "enabled" not in body:
            raise ValueError("patch needs 'config' and/or 'enabled'")
        # Validate the toggle before update() can log or apply the config.
        if "enabled" in body:
            _as_bool(body["enabled"])
        out: dict[str, Any] = {"ok": True, "name": name}
        if "config" in body:
            out = self.update(name, body["config"])
        if "enabled" in body:
            out = self.set_enabled(name, body["enabled"])
        return out

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
            cfg = plugin.validate_config(raw)  # authoritative validation
            changed = sorted(
                k
                for k, v in cfg.model_dump().items()
                if v != (m.get("config") or {}).get(k)
            )
            get_task_log().record(
                iid, "operator.update", task_type=m["type_id"], data={"fields": changed}
            )
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
        "machine",
        "bind_host",
        "bind_port",
        "control_host",
        "control_port",
        "api_token",
        "host_metrics",
        "llm_api_base",
        "llm_model",
        "llm_api_key",
        "llm_fallback1_api_base",
        "llm_fallback1_model",
        "llm_fallback1_api_key",
        "llm_fallback2_api_base",
        "llm_fallback2_model",
        "llm_fallback2_api_key",
        "llm_failover",
    )
    # Live-safe editable fields: api_token is read per request (token_ok) and the
    # LLM settings are read by monitors at call time through the process-wide
    # holders (#178; the fallback chain + failover switch, #192), so changing
    # them never needs a restart.
    _LIVE_CONFIG = (
        "api_token",
        "llm_api_base",
        "llm_model",
        "llm_api_key",
        "llm_fallback1_api_base",
        "llm_fallback1_model",
        "llm_fallback1_api_key",
        "llm_fallback2_api_base",
        "llm_fallback2_model",
        "llm_fallback2_api_key",
        "llm_failover",
    )
    # The write-only LLM keys, one per provider slot (#178, #190).
    _LLM_KEY_FIELDS = tuple(llm_slot_fields(slot)[2] for slot in LLM_SLOTS)
    # Editable fields that are NOT live-safe: changing them needs a restart. Used
    # for the restart-required baseline. (A set difference, not a comprehension:
    # a class-body comprehension can't see _LIVE_CONFIG. Order kept for clarity.)
    _NON_LIVE_CONFIG = tuple(
        sorted(set(_EDITABLE_CONFIG) - set(_LIVE_CONFIG), key=_EDITABLE_CONFIG.index)
    )

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
            # Same keep-on-blank/"***" contract for each LLM key (the primary's
            # and both fallbacks', #190), plus an explicit CLEAR: `null` → "" so
            # a stored key is never sent on to a LAN host after the operator
            # switches the base URL (#178 D12).
            for f in self._LLM_KEY_FIELDS:
                if f not in p:
                    continue
                key = p[f]
                if key is None:
                    p[f] = ""
                elif isinstance(key, str) and key.strip() in ("", "***"):
                    del p[f]
            # Merge over the DESIRED scalars (so a pending edit isn't lost by a
            # later save) on top of the running config's monitors (always current).
            editables = {
                **self._desired,
                **{k: v for k, v in p.items() if k in self._EDITABLE_CONFIG},
            }
            merged = {**self._config.model_dump(), **editables}
            validated = AgentConfig(**merged)  # full validation (ports/loopback/blank)
            # Network-exposure guard (constitution: no public/WAN exposure). The
            # network API binds bind_host after restart; from the UI we refuse a
            # wildcard/all-interfaces bind outright, and require a token for any
            # non-loopback bind — else /status and /events would be reachable
            # off-host unauthenticated (Codex #43 P1). Raised BEFORE save → reject
            # leaves config + disk untouched. Normalize via ipaddress so every
            # spelling is caught (e.g. `0:0:0:0:0:0:0:0` == `::`, `127.0.0.2` is
            # still loopback) — not a brittle exact-string set (Codex #43 r3).
            # Shared guard (wildcard / public / non-loopback-without-token), the
            # single source used by the agent startup and the Hub too (#114/Kimi).
            guard_bind_exposure(
                validated.bind_host, validated.api_token, label="network API"
            )
            # A restart is needed if any non-live DESIRED field differs from what
            # the agent is actually RUNNING (the still-at-boot _config) — this
            # stays True across saves until the agent restarts (Codex #43).
            restart_required = any(
                getattr(validated, f) != getattr(self._config, f)
                for f in self._NON_LIVE_CONFIG
            )
            # Persist the candidate desired scalars FIRST; commit to self._desired
            # (and the live token) ONLY if the write succeeds, so a failed save
            # leaves config + disk untouched — atomic (Codex #43 r7).
            new_desired = {f: getattr(validated, f) for f in self._EDITABLE_CONFIG}
            self._save(new_desired)
            get_task_log().record(
                "",
                "operator.update",
                task_type="agent",
                data={
                    "fields": sorted(
                        f
                        for f in self._EDITABLE_CONFIG
                        if new_desired[f] != self._desired[f]
                    )
                },
            )
            self._desired = new_desired
            # Live-apply ONLY the live-safe fields: api_token (token_ok reads it
            # per request) and the LLM settings (#178: published to the holder
            # monitors read at call time — only now, after a successful save).
            # Non-live fields stay at their BOOT values in the running _config,
            # so /status & events stay consistent until the restart that
            # restart_required asks for (Codex #43).
            for f in self._LIVE_CONFIG:
                setattr(self._config, f, getattr(validated, f))
            set_llm_settings(llm_settings_from_config(validated))
            set_llm_chain(
                llm_chain_from_config(validated), failover=validated.llm_failover
            )
            return {"ok": True, "restart_required": restart_required}

    def llm_test(self, candidate: dict, slot: str = "primary") -> dict[str, Any]:
        """Settings "Test connection" (#178) for one provider slot (#190: primary,
        fallback1 or fallback2): try the CURRENT FORM values of THAT slot (its own
        field names) without persisting. The candidate is merged over the
        effective (desired) settings and validated like a save; a blank/"***"
        field (or null) falls back to the effective value, and the key still
        resolves env-first for the slot. Touches neither _desired, _config, disk
        nor the holders.

        It sends the translator's provider probe (#192 G5/H4): the real prompt
        with one cue, json_mode on, once more without json_mode on an HTTP 400,
        strict like the translator; OK only when the reply passes the
        translator's validation. chat() runs OUTSIDE the admin lock (D11): a
        slow provider must not block monitor/config operations. Never returns
        exception text or the reply (D1): LLMError messages are fixed strings."""
        if not isinstance(candidate, dict):
            raise ValueError("llm test candidate must be an object")
        if slot not in LLM_SLOTS:
            raise ValueError("llm test slot must be primary, fallback1 or fallback2")
        with self._lock:
            overrides = {}
            for f in llm_slot_fields(slot):
                v = candidate.get(f)
                if v is None or (isinstance(v, str) and v.strip() in ("", "***")):
                    continue
                overrides[f] = v
            try:
                validated = AgentConfig(
                    **{**self._config.model_dump(), **self._desired, **overrides}
                )
            except ValidationError as e:
                raise ValueError(_validation_summary(e)) from None
            settings = llm_settings_from_config(validated, slot=slot)
        messages = probe_messages()
        try:
            try:
                r = chat(
                    settings,
                    messages,
                    max_tokens=PROBE_MAX_TOKENS,
                    json_mode=True,
                    timeout=20,
                    strict=True,
                )
            except LLMError as e:
                if e.status != 400:
                    raise
                # The provider rejects json_mode: once without it (the
                # translator does the same).
                r = chat(
                    settings,
                    messages,
                    max_tokens=PROBE_MAX_TOKENS,
                    json_mode=False,
                    timeout=20,
                    strict=True,
                )
        except LLMError as e:
            error = f"{e.kind}: {e.message}"
            if e.status and str(e.status) not in e.message:
                error += f" (HTTP {e.status})"
            return {"ok": False, "error": error}
        except Exception as e:  # chat() raises only LLMError; belt and braces (D1)
            log.warning("LLM test failed unexpectedly: %s", type(e).__name__)
            return {"ok": False, "error": f"unexpected error: {type(e).__name__}"}
        if not probe_ok(r.content):
            return {
                "ok": False,
                "error": "invalid: the reply is not a usable translation",
            }
        return {"ok": True, "model": r.model, "latency_ms": r.latency_ms}

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
            if command == "llm_test":
                # llm_test rejects a non-object candidate or an unknown slot
                # with a ValueError.
                return self.llm_test(
                    body.get("candidate") or body, body.get("slot", "primary")
                )
            return {"ok": False, "error": f"unknown command: {command!r}"}
        except (ValueError, KeyError) as e:
            return {"ok": False, "error": str(e)}
