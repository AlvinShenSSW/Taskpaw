"""P2-2 (#293): the 设备 page's operator commands and its read.

添加设备 registers a worker and issues a 15-minute, single-use pairing ticket;
the code `TP1:<host>:<port>:<pairing_id>:<ticket>` is returned once and never
logged. 核验并启用 approves a subset of the worker's own proposed profiles as a
new capability revision that reuses its probe (OD4); restore needs a 16 GB
class card (OD5). Every command carries the worker's `expected_version`.
"""

from __future__ import annotations

import ipaddress
import secrets
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import SecretStr

from taskpaw_v3.core.power_fleet import parse_address

from . import models as m
from . import scheduler as s
from .auth import digest_secret
from .fleet_live import FleetLive
from .local_runtime import _RESOURCE_NS, LocalRuntime

PAIRING_SECONDS = 15 * 60
CODE_PREFIX = "TP1"
STAGE_LABELS = {"restore": "修复", "asr": "识别", "translate": "翻译"}
NO_RESTORE = "显存不足，不参与修复"
READY = "可接单"


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def pairing_code(host: str, port: int, pairing_id: str, ticket: str) -> str:
    try:
        shown = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        shown = host
    return f"{CODE_PREFIX}:{shown}:{port}:{pairing_id}:{ticket}"


def _worker(rt: LocalRuntime, worker_id: str, expected_version: int) -> m.WorkerRecord:
    if worker_id == rt.identity.worker_id:  # 本机 has no device actions
        raise m.WorkflowError("precondition_failed")
    worker = rt.store.get_worker(rt.operator, worker_id).worker
    if worker.version != expected_version:
        raise m.WorkflowError("cas_conflict")
    return worker


def _issue(
    rt: LocalRuntime, worker: m.WorkerRecord, host: str, port: int
) -> dict[str, Any]:
    pairing_id, ticket = str(uuid.uuid4()), secrets.token_urlsafe(32)
    expires = rt.clock.now() + timedelta(seconds=PAIRING_SECONDS)
    rt.store.issue_pairing(
        rt.operator,
        rt.meta(),
        m.PairingIssue(
            pairing_id=pairing_id,
            worker_id=worker.worker_id,
            device_id=worker.device_id,
            ticket_digest=digest_secret(SecretStr(ticket), purpose="pairing"),
            expires_at=expires,
        ),
    )
    return {
        "worker_id": worker.worker_id,
        "code": pairing_code(host, port, pairing_id, ticket),
        "expires_at": iso(expires),
    }


def add_device(
    rt: LocalRuntime, name: str, address: str, *, host: str, port: int
) -> dict[str, Any]:
    """添加设备: register, then a pairing code for the worker's console."""
    try:
        worker_host, worker_port = parse_address(address)
    except ValueError:
        raise m.WorkflowError("invalid_request") from None
    worker_id, device_id = str(uuid.uuid4()), str(uuid.uuid4())
    rt.store.register_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRegistration(
            worker_id=worker_id,
            device_id=device_id,
            name=name.strip(),
            address=worker_host,
            port=worker_port,  # the agent's network port, 5680 unless given
        ),
    )
    worker = rt.store.get_worker(rt.operator, worker_id).worker
    return _issue(rt, worker, host, port)


def repair(
    rt: LocalRuntime, worker_id: str, expected_version: int, *, host: str, port: int
) -> dict[str, Any]:
    """重新配对: a new ticket; completing it takes generation + 1 and revokes
    the old credential, and the worker waits for 核验 again."""
    return _issue(rt, _worker(rt, worker_id, expected_version), host, port)


