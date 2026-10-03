"""Live monitor administration for the agent control API (#57).

The read-only console (#15) could only *show* monitors; this lets the local UI
**add / remove / update / enable / disable** them — validating against the
plugin, persisting `agent.yaml` atomically, and applying the change to the
running Supervisor with NO agent restart.

Disk is desired state; Supervisor owns actual instances. Candidate writes publish only
on success. Emergency stop admission is memory-only and independent of disk I/O.
Partial results remain queryable and retiring instances retain their names.

`enabled` lives at the spec top level ({type_id, name, config, enabled}) — the
pydantic config model forbids extra keys, so it can't go inside `config`.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
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


@dataclass
class _Reservation:
    name: str
    operation: str
    spec: dict
    revision: int
    deadline: float
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    generation: int = 0
    stage: str = "applying"
    thread: threading.Thread | None = None
    validated: Any = None
    error: ValueError | None = None
    expired: bool = False


class MonitorAdmin:
    """Serialized add/remove/update/enable/disable for one agent's monitors."""

    def __init__(
        self,
        config: AgentConfig,
        supervisor: Optional[Supervisor],
        registry: PluginRegistry,
        config_path: Optional[Path] = None,
        *,
        operation_timeout: float = 10.0,
    ) -> None:
        self._config = config
        self._sup = supervisor
        self._reg = registry
        self._path = config_path
        self._lock = threading.RLock()  # memory only; no plugin or storage calls
        self._mutation = threading.Lock()
        self._timeout = operation_timeout
        self._revision = 0
        self._sequence = 0
        self._epochs: dict[str, int] = {}
        self._stopping: set[str] = set()
        self._owners: dict[str, _Reservation] = {}
        self._stop_saves: dict[str, _Reservation] = {}
        self._stop_records: dict[str, _Reservation] = {}
        self._results: dict[str, dict] = {}
        self._removed: dict[str, dict] = {}
        self._overrides: set[str] = set()
        # The DESIRED editable scalars (what's persisted / will apply on the next
        # restart). The running `_config` keeps its BOOT values for non-live fields
        # (sockets, the EventQueue machine tag, supervisor membership are bound at
        # startup and can't be re-applied live), so `/status` never advertises a
        # machine the events don't use (Codex #43). Config edits accumulate here so
        # a pending change isn't lost by a later save, and restart_required is the
        # difference between desired and running.
        self._desired = {f: getattr(config, f) for f in self._EDITABLE_CONFIG}

    # ── candidate persistence / short state admission ─────────────────────
    @contextmanager
    def _settings_admission(self):
        if not self._mutation.acquire(timeout=1):
            raise ValueError("operation_busy")
        try:
            yield
        finally:
            self._mutation.release()

    def _candidate(
        self,
        monitors: list[dict],
        desired: dict | None = None,
        start: str | None = None,
    ) -> AgentConfig:
        with self._lock:
            items = copy.deepcopy(monitors)
            for item in items:
                if (
                    monitor_name(item) in self._overrides
                    and monitor_name(item) != start
                ):
                    item["enabled"] = False
            return AgentConfig(
                **{
                    **self._config.model_dump(),
                    **(desired or self._desired),
                    "monitors": items,
                }
            )

    def _save(self, desired: dict) -> None:
        candidate = self._candidate(self._config.monitors, desired)
        if self._path is not None:
            save_yaml(candidate, self._path)
        with self._lock:
            self._config.monitors = copy.deepcopy(candidate.monitors)
            self._revision += 1

    def _find(self, name: str) -> Optional[dict]:
        return next(
            (m for m in self._config.monitors if monitor_name(m) == str(name).strip()),
            None,
        )

    def _validated_config(self, spec: dict):
        plugin = self._reg.get(spec["type_id"])
        raw = dict(spec.get("config") or {})
        name = canonical_name(spec)
        if name:
            raw["name"] = name
        return plugin, plugin.validate_config(raw)

    def _result(
        self,
        iid: str,
        operation: str,
        persistence: str,
        runtime: str,
        code: str | None = None,
        *,
        retryable: bool = True,
        owner: _Reservation | None = None,
        **extra,
    ) -> dict:
        if runtime == "stopping":
            outcome = "stop_incomplete"
        elif persistence == "failed":
            outcome = "applied_not_persisted" if runtime == "stopped" else "not_applied"
        elif persistence in {"pending", "not_requested"}:
            outcome = "applied_not_persisted" if runtime == "stopped" else "not_applied"
        elif runtime == "starting":
            outcome = "persisted_runtime_pending"
        elif runtime == "failed":
            outcome = "persisted_runtime_failed"
        else:
            outcome = "applied"
        if outcome != "applied":
            extra.pop("monitor", None)
        result = {
            "ok": outcome == "applied",
            "name": iid,
            "operation": operation,
            "outcome": outcome,
            "persistence": persistence,
            "runtime": runtime,
            "retryable": retryable,
            "error_code": code,
            **extra,
        }
        with self._lock:
            if owner is None or self._epochs.get(iid) == owner.generation:
                self._results[iid] = result
                if operation == "stop" and runtime in {"stopped", "unavailable"}:
                    self._stopping.discard(iid)
                if operation == "remove" and runtime in {"stopped", "unavailable"}:
                    self._removed.pop(iid, None)
                    self._overrides.discard(iid)
        return dict(result)

    def _busy(self, iid: str, operation: str) -> dict:
        return {
            "ok": False,
            "name": iid,
            "operation": operation,
            "outcome": "busy",
            "persistence": "not_requested",
            "runtime": "unchanged",
            "error_code": "operation_busy",
            "retryable": False,
        }

    def _prune_validation(self) -> None:
        for iid, op in list(self._owners.items()):
            if (
                (op.expired or op.cancel.is_set())
                and op.done.is_set()
                and (op.thread is None or not op.thread.is_alive())
            ):
                self._release(op)

    def _refresh_stops(self) -> None:
        with self._lock:
            stopped = {iid: self._epochs.get(iid) for iid in self._stopping}
        for iid, generation in stopped.items():
            if self._sup is None or not self._sup.has(iid):
                with self._lock:
                    if self._epochs.get(iid) == generation:
                        self._stopping.discard(iid)

    def _reserve(
        self, iid: str, operation: str, spec: dict, deadline: float
    ) -> _Reservation | None:
        self._refresh_stops()
        with self._lock:
            self._prune_validation()
            if iid in self._owners or iid in self._stop_saves or iid in self._stopping:
                return None
            op = _Reservation(
                iid, operation, copy.deepcopy(spec), self._revision, deadline
            )
            self._sequence += 1
            op.generation = self._sequence
            self._epochs[iid] = op.generation
            self._owners[iid] = op
            return op

    def _release(self, op: _Reservation) -> None:
        with self._lock:
            if self._owners.get(op.name) is op:
                del self._owners[op.name]
            if (
                self._find(op.name) is None
                and op.name not in self._removed
                and op.name not in self._owners
                and op.name not in self._stop_saves
            ):
                self._results.pop(op.name, None)
                self._stop_records.pop(op.name, None)
                self._epochs.pop(op.name, None)
                self._overrides.discard(op.name)

    def _commit(
        self, op: _Reservation, monitors: list[dict], *, start: bool = False
    ) -> str:
        candidate = self._candidate(monitors, start=op.name if start else None)
        if self._path is not None:
            save_yaml(candidate, self._path)
        with self._lock:
            self._config.monitors = copy.deepcopy(candidate.monitors)
            self._revision += 1
            if start and not op.cancel.is_set():
                self._overrides.discard(op.name)
        return "in_memory" if self._path is None else "saved"

    def _runtime_apply(
        self, op: _Reservation, plugin, cfg, *, start=False, reconcile=False
    ) -> tuple[str, str | None]:
        sup = self._sup
        if sup is None:
            return "unavailable", None
        if op.cancel.is_set():
            return "stopping" if sup.has(
                op.name
            ) else "stopped", "stop_timeout" if sup.has(op.name) else None
        try:
            if reconcile and not sup.has(op.name):
                with self._lock:
                    committed = self._find(op.name)
                    should_run = bool(
                        committed
                        and committed.get("enabled", True)
                        and op.name not in self._overrides
                    )
                # Edit recovery follows effective committed passive policy; it
                # never turns manual/disabled/explicitly stopped config into Start.
                if should_run and not plugin.manual_start(cfg):
                    start = True
            if sup.has(op.name):
                view = sup.snapshot().get(op.name, {})
                if view.get("lifecycle") == "stopping":
                    return "stopping", "stop_timeout"
                if (
                    not start
                    or sup.config_matches(op.name, cfg.model_dump()) is not True
                ):
                    sup.reconfigure(
                        op.name,
                        cfg,
                        max(0, op.deadline - time.monotonic()),
                        cancel=op.cancel,
                    )
            elif start:
                sup.register(
                    plugin,
                    cfg,
                    instance_id=op.name,
                    cancel=op.cancel,
                    deadline=op.deadline,
                )
            else:
                return "applied", None
            r = sup.activation_result(op.name, max(0, op.deadline - time.monotonic()))
            return r["runtime"], r["error_code"]
        except Exception as exc:
            code = (
                str(exc)
                if str(exc) in {"operation_busy", "stop_timeout"}
                else "create_failed"
            )
            return "stopping" if code == "stop_timeout" else "failed", code

    def add(self, spec: dict) -> dict[str, Any]:
        iid = canonical_name(spec)
        with self._lock:
            monitors = copy.deepcopy(self._config.monitors)
            existing = self._find(iid)
            partial = (
                self._results.get(iid, {}).get("outcome") == "persisted_runtime_failed"
            )
            if existing is not None and not partial:
                raise ValueError(f"a monitor named {iid!r} already exists")
            if existing is None and iid in {
                monitor_name(m) for m in effective_monitors(self._config)
            }:
                raise ValueError(f"a monitor named {iid!r} already exists")
            if iid in self._removed:
                return self._busy(iid, "add")
        new_list = catalog.add_monitor([] if existing else monitors, spec, self._reg)
        added = dict(new_list[-1])
        plugin, cfg = self._validated_config(added)
        added["enabled"] = _as_bool(spec.get("enabled", not plugin.manual_start(cfg)))
        if existing:
            if existing != added:
                raise ValueError(f"a monitor named {iid!r} already exists")
            new_list = monitors
        else:
            new_list[-1] = added
        op = self._reserve(iid, "add", added, time.monotonic() + self._timeout)
        if op is None:
            return self._busy(iid, "add")
        try:
            if not self._mutation.acquire(timeout=1):
                return self._busy(iid, "add")
            try:
                with self._lock:
                    # Validation precedes admission: recheck eligibility against
                    # the latest commit, including a same-name overlapping Add.
                    latest = copy.deepcopy(self._config.monitors)
                    current = self._find(iid)
                    partial = (
                        self._results.get(iid, {}).get("outcome")
                        == "persisted_runtime_failed"
                    )
                    if current is not None:
                        if not partial or current != added:
                            raise ValueError(f"a monitor named {iid!r} already exists")
                    elif iid in {
                        monitor_name(m) for m in effective_monitors(self._config)
                    }:
                        raise ValueError(f"a monitor named {iid!r} already exists")
                    if iid in self._removed:
                        return self._busy(iid, "add")
                    if current is None:
                        latest.append(added)
                try:
                    persistence = self._commit(op, latest)
                except Exception:
                    return self._result(
                        iid,
                        "add",
                        "failed",
                        "unchanged",
                        "persistence_failed",
                        owner=op,
                        monitor=added,
                    )
                get_task_log().record(iid, "operator.add", task_type=added["type_id"])
                runtime, code = self._runtime_apply(
                    op, plugin, cfg, start=bool(added["enabled"])
                )
                return self._result(
                    iid, "add", persistence, runtime, code, owner=op, monitor=added
                )
            finally:
                self._mutation.release()
        finally:
            self._release(op)

    def remove(self, name: str) -> dict[str, Any]:
        iid = str(name).strip()
        with self._lock:
            spec = copy.deepcopy(self._find(iid) or self._removed.get(iid))
        if spec is None:
            raise ValueError(f"no monitor named {iid!r}")
        op = self._reserve(iid, "remove", spec, time.monotonic() + self._timeout)
        if op is None:
            return self._busy(iid, "remove")
        try:
            if not self._mutation.acquire(timeout=1):
                return self._busy(iid, "remove")
            try:
                with self._lock:
                    remaining = [
                        copy.deepcopy(m)
                        for m in self._config.monitors
                        if monitor_name(m) != iid
                    ]
                try:
                    persistence = self._commit(op, remaining)
                except Exception:
                    return self._result(
                        iid,
                        "remove",
                        "failed",
                        "unchanged",
                        "persistence_failed",
                        owner=op,
                    )
                with self._lock:
                    self._removed[iid] = spec
                get_task_log().record(iid, "operator.remove", task_type=spec["type_id"])
                if self._sup is not None and self._sup.has(iid):
                    r = self._sup.unregister(
                        iid, max(0, op.deadline - time.monotonic())
                    )
                    runtime, code = (
                        ("stopped", None)
                        if r["complete"]
                        else ("stopping", r["error_code"])
                    )
                else:
                    runtime, code = (
                        ("stopped", None) if self._sup else ("unavailable", None)
                    )
                return self._result(
                    iid, "remove", persistence, runtime, code, owner=op, removed=iid
                )
            finally:
                self._mutation.release()
        finally:
            self._release(op)

    def _stop(
        self,
        iid: str,
        *,
        candidate: dict | None = None,
        parent: _Reservation | None = None,
        deadline: float | None = None,
    ) -> dict:
        deadline = (
            deadline if deadline is not None else time.monotonic() + self._timeout
        )
        with self._lock:
            if parent is not None and (
                parent.cancel.is_set()
                or self._epochs.get(iid) != parent.generation
                or time.monotonic() >= deadline
            ):
                return self._result(
                    iid,
                    "update",
                    "not_requested",
                    "unchanged",
                    "validation_timeout"
                    if time.monotonic() >= deadline
                    else "validation_cancelled",
                    owner=parent,
                )
            spec = copy.deepcopy(
                self._find(iid)
                or self._removed.get(iid)
                or (self._owners[iid].spec if iid in self._owners else None)
            )
            if spec is None:
                raise ValueError(f"no monitor named {iid!r}")
            owner = self._owners.get(iid)
            if owner is not None and owner is not parent:
                owner.cancel.set()
            self._overrides.add(iid)
            self._stopping.add(iid)
            stop_op = parent or _Reservation(
                iid, "stop", spec, self._revision, deadline
            )
            if parent is None:
                self._sequence += 1
                stop_op.generation = self._sequence
                self._epochs[iid] = stop_op.generation
        if self._sup is not None:
            self._sup.request_stop(iid, max(0, deadline - time.monotonic()))
        # No save/log/validator precedes the stop signal above.
        with self._lock:
            save = self._stop_saves.get(iid)
        if save is None and self._mutation.acquire(blocking=False):
            save = _Reservation(
                iid,
                "stop",
                spec,
                self._revision,
                deadline,
                generation=stop_op.generation,
            )
            with self._lock:
                self._stop_saves[iid] = save
                self._stop_records[iid] = save

            def persist_stop():
                try:
                    with self._lock:
                        monitors = copy.deepcopy(self._config.monitors)
                        if (
                            candidate is not None
                            and parent is not None
                            and not parent.cancel.is_set()
                        ):
                            monitors = [
                                copy.deepcopy(candidate)
                                if monitor_name(m) == iid
                                else m
                                for m in monitors
                            ]
                        unchanged = monitors == self._config.monitors and not any(
                            monitor_name(m) == iid and m.get("enabled", True)
                            for m in monitors
                        )
                    try:
                        persistence = (
                            ("in_memory" if self._path is None else "saved")
                            if unchanged
                            else self._commit(save, monitors)
                        )
                    except Exception:
                        persistence = "failed"
                    with self._lock:
                        save.validated = persistence
                finally:
                    self._mutation.release()
                    save.done.set()
                    with self._lock:
                        if self._stop_saves.get(iid) is save:
                            del self._stop_saves[iid]
                get_task_log().record(iid, "operator.stop", task_type=spec["type_id"])

            try:
                save.thread = threading.Thread(
                    target=persist_stop, name=f"persist-stop-{iid}", daemon=True
                )
                save.thread.start()
            except Exception:
                if save.thread is None or save.thread.ident is None:
                    # Only an unstarted writer can return its admission here.
                    # A started writer owns completion even if start() raised.
                    with self._lock:
                        save.validated = "failed"
                        save.done.set()
                        if self._stop_saves.get(iid) is save:
                            del self._stop_saves[iid]
                    self._mutation.release()
        if save is not None:
            save.done.wait(max(0, deadline - time.monotonic()))
            persistence = save.validated if save.done.is_set() else "pending"
        else:
            persistence = "pending"
        if self._sup is None:
            runtime, code = "unavailable", None
        else:
            r = self._sup.stop_result(iid, max(0, deadline - time.monotonic()))
            runtime, code = (
                ("stopped", None) if r["complete"] else ("stopping", r["error_code"])
            )
        if persistence == "failed" and code is None:
            code = "persistence_failed"
        return self._result(
            iid, "stop", persistence, runtime, code, owner=stop_op, enabled=False
        )

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        enabled = _as_bool(enabled)
        iid = str(name).strip()
        if not enabled:
            return self._stop(iid)
        return self._patch(iid, {"enabled": True}, operation="start", bounded=False)

    def patch(self, name: str, body: dict) -> dict[str, Any]:
        if "config" not in body and "enabled" not in body:
            raise ValueError("patch needs 'config' and/or 'enabled'")
        if "enabled" in body:
            _as_bool(body["enabled"])
        if "config" not in body and body.get("enabled") is False:
            return self._stop(str(name).strip())
        return self._patch(
            str(name).strip(), body, operation="update", bounded="config" in body
        )

    def update(self, name: str, config: dict) -> dict[str, Any]:
        return self._patch(
            str(name).strip(), {"config": config}, operation="update", bounded=True
        )

    def _patch(self, iid: str, body: dict, *, operation: str, bounded: bool) -> dict:
        deadline = time.monotonic() + self._timeout
        if "config" in body and not isinstance(body["config"], dict):
            raise ValueError("monitor config must be an object")
        with self._lock:
            original = copy.deepcopy(self._find(iid))
        if original is None:
            raise ValueError(f"no monitor named {iid!r}")
        if (
            self._sup is not None
            and self._sup.snapshot().get(iid, {}).get("lifecycle") == "stopping"
        ):
            return self._busy(iid, operation)
        op = self._reserve(iid, operation, original, deadline)
        if op is None:
            return self._busy(iid, operation)
        accepted = False
        try:
            spec = copy.deepcopy(original)
            spec["config"] = {
                **(spec.get("config") or {}),
                **body.get("config", {}),
                "name": iid,
            }

            def validate():
                try:
                    op.validated = self._validated_config(spec)
                except Exception:
                    op.error = ValueError("invalid monitor config")
                finally:
                    op.done.set()

            if bounded:
                op.stage = "validating"
                try:
                    op.thread = threading.Thread(
                        target=validate, name=f"validate-{iid}", daemon=True
                    )
                    op.thread.start()
                except Exception:
                    if op.thread is None or op.thread.ident is None:
                        # Known unstarted owner: nothing can later finish/apply it.
                        op.done.set()
                        return self._result(
                            iid,
                            operation,
                            "not_requested",
                            "unchanged",
                            "start_failed",
                            owner=op,
                        )
                    # A live owner remains tracked and follows the same deadline.
                op.done.wait(max(0, deadline - time.monotonic()))
                with self._lock:
                    if not op.done.is_set() or time.monotonic() >= deadline:
                        op.expired = True
                        op.stage = "validation_expired"
                        return self._result(
                            iid,
                            operation,
                            "not_requested",
                            "unchanged",
                            "validation_timeout",
                            retryable=op.done.is_set(),
                            owner=op,
                        )
            else:
                validate()
            with self._lock:
                if op.cancel.is_set() or op.revision != self._revision:
                    op.stage = "validation_cancelled"
                    return self._result(
                        iid,
                        operation,
                        "not_requested",
                        "unchanged",
                        "validation_cancelled",
                        owner=op,
                    )
                if op.error is not None:
                    raise op.error
                plugin, cfg = op.validated
                spec["config"] = cfg.model_dump()
                if "enabled" in body:
                    if not body["enabled"] or not plugin.manual_start(cfg):
                        spec["enabled"] = body["enabled"]
                op.stage = "applying"
            if body.get("enabled") is False:
                accepted = True
                return self._stop(iid, candidate=spec, parent=op, deadline=deadline)
            if not self._mutation.acquire(
                timeout=max(0, min(1, deadline - time.monotonic()))
            ):
                return self._busy(iid, operation)
            try:
                with self._lock:
                    if (
                        op.cancel.is_set()
                        or (bounded and time.monotonic() >= deadline)
                        or op.revision != self._revision
                    ):
                        return self._result(
                            iid,
                            operation,
                            "not_requested",
                            "unchanged",
                            "validation_cancelled",
                            owner=op,
                        )
                    monitors = [
                        spec if monitor_name(m) == iid else copy.deepcopy(m)
                        for m in self._config.monitors
                    ]
                try:
                    persistence = self._commit(
                        op, monitors, start=body.get("enabled") is True
                    )
                except Exception:
                    return self._result(
                        iid, operation, "failed", "unchanged", "persistence_failed"
                    )
                fields = sorted(
                    k
                    for k, v in spec["config"].items()
                    if v != original.get("config", {}).get(k)
                )
                get_task_log().record(
                    iid,
                    "operator.start" if operation == "start" else "operator.update",
                    task_type=spec["type_id"],
                    data={"fields": fields} if operation == "update" else None,
                )
                runtime, code = self._runtime_apply(
                    op,
                    plugin,
                    cfg,
                    start=body.get("enabled") is True,
                    reconcile=operation == "update",
                )
                return self._result(
                    iid,
                    operation,
                    persistence,
                    runtime,
                    code,
                    owner=op,
                    **({"enabled": True} if operation == "start" else {}),
                )
            finally:
                self._mutation.release()
        finally:
            if not bounded or (op.done.is_set() and not op.expired) or accepted:
                self._release(op)

    def status_view(self) -> dict:
        from taskpaw_v3.monitors.runtime import merge_status

        with self._lock:
            config = self._config.model_copy(deep=True)
            removed = copy.deepcopy(self._removed)
            results = copy.deepcopy(self._results)
            for iid, result in results.items():
                save = self._stop_records.get(iid)
                if (
                    result.get("operation") == "stop"
                    and save is not None
                    and save.done.is_set()
                ):
                    result["persistence"] = save.validated
                    if save.validated == "failed":
                        result["error_code"] = "persistence_failed"
            for iid in list(self._stop_records):
                if iid not in results:
                    self._stop_records.pop(iid, None)
            overrides = set(self._overrides)
        live = self._sup.snapshot() if self._sup else {}
        out = merge_status(config, live)
        for iid, spec in removed.items():
            if iid in live:
                out[iid].update(
                    type_id=spec["type_id"],
                    configured=False,
                    lifecycle="remove_pending",
                )
            else:
                with self._lock:
                    self._removed.pop(iid, None)
                    if iid not in self._owners and iid not in self._stop_saves:
                        self._results.pop(iid, None)
                        self._stop_records.pop(iid, None)
                        self._epochs.pop(iid, None)
                        self._overrides.discard(iid)
                        self._stopping.discard(iid)
        for iid, entry in out.items():
            entry["configured"] = self._find(iid) is not None
            entry["desired_enabled"] = entry.get("enabled", True)
            if iid in overrides and iid not in live:
                entry["lifecycle"] = "stopped"
            latest_result = results.get(iid)
            if latest_result:
                entry["persistence"] = latest_result["persistence"]
                entry["runtime_error_code"] = latest_result["error_code"]
                if latest_result["operation"] == "stop" and iid not in live:
                    # A late successful retirement is now proven by the registry;
                    # the earlier wait timeout is not an ongoing cleanup failure.
                    entry["runtime_error_code"] = (
                        "persistence_failed"
                        if latest_result["persistence"] == "failed"
                        else None
                    )
            configured_spec = next(
                (m for m in config.monitors if monitor_name(m) == iid), None
            )
            entry["config_in_sync"] = (
                self._sup.config_matches(iid, configured_spec.get("config", {}))
                if self._sup and configured_spec
                else None
            )
        return out

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
        "llm_thinking_off",
        "llm_fallback1_api_base",
        "llm_fallback1_model",
        "llm_fallback1_api_key",
        "llm_fallback1_thinking_off",
        "llm_fallback2_api_base",
        "llm_fallback2_model",
        "llm_fallback2_api_key",
        "llm_fallback2_thinking_off",
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
        "llm_thinking_off",
        "llm_fallback1_api_base",
        "llm_fallback1_model",
        "llm_fallback1_api_key",
        "llm_fallback1_thinking_off",
        "llm_fallback2_api_base",
        "llm_fallback2_model",
        "llm_fallback2_api_key",
        "llm_fallback2_thinking_off",
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
            self._prune_validation()
            return {
                **copy.deepcopy(self._config.model_dump()),
                **self._desired,
                "monitor_operations": {
                    iid: {
                        **self._results.get(iid, {}),
                        "stage": op.stage,
                        "retryable": False,
                    }
                    for iid, op in self._owners.items()
                },
            }

    def update_config(self, patch: dict) -> dict[str, Any]:
        """Edit top-level agent config (machine/ports/token) from the Settings UI
        (#43). Validates the merged result and persists agent.yaml atomically. Only
        api_token is live (read per request); machine/ports/host/host_metrics are
        bound at startup, so they persist for the next restart and the call returns
        restart_required while they differ from the running config."""
        if not isinstance(patch, dict):
            raise ValueError("config patch must be an object")
        with self._settings_admission():
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
        base/model/key field (or null) falls back to the effective value. A
        present thinking field overrides it even when null (automatic). The key still
        resolves env-first for the slot. Touches neither _desired, _config, disk
        nor the holders.

        It sends the translator's provider probe (#192 G5/H4): the real prompt
        with one cue, trying thinking off/on and JSON mode on/off on rejection,
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
            thinking_field = llm_slot_fields(slot)[0].replace(
                "api_base", "thinking_off"
            )
            if thinking_field in candidate:  # null explicitly requests automatic
                overrides[thinking_field] = candidate[thinking_field]
            try:
                validated = AgentConfig(
                    **{**self._config.model_dump(), **self._desired, **overrides}
                )
            except ValidationError as e:
                raise ValueError(_validation_summary(e)) from None
            settings = llm_settings_from_config(validated, slot=slot)
        messages = probe_messages()
        steps = (
            [(True, True), (True, False), (False, True), (False, False)]
            if settings.thinking_off
            else [(True, False), (False, False)]
        )
        try:
            for index, (json_mode, thinking_off) in enumerate(steps):
                try:
                    r = chat(
                        replace(settings, thinking_off=thinking_off),
                        messages,
                        max_tokens=PROBE_MAX_TOKENS,
                        json_mode=json_mode,
                        timeout=20,
                        strict=True,
                    )
                    break
                except LLMError as e:
                    if index == len(steps) - 1 or not (
                        e.status == 400 or (thinking_off and e.status == 422)
                    ):
                        raise
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
        result = {"ok": True, "model": r.model, "latency_ms": r.latency_ms}
        if settings.thinking_off and not thinking_off:
            result["note"] = "thinking_unsupported"
        return result

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
