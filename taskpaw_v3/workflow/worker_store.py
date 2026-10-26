"""Credential and device enrollment commands, independent of monitoring records."""

from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import datetime
from uuid import UUID, uuid5

from .auth import AuthProof, PairingProof
from .models import (
    AssignmentRecord,
    AttemptRecord,
    AuditPage,
    AuditRecord,
    BootObservation,
    CapabilityUpdate,
    ClaimRequest,
    CommandMeta,
    CommandResult,
    ComponentGrant,
    CredentialRevocation,
    InputManifest,
    Model,
    PairingComplete,
    PairingIssue,
    StageRecord,
    WorkerAttempt,
    WorkerRecord,
    WorkerRegistration,
    WorkerRetirement,
    WorkerRevocation,
    WorkerSelf,
    WorkerUpdate,
    WorkerView,
    WorkflowError,
    canonical_json,
    digest,
)
from .read_store import ReadCommands
from .store_core import Mutation, parse_time, timestamp


class WorkerCommands(ReadCommands):
    def _worker(self, worker_id: str) -> WorkerRecord:
        return WorkerRecord.model_validate_json(
            self._one("SELECT body_json FROM workers WHERE worker_id=?", (worker_id,))[
                0
            ]
        )

    def _save_worker(self, worker: WorkerRecord, expected_version: int) -> int:
        return self._update(
            "workers",
            "worker_id",
            worker.worker_id,
            expected_version,
            address=worker.address,
            port=worker.port,
            network_revision=worker.network_revision,
            participation=int(worker.workflow_participation),
            authorization_revision=worker.authorization_revision,
            enrollment_state=worker.enrollment_state,
            admission_state=worker.admission_state,
            current_boot_id=worker.boot_id,
            approved_capability_revision=worker.capability.approved.revision
            if worker.capability and worker.capability.approved
            else None,
            body_json=canonical_json(worker),
        )

    def register_worker(
        self, auth: AuthProof, meta: CommandMeta, command: WorkerRegistration
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            if command.workflow_participation:
                raise WorkflowError("precondition_failed")
            worker = WorkerRecord(
                **command.model_dump(),
                authorization_revision=1,
                enrollment_state="pending_pairing",
                admission_state="disabled",
                boot_id=None,
                version=1,
            )
            self._insert(
                "workers",
                worker_id=worker.worker_id,
                device_id=worker.device_id,
                monitoring_reference=worker.monitoring_reference,
                name=worker.name,
                address=worker.address,
                port=worker.port,
                network_revision=worker.network_revision,
                participation=0,
                role=worker.role,
                authorization_revision=1,
                enrollment_state=worker.enrollment_state,
                admission_state=worker.admission_state,
                body_json=canonical_json(worker),
            )
            return Mutation(worker.worker_id, 1, worker.enrollment_state)

        return self._execute(auth, meta, command, "register_worker", apply)

    def update_worker(
        self, auth: AuthProof, meta: CommandMeta, command: WorkerUpdate
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            old = self._worker(command.worker_id)
            if (
                old.enrollment_state == "retired"
                or old.admission_state == "revoked"
                or command.admission_state == "revoked"
            ):
                raise WorkflowError("precondition_failed")
            network_changed = (old.address, old.port) != (command.address, command.port)
            if command.network_revision != old.network_revision + int(network_changed):
                raise WorkflowError("precondition_failed")
            if command.admission_state == "enabled" and (
                not command.workflow_participation or old.enrollment_state != "verified"
            ):
                raise WorkflowError("precondition_failed")
            worker = old.model_copy(
                update={
                    "address": command.address,
                    "port": command.port,
                    "network_revision": command.network_revision,
                    "workflow_participation": command.workflow_participation,
                    "admission_state": command.admission_state,
                    "authorization_revision": old.authorization_revision + 1,
                    "version": command.expected_version + 1,
                }
            )
            version = self._save_worker(worker, command.expected_version)
            return Mutation(worker.worker_id, version, worker.admission_state)

        return self._execute(auth, meta, command, "update_worker", apply)

    def record_capabilities(
        self, auth: AuthProof, meta: CommandMeta, command: CapabilityUpdate
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            old = self._worker(command.worker_id)
            cap = command.capability
            if old.enrollment_state == "retired" or old.admission_state == "revoked":
                raise WorkflowError("precondition_failed")
            if principal["role"] == "compute_worker" and (
                principal["worker_id"] != old.worker_id or cap.approved is not None
            ):
                raise WorkflowError("forbidden")
            if old.capability and cap.revision <= old.capability.revision:
                raise WorkflowError("cas_conflict")
            if not any((cap.declared, cap.probed, cap.approved)):
                raise WorkflowError("invalid_request")
            binding = command.physical_binding
            if binding:
                probe = cap.probed
                if (
                    probe is None
                    or probe.evidence.status != "passed"
                    or binding.evidence.status != "passed"
                    or binding.device_id != old.device_id
                    or binding.capability_revision != cap.revision
                    or (binding.device_identity, binding.gpu_identity)
                    != (probe.device_identity, probe.gpu_identity)
                ):
                    raise WorkflowError("precondition_failed")
                physical_hash = digest(
                    {
                        "device_identity": binding.device_identity,
                        "gpu_identity": binding.gpu_identity,
                    }
                )
                existing = self._conn.execute(
                    "SELECT * FROM physical_resources WHERE identity_hash=? OR physical_resource_id=?",
                    (physical_hash, binding.resource_id),
                ).fetchall()
                if existing:
                    if (
                        len(existing) != 1
                        or existing[0]["identity_hash"] != physical_hash
                        or existing[0]["physical_resource_id"] != binding.resource_id
                    ):
                        raise WorkflowError("owner_unresolved")
                else:
                    self._insert(
                        "physical_resources",
                        physical_resource_id=binding.resource_id,
                        device_id=old.device_id,
                        identity_hash=physical_hash,
                        body_json=canonical_json(binding),
                    )
            if cap.approved:
                if (
                    principal["role"] != "operator"
                    or cap.approved.approved_by != principal["principal_id"]
                    or binding is None
                    or old.enrollment_state == "pending_pairing"
                    or not cap.probed
                    or not cap.probed.model_ready
                ):
                    raise WorkflowError("precondition_failed")
            for kind, value in (
                ("declared", cap.declared),
                ("probed", cap.probed),
                ("approved", cap.approved),
            ):
                if value is not None:
                    existing_cap = self._conn.execute(
                        "SELECT body_json FROM capabilities WHERE worker_id=? AND kind=? AND revision=?",
                        (old.worker_id, kind, value.revision),
                    ).fetchone()
                    if existing_cap:
                        if existing_cap[0] != canonical_json(value):
                            raise WorkflowError("cas_conflict")
                    else:
                        self._insert(
                            "capabilities",
                            worker_id=old.worker_id,
                            kind=kind,
                            revision=value.revision,
                            probe_revision=cap.approved.probe_revision
                            if kind == "approved" and cap.approved
                            else None,
                            physical_resource_id=binding.resource_id
                            if binding
                            else None,
                            created_at=timestamp(now),
                            body_json=canonical_json(value),
                        )
            state = (
                "verified"
                if cap.approved
                else (
                    "pending_pairing"
                    if old.enrollment_state == "pending_pairing"
                    else "pending_validation"
                )
            )
            worker = old.model_copy(
                update={
                    "capability": cap,
                    "physical_binding": binding,
                    "enrollment_state": state,
                    "admission_state": old.admission_state
                    if state == "verified"
                    else "disabled",
                    "version": command.expected_version + 1,
                }
            )
            version = self._save_worker(worker, command.expected_version)
            return Mutation(worker.worker_id, version, state)

        return self._execute(auth, meta, command, "record_capabilities", apply)

    def issue_pairing(
        self, auth: AuthProof, meta: CommandMeta, command: PairingIssue
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            worker = self._worker(command.worker_id)
            if (
                command.device_id != worker.device_id
                or command.expires_at <= now
                or worker.enrollment_state == "retired"
                or worker.admission_state == "revoked"
            ):
                raise WorkflowError("precondition_failed")
            self._insert(
                "pairings",
                pairing_id=command.pairing_id,
                worker_id=command.worker_id,
                device_id=command.device_id,
                ticket_digest=command.ticket_digest,
                expires_at=timestamp(command.expires_at),
                body_json="{}",
            )
            return Mutation(command.pairing_id, 1, "pending")

        return self._execute(auth, meta, command, "issue_pairing", apply)

    def _pairing_principal(
        self, auth: PairingProof, command: Model, now: datetime
    ) -> sqlite3.Row:
        if not isinstance(command, PairingComplete):
            raise WorkflowError("unauthorized")
        authority = self._one("SELECT * FROM authority WHERE singleton=1")
        pairing = self._one(
            "SELECT * FROM pairings WHERE pairing_id=?",
            (command.pairing_id,),
            code="unauthorized",
        )
        if (
            auth.pairing_id != command.pairing_id
            or auth.authority_id != authority["authority_id"]
            or pairing["consumed_at"] is not None
            or parse_time(pairing["expires_at"]) <= now
            or not hmac.compare_digest(auth.ticket_digest, pairing["ticket_digest"])
            or not hmac.compare_digest(auth.new_secret_digest, command.secret_digest)
            or pairing["worker_id"] != command.worker_id
            or pairing["device_id"] != command.device_id
        ):
            raise WorkflowError("unauthorized")
        worker = self._worker(command.worker_id)
        if (
            worker.enrollment_state == "retired"
            or worker.admission_state == "revoked"
            or command.expires_at <= now
        ):
            raise WorkflowError("precondition_failed")
        principal_id = str(
            uuid5(UUID(command.worker_id), "workflow-compute-principal-v1")
        )
        old = self._conn.execute(
            "SELECT * FROM principals WHERE principal_id=?", (principal_id,)
        ).fetchone()
        maximum = self._one(
            "SELECT COALESCE(max(generation),0) FROM credentials WHERE principal_id=?",
            (principal_id,),
        )[0]
        if command.generation != maximum + 1:
            raise WorkflowError("precondition_failed")
        if old is None:
            self._insert(
                "principals",
                principal_id=principal_id,
                role="compute_worker",
                worker_id=command.worker_id,
                body_json="{}",
            )
        self._conn.execute(
            "UPDATE credentials SET revoked_at=?,version=version+1 WHERE principal_id=? AND revoked_at IS NULL",
            (timestamp(now), principal_id),
        )
        self._insert(
            "credentials",
            credential_id=command.credential_id,
            principal_id=principal_id,
            secret_digest=command.secret_digest,
            generation=command.generation,
            issued_at=timestamp(now),
            expires_at=timestamp(command.expires_at),
            body_json="{}",
        )
        return self._credential(command.credential_id, command.secret_digest, now)

    def complete_pairing(
        self,
        auth: AuthProof | PairingProof,
        meta: CommandMeta,
        command: PairingComplete,
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            pairing = self._one(
                "SELECT * FROM pairings WHERE pairing_id=?", (command.pairing_id,)
            )
            if (
                principal["worker_id"] != command.worker_id
                or principal["credential_id"] != command.credential_id
                or pairing["worker_id"] != command.worker_id
                or pairing["device_id"] != command.device_id
                or pairing["consumed_at"] is not None
                or not isinstance(auth, PairingProof)
            ):
                raise WorkflowError("unauthorized")
            self._update(
                "pairings",
                "pairing_id",
                command.pairing_id,
                pairing["version"],
                consumed_at=timestamp(now),
                credential_id=command.credential_id,
                request_digest=digest(command),
            )
            old = self._worker(command.worker_id)
            port = command.agent_port or old.port
            worker = old.model_copy(
                update={
                    "port": port,
                    "network_revision": old.network_revision + int(port != old.port),
                    "enrollment_state": "pending_validation",
                    "admission_state": "disabled",
                    "boot_id": None,
                    "version": old.version + 1,
                }
            )
            version = self._save_worker(worker, old.version)
            return Mutation(worker.worker_id, version, "pending_validation")

        return self._execute(auth, meta, command, "complete_pairing", apply)

    def _isolate_worker_assignments(self, worker_id: str, now: datetime) -> None:
        rows = self._conn.execute(
            "SELECT * FROM assignments WHERE worker_id=? AND resolved_at IS NULL",
            (worker_id,),
        ).fetchall()
        for row in rows:
            assignment = AssignmentRecord.model_validate_json(
                row["body_json"]
            ).model_copy(update={"status": "unknown", "version": row["version"] + 1})
            self._update(
                "assignments",
                "assignment_id",
                row["assignment_id"],
                row["version"],
                state="unknown",
                body_json=canonical_json(assignment),
            )
            self._conn.execute(
                "UPDATE resource_owners SET state='unknown',version=version+1 WHERE assignment_id=? AND resolved_at IS NULL",
                (row["assignment_id"],),
            )
            stage = self._one(
                "SELECT * FROM stages WHERE current_attempt_id=?", (row["attempt_id"],)
            )
            # A boot or revocation isolates resources, not completed business facts.
            if stage["status"] in ("succeeded", "failed", "blocked"):
                continue
            attempt = self._one(
                "SELECT * FROM attempts WHERE attempt_id=?", (row["attempt_id"],)
            )
            attempt_body = AttemptRecord.model_validate_json(
                attempt["body_json"]
            ).model_copy(update={"status": "unknown"})
            self._update(
                "attempts",
                "attempt_id",
                row["attempt_id"],
                attempt["version"],
                status="unknown",
                body_json=canonical_json(attempt_body),
            )
            stage_body = StageRecord.model_validate_json(stage["body_json"]).model_copy(
                update={"status": "unknown", "version": stage["version"] + 1}
            )
            self._update(
                "stages",
                "job_id",
                stage["job_id"],
                stage["version"],
                status="unknown",
                body_json=canonical_json(stage_body),
            )
            self._conn.execute(
                "UPDATE operations SET status='unknown',updated_at=?,version=version+1 WHERE attempt_id=? AND resolved_at IS NULL",
                (timestamp(now), row["attempt_id"]),
            )

    def record_boot(
        self, auth: AuthProof, meta: CommandMeta, command: BootObservation
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            if (
                principal["worker_id"] != command.worker_id
                or command.known_epoch != meta.authority_epoch
            ):
                raise WorkflowError("forbidden")
            old = self._worker(command.worker_id)
            if old.boot_id != command.boot_id:
                self._isolate_worker_assignments(old.worker_id, now)
            owned = {
                row[0]
                for row in self._conn.execute(
                    "SELECT assignment_id FROM assignments WHERE worker_id=? AND resolved_at IS NULL",
                    (old.worker_id,),
                )
            }
            if set(command.assignment_ids) - owned:
                raise WorkflowError("precondition_failed")
            reconciled = not owned and not any(
                owner.state != "stopped" for owner in command.local_owners
            )
            existing = self._conn.execute(
                "SELECT 1 FROM worker_boots WHERE worker_id=? AND boot_id=?",
                (old.worker_id, command.boot_id),
            ).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE worker_boots SET observed_at=?,reconciled=?,body_json=? WHERE worker_id=? AND boot_id=?",
                    (
                        timestamp(now),
                        int(reconciled),
                        canonical_json(command),
                        old.worker_id,
                        command.boot_id,
                    ),
                )
            else:
                self._insert(
                    "worker_boots",
                    worker_id=old.worker_id,
                    boot_id=command.boot_id,
                    observed_at=timestamp(now),
                    reconciled=int(reconciled),
                    body_json=canonical_json(command),
                )
            self._conn.execute(
                "UPDATE principals SET boot_id=?,version=version+1 WHERE principal_id=?",
                (command.boot_id, principal["principal_id"]),
            )
            port = command.agent_port or old.port
            worker = old.model_copy(
                update={
                    "port": port,
                    "network_revision": old.network_revision + int(port != old.port),
                    "boot_id": command.boot_id,
                    "version": command.expected_version + 1,
                }
            )
            version = self._save_worker(worker, command.expected_version)
            return Mutation(
                worker.worker_id, version, "reconciled" if reconciled else "unknown"
            )

        return self._execute(auth, meta, command, "record_boot", apply)

    def revoke_worker(
        self, auth: AuthProof, meta: CommandMeta, command: WorkerRevocation
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            old = self._worker(command.worker_id)
            worker = old.model_copy(
                update={
                    "admission_state": "revoked",
                    "workflow_participation": False,
                    "authorization_revision": old.authorization_revision + 1,
                    "version": command.expected_version + 1,
                }
            )
            version = self._save_worker(worker, command.expected_version)
            self._conn.execute(
                "UPDATE credentials SET revoked_at=?,version=version+1 WHERE principal_id IN (SELECT principal_id FROM principals WHERE worker_id=?) AND revoked_at IS NULL",
                (timestamp(now), worker.worker_id),
            )
            self._isolate_worker_assignments(worker.worker_id, now)
            return Mutation(worker.worker_id, version, "revoked")

        return self._execute(auth, meta, command, "revoke_worker", apply)

    def retire_worker(
        self, auth: AuthProof, meta: CommandMeta, command: WorkerRetirement
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            old = self._worker(command.worker_id)
            if old.admission_state != "revoked":
                raise WorkflowError("precondition_failed")
            resource = (
                old.physical_binding.resource_id if old.physical_binding else None
            )
            if self._conn.execute(
                "SELECT 1 FROM assignments WHERE (worker_id=? OR physical_resource_id=?) AND resolved_at IS NULL LIMIT 1",
                (old.worker_id, resource),
            ).fetchone():
                raise WorkflowError("owner_unresolved")
            worker = old.model_copy(
                update={
                    "enrollment_state": "retired",
                    "version": command.expected_version + 1,
                }
            )
            version = self._save_worker(worker, command.expected_version)
            self._conn.execute(
                "UPDATE workers SET retired_at=? WHERE worker_id=?",
                (timestamp(now), worker.worker_id),
            )
            return Mutation(worker.worker_id, version, "retired")

        return self._execute(auth, meta, command, "retire_worker", apply)

    def _check_worker_claim(
        self,
        principal: sqlite3.Row,
        command: ClaimRequest,
        manifest: InputManifest,
        stage: str,
    ) -> WorkerRecord:
        if command.worker_id is None or principal["worker_id"] != command.worker_id:
            raise WorkflowError("forbidden")
        worker = self._worker(command.worker_id)
        cap = worker.capability
        binding = worker.physical_binding
        if (
            not worker.workflow_participation
            or worker.enrollment_state != "verified"
            or worker.admission_state != "enabled"
            or worker.boot_id != command.boot_id
            or principal["boot_id"] != command.boot_id
            or not cap
            or not cap.approved
            or not cap.probed
            or not binding
            or binding.resource_id != command.resource_id
        ):
            raise WorkflowError("precondition_failed")
        boot = self._one(
            "SELECT * FROM worker_boots WHERE worker_id=? AND boot_id=?",
            (worker.worker_id, command.boot_id),
            code="precondition_failed",
        )
        if not boot["reconciled"]:
            raise WorkflowError("owner_unresolved")
        tools = command.tool_versions
        profiles = [
            profile
            for profile in cap.approved.profiles
            if profile.profile_id == tools.profile
            and profile.profile_revision == tools.profile_revision
            and profile.profile_revision == command.profile_revision
            and stage in profile.allowed_stages
            and (
                profile.tool,
                profile.tool_version,
                profile.model,
                profile.model_version,
            )
            == (tools.tool, tools.tool_version, tools.model, tools.model_version)
        ]
        if len(profiles) != 1 or command.config_digest != tools.config_digest:
            raise WorkflowError("precondition_failed")
        profile = profiles[0]
        if any(
            probe.width is None
            or probe.height is None
            or probe.evidence.status != "passed"
            or probe.width > profile.max_width
            or probe.height > profile.max_height
            for probe in manifest.readiness.media_probes
        ):
            raise WorkflowError("precondition_failed")
        if self._conn.execute(
            "SELECT 1 FROM resource_owners WHERE physical_resource_id=? AND resolved_at IS NULL",
            (binding.resource_id,),
        ).fetchone():
            raise WorkflowError("owner_unresolved")
        return worker

    def get_worker(self, auth: AuthProof, worker_id: str) -> WorkerView:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if (
                principal["role"] not in ("operator", "observer")
                and principal["worker_id"] != worker_id
            ):
                raise WorkflowError("forbidden")
            worker = self._worker(worker_id)
            rows = self._conn.execute(
                "SELECT body_json FROM assignments WHERE worker_id=? ORDER BY claimed_at,assignment_id",
                (worker_id,),
            ).fetchall()
            return WorkerView(
                worker=worker,
                assignments=tuple(
                    AssignmentRecord.model_validate_json(row[0]) for row in rows
                ),
            )

    def authenticated_worker(self, auth: AuthProof) -> WorkerSelf:
        """The calling compute worker's own record and whether its current boot
        is reconciled; any other principal is forbidden (P2-2 worker API)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "compute_worker" or not principal["worker_id"]:
                raise WorkflowError("forbidden")
            worker = self._worker(principal["worker_id"])
            row = self._conn.execute(
                "SELECT reconciled FROM worker_boots WHERE worker_id=? AND boot_id=?",
                (worker.worker_id, worker.boot_id),
            ).fetchone()
            return WorkerSelf(worker=worker, boot_reconciled=bool(row and row[0]))

    def worker_attempt(self, auth: AuthProof, attempt_id: str) -> WorkerAttempt:
        """C7: the caller's own compute attempt while it may still upload — its
        assignment unresolved (an `unknown` one included) and the attempt
        current for its job; anything else is `stale_attempt`."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "compute_worker" or not principal["worker_id"]:
                raise WorkflowError("forbidden")
            row = self._conn.execute(
                "SELECT a.attempt_id,a.job_id,s.film_id,s.input_revision,s.stage,"
                "s.current_attempt_id,g.worker_id,g.resolved_at FROM attempts a "
                "JOIN stages s ON s.job_id=a.job_id "
                "JOIN assignments g ON g.attempt_id=a.attempt_id WHERE a.attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if (
                row is None
                or row["worker_id"] != principal["worker_id"]
                or row["resolved_at"] is not None
                or row["current_attempt_id"] != attempt_id
                or row["stage"] not in ("restore", "asr", "translate")
            ):
                raise WorkflowError("stale_attempt")
            return WorkerAttempt(
                attempt_id=row["attempt_id"],
                job_id=row["job_id"],
                film_id=row["film_id"],
                input_revision=row["input_revision"],
                stage=row["stage"],
            )

    def grant_component(
        self, auth: AuthProof, meta: CommandMeta, command: ComponentGrant
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            if command.expires_at <= now:
                raise WorkflowError("invalid_request")
            old = self._conn.execute(
                "SELECT * FROM principals WHERE principal_id=?", (command.principal_id,)
            ).fetchone()
            if old is None:
                if command.generation != 1:
                    raise WorkflowError("precondition_failed")
                self._insert(
                    "principals",
                    principal_id=command.principal_id,
                    role=command.role,
                    component_id=command.component_id,
                    boot_id=command.boot_id,
                    body_json="{}",
                )
            else:
                generation = self._one(
                    "SELECT max(generation) FROM credentials WHERE principal_id=?",
                    (command.principal_id,),
                )[0]
                # Archive/cleanup adapters are fixed principals that keep owning
                # their open operations across controller restarts (F02-P1 D11).
                reboot = old["role"] in ("archive_adapter", "cleanup_adapter")
                if (
                    old["role"] != command.role
                    or old["component_id"] != command.component_id
                    or (old["boot_id"] != command.boot_id and not reboot)
                    or command.generation != generation + 1
                ):
                    raise WorkflowError("precondition_failed")
                if old["boot_id"] != command.boot_id:
                    self._update(
                        "principals",
                        "principal_id",
                        command.principal_id,
                        old["version"],
                        boot_id=command.boot_id,
                    )
                self._conn.execute(
                    "UPDATE credentials SET revoked_at=?,version=version+1 WHERE principal_id=? AND revoked_at IS NULL",
                    (timestamp(now), command.principal_id),
                )
            self._insert(
                "credentials",
                credential_id=command.credential_id,
                principal_id=command.principal_id,
                secret_digest=command.secret_digest,
                generation=command.generation,
                issued_at=timestamp(now),
                expires_at=timestamp(command.expires_at),
                body_json="{}",
            )
            return Mutation(command.credential_id, 1, "granted")

        return self._execute(auth, meta, command, "grant_component", apply)

    def revoke_credential(
        self, auth: AuthProof, meta: CommandMeta, command: CredentialRevocation
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            authority = self._one("SELECT * FROM authority WHERE singleton=1")
            if command.credential_id == authority["operator_credential_id"]:
                raise WorkflowError("precondition_failed")
            version = self._update(
                "credentials",
                "credential_id",
                command.credential_id,
                command.expected_version,
                revoked_at=timestamp(now),
            )
            return Mutation(command.credential_id, version, "revoked")

        return self._execute(auth, meta, command, "revoke_credential", apply)

    def verify_operator(self, auth: AuthProof) -> None:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            authority = self._one("SELECT * FROM authority WHERE singleton=1")
            if (
                principal["role"] != "operator"
                or auth.credential_id != authority["operator_credential_id"]
            ):
                raise WorkflowError("forbidden")

    def verify_worker(self, auth: AuthProof) -> None:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "compute_worker":
                raise WorkflowError("forbidden")

    def list_audit(self, auth: AuthProof, after: int, limit: int = 100) -> AuditPage:
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 200
        ):
            raise WorkflowError("invalid_request")
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] not in ("operator", "observer"):
                raise WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT * FROM audit WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
            ).fetchall()
            events = tuple(
                AuditRecord(
                    audit_id=row["seq"],
                    entity_id=row["entity_id"],
                    principal_id=row["principal_id"],
                    command_kind=row["command_kind"],
                    request_id=row["request_id"],
                    observed_at=row["created_at"],
                    result_id=json.loads(row["body_json"])["result_id"],
                )
                for row in rows
            )
            return AuditPage(
                events=events, next_after=events[-1].audit_id if events else after
            )