def approve(
    rt: LocalRuntime,
    worker_id: str,
    expected_version: int,
    *,
    restore: bool,
    asr: bool,
) -> m.WorkerRecord:
    """核验并启用: the chosen stages of the worker's probe, then enabled (R5)."""
    worker = _worker(rt, worker_id, expected_version)
    cap = worker.capability
    probe = cap.probed if cap else None
    if (
        cap is None
        or probe is None
        or not probe.profiles
        or worker.enrollment_state == "pending_pairing"
    ):
        raise m.WorkflowError("precondition_failed")
    if restore and probe.vram_mib < s.RESTORE_MIN_VRAM_MIB:  # OD5
        raise m.WorkflowError("precondition_failed")
    stages = {stage for stage, on in (("restore", restore), ("asr", asr)) if on}
    profiles = tuple(
        p
        for p in probe.profiles
        if p.allowed_stages and set(p.allowed_stages) <= stages
    )
    if not profiles:
        raise m.WorkflowError("precondition_failed")
    revision, now = cap.revision + 1, rt.clock.now()
    rt.store.record_capabilities(
        rt.operator,
        rt.meta(),
        m.CapabilityUpdate(
            worker_id=worker_id,
            expected_version=worker.version,
            capability=m.CapabilityRevision(
                revision=revision,
                declared=None,
                probed=probe,
                approved=m.ApprovedCapability(
                    revision=revision,
                    probe_revision=probe.revision,
                    approved_by=rt.operator.principal_id,
                    approved_at=now,
                    profiles=profiles,
                ),
            ),
            physical_binding=m.PhysicalGpuBinding(
                resource_id=str(
                    uuid.uuid5(
                        _RESOURCE_NS, f"{probe.device_identity}|{probe.gpu_identity}"
                    )
                ),
                device_id=worker.device_id,
                device_identity=probe.device_identity,
                gpu_identity=probe.gpu_identity,
                capability_revision=revision,
                evidence=rt.evidence("physical_identity"),
            ),
        ),
    )
    return _enable(rt, worker_id)


def _enable(rt: LocalRuntime, worker_id: str) -> m.WorkerRecord:
    """IR9: approval's second write, idempotent and re-read on a lost CAS, so
    a write between the two never leaves a verified worker disabled."""
    for _ in range(3):
        worker = rt.store.get_worker(rt.operator, worker_id).worker
        if worker.enrollment_state != "verified" or worker.admission_state == "revoked":
            raise m.WorkflowError("precondition_failed")
        if worker.workflow_participation and worker.admission_state == "enabled":
            return worker
        try:
            return set_admission(rt, worker_id, worker.version, "enabled")
        except m.WorkflowError as exc:
            if exc.code != "cas_conflict":
                raise
    raise m.WorkflowError("cas_conflict")


def set_admission(
    rt: LocalRuntime,
    worker_id: str,
    expected_version: int,
    state: Literal["enabled", "draining"],
) -> m.WorkerRecord:
    """停领 (draining) / 恢复 (enabled)."""
    worker = _worker(rt, worker_id, expected_version)
    rt.store.update_worker(
        rt.operator,
        rt.meta(),
        m.WorkerUpdate(
            worker_id=worker_id,
            expected_version=worker.version,
            address=worker.address,
            port=worker.port,
            network_revision=worker.network_revision,
            workflow_participation=True,
            admission_state=state,
        ),
    )
    return rt.store.get_worker(rt.operator, worker_id).worker


def revoke(
    rt: LocalRuntime,
    worker_id: str,
    expected_version: int,
    fleet: FleetLive | None = None,
) -> None:
    """撤销: the credential stops working; open assignments become unknown and
    the worker leaves the live state at once (IR14)."""
    _worker(rt, worker_id, expected_version)
    rt.store.revoke_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRevocation(
            worker_id=worker_id, expected_version=expected_version, reason="撤销"
        ),
    )
    if fleet is not None:
        fleet.forget(worker_id)


def retire(rt: LocalRuntime, worker_id: str, expected_version: int) -> None:
    """退休: only revoked and with no unresolved owner (the store refuses)."""
    _worker(rt, worker_id, expected_version)
    rt.store.retire_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRetirement(
            worker_id=worker_id, expected_version=expected_version, reason="退休"
        ),
    )


