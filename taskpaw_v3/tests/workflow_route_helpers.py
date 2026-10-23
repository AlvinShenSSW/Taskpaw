"""Remote fake workers in the controller's store (P2-1): enrol, route, claim,
resolve and crash them through the public store commands only."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from pydantic import SecretStr

from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.auth import (
    AuthProof,
    authenticate,
    authenticate_pairing,
    digest_secret,
)
from taskpaw_v3.workflow.executors import SyntheticExecutor, failed_kind
from taskpaw_v3.workflow.local_gpu import UHD
from taskpaw_v3.workflow.local_runtime import LocalRuntime

GPU_16 = ("NVIDIA GeForce RTX 5060 Ti", 16311)
GPU_16_FAST = ("NVIDIA GeForce RTX 5070 Ti", 16303)
GPU_8 = ("NVIDIA GeForce RTX 5060", 8151)
ATTESTED = "operator_attested_stopped"
QUIET = timedelta(minutes=5, seconds=1)  # 判定已停止 needs 5 min without a renewal


class StepClock:
    """Real time plus an offset that only grows (the store refuses a step back)."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def advance(self, delta: timedelta = QUIET) -> None:
        self.offset += delta


@dataclass(frozen=True)
class Remote:
    worker_id: str
    name: str
    auth: AuthProof
    boot_id: str
    resource_id: str


def _secret() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


def enrol_remote(
    rt: LocalRuntime,
    name: str,
    *,
    gpu: tuple[str, int] = GPU_16,
    stages: tuple[str, ...] = ("restore", "asr"),
    limit: tuple[int, int] = UHD,
    address: str = "192.0.2.30",
) -> Remote:
    """Register, pair, probe, approve, enable and boot one remote worker."""
    store, op, now = rt.store, rt.operator, rt.clock.now()
    worker_id, device_id = identity(), identity()
    store.register_worker(
        op,
        rt.meta(),
        m.WorkerRegistration(
            worker_id=worker_id,
            device_id=device_id,
            name=name,
            address=address,
            port=5682,
        ),
    )
    ticket, secret, pairing_id, credential_id = (
        _secret(),
        _secret(),
        identity(),
        identity(),
    )
    store.issue_pairing(
        op,
        rt.meta(),
        m.PairingIssue(
            pairing_id=pairing_id,
            worker_id=worker_id,
            device_id=device_id,
            ticket_digest=digest_secret(ticket, purpose="pairing"),
            expires_at=now + timedelta(days=1),
        ),
    )
    store.complete_pairing(
        authenticate_pairing(store, pairing_id, ticket, secret),
        rt.meta(),
        m.PairingComplete(
            pairing_id=pairing_id,
            worker_id=worker_id,
            device_id=device_id,
            credential_id=credential_id,
            secret_digest=digest_secret(secret),
            generation=1,
            expires_at=now + timedelta(days=3650),
        ),
    )
    auth = authenticate(store, credential_id, secret)
    profiles = []
    for stage in stages:
        tools = SyntheticExecutor(stage).tools()  # type: ignore[arg-type]
        profiles.append(
            m.ProfileCapability(
                profile_id=tools.profile,
                profile_revision=tools.profile_revision,
                tool=tools.tool,
                tool_version=tools.tool_version,
                model=tools.model,
                model_version=tools.model_version,
                allowed_stages=(stage,),  # type: ignore[arg-type]
                max_width=limit[0],
                max_height=limit[1],
            )
        )
    probe = m.HardwareProbe(
        revision=1,
        source="synthetic",
        observed_at=now,
        cpu="synthetic",
        ram_mib=32768,
        gpu_model=gpu[0],
        vram_mib=gpu[1],
        evidence=rt.evidence("hardware_probe"),
        device_identity=f"device-{worker_id}",
        gpu_identity=f"gpu-{worker_id}",
        free_disk_bytes=10**12,
        model_ready=True,
    )
    resource_id = identity()
    worker = store.get_worker(op, worker_id).worker
    store.record_capabilities(
        op,
        rt.meta(),
        m.CapabilityUpdate(
            worker_id=worker_id,
            expected_version=worker.version,
            capability=m.CapabilityRevision(
                revision=1,
                declared=None,
                probed=probe,
                approved=m.ApprovedCapability(
                    revision=1,
                    probe_revision=1,
                    approved_by=op.principal_id,
                    approved_at=now,
                    profiles=tuple(profiles),
                ),
            ),
            physical_binding=m.PhysicalGpuBinding(
                resource_id=resource_id,
                device_id=device_id,
                device_identity=probe.device_identity,
                gpu_identity=probe.gpu_identity,
                capability_revision=1,
                evidence=rt.evidence("physical_identity"),
            ),
        ),
    )
    worker = store.get_worker(op, worker_id).worker
    store.update_worker(
        op,
        rt.meta(),
        m.WorkerUpdate(
            worker_id=worker_id,
            expected_version=worker.version,
            address=worker.address,
            port=worker.port,
            network_revision=worker.network_revision,
            workflow_participation=True,
            admission_state="enabled",
        ),
    )
    remote = Remote(worker_id, name, auth, "", resource_id)
    return boot(rt, remote, identity())


