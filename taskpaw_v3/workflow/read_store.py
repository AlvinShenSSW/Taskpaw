"""Scoped workflow views never expose credential or pairing digests."""

from .auth import AuthProof
from .models import (
    ApprovalContext,
    ArchiveOperation,
    CleanupIntent,
    ExecutionOperation,
    FilmRecord,
    FilmView,
    OperationView,
    StageRecord,
    WorkflowError,
)
from .store_core import WorkflowCore


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
