"""The controller's own workflow runtime: self-enrolment and restart settlement.

T03-P1 §3.1/§3.5/§3.6. The master machine needs no manual pairing: this module
creates or opens the workflow store, enrols the local GPU as a worker and, at
every start, settles what the previous (exited) agent process left behind.
Home-LAN single-user (P0): secrets live in a plain user-profile file.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import threading
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, cast

from pydantic import SecretStr

from taskpaw_v3.core.state import atomic_json

from . import intake_models as im
from . import models as m
from .auth import AuthProof, authenticate, authenticate_pairing, digest_secret
from .executors import INTERRUPTED_KIND, StageExecutor
from .local_clock import MonotonicWallClock, read_high_water
from .local_gpu import FULL_HD, UHD, LocalGpu, machine_identity, query_gpu
from .owned_process import own_identity, process_alive
from .store import WorkflowStore
from .store_core import parse_time

if TYPE_CHECKING:
    from .lookup import MetadataLookup
    from .stage_results import StageResult

log = logging.getLogger("taskpaw.workflow.runtime")

PRODUCER = "taskpaw-local-runtime"
LONG = timedelta(days=3650)
LEASE_SECONDS = 86400
SETTLE_WAIT_SECONDS = 30.0
_RESOURCE_NS = uuid.UUID("5d0f6a3e-2a7c-4c55-9b7e-8a3f2a1d6c01")


def _new_id() -> str:
    return str(uuid.uuid4())


def _secret() -> str:
    return secrets.token_urlsafe(32)


_BACKFILLED = (
    "receiver_component_id",
    "scrape_component_id",
    "archive_component_id",
    "archive_principal_id",
    "cleanup_component_id",
    "cleanup_principal_id",
)


@dataclass
class LocalIdentity:
    """Ids, secrets and fixed idempotency keys, persisted before any command (D11)."""

    authority_id: str
    operator_principal_id: str
    operator_credential_id: str
    operator_secret: str
    worker_id: str
    device_id: str
    worker_credential_id: str
    worker_secret: str
    pairing_id: str
    pairing_ticket: str
    intake_component_id: str
    root_id: str
    work_root_id: str
    created_at: str
    keys: dict[str, str] = field(default_factory=dict)
    # Fixed components of every boot's receiver (T04 D17) and scrape (M01 §4.1)
    # principals; "" in older files.
    receiver_component_id: str = ""
    scrape_component_id: str = ""
    # F02-P1 C9: fixed archive / cleanup principals, re-granted every boot so their
    # open operations survive a restart and exactly one cleanup adapter exists.
    archive_component_id: str = ""
    archive_principal_id: str = ""
    cleanup_component_id: str = ""
    cleanup_principal_id: str = ""

    @classmethod
    def load_or_create(cls, path: Path, now: datetime) -> LocalIdentity:
        if path.exists():
            loaded = cls(**json.loads(path.read_text(encoding="utf-8")))
            missing = [name for name in _BACKFILLED if not getattr(loaded, name)]
            for name in missing:  # back-fill once, then stable
                setattr(loaded, name, _new_id())
            if missing:
                atomic_json(path, asdict(loaded))
            return loaded
        value = cls(
            authority_id=_new_id(),
            operator_principal_id=_new_id(),
            operator_credential_id=_new_id(),
            operator_secret=_secret(),
            worker_id=_new_id(),
            device_id=_new_id(),
            worker_credential_id=_new_id(),
            worker_secret=_secret(),
            pairing_id=_new_id(),
            pairing_ticket=_secret(),
            intake_component_id=_new_id(),
            root_id=_new_id(),
            work_root_id=_new_id(),
            created_at=now.isoformat(),
            keys={
                name: _new_id()
                for name in ("register", "pairing", "complete", "participation")
            },
            **{name: _new_id() for name in _BACKFILLED},
        )
        atomic_json(path, asdict(value))
        return value

    @property
    def created(self) -> datetime:
        return parse_time(self.created_at)  # 'Z' too, on Python 3.10


@dataclass(frozen=True)
class AttemptFile:
    """What a restarted runtime needs about an attempt the store cannot hold."""

    attempt_id: str
    operation_id: str
    job_id: str
    children: tuple[str, ...] = ()
    assignment_id: str = ""


class LocalRuntime:
    def __init__(
        self,
        data_dir: Path,
        executors: Sequence[StageExecutor],
        *,
        gpu: Callable[[], LocalGpu | None] = query_gpu,
        machine: Callable[[], str] = machine_identity,
        clock_source: Callable[[], datetime] | None = None,
        agent_identity: Callable[[], str] = own_identity,
    ) -> None:
        self.data_dir = data_dir
        self.executors = {executor.stage: executor for executor in executors}
        self._gpu, self._machine = gpu, machine
        self._agent_identity = agent_identity
        self.db_path = data_dir / "workflow.db"
        self.attempt_dir = data_dir / "attempts"
        self.work_dir = data_dir / "work"
        floor = read_high_water(self.db_path)
        self.clock = (
            MonotonicWallClock(floor, clock_source)
            if clock_source
            else MonotonicWallClock(floor)
        )
        self.lock = threading.RLock()
        self._store: WorkflowStore | None = None
        self._identity: LocalIdentity | None = None
        self._operator: AuthProof | None = None
        self._worker_auth: AuthProof | None = None
        self._receiver_auth: AuthProof | None = None
        self._scrape_auth: AuthProof | None = None
        self._archive_auth: AuthProof | None = None
        self._cleanup_auth: AuthProof | None = None
        self._boot_id: str | None = None
        self.gpu: LocalGpu | None = None
        self._gpu_read: tuple[LocalGpu | None] | None = None
        self.reason = "starting"
        self.reconciled = False
        self.agent_identity = ""
        # U1 L: the metadata preview the host attaches (None: Miko not configured)
        self.lookup: MetadataLookup | None = None

    # ── opened state ────────────────────────────────────────────────────
    @staticmethod
    def _need(value: Any, what: str) -> Any:
        if value is None:
            raise RuntimeError(f"workflow runtime has no {what} yet")
        return value

    @property
    def store(self) -> WorkflowStore:
        return cast(WorkflowStore, self._need(self._store, "store"))

    @property
    def identity(self) -> LocalIdentity:
        return cast(LocalIdentity, self._need(self._identity, "identity"))

    @property
    def operator(self) -> AuthProof:
        return cast(AuthProof, self._need(self._operator, "operator"))

    @property
    def worker_auth(self) -> AuthProof:
        return cast(AuthProof, self._need(self._worker_auth, "worker credential"))

    @property
    def receiver_auth(self) -> AuthProof:
        return cast(AuthProof, self._need(self._receiver_auth, "receiver credential"))

    @property
    def scrape_auth(self) -> AuthProof:
        return cast(AuthProof, self._need(self._scrape_auth, "scrape credential"))

    @property
    def archive_auth(self) -> AuthProof:
        return cast(AuthProof, self._need(self._archive_auth, "archive credential"))

    @property
    def cleanup_auth(self) -> AuthProof:
        return cast(AuthProof, self._need(self._cleanup_auth, "cleanup credential"))

    @property
    def boot_id(self) -> str:
        return cast(str, self._need(self._boot_id, "boot"))

    @boot_id.setter
    def boot_id(self, value: str) -> None:
        self._boot_id = value

    # ── store and identity ──────────────────────────────────────────────
    def open(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        now = self.clock.now()
        self._identity = ident = LocalIdentity.load_or_create(
            self.data_dir / "local-identity.json", now
        )
        if self.db_path.exists():
            self._store = WorkflowStore.open(self.db_path, clock=self.clock)
        else:
            tls = m.TlsBinding(
                authority_id=ident.authority_id,
                certificate_sha256="0" * 64,  # P1 serves no HTTPS worker API
                binding_revision=1,
            )
            self._store = WorkflowStore.create(
                self.db_path,
                m.AuthorityBootstrap(
                    authority_id=ident.authority_id,
                    epoch=1,
                    operator_principal_id=ident.operator_principal_id,
                    operator_credential_id=ident.operator_credential_id,
                    operator_secret_digest=digest_secret(
                        SecretStr(ident.operator_secret)
                    ),
                    operator_expires_at=ident.created + LONG,
                    tls=tls,
                    lease_seconds=LEASE_SECONDS,
                ),
                clock=self.clock,
            )
        self.store.controller_id = ident.worker_id  # P2-1 IR4/IR9
        self._operator = authenticate(
            self.store, ident.operator_credential_id, SecretStr(ident.operator_secret)
        )
        self.agent_identity = self._agent_identity()

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def meta(self, key: str | None = None) -> m.CommandMeta:
        return m.CommandMeta(
            request_id=_new_id(),
            idempotency_key=key or _new_id(),
            authority_id=self.identity.authority_id,
            authority_epoch=1,
        )

    def evidence(
        self,
        kind: str,
        note: str | None = None,
        *,
        producer: AuthProof | None = None,
        payload_digest: str | None = None,
        item_ids: tuple[str, ...] = (),
        timing: m.RestoreTiming | None = None,
        energy: m.AttemptEnergy | None = None,
    ) -> m.EvidenceRef:
        """Passed evidence, signed by the operator unless another producer is named."""
        now = self.clock.now()
        return m.EvidenceRef(
            evidence_id=_new_id(),
            kind=kind,
            producer_id=(producer or self.operator).principal_id,
            producer_version=PRODUCER,
            observed_at=now,
            received_at=now,
            payload_digest=payload_digest
            or m.digest({"kind": kind, "note": note or ""}),
            status="passed",
            summary=m.EvidenceSummary(
                note=note, item_ids=item_ids, timing=timing, energy=energy
            ),
        )

    def stage_result(self, film_id: str, stage: str) -> StageResult | None:
        """`ExecRuntime`: the store's verified ledger of the film's `stage`."""
        from .stage_results import store_stage_result

        return store_stage_result(self, film_id, stage)

    def worker_view(self) -> m.WorkerView:
        return self.store.get_worker(self.operator, self.identity.worker_id)

    def worker(self) -> m.WorkerRecord:
        return self.worker_view().worker

    # ── enrolment ───────────────────────────────────────────────────────
    def ensure_worker(self) -> m.WorkerRecord:
        """Idempotently enrol, probe and (when possible) approve the local GPU."""
        store, ident, op = self.store, self.identity, self.operator
        try:
            current = self.worker()
        except m.WorkflowError as exc:
            if str(exc) != "not_found":
                raise
            store.register_worker(
                op,
                self.meta(ident.keys["register"]),
                m.WorkerRegistration(
                    worker_id=ident.worker_id,
                    device_id=ident.device_id,
                    name="本机",
                    address="127.0.0.1",
                    port=5682,
                ),
            )
            current = self.worker()
        if current.enrollment_state == "pending_pairing":
            store.issue_pairing(
                op,
                self.meta(ident.keys["pairing"]),
                m.PairingIssue(
                    pairing_id=ident.pairing_id,
                    worker_id=ident.worker_id,
                    device_id=ident.device_id,
                    ticket_digest=digest_secret(
                        SecretStr(ident.pairing_ticket), purpose="pairing"
                    ),
                    expires_at=ident.created + LONG,
                ),
            )
            proof = authenticate_pairing(
                store,
                ident.pairing_id,
                SecretStr(ident.pairing_ticket),
                SecretStr(ident.worker_secret),
            )
            store.complete_pairing(
                proof,
                self.meta(ident.keys["complete"]),
                m.PairingComplete(
                    pairing_id=ident.pairing_id,
                    worker_id=ident.worker_id,
                    device_id=ident.device_id,
                    credential_id=ident.worker_credential_id,
                    secret_digest=digest_secret(SecretStr(ident.worker_secret)),
                    generation=1,
                    expires_at=ident.created + LONG,
                ),
            )
            current = self.worker()
        self._worker_auth = authenticate(
            store, ident.worker_credential_id, SecretStr(ident.worker_secret)
        )
        self._ensure_capability(current)
        current = self.worker()
        if current.enrollment_state == "verified" and (
            not current.workflow_participation or current.admission_state != "enabled"
        ):
            store.update_worker(
                op,
                self.meta(),
                m.WorkerUpdate(
                    worker_id=current.worker_id,
                    expected_version=current.version,
                    address=current.address,
                    port=current.port,
                    network_revision=current.network_revision,
                    workflow_participation=True,
                    admission_state="enabled",
                ),
            )
            current = self.worker()
        return current

    def profiles(self, vram_mib: int) -> tuple[m.ProfileCapability, ...]:
        values = []
        for stage, executor in sorted(self.executors.items()):
            tools = executor.tools()
            width, height = executor.max_resolution(vram_mib)
            values.append(
                m.ProfileCapability(
                    profile_id=tools.profile,
                    profile_revision=tools.profile_revision,
                    tool=tools.tool,
                    tool_version=tools.tool_version,
                    model=tools.model,
                    model_version=tools.model_version,
                    allowed_stages=(stage,),
                    max_width=width,
                    max_height=height,
                )
            )
        return tuple(values)

    def boot_gpu(self) -> LocalGpu | None:
        """The local GPU, read once for the boot: the executor factory (restore
        preset) and the capability probe share this one nvidia-smi call."""
        with self.lock:
            if self._gpu_read is None:
                self._gpu_read = (self._gpu(),)
            return self._gpu_read[0]

    def _ensure_capability(self, current: m.WorkerRecord) -> None:
        self.gpu = gpu = self.boot_gpu()
        if gpu is None:
            self.reason = "无法读取本机 GPU（nvidia-smi）"
            return
        device_identity = self._machine()
        profiles = self.profiles(gpu.vram_mib)
        old = current.capability
        if (
            old is not None
            and old.probed is not None
            and (
                old.probed.gpu_identity,
                old.probed.device_identity,
                old.probed.vram_mib,
            )
            == (gpu.uuid, device_identity, gpu.vram_mib)
            and (old.approved.profiles if old.approved else ()) == profiles
            and (old.approved is not None or not profiles)
        ):
            self.reason = "ready" if profiles else "没有可用的执行器"
            return
        revision = (old.revision if old else 0) + 1
        now = self.clock.now()
        probe = m.HardwareProbe(
            revision=revision,
            source="nvidia-smi",
            observed_at=now,
            cpu="local",
            ram_mib=1,
            gpu_model=gpu.name,
            vram_mib=gpu.vram_mib,
            evidence=self.evidence("hardware_probe"),
            device_identity=device_identity,
            gpu_identity=gpu.uuid,
            free_disk_bytes=0,
            model_ready=bool(profiles),
        )
        approved = (
            m.ApprovedCapability(
                revision=revision,
                probe_revision=revision,
                approved_by=self.operator.principal_id,
                approved_at=now,
                profiles=profiles,
            )
            if profiles
            else None
        )
        binding = m.PhysicalGpuBinding(
            resource_id=str(uuid.uuid5(_RESOURCE_NS, f"{device_identity}|{gpu.uuid}")),
            device_id=current.device_id,
            device_identity=device_identity,
            gpu_identity=gpu.uuid,
            capability_revision=revision,
            evidence=self.evidence("physical_identity"),
        )
        self.store.record_capabilities(
            self.operator,
            self.meta(),
            m.CapabilityUpdate(
                worker_id=current.worker_id,
                expected_version=current.version,
                capability=m.CapabilityRevision(
                    revision=revision, declared=None, probed=probe, approved=approved
                ),
                physical_binding=binding,
            ),
        )
        self.reason = "ready" if profiles else "没有可用的执行器"

    # ── boot and settlement ─────────────────────────────────────────────
    def start_boot(self) -> None:
        """New boot, settle what the exited previous boot left, then reconcile."""
        self._boot_id = _new_id()
        self._record_boot()
        _, self._receiver_auth = self._grant("receiver_adapter")
        _, self._scrape_auth = self._grant("scrape_adapter")
        self._archive_auth = self._grant_fixed("archive_adapter")
        self._cleanup_auth = self._grant_fixed("cleanup_adapter")
        self.settle_previous()

    def _record_boot(self) -> None:
        current = self.worker()
        self.store.record_boot(
            self.worker_auth,
            self.meta(),
            m.BootObservation(
                worker_id=current.worker_id,
                expected_version=current.version,
                boot_id=self.boot_id,
                known_epoch=1,
                assignment_ids=(),
                local_owners=(),
            ),
        )

    def attempt_path(self, attempt_id: str) -> Path:
        return self.attempt_dir / f"{attempt_id}.json"

    def write_attempt(self, value: AttemptFile) -> None:
        atomic_json(
            self.attempt_path(value.attempt_id),
            {**asdict(value), "children": list(value.children)},
        )

    def read_attempt(self, attempt_id: str) -> AttemptFile | None:
        """None when absent or unreadable (R4).

        A torn file only follows an OS crash or power loss, which also ended
        every child, so "no recorded children" is the true state then.
        """
        path = self.attempt_path(attempt_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return AttemptFile(
                attempt_id=data["attempt_id"],
                operation_id=data["operation_id"],
                job_id=data["job_id"],
                children=tuple(data.get("children", ())),
                assignment_id=data.get("assignment_id", ""),
            )
        except (ValueError, KeyError, TypeError):
            log.warning("unreadable workflow attempt record %s ignored", path.name)
            return None

    def drop_attempt(self, attempt_id: str) -> None:
        try:
            self.attempt_path(attempt_id).unlink()
        except FileNotFoundError:
            pass

    def settle_previous(self, wait: float = SETTLE_WAIT_SECONDS) -> bool:
        """Resolve assignments of earlier boots; True when the boot is reconciled.

        The earlier agent process has exited (this one holds the store lock), so
        its kill-on-close Jobs are closed. A recorded child that still runs (for
        example stuck in a driver after a stop timeout) keeps its owner (D13).
        """
        with self.lock:
            pending = [
                open_.assignment
                for open_ in self.store.list_open_assignments(self.operator)
                if open_.assignment.boot_id != self.boot_id and self._ours(open_)
            ]
            blocked = False
            for assignment in pending:
                record = self.read_attempt(assignment.attempt_id)
                children = record.children if record else ()
                if not self._wait_gone(children, wait):
                    blocked = True
                    continue
                self._resolve_interrupted(assignment, record)
            if blocked:
                self.reason = "上次的任务进程仍在运行"
                self.reconciled = False
                return False
            self._record_boot()
            self.reconciled = True
            return True

    def _ours(self, open_: m.OpenAssignment) -> bool:
        """Only this runtime's work: GPU worker, receiver or scrape (T04 D12, M01)."""
        assignment, ident = open_.assignment, self.identity
        return assignment.worker_id == ident.worker_id or (
            (open_.stage, open_.owner_component_id)
            in (
                ("return", ident.receiver_component_id),
                ("scrape", ident.scrape_component_id),
            )
        )

    def open_assignment(self, assignment_id: str) -> m.AssignmentRecord:
        for open_ in self.store.list_open_assignments(self.operator):
            if open_.assignment.assignment_id == assignment_id:
                return open_.assignment
        raise m.WorkflowError("not_found")

    @staticmethod
    def _wait_gone(children: Sequence[str], wait: float) -> bool:
        import time

        deadline = time.monotonic() + wait
        while any(process_alive(child) for child in children):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)
        return True

    def resolve_interrupted(self, assignment: m.AssignmentRecord) -> None:
        """P2-3 §5: a remote attempt whose worker restarted with its children
        gone and nothing to show: failed `interrupted`, then a free retry."""
        with self.lock:
            self._resolve_interrupted(assignment, None)

    def _resolve_interrupted(
        self, assignment: m.AssignmentRecord, record: AttemptFile | None
    ) -> None:
        film_job = self._job(assignment.job_id)
        outcome = (
            film_job.status
            if film_job is not None and film_job.status in ("succeeded", "failed")
            else "failed"
        )
        owner = assignment.local_owner or m.LocalOwnerEvidence(
            owner_id=_new_id(),
            generation=1,
            boot_id=assignment.boot_id,
            process_identity="unknown",
            state="stopped",
            evidence=self.evidence("native_owner"),
        )
        operation_id = record.operation_id if record else self._operation_id(assignment)
        self.store.resolve_assignment(
            self.operator,
            self.meta(),
            m.AssignmentResolution(
                assignment_id=assignment.assignment_id,
                attempt_id=assignment.attempt_id,
                expected_version=assignment.version,
                reason="上一次运行被中断",
                operation_id=operation_id,
                local_owner=owner.model_copy(
                    update={
                        "state": "stopped",
                        "evidence": self.evidence("native_owner_stopped"),
                    }
                ),
                evidence=(self.evidence(INTERRUPTED_KIND, "上一次运行被中断"),),
                outcome=outcome,
            ),
        )
        self.drop_attempt(assignment.attempt_id)
        if outcome == "failed":
            job = self._job(assignment.job_id)
            if job is not None and job.status == "failed":
                self.store.retry_stage(
                    self.operator,
                    self.meta(),
                    m.StageRetry(
                        job_id=job.job_id,
                        expected_version=job.version,
                        reason="上一次运行被中断，自动继续",
                    ),
                )

    def _job(self, job_id: str) -> m.StageRecord | None:
        try:
            return self.store.get_stage(self.operator, job_id)
        except m.WorkflowError:
            return None

    def _operation_id(self, assignment: m.AssignmentRecord) -> str:
        (attempt,) = [
            a
            for a in self.store.list_attempts(self.operator, assignment.job_id)
            if a.attempt_id == assignment.attempt_id
        ]
        return attempt.operation_id

    # ── intake ──────────────────────────────────────────────────────────
    def grant_intake_adapter(self) -> tuple[m.ComponentGrant, AuthProof]:
        """A fresh intake principal for this boot (same component, D01)."""
        return self._grant("intake_adapter")

    def _grant(
        self, role: Literal["intake_adapter", "receiver_adapter", "scrape_adapter"]
    ) -> tuple[m.ComponentGrant, AuthProof]:
        """A fresh principal of a fixed component for this boot."""
        component = {
            "intake_adapter": self.identity.intake_component_id,
            "receiver_adapter": self.identity.receiver_component_id,
            "scrape_adapter": self.identity.scrape_component_id,
        }[role]
        secret = SecretStr(_secret())
        grant = m.ComponentGrant(
            component_id=component,
            principal_id=_new_id(),
            credential_id=_new_id(),
            boot_id=self.boot_id,
            generation=1,
            role=role,
            secret_digest=digest_secret(secret),
            expires_at=self.clock.now() + LONG,
        )
        self.store.grant_component(self.operator, self.meta(), grant)
        return grant, authenticate(self.store, grant.credential_id, secret)

    def _grant_fixed(
        self, role: Literal["archive_adapter", "cleanup_adapter"]
    ) -> AuthProof:
        """A new credential (and boot) for the same principal every boot (F02-P1 C9)."""
        ident = self.identity
        component, principal = {
            "archive_adapter": (ident.archive_component_id, ident.archive_principal_id),
            "cleanup_adapter": (ident.cleanup_component_id, ident.cleanup_principal_id),
        }[role]
        secret = SecretStr(_secret())
        grant = m.ComponentGrant(
            component_id=component,
            principal_id=principal,
            credential_id=_new_id(),
            boot_id=self.boot_id,
            generation=self.store.component_generation(self.operator, principal) + 1,
            role=role,
            secret_digest=digest_secret(secret),
            expires_at=self.clock.now() + LONG,
        )
        self.store.grant_component(self.operator, self.meta(), grant)
        return authenticate(self.store, grant.credential_id, secret)

    def settle_intake(self) -> int:
        """Cancel intake operations of exited boots and drop their private copies."""
        settled = 0
        for op in self.store.list_unresolved_intake_operations(self.operator):
            if op.owner_boot_id == self.boot_id:
                continue
            self.store.settle_abandoned_intake(
                self.operator,
                self.meta(),
                im.IntakeAbandonedSettlement(
                    expected_version=op.version,
                    operation_id=op.operation_id,
                    current_owner_boot_id=self.boot_id,
                ),
            )
            shutil.rmtree(
                self.work_dir / "intake" / op.operation_id, ignore_errors=True
            )
            settled += 1
        return settled