def swap_gpu(rt: LocalRuntime, remote: Remote, gpu: tuple[str, int]) -> Remote:
    """A new card in the same machine: a new probe, physical GPU and approval."""
    store, op, now = rt.store, rt.operator, rt.clock.now()
    worker = store.get_worker(op, remote.worker_id).worker
    cap = worker.capability
    assert cap is not None and cap.probed is not None and cap.approved is not None
    revision = cap.revision + 1
    probe = cap.probed.model_copy(
        update={
            "revision": revision,
            "observed_at": now,
            "gpu_model": gpu[0],
            "vram_mib": gpu[1],
            "gpu_identity": f"gpu-{identity()}",
        }
    )
    resource_id = identity()
    store.record_capabilities(
        op,
        rt.meta(),
        m.CapabilityUpdate(
            worker_id=remote.worker_id,
            expected_version=worker.version,
            capability=m.CapabilityRevision(
                revision=revision,
                declared=None,
                probed=probe,
                approved=cap.approved.model_copy(
                    update={
                        "revision": revision,
                        "probe_revision": revision,
                        "approved_at": now,
                    }
                ),
            ),
            physical_binding=m.PhysicalGpuBinding(
                resource_id=resource_id,
                device_id=worker.device_id,
                device_identity=probe.device_identity,
                gpu_identity=probe.gpu_identity,
                capability_revision=revision,
                evidence=rt.evidence("physical_identity"),
            ),
        ),
    )
    return replace(remote, resource_id=resource_id)


def boot(rt: LocalRuntime, remote: Remote, boot_id: str) -> Remote:
    """Post a boot (a new id isolates the worker's open assignments)."""
    worker = rt.store.get_worker(rt.operator, remote.worker_id).worker
    rt.store.record_boot(
        remote.auth,
        rt.meta(),
        m.BootObservation(
            worker_id=remote.worker_id,
            expected_version=worker.version,
            boot_id=boot_id,
            known_epoch=1,
            assignment_ids=(),
            local_owners=(),
        ),
    )
    return replace(remote, boot_id=boot_id)


def pin_version(rt: LocalRuntime, film_id: str, revision: int = 1) -> int:
    pin = rt.store.get_pin(rt.operator, film_id, revision)
    return 0 if pin is None else pin.version


def route(
    rt: LocalRuntime,
    film_id: str,
    worker_id: str,
    *,
    kind: str = "auto",
    revision: int = 1,
    reason: str = "测试",
) -> m.CommandResult:
    return rt.store.route_film(
        rt.operator,
        rt.meta(),
        m.RouteOpen(
            route_id=identity(),
            film_id=film_id,
            input_revision=revision,
            worker_id=worker_id,
            kind=kind,  # type: ignore[arg-type]
            expected_pin_version=pin_version(rt, film_id, revision),
            reason=reason,
        ),
    )


def job(rt: LocalRuntime, film_id: str, name: str) -> m.StageRecord:
    return next(
        s for s in rt.store.get_film(rt.operator, film_id).stages if s.stage == name
    )