# ── automatic power peers (P2-4 R11) ────────────────────────────────────
def power_peers(host: Any) -> list[Any]:
    """The controller's enrolled workers as 功耗 peers (`host:port` of their
    5680 agent). Retired and revoked workers are dropped, except a revoked
    one that still holds an unresolved assignment: 判定已停止's status
    channel reads its `/status` (C4). Only with 允许 GPU 工作机接入 (§9
    IR11: a phase-1 controller's peers are unchanged)."""
    from taskpaw_v3.core.power_fleet import AutoPeer

    from . import operator_actions as ops

    rt = getattr(host, "runtime", None)
    if host is None or getattr(host, "state", None) != "running" or rt is None:
        return []
    if not getattr(getattr(host, "settings", None), "remote_workers", False):
        return []
    holding = {
        o.assignment.worker_id for o in rt.store.list_open_assignments(rt.operator)
    }
    peers = []
    for worker in rt.store.list_workers(rt.operator):
        if worker.worker_id == rt.identity.worker_id:
            continue
        if worker.enrollment_state == "retired" or (
            worker.admission_state == "revoked" and worker.worker_id not in holding
        ):
            continue
        peers.append(AutoPeer(worker.name, ops.worker_address(worker)))
    return peers


# ── the read ────────────────────────────────────────────────────────────
def _profiles(profiles: tuple[m.ProfileCapability, ...]) -> list[dict[str, Any]]:
    return [
        {
            "stage": stage,
            "tool": p.tool,
            "tool_version": p.tool_version,
            "model": p.model,
            "model_version": p.model_version,
            "profile": p.profile_id,
            "max_width": p.max_width,
            "max_height": p.max_height,
        }
        for p in profiles
        for stage in p.allowed_stages
    ]


def _probe(probe: m.HardwareProbe | None) -> dict[str, Any] | None:
    if probe is None:
        return None
    return {
        "gpu_model": probe.gpu_model,
        "vram_mib": probe.vram_mib,
        "free_disk_bytes": probe.free_disk_bytes,
        "profiles": _profiles(probe.profiles),
        "config_digest": probe.config_digest,
    }


def _synthetic_film(now: datetime) -> m.RoutingFilm:
    """A planned 1080p film with restore and ASR pending: the row's reason."""
    return m.RoutingFilm(
        film_id=str(uuid.UUID(int=0)),
        input_revision=1,
        display_name="1080p",
        created_at=now,
        planned=True,
        confirmed_at=now,
        ltx_model=None,
        pin_worker_id=None,
        pin_version=0,
        restore="pending",
        asr="pending",
        translate="pending",
        max_width=s.FULL_HD[0],
        max_height=s.FULL_HD[1],
        duration_ms=3_600_000,
        source_bytes=0,
    )


def _assignments(
    rt: LocalRuntime,
    worker: m.WorkerRecord,
    pending: list[m.OpenAssignment],
    fleet: FleetLive | None,
    channel: Any,
    now: datetime,
) -> list[dict[str, Any]]:
    """P2-4 §2: the worker's unresolved compute assignments, each with its
    判定已停止 state (`operator_actions.stop_action`)."""
    from . import board
    from . import operator_actions as ops

    found = []
    for item in pending:
        if item.assignment.worker_id != worker.worker_id:
            continue
        film_id = rt.store.get_stage(rt.operator, item.assignment.job_id).film_id
        found.append(
            {
                "assignment_id": item.assignment.assignment_id,
                "version": item.assignment.version,
                "film_id": film_id,
                "film_name": board.name_of(rt, film_id) or "",
                "stage": item.stage,
                "state": item.assignment.status,
                "stop": ops.stop_action(
                    rt, item, worker, fleet=fleet, channel=channel, now=now
                ),
            }
        )
    return found