def intake_profiles(approval: m.EvidenceRef) -> tuple[im.IntakeProfile, ...]:
    """What intake admits as processable; device limits are applied at claim (C1)."""
    common: dict[str, Any] = dict(
        policy_revision=1,
        tool="taskpaw-workflow",
        tool_version="1",
        min_width=1,
        min_height=1,
        min_fps={"numerator": 1, "denominator": 1},
        max_fps={"numerator": 120, "denominator": 1},
        codecs=("h264", "hevc", "mpeg4", "mpeg2video", "vc1", "wmv3", "vp9", "av1"),
        pixel_formats=(
            "yuv420p",
            "yuvj420p",
            "yuv420p10le",
            "nv12",
            "yuv422p",
            "yuv444p",
        ),
        field_orders=("progressive", "unknown"),
        sample_aspect_ratios=({"numerator": 1, "denominator": 1},),
        rotations=(0,),
        approval=approval,
    )
    return (
        im.IntakeProfile(
            profile_id="full-hd",
            profile_revision=1,
            max_width=FULL_HD[0],
            max_height=FULL_HD[1],
            **common,
        ),
        im.IntakeProfile(
            profile_id="uhd",
            profile_revision=1,
            max_width=UHD[0],
            max_height=UHD[1],
            **common,
        ),
    )
