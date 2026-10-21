"""Scoped workflow views never expose credential or pairing digests."""

import json
from pathlib import PurePosixPath
from typing import Any

from .auth import AuthProof
from .models import (
    ApprovalContext,
    ApprovalRecord,
    ArchiveOperation,
    AssignmentRecord,
    AttemptRecord,
    BoardFilm,
    BoardStage,
    CleanupIntent,
    ExecutionOperation,
    FilmPlan,
    FilmRecord,
    FilmView,
    FilmWorkflow,
    FinalManifest,
    OpenAssignment,
    OperationView,
    PackageFilm,
    PendingComputeJob,
    StageRecord,
    UnplannedFilm,
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
            # U1 P1': a restore is offered only under a confirmed plan, whose
            # tier rides along to the executor (P4')
            rows = self._conn.execute(
                "SELECT s.*,f.display_name,f.created_at AS film_created_at,"
                "p.ltx_model AS plan_ltx_model FROM stages s "
                "JOIN films f ON f.film_id=s.film_id AND f.current_input_revision=s.input_revision "
                "LEFT JOIN film_plans p ON p.film_id=s.film_id AND p.input_revision=s.input_revision "
                f"WHERE s.status=? AND s.stage IN ({marks}) "
                "AND (s.stage!='restore' OR p.film_id IS NOT NULL) "
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
                            ltx_model=row["plan_ltx_model"]
                            if row["stage"] == "restore"
                            else None,
                        )
                    )
            return tuple(jobs)

    def list_unplanned(self, auth: AuthProof) -> tuple[UnplannedFilm, ...]:
        """Films whose current-revision restore waits for a plan (待处理), oldest first."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT f.film_id,f.current_input_revision,f.display_name,f.created_at "
                "FROM films f JOIN stages s ON s.film_id=f.film_id "
                "AND s.input_revision=f.current_input_revision AND s.stage='restore' "
                "LEFT JOIN film_plans p ON p.film_id=f.film_id "
                "AND p.input_revision=f.current_input_revision "
                "WHERE s.status='pending' AND p.film_id IS NULL "
                "ORDER BY f.created_at,f.film_id"
            ).fetchall()
            return tuple(
                UnplannedFilm(
                    film_id=row[0],
                    input_revision=row[1],
                    display_name=row[2],
                    created_at=parse_time(row[3]),
                )
                for row in rows
            )

    def list_plans(self, auth: AuthProof) -> tuple[FilmPlan, ...]:
        """Every confirmed plan, oldest first (the recommendation's history)."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT film_id,input_revision,ltx_model,machine,confirmed_at,"
                "backfilled,actors FROM film_plans ORDER BY confirmed_at,film_id,input_revision"
            ).fetchall()
            return tuple(
                FilmPlan(
                    film_id=row[0],
                    input_revision=row[1],
                    ltx_model=row[2],
                    machine=row[3],
                    confirmed_at=parse_time(row[4]),
                    backfilled=bool(row[5]),
                    actors=tuple(json.loads(row[6])),
                )
                for row in rows
            )

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

    def film_board(
        self,
        auth: AuthProof,
        done_limit: int | None = 20,
        *,
        film_id: str | None = None,
    ) -> tuple[BoardFilm, ...]:
        """Every film's current-revision stages for the 媒体入库 page (T06-P1 D03, N01).

        Oldest film first. `done_limit` keeps only the newest fully finished
        films (both cleanups succeeded); `film_id` reads one film.

        Three linear reads merged here (IR01): films with their current stages,
        every archive operation once, and the current contexts' unrevoked
        approvals. Per-row subqueries over operations made the board quadratic
        while it held the store lock.
        """
        where: tuple[str, tuple[Any, ...]] = ("", ())
        ops: tuple[str, tuple[Any, ...]] = ("", ())
        approved: tuple[str, tuple[Any, ...]] = ("", ())
        linked: tuple[str, tuple[Any, ...]] = ("", ())
        receipted: tuple[str, tuple[Any, ...]] = ("", ())
        if film_id is not None:
            where = ("WHERE f.film_id=?", (film_id,))
            receipted = ("AND o.film_id=?", (film_id,))
            linked = (
                "WHERE i.package_id IN "
                "(SELECT package_id FROM intake_films WHERE film_id=?)",
                (film_id,),
            )
            # an approval counts as used by any archive operation naming it
            ops = (
                "AND (film_id=? OR approval_id IN "
                "(SELECT approval_id FROM approvals WHERE film_id=?))",
                (film_id, film_id),
            )
            approved = ("AND f.film_id=?", (film_id,))
        elif done_limit is not None:
            # D13 before reading stage bodies: drop all but the newest finished films
            where = (
                "WHERE f.film_id NOT IN (SELECT film_id FROM (SELECT d.film_id,"
                "row_number() OVER (ORDER BY d.created_at DESC,d.film_id DESC) AS newest "
                "FROM films d JOIN stages c ON c.film_id=d.film_id "
                "AND c.input_revision=d.current_input_revision "
                "WHERE c.stage IN ('cleanup_input','cleanup_workcopy') AND c.status='succeeded' "
                "GROUP BY d.film_id HAVING count(*)=2) WHERE newest>?)",
                (done_limit,),
            )
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT f.film_id,f.display_name,f.created_at,"
                "f.current_input_revision AS input_revision,"
                "json_extract(fm.body_json,'$.output_relative_path') AS output_path,"
                "i.package_id,p.relative_path AS package_path,"
                "s.stage,s.status,json_extract(s.body_json,'$.reason') AS reason,"
                "a.body_json AS attempt_json,"
                # U1 V' / V3 §7: the plan and both manifests' revisions and hashes
                "fp.film_id IS NOT NULL AS planned,fp.ltx_model AS plan_ltx_model,"
                "fp.machine AS plan_machine,im.manifest_hash AS input_hash,"
                "fm.revision AS final_revision,fm.manifest_hash AS final_hash "
                "FROM films f "
                "LEFT JOIN manifests fm ON fm.film_id=f.film_id AND fm.kind='final' "
                "AND fm.revision=f.current_final_revision "
                "LEFT JOIN manifests im ON im.film_id=f.film_id AND im.kind='input' "
                "AND im.revision=f.current_input_revision "
                "LEFT JOIN film_plans fp ON fp.film_id=f.film_id "
                "AND fp.input_revision=f.current_input_revision "
                "LEFT JOIN intake_films i ON i.film_id=f.film_id "
                "AND i.input_revision=f.current_input_revision "
                "LEFT JOIN intake_packages p ON p.package_id=i.package_id "
                "LEFT JOIN stages s ON s.film_id=f.film_id "
                "AND s.input_revision=f.current_input_revision "
                "LEFT JOIN attempts a ON a.attempt_id=s.current_attempt_id "
                f"{where[0]} ORDER BY f.created_at,f.film_id,s.rowid",
                where[1],
            ).fetchall()
            # H3 D06: how many films' current inputs each package holds
            counts = self._conn.execute(
                "SELECT i.package_id,count(*) AS films FROM intake_films i "
                "JOIN films f ON f.film_id=i.film_id "
                f"AND f.current_input_revision=i.input_revision {linked[0]} "
                "GROUP BY i.package_id",
                linked[1],
            ).fetchall()
            archives = self._conn.execute(
                "SELECT film_id,approval_id,resolved_at IS NULL AS open,"
                "json_extract(body_json,'$.error_code') AS error "
                f"FROM operations WHERE kind='archive' {ops[0]} "
                "ORDER BY created_at,rowid",
                ops[1],
            ).fetchall()
            approvals = self._conn.execute(
                "SELECT p.film_id,p.approval_id FROM approvals p JOIN films f "
                "ON f.film_id=p.film_id AND p.context_revision=f.current_context_revision "
                # an invalidated context can never be archived: confirm again (Codex T06 r1)
                f"WHERE p.revoked_at IS NULL AND f.context_state='ready' {approved[0]}",
                approved[1],
            ).fetchall()
            receipts = self._conn.execute(
                "SELECT o.film_id,r.receipt_id FROM receipts r "
                "JOIN operations o ON o.operation_id=r.operation_id "
                f"WHERE r.kind='archive' {receipted[0]} ORDER BY r.rowid",
                receipted[1],
            ).fetchall()
            # the largest probe of each current input (V3 §7 resolution)
            probes = self._conn.execute(
                "SELECT f.film_id,max(json_extract(p.value,'$.width')),"
                "max(json_extract(p.value,'$.height')) FROM films f "
                "JOIN manifests im ON im.film_id=f.film_id AND im.kind='input' "
                "AND im.revision=f.current_input_revision,"
                "json_each(im.body_json,'$.readiness.media_probes') p "
                f"{where[0]} GROUP BY f.film_id",
                where[1],
            ).fetchall()
        used = {row["approval_id"] for row in archives if row["approval_id"]}
        open_films = {row["film_id"] for row in archives if row["open"]}
        errors = {row["film_id"]: row["error"] for row in archives}  # newest wins
        active = {row["film_id"] for row in approvals if row["approval_id"] not in used}
        package_films = {row["package_id"]: row["films"] for row in counts}
        receipt_ids = {row[0]: row[1] for row in receipts}  # newest wins
        sizes = {row[0]: (row[1] or 0, row[2] or 0) for row in probes}
        films: dict[str, dict[str, Any]] = {}
        for row in rows:
            film = films.setdefault(
                row["film_id"],
                {
                    "film_id": row["film_id"],
                    "display_name": row["display_name"],
                    "created_at": parse_time(row["created_at"]),
                    "final_name": PurePosixPath(row["output_path"]).name
                    if row["output_path"]
                    else None,
                    "stages": [],
                    "approval_active": row["film_id"] in active,
                    "archive_open": row["film_id"] in open_films,
                    "archive_error": errors.get(row["film_id"]) or None,
                    "input_revision": row["input_revision"],
                    "package_path": row["package_path"],
                    "package_films": package_films.get(row["package_id"], 0),
                    "planned": bool(row["planned"]),
                    "plan_ltx_model": row["plan_ltx_model"],
                    "plan_machine": row["plan_machine"],
                    "receipt_id": receipt_ids.get(row["film_id"]),
                    "input_hash": row["input_hash"],
                    "final_revision": row["final_revision"],
                    "final_hash": row["final_hash"],
                    "max_width": sizes.get(row["film_id"], (0, 0))[0],
                    "max_height": sizes.get(row["film_id"], (0, 0))[1],
                },
            )
            if row["stage"] is not None:
                film["stages"].append(
                    BoardStage(
                        stage=row["stage"],
                        status=row["status"],
                        note=_attempt_note(row["attempt_json"]),
                        reason=row["reason"],
                    )
                )
        return tuple(BoardFilm.model_validate(film) for film in films.values())

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
            "AND s.input_revision=f.current_input_revision AND s.stage='archive'),"
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
                archive_status=row[2],
                cleanup_workcopy_status=row[3],
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


def _attempt_note(body: str | None) -> str | None:
    """`evidence[-1].summary.note` of an attempt (T06-P1 D02)."""
    if body is None:
        return None
    evidence = json.loads(body).get("evidence") or ()
    if not evidence:
        return None
    return (evidence[-1].get("summary") or {}).get("note") or None