def device_view(
    rt: LocalRuntime,
    fleet: FleetLive | None,
    *,
    remote_workers: bool,
    channel: Any = None,
) -> dict[str, Any]:
    """本机 and every enrolled, not retired worker (设备 v1). P2-4: each
    row's unresolved assignments and open routes (`route_depth`); `channel`
    is 判定已停止's `/status` channel."""
    now = rt.clock.now()
    op = rt.store
    workers = op.list_workers(rt.operator)
    pending = [
        o
        for o in op.list_open_assignments(rt.operator)
        if o.stage in ("restore", "asr", "translate")
    ]
    depth: dict[str, int] = {}
    for item in op.list_routes(rt.operator):
        depth[item.worker_id] = depth.get(item.worker_id, 0) + 1
    routing = {w.worker_id: w for w in op.routing_workers(rt.operator)}
    controller = rt.identity.worker_id
    remote = s.remote_mode(workers, controller, enabled=remote_workers)
    unresolved: dict[str, int] = {}
    for open_ in op.list_open_assignments(rt.operator):
        key = open_.assignment.worker_id or ""
        unresolved[key] = unresolved.get(key, 0) + 1
    live: Mapping[str, s.WorkerLive] = fleet.snapshot() if fleet is not None else {}
    film = _synthetic_film(now)
    rows = []
    for worker in workers:
        if worker.worker_id == controller or worker.enrollment_state == "retired":
            continue
        cap = worker.capability
        probe = cap.probed if cap else None
        entry = fleet.entry(worker.worker_id) if fleet is not None else None
        beat = entry.beat if entry is not None else None
        approval = op.last_approval(rt.operator, worker.worker_id)
        stages: tuple[str, ...] = (
            ("restore", "asr")
            if probe is not None and probe.vram_mib >= s.RESTORE_MIN_VRAM_MIB
            else ("asr",)
        )
        reason = s.eligibility(
            routing[worker.worker_id], film, stages, live.get(worker.worker_id), now
        )
        rows.append(
            {
                "worker_id": worker.worker_id,
                "version": worker.version,
                "name": worker.name,
                "address": worker.address,
                "port": worker.port,
                "enrollment_state": worker.enrollment_state,
                "admission_state": worker.admission_state,
                "participation": worker.workflow_participation,
                "heartbeat": {  # IR14: no 失联 while remote work is off
                    "state": fleet.state(worker.worker_id, now)
                    if fleet is not None and remote_workers
                    else "off",
                    "age_s": round((now - entry.last_seen).total_seconds())
                    if entry is not None
                    else None,
                },
                "gpu": beat.gpu.model_dump(mode="json", exclude={"schema_version"})
                if beat is not None
                else None,
                "disk": beat.disk.model_dump(mode="json", exclude={"schema_version"})
                if beat is not None
                else None,
                "tiers_ready": list(beat.tiers_ready) if beat is not None else [],
                "asr_ready": beat.asr_ready if beat is not None else False,
                "agent_version": beat.version if beat is not None else None,
                "version_ok": fleet.version_ok(beat)
                if fleet is not None and beat is not None
                else None,
                "boot_reconciled": routing[worker.worker_id].boot_reconciled,
                "eligibility": reason or READY,
                "eligible": reason is None,
                "probe": _probe(probe),
                "approved": None
                if approval is None
                else {
                    "probe": _probe(approval[1]),
                    "profiles": _profiles(approval[0].profiles),
                },
                "restore_allowed": probe is not None
                and probe.vram_mib >= s.RESTORE_MIN_VRAM_MIB,
                "unresolved": unresolved.get(worker.worker_id, 0),
                "assignments": _assignments(rt, worker, pending, fleet, channel, now),
                "route_depth": depth.get(worker.worker_id, 0),
                "can_retire": worker.admission_state == "revoked"
                and not unresolved.get(worker.worker_id, 0),
            }
        )
    order = tuple(STAGE_LABELS)
    executors = [stage for stage in order if stage in rt.executors]
    local_stages = executors if remote is None else ["translate"]
    return {
        "remote_workers": remote_workers,
        "server_time": iso(now),
        "controller": {
            "name": "本机",
            "stages": [STAGE_LABELS.get(stage, stage) for stage in local_stages],
        },
        "workers": rows,
    }
