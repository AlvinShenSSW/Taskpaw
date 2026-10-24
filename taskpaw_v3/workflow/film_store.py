"""Film snapshots and fenced attempts; no media or filesystem operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

from .auth import STAGE_ROLES, AuthProof
from .models import (
    ArtifactReceipt,
    AssignmentRecord,
    AssignmentRenewal,
    AssignmentResolution,
    AttemptRecord,
    AttemptResult,
    ClaimRequest,
    CommandMeta,
    CommandResult,
    EvidenceRef,
    FilmCreate,
    FilmPlanConfirm,
    FilmRecord,
    FilmRemove,
    FilmRoute,
    FinalManifest,
    FinalManifestPut,
    InputManifest,
    InputManifestPut,
    LocalOwnerEvidence,
    NativeIdentity,
    ResourceOwnerRecord,
    Stage,
    StageRecord,
    StageRetry,
    StageSkip,
    UnknownObservation,
    WorkflowError,
    canonical_json,
    digest,
)
from .store_core import Mutation, parse_time, timestamp
from .worker_store import WorkerCommands

_STAGES: tuple[Stage, ...] = (
    "intake",
    "restore",
    "asr",
    "translate",
    "return",
    "scrape",
    "archive",
    "cleanup_input",
    "cleanup_workcopy",
)
_COMPUTE = frozenset(("restore", "asr", "translate"))
_FINISHED = ("succeeded", "skipped")
# P2-1 R6: the stages a route serves. The controller's own `local` route runs
# the whole compute chain; any other route ends with the GPU stages.
_ROUTE_STAGES = {"local": ("restore", "asr", "translate")}
# OD7 (#298 R4): a worker's (auto/pinned) route closes once its restore is done
_GPU_STAGES = ("restore",)
# OD7 R5: stages only the controller claims, on its own local route
_CONTROLLER_ONLY = frozenset({"asr"})


def native_identity_key(identity: NativeIdentity | None) -> str | None:
    """Stable object key; observation timestamps never manufacture a new object."""
    if identity is None:
        return None
    return digest(
        {
            key: getattr(identity, key)
            for key in ("scheme", "root_id", "volume_or_export_id", "object_id")
        }
    )


def _passed(evidence: tuple[EvidenceRef, ...]) -> bool:
    return bool(evidence) and all(item.status == "passed" for item in evidence)


class FilmCommands(WorkerCommands):
    def _require_source_admissible(self, film: sqlite3.Row) -> None:
        # P3 #304 CX1: a removed film is gone for good; a route or claim decided
        # before the delete must not bring it back (nor create its work dirs)
        if FilmRecord.model_validate_json(film["body_json"]).removed_at is not None:
            raise WorkflowError("source_not_admissible")
        link = self._conn.execute(
            "SELECT i.*,p.source_generation AS current_generation,p.dirty,p.active_snapshot_id,p.state,"
            "p.root_config_revision,r.config_revision AS current_root_revision,r.enabled,"
            "COALESCE(s.required,0) AS rescan_required "
            "FROM intake_films i JOIN intake_packages p ON p.package_id=i.package_id "
            "JOIN intake_roots r ON r.root_id=p.root_id AND r.current=1 "
            "LEFT JOIN intake_root_scans s ON s.root_id=p.root_id "
            "WHERE i.film_id=? AND i.input_revision=?",
            (film["film_id"], film["current_input_revision"]),
        ).fetchone()
        if link is None:
            # Historical unbound manifests remain readable. Once managed intake
            # is enabled they are not proof of a current verified source.
            if self._conn.execute(
                "SELECT 1 FROM intake_roots WHERE current=1 AND enabled=1"
            ).fetchone():
                raise WorkflowError("source_not_admissible")
            return
        if (
            link["dirty"]
            or link["rescan_required"]
            or not link["enabled"]
            or link["source_generation"] != link["current_generation"]
            or link["snapshot_id"] != link["active_snapshot_id"]
            or link["root_config_revision"] != link["current_root_revision"]
            or link["state"] != "operational_ready"
        ):
            raise WorkflowError("source_not_admissible")

    def _source_admissible(self, film: sqlite3.Row) -> bool:
        """`_require_source_admissible` as a fact, for reads that must agree
        with the claim (P2-1 CX7)."""
        try:
            self._require_source_admissible(film)
        except WorkflowError:
            return False
        return True

    def _require_plan(self, job: sqlite3.Row) -> None:
        """U1 P1': a restore is claimed only under its revision's confirmed plan."""
        if (
            job["stage"] == "restore"
            and not self._conn.execute(
                "SELECT 1 FROM film_plans WHERE film_id=? AND input_revision=?",
                (job["film_id"], job["input_revision"]),
            ).fetchone()
        ):
            raise WorkflowError("plan_required")

    def confirm_plan(
        self, auth: AuthProof, meta: CommandMeta, command: FilmPlanConfirm
    ) -> CommandResult:
        """U1 P1': the operator's plan of one film, CAS on its input revision.

        Only a current revision whose restore is still pending can be planned,
        once: a confirmed plan is never changed (a new revision needs its own).
        P2-1: `machine` None is 自动; the plan's pin row starts as its machine,
        except that the controller (never a GPU worker, OD1) pins 自动 (NULL),
        the one encoding the v4 migration writes too (IR9).
        """

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["current_input_revision"] != command.input_revision:
                raise WorkflowError("cas_conflict")
            if self._conn.execute(
                "SELECT 1 FROM film_plans WHERE film_id=? AND input_revision=?",
                (command.film_id, command.input_revision),
            ).fetchone():
                raise WorkflowError("plan_exists")
            restore = self._conn.execute(
                "SELECT status FROM stages WHERE film_id=? AND input_revision=? "
                "AND stage='restore'",
                (command.film_id, command.input_revision),
            ).fetchone()
            if restore is None or restore["status"] != "pending":
                raise WorkflowError("precondition_failed")
            if command.machine is not None and not self._verified(command.machine):
                raise WorkflowError("precondition_failed")
            self._insert(
                "film_plans",
                film_id=command.film_id,
                input_revision=command.input_revision,
                ltx_model=command.ltx_model,
                machine=command.machine,
                confirmed_at=timestamp(now),
                actors=json.dumps(list(command.actors), ensure_ascii=False),
            )
            self._insert(
                "film_pins",
                film_id=command.film_id,
                input_revision=command.input_revision,
                worker_id=self._pin_target(command.machine),
                updated_at=timestamp(now),
            )
            return Mutation(command.film_id, film["version"], "planned")

        return self._execute(auth, meta, command, "confirm_plan", apply)

    def _pin_target(self, worker_id: str | None) -> str | None:
        """A pin's stored worker: the controller means 自动 (NULL, IR9)."""
        return None if worker_id == self.controller_id else worker_id

    def _verified(self, worker_id: str) -> bool:
        return bool(
            self._conn.execute(
                "SELECT 1 FROM workers WHERE worker_id=? AND enrollment_state='verified' "
                "AND admission_state!='revoked'",
                (worker_id,),
            ).fetchone()
        )

    # ── routes (P2-1) ───────────────────────────────────────────────────
    def _open_route(self, film_id: str, revision: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM film_routes WHERE film_id=? AND input_revision=? "
            "AND state='open'",
            (film_id, revision),
        ).fetchone()
        return row

    def _compute_claim(self, film_id: str, revision: int) -> sqlite3.Row | None:
        """The revision's unresolved compute assignment: what makes a route claimed (C9)."""
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT a.* FROM assignments a JOIN stages s ON s.job_id=a.job_id "
            "WHERE s.film_id=? AND s.input_revision=? "
            "AND s.stage IN ('restore','asr','translate') AND a.resolved_at IS NULL",
            (film_id, revision),
        ).fetchone()
        return row

    def _compute_statuses(self, film_id: str, revision: int) -> dict[str, str]:
        return {
            row[0]: row[1]
            for row in self._conn.execute(
                "SELECT stage,status FROM stages WHERE film_id=? AND input_revision=? "
                "AND stage IN ('restore','asr','translate')",
                (film_id, revision),
            )
        }

    def _close_route(self, row: sqlite3.Row, now: datetime, reason: str) -> int:
        value = FilmRoute.model_validate_json(row["body_json"]).model_copy(
            update={
                "state": "closed",
                "closed_at": now,
                "close_reason": reason,
                "version": row["version"] + 1,
            }
        )
        return self._update(
            "film_routes",
            "route_id",
            row["route_id"],
            row["version"],
            state="closed",
            closed_at=timestamp(now),
            close_reason=reason,
            body_json=canonical_json(value),
        )

    def _close_finished_route(self, film_id: str, revision: int, now: datetime) -> None:
        """R6: an unclaimed open route closes once its stages are all finished
        (完成); once one of them failed or blocked and waits for the operator
        or a retry, so it holds no worker slot and the retry is routed again
        (失败, IR2); or once its revision is no longer current (新版本, IR6)."""
        route = self._open_route(film_id, revision)
        if route is None or self._compute_claim(film_id, revision) is not None:
            return
        statuses = self._compute_statuses(film_id, revision)
        stages = _ROUTE_STAGES.get(route["kind"], _GPU_STAGES)
        if self._film_row(film_id)["current_input_revision"] != revision:
            self._close_route(route, now, "新版本")
        elif all(statuses.get(stage) in _FINISHED for stage in stages):
            self._close_route(route, now, "完成")
        elif any(statuses.get(stage) in ("failed", "blocked") for stage in stages):
            self._close_route(route, now, "失败")

    def _film_row(self, film_id: str) -> sqlite3.Row:
        return self._one("SELECT * FROM films WHERE film_id=?", (film_id,))

    def _set_film(self, row: sqlite3.Row, **changes: Any) -> int:
        value = FilmRecord.model_validate_json(row["body_json"]).model_copy(
            update={**changes, "version": row["version"] + 1}
        )
        return self._update(
            "films",
            "film_id",
            row["film_id"],
            row["version"],
            **changes,
            body_json=canonical_json(value),
        )

    def _current_job(self, job_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM stages WHERE job_id=?", (job_id,))
        film = self._film_row(row["film_id"])
        if film["current_input_revision"] != row["input_revision"]:
            raise WorkflowError("stale_attempt")
        return row

    def _set_stage(
        self,
        row: sqlite3.Row,
        status: str,
        *,
        attempt_id: str | None = None,
        evidence: tuple[EvidenceRef, ...] | None = None,
        reason: str | None = None,
    ) -> int:
        value = StageRecord.model_validate_json(row["body_json"]).model_copy(
            update={
                "status": status,
                "attempt_id": attempt_id
                if attempt_id is not None
                else row["current_attempt_id"],
                "version": row["version"] + 1,
            }
        )
        if evidence is not None:
            value = value.model_copy(update={"evidence": evidence, "reason": reason})
        return self._update(
            "stages",
            "job_id",
            row["job_id"],
            row["version"],
            status=status,
            current_attempt_id=value.attempt_id,
            body_json=canonical_json(value),
        )

    def _dependencies(self, job: sqlite3.Row) -> None:
        value = StageRecord.model_validate_json(job["body_json"])
        for dependency_id in value.dependency_job_ids:
            dependency = self._one(
                "SELECT * FROM stages WHERE job_id=?", (dependency_id,)
            )
            if dependency["status"] not in ("succeeded", "skipped"):
                raise WorkflowError("precondition_failed")

    def _input(self, job: sqlite3.Row) -> InputManifest:
        row = self._one(
            "SELECT body_json FROM manifests WHERE film_id=? AND kind='input' AND revision=?",
            (job["film_id"], job["input_revision"]),
        )
        return InputManifest.model_validate_json(row["body_json"])

    def _manifest_insert(
        self,
        value: InputManifest | FinalManifest,
        now: datetime,
        *,
        attempt_id: str | None = None,
    ) -> None:
        if isinstance(value, InputManifest):
            kind, input_revision, input_hash = (
                "input",
                value.revision,
                value.manifest_hash,
            )
        else:
            kind, input_revision, input_hash = (
                "final",
                value.input_revision,
                value.input_hash,
            )
        self._insert(
            "manifests",
            film_id=value.film_id,
            kind=kind,
            revision=value.revision,
            manifest_hash=value.manifest_hash,
            input_revision=input_revision,
            input_hash=input_hash,
            attempt_id=attempt_id,
            created_at=timestamp(now),
            body_json=canonical_json(value),
        )
        for item in value.items:
            key = native_identity_key(item.native_identity)
            self._insert(
                "manifest_items",
                film_id=value.film_id,
                kind=kind,
                revision=value.revision,
                item_id=item.item_id,
                role=item.role,
                relative_path=item.relative_path,
                path_key=item.relative_path.casefold(),
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                source_root_id=item.source_root_id,
                native_identity_key=key,
                parent_item_id=item.parent_item_id,
                video_item_id=item.video_item_id,
                volume_ordinal=item.volume_ordinal,
                language=item.language,
                body_json=canonical_json(item),
            )
            if kind == "input":
                self._insert(
                    "source_refs",
                    film_id=value.film_id,
                    input_revision=value.revision,
                    item_id=item.item_id,
                    source_root_id=item.source_root_id,
                    native_identity_key=key,
                    body_json=canonical_json(item),
                )

    def _create_film_sql(self, command: FilmCreate, now: datetime) -> Mutation:
        value = FilmRecord(
            **command.model_dump(),
            current_input_revision=None,
            current_final_revision=None,
            current_context_revision=None,
            context_state="invalidated",
            version=1,
            created_at=now,
        )
        self._insert(
            "films",
            film_id=value.film_id,
            intake_session_id=value.intake_session_id,
            display_name=value.display_name,
            policy_revision=value.policy.policy_revision,
            created_at=timestamp(now),
            body_json=canonical_json(value),
        )
        return Mutation(value.film_id, 1, "receiving")

    def create_film(
        self, auth: AuthProof, meta: CommandMeta, command: FilmCreate
    ) -> CommandResult:
        return self._execute(
            auth,
            meta,
            command,
            "create_film",
            lambda principal, now: self._create_film_sql(command, now),
        )

    def _put_input_sql(self, command: InputManifestPut, now: datetime) -> Mutation:
        row = self._film_row(command.film_id)
        if row["version"] != command.expected_version:
            raise WorkflowError("cas_conflict")
        film = FilmRecord.model_validate_json(row["body_json"])
        value = command.manifest
        readiness = value.readiness
        proofs = (
            readiness.writer_check,
            readiness.full_read,
            readiness.source_recheck,
            readiness.workcopy.verification,
            *(probe.evidence for probe in readiness.media_probes),
        )
        if (
            not _passed(proofs)
            or readiness.writer_check.summary.active_writers is not False
            or value.policy_revision != film.policy.policy_revision
            or value.revision <= (row["current_input_revision"] or 0)
        ):
            raise WorkflowError("precondition_failed")
        self._check_attachments(film, value)
        self._manifest_insert(value, now)
        for route in self._conn.execute(
            "SELECT * FROM film_routes WHERE film_id=? AND state='open'",
            (value.film_id,),
        ).fetchall():
            # C9: a new revision. IR6: a route whose old-revision work still
            # runs stays open (its worker is busy) and closes when that resolves.
            if self._compute_claim(route["film_id"], route["input_revision"]) is None:
                self._close_route(route, now, "新版本")
        job_ids = {name: str(uuid4()) for name in _STAGES}
        for index, name in enumerate(_STAGES):
            previous = (
                "archive"
                if name == "cleanup_workcopy"
                else _STAGES[index - 1]
                if index
                else None
            )
            stage = StageRecord(
                job_id=job_ids[name],
                film_id=value.film_id,
                stage=name,
                input_revision=value.revision,
                input_hash=value.manifest_hash,
                dependency_job_ids=(job_ids[previous],) if previous else (),
                status="succeeded" if name == "intake" else "pending",
                attempt_id=None,
                version=1,
            )
            self._insert(
                "stages",
                job_id=stage.job_id,
                film_id=stage.film_id,
                input_revision=stage.input_revision,
                input_hash=stage.input_hash,
                stage=name,
                status=stage.status,
                body_json=canonical_json(stage),
            )
        version = self._set_film(
            row,
            current_input_revision=value.revision,
            current_final_revision=None,
            context_state="invalidated",
        )
        return Mutation(value.film_id, version, "ready")

    def put_input_manifest(
        self, auth: AuthProof, meta: CommandMeta, command: InputManifestPut
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            if (
                command.manifest.source_binding is not None
                or self._conn.execute(
                    "SELECT 1 FROM intake_films WHERE film_id=?", (command.film_id,)
                ).fetchone()
            ):
                raise WorkflowError("managed_snapshot_required")
            return self._put_input_sql(command, now)

        return self._execute(auth, meta, command, "put_input_manifest", apply)

    @staticmethod
    def _check_attachments(
        film: FilmRecord, value: InputManifest | FinalManifest
    ) -> None:
        allowed = {
            suffix.casefold() for suffix in film.policy.allowed_attachment_extensions
        }
        if any(
            item.role == "attachment"
            and PurePosixPath(item.relative_path).suffix.casefold() not in allowed
            for item in value.items
        ):
            raise WorkflowError("precondition_failed")

    def put_final_manifest(
        self, auth: AuthProof, meta: CommandMeta, command: FinalManifestPut
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            attempt = self._one(
                "SELECT * FROM attempts WHERE attempt_id=?", (command.attempt_id,)
            )
            job = self._current_job(attempt["job_id"])
            assignment = self._one(
                "SELECT * FROM assignments WHERE attempt_id=?", (command.attempt_id,)
            )
            value = command.manifest
            if (
                job["film_id"] != command.film_id
                or job["stage"] != "scrape"
                or job["status"] != "running"
                or job["current_attempt_id"] != command.attempt_id
                or assignment["state"] != "running"
                or assignment["owner_principal_id"] != principal["principal_id"]
                or parse_time(assignment["lease_deadline"]) <= now
                or value.input_revision != job["input_revision"]
                or value.input_hash != job["input_hash"]
            ):
                raise WorkflowError("stale_attempt")
            high = self._one(
                "SELECT max(revision) FROM manifests WHERE film_id=? AND kind='final'",
                (command.film_id,),
            )[0]
            if value.revision <= (high or 0):
                raise WorkflowError("precondition_failed")
            claimed = AttemptRecord.model_validate_json(attempt["body_json"])
            if (
                value.postprocess.tool_version != claimed.tool_versions.tool_version
                or value.postprocess.config_digest
                != claimed.tool_versions.config_digest
            ):
                raise WorkflowError("precondition_failed")
            self._check_attachments(
                FilmRecord.model_validate_json(film["body_json"]), value
            )
            self._manifest_insert(value, now, attempt_id=command.attempt_id)
            version = self._set_film(film, context_state="invalidated")
            return Mutation(
                command.film_id, version, "candidate", attempt_id=command.attempt_id
            )

        return self._execute(auth, meta, command, "put_final_manifest", apply)

    def claim(
        self, auth: AuthProof, meta: CommandMeta, command: ClaimRequest
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            job = self._current_job(command.job_id)
            if job["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            if job["status"] != "pending":
                raise WorkflowError("precondition_failed")
            if STAGE_ROLES.get(job["stage"]) != principal["role"]:
                raise WorkflowError("forbidden")
            self._require_source_admissible(self._film_row(job["film_id"]))
            self._require_plan(job)
            self._dependencies(job)
            if (
                command.profile_revision != command.tool_versions.profile_revision
                or command.config_digest != command.tool_versions.config_digest
            ):
                raise WorkflowError("precondition_failed")
            if job["stage"] in _COMPUTE:
                self._check_worker_claim(
                    principal, command, self._input(job), job["stage"]
                )
                # P2-1 C10: only the worker the film revision is routed to
                route = self._open_route(job["film_id"], job["input_revision"])
                if route is None or route["worker_id"] != command.worker_id:
                    raise WorkflowError("route_required")
                if job["stage"] in _CONTROLLER_ONLY and route["kind"] != "local":
                    # OD7 R5: only the controller opens `local` routes (C10);
                    # e.g. an older worker build claiming ASR on its route
                    raise WorkflowError("route_required")
            elif (
                command.worker_id is not None
                or command.resource_id is not None
                or command.boot_id != principal["boot_id"]
            ):
                raise WorkflowError("stale_attempt")
            authority = self._one("SELECT * FROM authority WHERE singleton=1")
            fence = authority["next_fence"]
            self._update(
                "authority",
                "singleton",
                "1",
                authority["version"],
                next_fence=fence + 1,
            )
            number = self._one(
                "SELECT coalesce(max(attempt_number),0)+1 FROM attempts WHERE job_id=?",
                (job["job_id"],),
            )[0]
            attempt = AttemptRecord(
                attempt_id=command.attempt_id,
                job_id=job["job_id"],
                attempt_number=number,
                tool_versions=command.tool_versions,
                input_hash=job["input_hash"],
                owner_principal_id=principal["principal_id"],
                worker_id=command.worker_id,
                boot_id=command.boot_id,
                authority_epoch=authority["epoch"],
                fencing_token=fence,
                status="running",
                started_at=now,
                ended_at=None,
                error_code=None,
                operation_id=command.operation_id,
                evidence=(),
            )
            self._insert(
                "attempts",
                attempt_id=attempt.attempt_id,
                job_id=attempt.job_id,
                attempt_number=number,
                owner_principal_id=attempt.owner_principal_id,
                worker_id=attempt.worker_id,
                boot_id=attempt.boot_id,
                epoch=attempt.authority_epoch,
                fence=fence,
                input_hash=attempt.input_hash,
                status="running",
                operation_id=command.operation_id,
                started_at=timestamp(now),
                body_json=canonical_json(attempt),
            )
            self._insert(
                "operations",
                operation_id=command.operation_id,
                film_id=job["film_id"],
                kind=job["stage"],
                job_id=job["job_id"],
                attempt_id=attempt.attempt_id,
                owner_principal_id=attempt.owner_principal_id,
                status="running",
                created_at=timestamp(now),
                updated_at=timestamp(now),
                body_json="{}",
            )
            assignment = AssignmentRecord(
                assignment_id=str(uuid4()),
                job_id=job["job_id"],
                attempt_id=attempt.attempt_id,
                owner_principal_id=attempt.owner_principal_id,
                worker_id=attempt.worker_id,
                boot_id=attempt.boot_id,
                authority_epoch=attempt.authority_epoch,
                fencing_token=fence,
                resource_id=command.resource_id,
                claimed_at=now,
                renewed_at=now,
                expires_at=now + timedelta(seconds=authority["lease_seconds"]),
                status="running",
                local_owner=None,
                version=1,
            )
            self._insert(
                "assignments",
                assignment_id=assignment.assignment_id,
                job_id=assignment.job_id,
                attempt_id=assignment.attempt_id,
                owner_principal_id=assignment.owner_principal_id,
                worker_id=assignment.worker_id,
                boot_id=assignment.boot_id,
                epoch=assignment.authority_epoch,
                fence=fence,
                physical_resource_id=command.resource_id,
                claimed_at=timestamp(now),
                renewed_at=timestamp(now),
                lease_deadline=timestamp(assignment.expires_at),
                state="running",
                body_json=canonical_json(assignment),
            )
            if command.resource_id is not None:
                owner = ResourceOwnerRecord(
                    resource_id=command.resource_id,
                    assignment_id=assignment.assignment_id,
                    attempt_id=attempt.attempt_id,
                    generation=fence,
                    state="reserved",
                    local_owner=None,
                )
                self._insert(
                    "resource_owners",
                    assignment_id=assignment.assignment_id,
                    physical_resource_id=command.resource_id,
                    attempt_id=attempt.attempt_id,
                    owner_generation=fence,
                    state="reserved",
                    body_json=canonical_json(owner),
                )
            version = self._set_stage(job, "running", attempt_id=attempt.attempt_id)
            return Mutation(
                job["job_id"],
                version,
                "running",
                operation_id=command.operation_id,
                assignment_id=assignment.assignment_id,
                attempt_id=attempt.attempt_id,
            )

        return self._execute(auth, meta, command, "claim", apply)

    def _assignment(
        self, command: AssignmentRenewal, principal: sqlite3.Row, now: datetime
    ) -> sqlite3.Row:
        row = self._one(
            "SELECT * FROM assignments WHERE assignment_id=?", (command.assignment_id,)
        )
        if row["version"] != command.expected_version:
            raise WorkflowError("cas_conflict")
        if (
            row["owner_principal_id"] != principal["principal_id"]
            or row["attempt_id"] != command.attempt_id
            or row["worker_id"] != command.worker_id
            or row["boot_id"] != command.boot_id
            or row["epoch"] != command.authority_epoch
            or row["fence"] != command.fencing_token
            or row["state"] != "running"
            or parse_time(row["lease_deadline"]) <= now
        ):
            raise WorkflowError("stale_attempt")
        authority = self._one("SELECT epoch FROM authority WHERE singleton=1")
        if row["epoch"] != authority["epoch"]:
            raise WorkflowError("stale_attempt")
        if row["worker_id"] is not None:
            worker = self._one(
                "SELECT current_boot_id FROM workers WHERE worker_id=?",
                (row["worker_id"],),
            )
            if worker["current_boot_id"] != command.boot_id:
                raise WorkflowError("stale_attempt")
        elif principal["boot_id"] != command.boot_id:
            raise WorkflowError("stale_attempt")
        job = self._current_job(row["job_id"])
        if job["current_attempt_id"] != command.attempt_id or job["status"] not in (
            "running",
            "succeeded",
            "failed",
        ):
            raise WorkflowError("stale_attempt")
        self._check_local_owner(row, command.local_owner)
        return row

    @staticmethod
    def _check_local_owner(
        row: sqlite3.Row, owner: LocalOwnerEvidence | None, *, stopped: bool = False
    ) -> None:
        old = AssignmentRecord.model_validate_json(row["body_json"]).local_owner
        if owner is not None:
            if owner.boot_id != row["boot_id"] or (
                owner.state in ("running", "stopped")
                and owner.evidence.status != "passed"
            ):
                raise WorkflowError("stale_attempt")
            if old is not None and (
                owner.owner_id,
                owner.generation,
                owner.boot_id,
                owner.process_identity,
            ) != (old.owner_id, old.generation, old.boot_id, old.process_identity):
                raise WorkflowError("stale_attempt")
        if stopped and (row["physical_resource_id"] is not None or old is not None):
            if (
                owner is None
                or owner.state != "stopped"
                or owner.evidence.status != "passed"
            ):
                raise WorkflowError("precondition_failed")

    def _set_assignment(
        self,
        row: sqlite3.Row,
        status: str,
        now: datetime,
        *,
        owner: LocalOwnerEvidence | None,
        expires_at: datetime | None = None,
    ) -> int:
        old = AssignmentRecord.model_validate_json(row["body_json"])
        actual_owner = owner if owner is not None else old.local_owner
        changes: dict[str, Any] = {
            "state": status,
            "resolved_at": timestamp(now) if status == "resolved" else None,
        }
        body_changes: dict[str, Any] = {
            "status": status,
            "local_owner": actual_owner,
            "version": row["version"] + 1,
        }
        if expires_at is not None:
            changes.update(
                renewed_at=timestamp(now), lease_deadline=timestamp(expires_at)
            )
            body_changes.update(renewed_at=now, expires_at=expires_at)
        version = self._update(
            "assignments",
            "assignment_id",
            row["assignment_id"],
            row["version"],
            **changes,
            body_json=canonical_json(old.model_copy(update=body_changes)),
        )
        resource = self._conn.execute(
            "SELECT * FROM resource_owners WHERE assignment_id=?",
            (row["assignment_id"],),
        ).fetchone()
        if resource is not None:
            owner_value = {
                "schema_version": 1,
                "resource_id": resource["physical_resource_id"],
                "assignment_id": row["assignment_id"],
                "attempt_id": row["attempt_id"],
                "generation": resource["owner_generation"],
                "state": status,
                "local_owner": actual_owner,
            }
            self._update(
                "resource_owners",
                "assignment_id",
                row["assignment_id"],
                resource["version"],
                state=status,
                resolved_at=timestamp(now) if status == "resolved" else None,
                body_json=canonical_json(owner_value),
            )
        return version

    def renew(
        self, auth: AuthProof, meta: CommandMeta, command: AssignmentRenewal
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._assignment(command, principal, now)
            seconds = self._one(
                "SELECT lease_seconds FROM authority WHERE singleton=1"
            )[0]
            version = self._set_assignment(
                row,
                "running",
                now,
                owner=command.local_owner,
                expires_at=now + timedelta(seconds=seconds),
            )
            return Mutation(
                row["assignment_id"],
                version,
                "running",
                assignment_id=row["assignment_id"],
                attempt_id=row["attempt_id"],
            )

        return self._execute(auth, meta, command, "renew", apply)

    def _set_attempt(
        self,
        row: sqlite3.Row,
        status: str,
        now: datetime,
        evidence: tuple[EvidenceRef, ...],
        error_code: str | None = None,
    ) -> None:
        value = AttemptRecord.model_validate_json(row["body_json"]).model_copy(
            update={
                "status": status,
                "ended_at": now
                if status in ("succeeded", "failed", "blocked")
                else None,
                "evidence": evidence,
                "error_code": error_code,
            }
        )
        self._update(
            "attempts",
            "attempt_id",
            row["attempt_id"],
            row["version"],
            status=status,
            ended_at=timestamp(value.ended_at) if value.ended_at else None,
            body_json=canonical_json(value),
        )

    def _set_operation(
        self, operation_id: str, status: str, now: datetime, *, resolved: bool = False
    ) -> None:
        row = self._one(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        )
        self._update(
            "operations",
            "operation_id",
            operation_id,
            row["version"],
            status=status,
            updated_at=timestamp(now),
            resolved_at=timestamp(now) if resolved else None,
        )

    def _completion(
        self,
        job: sqlite3.Row,
        attempt_id: str,
        *,
        output_hash: str | None = None,
        receipt_id: str | None = None,
        reconcile: bool = False,
    ) -> FinalManifest | None:
        if job["stage"] == "return":
            row = self._conn.execute(
                "SELECT * FROM receipts WHERE kind='artifact' AND attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise WorkflowError("precondition_failed")
            value = ArtifactReceipt.model_validate_json(row["body_json"])
            if (
                value.status != "passed"
                or value.input_hash != job["input_hash"]
                or (
                    not reconcile
                    and (
                        value.receipt_id != receipt_id
                        or value.output_manifest_hash != output_hash
                    )
                )
            ):
                raise WorkflowError("precondition_failed")
        if job["stage"] != "scrape":
            return None
        row = self._conn.execute(
            "SELECT * FROM manifests WHERE kind='final' AND attempt_id=? ORDER BY revision DESC LIMIT 1",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError("precondition_failed")
        final = FinalManifest.model_validate_json(row["body_json"])
        film = FilmRecord.model_validate_json(
            self._film_row(job["film_id"])["body_json"]
        )
        policy = film.policy
        pp = final.postprocess
        processed_ids = {item.item_id for item in pp.items if item.status == "passed"}
        if (
            (not reconcile and final.manifest_hash != output_hash)
            or final.input_hash != job["input_hash"]
            or final.input_revision != job["input_revision"]
            or pp.status != "passed"
            or not pp.full_process
            or pp.evidence.status != "passed"
            or pp.image_refresh in ("failed", "unknown")
            or any(item.status != "passed" for item in pp.items)
            or any(probe.evidence.status != "passed" for probe in final.media_probes)
            or not set(policy.required_image_roles).issubset(
                {item.role for item in final.items if item.item_id in processed_ids}
            )
            or (
                policy.folder_cover_required
                and (not pp.folder_required or pp.folder_item_id is None)
            )
        ):
            raise WorkflowError("precondition_failed")
        if "restore" in policy.required_stages and (
            final.restoration.status not in ("applied", "reused")
            or final.restoration.evidence.status != "passed"
        ):
            raise WorkflowError("precondition_failed")
        subtitle = final.subtitles
        if policy.subtitle_requirement != "none" and (
            subtitle.status not in ("present", "no_speech")
            or subtitle.evidence.status != "passed"
            or (
                subtitle.status == "present"
                and policy.subtitle_requirement == "chinese"
                and subtitle.language
                not in ("zh", "zh-CN", "zh-TW", "zh-Hans", "zh-Hant")
            )
        ):
            raise WorkflowError("precondition_failed")
        return final

    def _publish_final(self, job: sqlite3.Row, final: FinalManifest | None) -> None:
        if final is not None:
            film = self._film_row(job["film_id"])
            # Reconciliation can preserve historical facts without advancing a newer input.
            if (
                film["current_input_revision"] == job["input_revision"]
                and film["current_final_revision"] != final.revision
            ):
                self._set_film(
                    film,
                    current_final_revision=final.revision,
                    context_state="invalidated",
                )

    def finish_attempt(
        self, auth: AuthProof, meta: CommandMeta, command: AttemptResult
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            assignment = self._assignment(command, principal, now)
            job = self._current_job(assignment["job_id"])
            if (
                job["job_id"] != command.job_id
                or job["input_hash"] != command.input_hash
                or job["status"] != "running"
            ):
                raise WorkflowError("stale_attempt")
            attempt = self._one(
                "SELECT * FROM attempts WHERE attempt_id=?", (command.attempt_id,)
            )
            status = command.outcome
            if status != "unknown" and (
                not _passed(command.evidence)
                or any(
                    item.producer_id != principal["principal_id"]
                    for item in command.evidence
                )
            ):
                raise WorkflowError("precondition_failed")
            final = None
            if status == "succeeded":
                final = self._completion(
                    job,
                    command.attempt_id,
                    output_hash=command.result_manifest_hash,
                    receipt_id=command.artifact_receipt_id,
                )
            elif status == "failed" and (
                command.local_owner is None or command.local_owner.state != "stopped"
            ):
                status = "unknown"
            self._set_attempt(
                attempt, status, now, command.evidence, command.error_code
            )
            self._set_stage(job, status)
            version = self._set_assignment(
                assignment,
                "unknown" if status == "unknown" else "running",
                now,
                owner=command.local_owner,
            )
            self._set_operation(
                attempt["operation_id"],
                "verified" if status == "succeeded" else status,
                now,
            )
            self._publish_final(job, final)
            return Mutation(
                assignment["assignment_id"],
                version,
                status,
                assignment_id=assignment["assignment_id"],
                attempt_id=command.attempt_id,
            )

        return self._execute(auth, meta, command, "finish_attempt", apply)

    def _observation(
        self, command: UnknownObservation
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        assignment = self._one(
            "SELECT * FROM assignments WHERE assignment_id=?", (command.assignment_id,)
        )
        if assignment["version"] != command.expected_version:
            raise WorkflowError("cas_conflict")
        attempt = self._one(
            "SELECT * FROM attempts WHERE attempt_id=?", (assignment["attempt_id"],)
        )
        if (
            assignment["resolved_at"] is not None
            or command.attempt_id != assignment["attempt_id"]
            or command.operation_id != attempt["operation_id"]
        ):
            raise WorkflowError("stale_attempt")
        self._check_local_owner(assignment, command.local_owner)
        job = self._one("SELECT * FROM stages WHERE job_id=?", (assignment["job_id"],))
        return assignment, attempt, job

    def mark_unknown(
        self, auth: AuthProof, meta: CommandMeta, command: UnknownObservation
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            assignment, attempt, job = self._observation(command)
            # Proven business success survives uncertainty about process ownership.
            if job["status"] not in ("succeeded", "failed", "blocked"):
                self._set_stage(job, "unknown")
                self._set_attempt(attempt, "unknown", now, command.evidence)
                self._set_operation(attempt["operation_id"], "unknown", now)
            version = self._set_assignment(
                assignment, "unknown", now, owner=command.local_owner
            )
            return Mutation(
                assignment["assignment_id"],
                version,
                "unknown",
                assignment_id=assignment["assignment_id"],
                attempt_id=attempt["attempt_id"],
            )

        return self._execute(auth, meta, command, "mark_unknown", apply)

    def resolve_assignment(
        self, auth: AuthProof, meta: CommandMeta, command: AssignmentResolution
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            assignment, attempt, job = self._observation(command)
            if not _passed(command.evidence):
                raise WorkflowError("precondition_failed")
            self._check_local_owner(assignment, command.local_owner, stopped=True)
            if (
                job["status"] in ("succeeded", "failed", "blocked")
                and job["status"] != command.outcome
            ):
                raise WorkflowError("precondition_failed")
            final = (
                self._completion(job, attempt["attempt_id"], reconcile=True)
                if command.outcome == "succeeded"
                else None
            )
            if job["status"] != command.outcome:
                self._set_stage(job, command.outcome)
                self._set_attempt(attempt, command.outcome, now, command.evidence)
            self._set_operation(
                attempt["operation_id"],
                "verified" if command.outcome == "succeeded" else "failed",
                now,
                resolved=True,
            )
            version = self._set_assignment(
                assignment, "resolved", now, owner=command.local_owner
            )
            self._publish_final(job, final)
            self._close_finished_route(job["film_id"], job["input_revision"], now)
            return Mutation(
                assignment["assignment_id"],
                version,
                command.outcome,
                assignment_id=assignment["assignment_id"],
                attempt_id=attempt["attempt_id"],
            )

        return self._execute(auth, meta, command, "resolve_assignment", apply)

    def retry_stage(
        self, auth: AuthProof, meta: CommandMeta, command: StageRetry
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            job = self._current_job(command.job_id)
            if job["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            if job["stage"] not in STAGE_ROLES or job["status"] not in (
                "failed",
                "blocked",
            ):
                raise WorkflowError("precondition_failed")
            if self._conn.execute(
                "SELECT 1 FROM assignments WHERE job_id=? AND resolved_at IS NULL",
                (job["job_id"],),
            ).fetchone():
                raise WorkflowError("precondition_failed")
            self._dependencies(job)
            version = self._set_stage(job, "pending")
            return Mutation(job["job_id"], version, "pending")

        return self._execute(auth, meta, command, "retry_stage", apply)

    def skip_stage(
        self, auth: AuthProof, meta: CommandMeta, command: StageSkip
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            job = self._current_job(command.job_id)
            if job["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            film = FilmRecord.model_validate_json(
                self._film_row(job["film_id"])["body_json"]
            )
            if (
                job["stage"] not in _COMPUTE
                or job["stage"] not in film.policy.skippable_stages
                or job["status"] not in ("pending", "blocked")
                or command.policy_revision != film.policy.policy_revision
                or command.input_hash != job["input_hash"]
                or not _passed(command.evidence)
                or any(
                    item.kind not in film.policy.skip_evidence_kinds
                    for item in command.evidence
                )
                or self._conn.execute(
                    "SELECT 1 FROM assignments WHERE job_id=? AND resolved_at IS NULL",
                    (job["job_id"],),
                ).fetchone()
            ):
                raise WorkflowError("precondition_failed")
            self._dependencies(job)
            version = self._set_stage(
                job, "skipped", evidence=command.evidence, reason=command.reason
            )
            self._close_finished_route(job["film_id"], job["input_revision"], now)
            return Mutation(job["job_id"], version, "skipped")

        return self._execute(auth, meta, command, "skip_stage", apply)

    def film_intake_open(self, auth: AuthProof, film_id: str) -> bool:
        """Shared removal guard: this film's package is still being copied."""
        with self._lock:
            self._check_auth(auth, self._observe_time())
            return self._film_intake_open(film_id)

    def _film_intake_open(self, film_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM intake_operations o JOIN intake_films i "
                "ON i.package_id=o.package_id WHERE i.film_id=? AND o.unresolved=1",
                (film_id,),
            ).fetchone()
            is not None
        )

    def remove_film(
        self, auth: AuthProof, meta: CommandMeta, command: FilmRemove
    ) -> CommandResult:
        """删除此任务 (P3 E3): mark the film `removed` — terminal, every reader
        skips it, its history stays. Refused (`precondition_failed`) while any
        of its stages runs, any assignment or route of it is open, or an
        approval, archive or cleanup exists: the checks run in this
        transaction, so a claim racing the delete wins and nothing changes.
        Nothing on disk is touched here."""

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._film_row(command.film_id)
            if row["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            film = FilmRecord.model_validate_json(row["body_json"])
            fid = command.film_id
            if self._film_intake_open(fid):
                raise WorkflowError("film_intaking")
            if (
                film.removed_at is not None
                or self._conn.execute(
                    "SELECT 1 FROM stages WHERE film_id=? AND status IN ('running','unknown') "
                    "UNION ALL SELECT 1 FROM assignments a JOIN stages s ON s.job_id=a.job_id "
                    "WHERE s.film_id=? AND a.resolved_at IS NULL "
                    "UNION ALL SELECT 1 FROM film_routes WHERE film_id=? AND state='open' "
                    "UNION ALL SELECT 1 FROM operations WHERE film_id=? "
                    "AND kind IN ('archive','cleanup_input','cleanup_workcopy') "
                    # the board's `approval_active`: a live approval of the current
                    # ready context (an older one can never archive: confirm again)
                    "UNION ALL SELECT 1 FROM approvals p JOIN films f "
                    "ON f.film_id=p.film_id AND p.context_revision=f.current_context_revision "
                    "WHERE p.film_id=? AND p.revoked_at IS NULL AND f.context_state='ready' "
                    "LIMIT 1",
                    (fid, fid, fid, fid, fid),
                ).fetchone()
            ):
                raise WorkflowError("precondition_failed")
            removed = film.model_copy(
                update={"removed_at": now, "version": row["version"] + 1}
            )
            version = self._update(
                "films",
                "film_id",
                fid,
                row["version"],
                body_json=canonical_json(removed),
            )
            # Removal releases this film's cleanup hold, never the source files.
            self._conn.execute(
                "UPDATE source_refs SET retained=0 WHERE film_id=?", (fid,)
            )
            return Mutation(fid, version, "removed")

        return self._execute(auth, meta, command, "remove_film", apply)

    def accept_artifact_receipt(
        self, auth: AuthProof, meta: CommandMeta, command: ArtifactReceipt
    ) -> CommandResult:
        """Trust the authorized receiver's complete-package assertion, not a sender.

        T04 must calculate the hash from its actual intermediate package manifest.
        This ledger cannot discover items omitted from an otherwise valid receiver
        assertion; it does verify every declared item's evidence and ownership.
        """

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            operation = self._one(
                "SELECT * FROM operations WHERE operation_id=?", (command.operation_id,)
            )
            attempt = self._one(
                "SELECT * FROM attempts WHERE attempt_id=?", (command.attempt_id,)
            )
            if (
                command.receiver_id != principal["principal_id"]
                or operation["owner_principal_id"] != principal["principal_id"]
                or operation["kind"] != "return"
                or operation["attempt_id"] != command.attempt_id
                or command.input_hash != attempt["input_hash"]
                or command.status != "passed"
                or any(
                    item.evidence.status != "passed"
                    or item.evidence.producer_id != principal["principal_id"]
                    or item.item_id not in item.evidence.summary.item_ids
                    for item in command.items
                )
            ):
                raise WorkflowError("precondition_failed")
            value = command.model_copy(update={"accepted_at": now})
            self._insert(
                "receipts",
                receipt_id=value.receipt_id,
                kind="artifact",
                operation_id=value.operation_id,
                attempt_id=value.attempt_id,
                producer_id=principal["principal_id"],
                manifest_hash=value.output_manifest_hash,
                accepted_at=timestamp(now),
                body_json=canonical_json(value),
            )
            return Mutation(
                value.receipt_id,
                1,
                "accepted",
                operation_id=value.operation_id,
                attempt_id=value.attempt_id,
            )

        return self._execute(auth, meta, command, "accept_artifact_receipt", apply)
