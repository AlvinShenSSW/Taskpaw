"""Scoped workflow views never expose credential or pairing digests."""

import json

from .auth import AuthProof
from .models import (
    ApprovalContext,
    ApprovalRecord,
    ArchiveOperation,
    AssignmentRecord,
    AttemptRecord,
    CleanupIntent,
    ExecutionOperation,
    FilmRecord,
    FilmView,
    FilmWorkflow,
    FinalManifest,
    OpenAssignment,
    OperationView,
    PackageFilm,
    PendingComputeJob,
    StageRecord,
    WorkflowApproval,
    WorkflowArchiveOperation,
    WorkflowCleanupItem,
    WorkflowCleanupOperation,
    WorkflowError,
)
from .store_core import WorkflowCore, parse_time

# Stages the controller's own runtime claims (compute on its GPU; return and
# Miko scrape locally).
_LOCAL_STAGES = frozenset({"restore", "asr", "translate", "return", "scrape"})
# Approval-bound stages driven through operations, never assignments (F02-P1).
_EXTERNAL_STAGES = frozenset({"archive", "cleanup_input", "cleanup_workcopy"})


class ReadCommands(WorkflowCore):
    def get_film(self, auth: AuthProof, film_id: str) -> FilmView:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            own_jobs = None
            if principal["role"] not in ("operator", "observer"):
                owned = self._conn.execute(
                    "SELECT job_id FROM operations WHERE film_id=? AND owner_principal_id=?",
                    (film_id, principal["principal_id"]),
                ).fetchall()
                created = (
                    principal["role"] == "intake_adapter"
                    and self._conn.execute(
                        "SELECT 1 FROM audit WHERE entity_id=? AND principal_id=? AND command_kind='create_film' LIMIT 1",
                        (film_id, principal["principal_id"]),
                    ).fetchone()
                )
                if not created and principal["role"] == "intake_adapter":
                    created = self._conn.execute(
                        "SELECT 1 FROM intake_films i JOIN intake_packages p ON p.package_id=i.package_id "
                        "JOIN intake_roots r ON r.root_id=p.root_id AND r.current=1 "
                        "WHERE i.film_id=? AND r.component_id=? LIMIT 1",
                        (film_id, principal["component_id"]),
                    ).fetchone()
                if not owned and not created:
                    raise WorkflowError("forbidden")
                if principal["role"] == "compute_worker":
                    own_jobs = {row[0] for row in owned}
            row = self._one("SELECT * FROM films WHERE film_id=?", (film_id,))
            film = FilmRecord.model_validate_json(row["body_json"])
            jobs = self._conn.execute(
                "SELECT * FROM stages WHERE film_id=? AND input_revision=? ORDER BY rowid",
                (film_id, row["current_input_revision"]),
            ).fetchall()
            stages = tuple(
                StageRecord.model_validate_json(job["body_json"])
                for job in jobs
                if own_jobs is None or job["job_id"] in own_jobs
            )
            context = None
            if (
                principal["role"] == "operator"
                and row["current_context_revision"] is not None
            ):
                context = ApprovalContext.model_validate_json(
                    self._one(
                        "SELECT body_json FROM approval_contexts WHERE film_id=? AND context_revision=?",
                        (film_id, row["current_context_revision"]),
                    )[0]
                )
            return FilmView(film=film, stages=stages, context=context)

    def get_operation(self, auth: AuthProof, operation_id: str) -> OperationView:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            row = self._one(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            )
            if (
                principal["role"] not in ("operator", "observer")
                and row["owner_principal_id"] != principal["principal_id"]
            ):
                raise WorkflowError("forbidden")
            if principal["role"] != "observer":
                if row["kind"] == "archive":
                    return OperationView(
                        archive=ArchiveOperation.model_validate_json(row["body_json"])
                    )
                if row["kind"] in ("cleanup_input", "cleanup_workcopy"):
                    return OperationView(
                        cleanup=CleanupIntent.model_validate_json(row["body_json"])
                    )
            return OperationView(
                execution=ExecutionOperation(
                    operation_id=operation_id,
                    film_id=row["film_id"],
                    kind=row["kind"],
                    job_id=row["job_id"],
                    attempt_id=row["attempt_id"],
                    status=row["status"],
                    version=row["version"],
                )
            )

    def list_pending_compute_jobs(
        self, auth: AuthProof
    ) -> tuple[PendingComputeJob, ...]:
        """Pending restore/asr/translate jobs ready to claim, oldest film first.

        Only the dependency status is checked here; `claim` enforces the rest.
        """
        return self.list_compute_jobs(auth, "pending")

    def list_compute_jobs(
        self, auth: AuthProof, status: str
    ) -> tuple[PendingComputeJob, ...]:
        """Current-revision compute jobs in `status` whose dependencies are done."""
        return self.list_jobs(auth, status, ("restore", "asr", "translate"))

    def list_jobs(
        self, auth: AuthProof, status: str, stages: tuple[str, ...]
    ) -> tuple[PendingComputeJob, ...]:
        """Current-revision jobs of `stages` in `status` whose dependencies are done."""
        if not stages or not set(stages) <= _LOCAL_STAGES | _EXTERNAL_STAGES:
            raise WorkflowError("invalid_request")
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            marks = ",".join("?" for _ in stages)
            rows = self._conn.execute(
                "SELECT s.*,f.display_name,f.created_at AS film_created_at FROM stages s "
                "JOIN films f ON f.film_id=s.film_id AND f.current_input_revision=s.input_revision "
                f"WHERE s.status=? AND s.stage IN ({marks}) "
                "ORDER BY f.created_at,f.film_id,s.rowid",
                (status, *stages),
            ).fetchall()
            statuses = {
                row[0]: row[1]
                for row in self._conn.execute("SELECT job_id,status FROM stages")
            }
            jobs = []
            for row in rows:
                record = StageRecord.model_validate_json(row["body_json"])
                if all(
                    statuses.get(dep) in ("succeeded", "skipped")
                    for dep in record.dependency_job_ids
                ):
                    jobs.append(
                        PendingComputeJob(
                            job_id=row["job_id"],
                            film_id=row["film_id"],
                            display_name=row["display_name"],
                            stage=row["stage"],
                            input_revision=row["input_revision"],
                            version=row["version"],
                            film_created_at=parse_time(row["film_created_at"]),
                        )
                    )
            return tuple(jobs)

    def list_attempts(self, auth: AuthProof, job_id: str) -> tuple[AttemptRecord, ...]:
        """Every attempt of one job, oldest first (retry accounting, T03-P1 C6)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT body_json FROM attempts WHERE job_id=? ORDER BY attempt_number",
                (job_id,),
            ).fetchall()
            return tuple(AttemptRecord.model_validate_json(row[0]) for row in rows)

    def list_open_assignments(self, auth: AuthProof) -> tuple[OpenAssignment, ...]:
        """Every unresolved assignment with its stage and owner component (T04 D02/D17)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT a.body_json,s.stage,p.component_id FROM assignments a "
                "JOIN stages s ON s.job_id=a.job_id "
                "JOIN principals p ON p.principal_id=a.owner_principal_id "
                "WHERE a.resolved_at IS NULL ORDER BY a.claimed_at,a.assignment_id"
            ).fetchall()
            return tuple(
                OpenAssignment(
                    assignment=AssignmentRecord.model_validate_json(row[0]),
                    stage=row[1],
                    owner_component_id=row[2],
                )
                for row in rows
            )

    def get_stage(self, auth: AuthProof, job_id: str) -> StageRecord:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            row = self._one("SELECT body_json FROM stages WHERE job_id=?", (job_id,))
            return StageRecord.model_validate_json(row[0])

    def component_generation(self, auth: AuthProof, principal_id: str) -> int:
        """Highest credential generation of a principal, 0 if unknown (F02-P1 D11)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            row = self._one(
                "SELECT coalesce(max(generation),0) FROM credentials WHERE principal_id=?",
                (principal_id,),
            )
            return int(row[0])

    def film_workflow(self, auth: AuthProof, film_id: str) -> FilmWorkflow:
        """Approval, archive and cleanup state for the F02 runtime (D09)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            film = self._one("SELECT * FROM films WHERE film_id=?", (film_id,))
            revision = film["current_input_revision"]
            statuses = {
                row["stage"]: row["status"]
                for row in self._conn.execute(
                    "SELECT stage,status FROM stages WHERE film_id=? AND input_revision=?",
                    (film_id, revision),
                )
            }
            approval = None
            row = self._conn.execute(
                "SELECT body_json FROM approvals WHERE film_id=? AND context_revision=? "
                "ORDER BY rowid DESC LIMIT 1",
                (film_id, film["current_context_revision"]),
            ).fetchone()
            if row is not None:
                record = ApprovalRecord.model_validate_json(row[0])
                approval = WorkflowApproval(
                    approval_id=record.approval_id,
                    context_revision=record.context_revision,
                    binding=record.binding,
                    binding_hash=record.binding_hash,
                    expires_at=record.expires_at,
                    revoked=record.revoked_at is not None,
                )
            archives = []
            cleanups = []
            for row in self._conn.execute(
                "SELECT * FROM operations WHERE film_id=? AND kind IN "
                "('archive','cleanup_input','cleanup_workcopy') ORDER BY created_at,rowid",
                (film_id,),
            ).fetchall():
                if row["kind"] == "archive":
                    value = ArchiveOperation.model_validate_json(row["body_json"])
                    archives.append(
                        WorkflowArchiveOperation(
                            operation_id=row["operation_id"],
                            approval_id=row["approval_id"],
                            status=row["status"],
                            version=row["version"],
                            resolved=row["resolved_at"] is not None,
                            error_code=value.error_code,
                        )
                    )
                    continue
                items = self._conn.execute(
                    "SELECT item_id,status FROM cleanup_items WHERE operation_id=? ORDER BY rowid",
                    (row["operation_id"],),
                ).fetchall()
                cleanups.append(
                    WorkflowCleanupOperation(
                        operation_id=row["operation_id"],
                        approval_id=row["approval_id"],
                        scope=row["scope"],
                        version=row["version"],
                        status=row["status"],
                        resolved=row["resolved_at"] is not None,
                        items=tuple(
                            WorkflowCleanupItem(item_id=item[0], status=item[1])
                            for item in items
                        ),
                    )
                )
            receipt = self._conn.execute(
                "SELECT r.receipt_id,r.approval_id FROM receipts r "
                "JOIN operations o ON o.operation_id=r.operation_id "
                "WHERE r.kind='archive' AND o.film_id=? ORDER BY r.rowid DESC LIMIT 1",
                (film_id,),
            ).fetchone()
            intake = self._conn.execute(
                "SELECT i.package_id,i.snapshot_id,s.operation_id,s.body_json,"
                "p.version AS package_version,p.relative_path AS package_path,p.current AS package_current "
                "FROM intake_films i JOIN intake_snapshots s ON s.snapshot_id=i.snapshot_id "
                "JOIN intake_packages p ON p.package_id=i.package_id "
                "WHERE i.film_id=? AND i.input_revision=?",
                (film_id, revision),
            ).fetchone()
            return FilmWorkflow(
                film_id=film_id,
                version=film["version"],
                input_revision=revision,
                context_revision=film["current_context_revision"],
                context_state=film["context_state"],
                approval=approval,
                archive_operations=tuple(archives),
                archive_receipt_id=receipt[0] if receipt else None,
                archive_receipt_approval_id=receipt[1] if receipt else None,
                cleanup_operations=tuple(cleanups),
                scrape_status=statuses.get("scrape"),
                archive_status=statuses.get("archive"),
                cleanup_input_status=statuses.get("cleanup_input"),
                cleanup_workcopy_status=statuses.get("cleanup_workcopy"),
                package_id=intake["package_id"] if intake else None,
                intake_operation_id=intake["operation_id"] if intake else None,
                snapshot_id=intake["snapshot_id"] if intake else None,
                snapshot_work_path=json.loads(intake["body_json"])["relative_work_path"]
                if intake
                else None,
                package_version=intake["package_version"] if intake else None,
                package_path=intake["package_path"] if intake else None,
                package_current=bool(intake["package_current"]) if intake else False,
            )

    def package_films(
        self, auth: AuthProof, package_id: str
    ) -> tuple[PackageFilm, ...]:
        """Films whose current input revision is linked to the package (V3-3)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            return self._package_films(package_id)

    def _package_films(self, package_id: str) -> tuple[PackageFilm, ...]:
        # Same link as `_invalidate_intake_dependents`: older input revisions of
        # a re-ingested film no longer belong to this package.
        rows = self._conn.execute(
            "SELECT f.film_id,"
            "(SELECT status FROM stages s WHERE s.film_id=f.film_id "
            "AND s.input_revision=f.current_input_revision AND s.stage='cleanup_input'),"
            "(SELECT status FROM stages s WHERE s.film_id=f.film_id "
            "AND s.input_revision=f.current_input_revision AND s.stage='cleanup_workcopy') "
            "FROM films f JOIN intake_films i ON i.film_id=f.film_id "
            "AND i.input_revision=f.current_input_revision WHERE i.package_id=? "
            "ORDER BY f.created_at,f.film_id",
            (package_id,),
        ).fetchall()
        return tuple(
            PackageFilm(
                film_id=row[0],
                cleanup_input_status=row[1],
                cleanup_workcopy_status=row[2],
            )
            for row in rows
        )

    def get_final_manifest(
        self, auth: AuthProof, film_id: str, revision: int
    ) -> FinalManifest:
        """A published final manifest (F02-P1: archive files and workcopy paths)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            row = self._one(
                "SELECT body_json FROM manifests WHERE film_id=? AND kind='final' AND revision=?",
                (film_id, revision),
            )
            return FinalManifest.model_validate_json(row[0])

    def latest_final_revision(self, auth: AuthProof, film_id: str) -> int:
        """Highest final-manifest revision of a film, published or not (M01 M4)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            row = self._one(
                "SELECT coalesce(max(revision),0) FROM manifests WHERE film_id=? AND kind='final'",
                (film_id,),
            )
            return int(row[0])