def claim_remote(
    rt: LocalRuntime, remote: Remote, film_id: str, name: str
) -> tuple[m.CommandResult, m.ClaimRequest]:
    stage = job(rt, film_id, name)
    tools = SyntheticExecutor(name).tools()  # type: ignore[arg-type]
    command = m.ClaimRequest(
        job_id=stage.job_id,
        expected_version=stage.version,
        attempt_id=identity(),
        operation_id=identity(),
        worker_id=remote.worker_id,
        boot_id=remote.boot_id,
        profile_revision=tools.profile_revision,
        config_digest=tools.config_digest,
        resource_id=remote.resource_id,
        tool_versions=tools,
    )
    return rt.store.claim(remote.auth, rt.meta(), command), command


def assignment(rt: LocalRuntime, assignment_id: str) -> m.AssignmentRecord:
    return next(
        o.assignment
        for o in rt.store.list_open_assignments(rt.operator)
        if o.assignment.assignment_id == assignment_id
    )


def resolve_remote(
    rt: LocalRuntime,
    remote: Remote,
    claimed: tuple[m.CommandResult, m.ClaimRequest],
    outcome: str = "succeeded",
    *,
    note: str | None = None,
    timing: m.RestoreTiming | None = None,
) -> m.CommandResult:
    assert claimed[0].assignment_id is not None
    return resolve_open(rt, claimed[0].assignment_id, outcome, note=note, timing=timing)


def operation_of(rt: LocalRuntime, current: m.AssignmentRecord) -> str:
    return next(
        a.operation_id
        for a in rt.store.list_attempts(rt.operator, current.job_id)
        if a.attempt_id == current.attempt_id
    )


def resolve_open(
    rt: LocalRuntime,
    assignment_id: str,
    outcome: str = "succeeded",
    *,
    note: str | None = None,
    timing: m.RestoreTiming | None = None,
) -> m.CommandResult:
    """The controller's verified finish: the worker's owner stopped, then resolve.
    `note` and `timing` are the executor's outcome message and Jasna timing."""
    current = assignment(rt, assignment_id)
    kind = "exec_succeeded" if outcome == "succeeded" else failed_kind("transient")
    return rt.store.resolve_assignment(
        rt.operator,
        rt.meta(),
        m.AssignmentResolution(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="远程完成",
            operation_id=operation_of(rt, current),
            local_owner=m.LocalOwnerEvidence(
                owner_id=identity(),
                generation=1,
                boot_id=current.boot_id,
                process_identity="remote-synthetic",
                state="stopped",
                evidence=rt.evidence("native_owner_stopped"),
            ),
            evidence=(rt.evidence(kind, note, timing=timing),),
            outcome=outcome,  # type: ignore[arg-type]
        ),
    )


def attest(rt: LocalRuntime, assignment_id: str) -> m.CommandResult:
    """判定已停止 by the operator."""
    current = assignment(rt, assignment_id)
    return rt.store.attest_stopped(
        rt.operator,
        rt.meta(),
        m.StoppedAttestation(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            operation_id=operation_of(rt, current),
            reason="我确认该机器已关机或任务已停止",
            evidence=(
                rt.evidence(ATTESTED, "我确认该机器已关机或任务已停止"),
                rt.evidence(failed_kind("cancelled"), "判定已停止"),
            ),
        ),
    )


def finish_remote(
    rt: LocalRuntime, remote: Remote, claimed: tuple[m.CommandResult, m.ClaimRequest]
) -> m.CommandResult:
    """The worker's own success report (business fact), its owner still running."""
    result, command = claimed
    assert result.assignment_id is not None
    current = assignment(rt, result.assignment_id)
    stage = rt.store.get_stage(rt.operator, command.job_id)
    return rt.store.finish_attempt(
        remote.auth,
        rt.meta(),
        m.AttemptResult(
            assignment_id=current.assignment_id,
            expected_version=current.version,
            worker_id=remote.worker_id,
            boot_id=remote.boot_id,
            attempt_id=command.attempt_id,
            authority_epoch=current.authority_epoch,
            fencing_token=current.fencing_token,
            local_owner=None,
            job_id=command.job_id,
            input_hash=stage.input_hash,
            outcome="succeeded",
            result_manifest_hash=None,
            artifact_receipt_id=None,
            evidence=(rt.evidence("exec_succeeded", producer=remote.auth),),
            error_code=None,
        ),
    )
