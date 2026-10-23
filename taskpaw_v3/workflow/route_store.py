"""P2-1 (#292): pins, routes and 判定已停止 in the store; reads for the scheduler.

Every command is an operator command through `_execute` (idempotent replay,
audit, CAS). A route is "claimed" while its film revision has an unresolved
compute assignment (C9); a claimed route is never withdrawn or re-pinned.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from .auth import AuthProof
from .executors import ASR_DONE, RESTORE_DONE
from .film_store import FilmCommands, _passed
from .models import (
    ApprovedCapability,
    CommandMeta,
    CommandResult,
    FilmPin,
    FilmRoute,
    HardwareProbe,
    PinSet,
    RateSample,
    RouteOpen,
    RouteWithdraw,
    RoutingFilm,
    RoutingProfile,
    RoutingSnapshot,
    RoutingWorker,
    StoppedAttestation,
    WorkerRecord,
    WorkflowError,
    canonical_json,
)
from .store_core import Mutation, parse_time, timestamp

# 判定已停止's evidence kinds: the operator's attestation, and the failed
# `cancelled` class (`executors.failed_kind("cancelled")`) that keeps the
# attempt a free retry (T08-P1a R3).
ATTESTED_KIND = "operator_attested_stopped"
CANCELLED_KIND = "exec_failed:cancelled"
_FINISHED = ("succeeded", "skipped")
_TERMINAL = ("succeeded", "failed", "blocked")
QUIET_SECONDS = 300.0  # IR4: 判定已停止 needs this long without a renewal
WITHDRAWN_LOST = "改派：设备失联"  # P2-4 R7: 自动 off a 失联 worker
_RESTORE_DONE = re.compile(
    re.escape(RESTORE_DONE)
    .replace(r"\{applied\}", r"(\d+)")
    .replace(r"\{reused\}", r"(\d+)")
)
_ASR_PREFIX = ASR_DONE.split("{", 1)[0]
_ASR_RAN = frozenset(("generated_ja", "no_speech"))  # WhisperJAV ran on the volume


def _sample_wall(stage: str, body: str, started: str, ended: str) -> float | None:
    """CX3: an attempt's GPU wall time when its tool ran on every volume, else
    None. A restore needs Jasna's measured wall and no reused volume; an ASR
    needs WhisperJAV on every volume and is timed by the attempt's span. The
    evidence counts reused volumes but not their durations, so a mixed attempt
    is no sample either."""
    evidence = json.loads(body).get("evidence") or ()
    summary = (evidence[-1].get("summary") or {}) if evidence else {}
    note = summary.get("note") or ""
    if stage == "restore":
        wall = (summary.get("timing") or {}).get("wall")
        done = _RESTORE_DONE.fullmatch(note)
        if not (isinstance(wall, (int, float)) and wall > 0 and done):
            return None
        return float(wall) if int(done[1]) > 0 and int(done[2]) == 0 else None
    if not note.startswith(_ASR_PREFIX):
        return None
    decisions = {
        part.rsplit(" ", 1)[0] for part in note[len(_ASR_PREFIX) :].split("，") if part
    }
    if not decisions or not decisions <= _ASR_RAN:
        return None
    return max(0.0, (parse_time(ended) - parse_time(started)).total_seconds())


def _sample_watts(body: str) -> float | None:
    evidence = json.loads(body).get("evidence") or ()
    summary = (evidence[-1].get("summary") or {}) if evidence else {}
    value = (summary.get("energy") or {}).get("avg_w")
    return float(value) if isinstance(value, (int, float)) and value >= 0 else None


class RouteCommands(FilmCommands):
    # ── commands ────────────────────────────────────────────────────────
    def _pin_row(self, film_id: str, revision: int) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM film_pins WHERE film_id=? AND input_revision=?",
            (film_id, revision),
        ).fetchone()
        return row

    def route_film(
        self, auth: AuthProof, meta: CommandMeta, command: RouteOpen
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["current_input_revision"] != command.input_revision:
                raise WorkflowError("cas_conflict")
            statuses = self._compute_statuses(command.film_id, command.input_revision)
            if all(status in _FINISHED for status in statuses.values()):
                raise WorkflowError("precondition_failed")
            self._require_source_admissible(film)  # CX7: the claim's own check
            worker = self._worker(command.worker_id)
            if (
                not worker.workflow_participation
                or worker.enrollment_state != "verified"
                or worker.admission_state != "enabled"
            ):
                raise WorkflowError("precondition_failed")
            if command.kind == "local" and command.worker_id != self.controller_id:
                # hardening: only the controller's own worker gets a local
                # route (no pin check, serves ASR) — `LocalDispatcher._ensure_route`
                raise WorkflowError("precondition_failed")
            if command.kind != "local":
                pin = self._pin_row(command.film_id, command.input_revision)
                if pin is None:  # IR13: the scheduler routes planned films only
                    raise WorkflowError("precondition_failed")
                if pin["version"] != command.expected_pin_version:
                    raise WorkflowError("cas_conflict")
                pinned = pin["worker_id"]
                if (command.kind == "auto" and pinned is not None) or (
                    command.kind == "pinned" and pinned != command.worker_id
                ):
                    raise WorkflowError("precondition_failed")
            if self._open_route(command.film_id, command.input_revision):
                raise WorkflowError("route_exists")
            claim = self._compute_claim(command.film_id, command.input_revision)
            if claim is not None and claim["worker_id"] != command.worker_id:
                raise WorkflowError("route_claimed")
            value = FilmRoute(
                route_id=command.route_id,
                film_id=command.film_id,
                input_revision=command.input_revision,
                worker_id=command.worker_id,
                kind=command.kind,
                state="open",
                reason=command.reason,
                routed_at=now,
                version=1,
            )
            self._insert(
                "film_routes",
                route_id=value.route_id,
                film_id=value.film_id,
                input_revision=value.input_revision,
                worker_id=value.worker_id,
                kind=value.kind,
                state="open",
                reason=value.reason,
                routed_at=timestamp(now),
                body_json=canonical_json(value),
            )
            return Mutation(value.route_id, 1, "open")

        return self._execute(auth, meta, command, "route_film", apply)

    def withdraw_route(
        self, auth: AuthProof, meta: CommandMeta, command: RouteWithdraw
    ) -> CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._one(
                "SELECT * FROM film_routes WHERE route_id=?", (command.route_id,)
            )
            if row["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            if row["state"] != "open":
                raise WorkflowError("precondition_failed")
            if self._compute_claim(row["film_id"], row["input_revision"]) is not None:
                raise WorkflowError("route_claimed")
            version = self._close_route(row, now, command.reason)
            return Mutation(row["route_id"], version, "closed")

        return self._execute(auth, meta, command, "withdraw_route", apply)

    def set_pin(
        self, auth: AuthProof, meta: CommandMeta, command: PinSet
    ) -> CommandResult:
        """改派: a new pin; an unclaimed open route elsewhere is withdrawn with it.

        改派 to 自动 keeps any route (a healthy route needs no move, IR8)
        unless the command names it in `withdraw_route_id` (a 失联 worker's).
        A kept route's version moves, so a scheduler withdrawal decided before
        this 改派 loses its CAS (CX2). The controller means 自动 (IR9). P2-4
        §9 CX4: a claimed film is never re-pinned — the unresolved compute
        assignment is checked here, in the pin's own transaction."""

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["current_input_revision"] != command.input_revision:
                raise WorkflowError("cas_conflict")
            pin = self._one(
                "SELECT * FROM film_pins WHERE film_id=? AND input_revision=?",
                (command.film_id, command.input_revision),
            )
            if pin["version"] != command.expected_version:
                raise WorkflowError("cas_conflict")
            if command.worker_id is not None and not self._verified(command.worker_id):
                raise WorkflowError("precondition_failed")
            target = self._pin_target(command.worker_id)
            if self._compute_claim(command.film_id, command.input_revision):
                raise WorkflowError("route_claimed")
            route = self._open_route(command.film_id, command.input_revision)
            if (
                route is not None
                and target is not None
                and route["worker_id"] != target
            ):
                self._close_route(route, now, "改派")
            elif (
                route is not None
                and target is None
                and route["route_id"] == command.withdraw_route_id
            ):
                self._close_route(route, now, WITHDRAWN_LOST)
            elif route is not None:
                self._touch_route(route)
            changed = self._conn.execute(
                "UPDATE film_pins SET worker_id=?,updated_at=?,version=version+1 "
                "WHERE film_id=? AND input_revision=? AND version=?",
                (
                    target,
                    timestamp(now),
                    command.film_id,
                    command.input_revision,
                    command.expected_version,
                ),
            )
            if changed.rowcount != 1:
                raise WorkflowError("cas_conflict")
            return Mutation(
                command.film_id,
                command.expected_version + 1,
                "pinned" if target else "auto",
            )

        return self._execute(auth, meta, command, "set_pin", apply)

    def _touch_route(self, row: sqlite3.Row) -> None:
        """A new route version with the same content (CX2)."""
        value = FilmRoute.model_validate_json(row["body_json"]).model_copy(
            update={"version": row["version"] + 1}
        )
        self._update(
            "film_routes",
            "route_id",
            row["route_id"],
            row["version"],
            body_json=canonical_json(value),
        )

    def attest_stopped(
        self, auth: AuthProof, meta: CommandMeta, command: StoppedAttestation
    ) -> CommandResult:
        """判定已停止 (D4): the operator's word replaces the owner-stopped proof.

        A proven terminal stage keeps its status; anything else fails as
        `cancelled` (a free retry). The assignment and its GPU owner resolve,
        and the film's open route to that worker closes so it can be routed
        again. Refused for the controller's own work and within five minutes
        of the assignment's last renewal (IR4; the store has no heartbeat yet,
        so the UI's two-channel check (C4, P2-4) stays the other guard).
        """

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            assignment = self._one(
                "SELECT * FROM assignments WHERE assignment_id=?",
                (command.assignment_id,),
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
            kinds = {item.kind for item in command.evidence}
            if (
                assignment["worker_id"] is None
                or not _passed(command.evidence)
                or any(
                    item.producer_id != principal["principal_id"]
                    for item in command.evidence
                )
                or not {ATTESTED_KIND, CANCELLED_KIND} <= kinds
                # IR4: the controller proves its own stop (restart settlement),
                # and a worker that renewed lately may still be running it
                or assignment["worker_id"] == self.controller_id
                or (now - parse_time(assignment["renewed_at"])).total_seconds()
                < QUIET_SECONDS
            ):
                raise WorkflowError("precondition_failed")
            job = self._one(
                "SELECT * FROM stages WHERE job_id=?", (assignment["job_id"],)
            )
            status = job["status"] if job["status"] in _TERMINAL else "failed"
            if job["status"] not in _TERMINAL:
                self._set_stage(job, "failed")
                self._set_attempt(attempt, "failed", now, command.evidence, "cancelled")
            self._set_operation(
                attempt["operation_id"],
                "verified" if status == "succeeded" else "failed",
                now,
                resolved=True,
            )
            version = self._set_assignment(assignment, "resolved", now, owner=None)
            route = self._open_route(job["film_id"], job["input_revision"])
            if route is not None and route["worker_id"] == assignment["worker_id"]:
                self._close_route(route, now, "判定已停止")
            return Mutation(
                assignment["assignment_id"],
                version,
                status,
                assignment_id=assignment["assignment_id"],
                attempt_id=assignment["attempt_id"],
            )

        return self._execute(auth, meta, command, "attest_stopped", apply)

    # ── reads ───────────────────────────────────────────────────────────
    def _operator_read(self, auth: AuthProof) -> None:
        principal = self._check_auth(auth, self._observe_time())
        if principal["role"] != "operator":
            raise WorkflowError("forbidden")

    def film_route(
        self, auth: AuthProof, film_id: str, input_revision: int
    ) -> FilmRoute | None:
        """The film revision's open route, if any."""
        with self._lock:
            self._operator_read(auth)
            row = self._open_route(film_id, input_revision)
            return (
                None if row is None else FilmRoute.model_validate_json(row["body_json"])
            )

    def list_routes(
        self, auth: AuthProof, *, open_only: bool = True
    ) -> tuple[FilmRoute, ...]:
        """Routes in routing order (open ones only by default)."""
        with self._lock:
            self._operator_read(auth)
            rows = self._conn.execute(
                "SELECT body_json FROM film_routes "
                + ("WHERE state='open' " if open_only else "")
                + "ORDER BY routed_at,rowid"
            ).fetchall()
            return tuple(FilmRoute.model_validate_json(row[0]) for row in rows)

    def get_pin(
        self, auth: AuthProof, film_id: str, input_revision: int
    ) -> FilmPin | None:
        with self._lock:
            self._operator_read(auth)
            row = self._pin_row(film_id, input_revision)
            if row is None:
                return None
            return FilmPin(
                film_id=row["film_id"],
                input_revision=row["input_revision"],
                worker_id=row["worker_id"],
                version=row["version"],
                updated_at=parse_time(row["updated_at"]),
            )

    def list_workers(self, auth: AuthProof) -> tuple[WorkerRecord, ...]:
        """Every enrolled worker, the controller's own included, by name."""
        with self._lock:
            self._operator_read(auth)
            rows = self._conn.execute(
                "SELECT body_json FROM workers ORDER BY name,worker_id"
            ).fetchall()
            return tuple(WorkerRecord.model_validate_json(row[0]) for row in rows)

    def routing_workers(self, auth: AuthProof) -> tuple[RoutingWorker, ...]:
        """Every worker as the scheduler sees it (the 设备 page, P2-2)."""
        with self._lock:
            self._operator_read(auth)
            return self._routing_workers()

    def last_approval(
        self, auth: AuthProof, worker_id: str
    ) -> tuple[ApprovedCapability, HardwareProbe] | None:
        """The worker's newest approved capability and the probe it approved
        (核验并启用 compares a new probe with it, P2-2)."""
        with self._lock:
            self._operator_read(auth)
            row = self._conn.execute(
                "SELECT a.body_json,p.body_json FROM capabilities a "
                "JOIN capabilities p ON p.worker_id=a.worker_id AND p.kind='probed' "
                "AND p.revision=a.probe_revision "
                "WHERE a.worker_id=? AND a.kind='approved' "
                "ORDER BY a.revision DESC LIMIT 1",
                (worker_id,),
            ).fetchone()
            if row is None:
                return None
            return (
                ApprovedCapability.model_validate_json(row[0]),
                HardwareProbe.model_validate_json(row[1]),
            )

    def pairing_slot(self, auth: AuthProof, pairing_id: str) -> tuple[str, str, int]:
        """(worker, device, next credential generation) a pairing completes
        with; the server fills them in for `POST /pair` (P2-2 R2)."""
        with self._lock:
            self._operator_read(auth)
            row = self._one(
                "SELECT worker_id,device_id FROM pairings WHERE pairing_id=?",
                (pairing_id,),
                code="unauthorized",
            )
            principal = str(
                uuid5(UUID(row["worker_id"]), "workflow-compute-principal-v1")
            )
            generation = self._one(
                "SELECT coalesce(max(generation),0) FROM credentials WHERE principal_id=?",
                (principal,),
            )[0]
            return row["worker_id"], row["device_id"], int(generation) + 1

    def routing_snapshot(self, auth: AuthProof) -> RoutingSnapshot:
        """The scheduler's store input, read in one locked snapshot (§5)."""
        with self._lock:
            self._operator_read(auth)
            films = self._routing_films()
            return RoutingSnapshot(
                films=tuple(film for current, film in films if current),
                older_revisions=tuple(film for current, film in films if not current),
                routes=tuple(
                    FilmRoute.model_validate_json(row[0])
                    for row in self._conn.execute(
                        "SELECT body_json FROM film_routes WHERE state='open' "
                        "ORDER BY routed_at,rowid"
                    )
                ),
                workers=self._routing_workers(),
                samples=self._rate_samples(),
            )

    def _probes(self) -> dict[tuple[str, int], tuple[Any, ...]]:
        """(largest width, height, summed duration) per input manifest."""
        rows = self._conn.execute(
            "SELECT im.film_id,im.revision,max(json_extract(p.value,'$.width')),"
            "max(json_extract(p.value,'$.height')),"
            "sum(json_extract(p.value,'$.duration_ms')) FROM manifests im,"
            "json_each(im.body_json,'$.readiness.media_probes') p "
            "WHERE im.kind='input' GROUP BY im.film_id,im.revision"
        ).fetchall()
        return {(row[0], row[1]): (row[2] or 0, row[3] or 0, row[4]) for row in rows}

    def _routing_films(self) -> list[tuple[bool, RoutingFilm]]:
        """(current, facts) of each current revision with GPU work left or an
        open route, and of each older revision an open route holds (CX5)."""
        rows = self._conn.execute(
            "WITH k(film_id,revision) AS (SELECT f.film_id,f.current_input_revision "
            "FROM films f WHERE f.current_input_revision IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM stages s WHERE s.film_id=f.film_id "
            "AND s.input_revision=f.current_input_revision "
            "AND s.stage IN ('restore','asr') AND s.status NOT IN ('succeeded','skipped')) "
            "UNION SELECT film_id,input_revision FROM film_routes WHERE state='open') "
            "SELECT k.film_id,k.revision,k.revision=f.current_input_revision AS current,"
            "f.display_name,f.created_at,p.film_id IS NOT NULL AS planned,"
            "p.confirmed_at,p.ltx_model,pin.worker_id AS pin_worker_id,"
            "coalesce(pin.version,0) AS pin_version FROM k "
            "JOIN films f ON f.film_id=k.film_id "
            "LEFT JOIN film_plans p ON p.film_id=k.film_id AND p.input_revision=k.revision "
            "LEFT JOIN film_pins pin ON pin.film_id=k.film_id "
            "AND pin.input_revision=k.revision "
            "ORDER BY f.created_at,f.film_id,k.revision"
        ).fetchall()
        statuses: dict[tuple[str, int], dict[str, Any]] = {}
        for film_id, revision, stage, status in self._conn.execute(
            "SELECT film_id,input_revision,stage,status FROM stages "
            "WHERE stage IN ('restore','asr','translate')"
        ):
            statuses.setdefault((film_id, revision), {})[stage] = status
        claims = {
            (row[0], row[1]): row[2:]
            for row in self._conn.execute(
                "SELECT s.film_id,s.input_revision,a.worker_id,s.stage,a.claimed_at "
                "FROM assignments a JOIN stages s ON s.job_id=a.job_id "
                "WHERE a.resolved_at IS NULL AND s.stage IN ('restore','asr','translate')"
            )
        }
        sizes = {
            (row[0], row[1]): row[2]
            for row in self._conn.execute(
                "SELECT film_id,revision,sum(size_bytes) FROM manifest_items "
                "WHERE kind='input' GROUP BY film_id,revision"
            )
        }
        probes = self._probes()
        films = []
        for row in rows:
            film_id, revision = row["film_id"], row["revision"]
            admissible = not row["current"] or self._source_admissible(
                self._film_row(film_id)
            )
            stage = statuses.get((film_id, revision), {})
            claim = claims.get((film_id, revision))
            width, height, duration = probes.get((film_id, revision), (0, 0, None))
            films.append(
                (
                    bool(row["current"]),
                    RoutingFilm(
                        film_id=film_id,
                        input_revision=revision,
                        display_name=row["display_name"],
                        created_at=parse_time(row["created_at"]),
                        planned=bool(row["planned"]),
                        confirmed_at=parse_time(row["confirmed_at"])
                        if row["confirmed_at"]
                        else None,
                        ltx_model=row["ltx_model"],
                        pin_worker_id=row["pin_worker_id"],
                        pin_version=row["pin_version"],
                        restore=stage.get("restore", "pending"),
                        asr=stage.get("asr", "pending"),
                        translate=stage.get("translate", "pending"),
                        claimed_by=claim[0] if claim else None,
                        claimed_stage=claim[1] if claim else None,
                        claimed_at=parse_time(claim[2]) if claim else None,
                        max_width=int(width),
                        max_height=int(height),
                        duration_ms=int(duration) if duration else None,
                        source_bytes=int(sizes.get((film_id, revision)) or 0),
                        source_admissible=admissible,
                    ),
                )
            )
        return films

    def _routing_workers(self) -> tuple[RoutingWorker, ...]:
        rows = self._conn.execute(
            "SELECT w.body_json,coalesce(b.reconciled,0) FROM workers w "
            "LEFT JOIN worker_boots b ON b.worker_id=w.worker_id "
            "AND b.boot_id=w.current_boot_id ORDER BY w.name,w.worker_id"
        ).fetchall()
        workers = []
        for body, reconciled in rows:
            worker = WorkerRecord.model_validate_json(body)
            cap = worker.capability
            probe = cap.probed if cap else None
            approved = cap.approved if cap else None
            workers.append(
                RoutingWorker(
                    worker_id=worker.worker_id,
                    name=worker.name,
                    enrollment_state=worker.enrollment_state,
                    admission_state=worker.admission_state,
                    participation=worker.workflow_participation,
                    boot_reconciled=bool(reconciled),
                    gpu_model=probe.gpu_model if probe else None,
                    vram_mib=probe.vram_mib if probe else 0,
                    profiles=tuple(
                        RoutingProfile(
                            stage=stage,
                            max_width=profile.max_width,
                            max_height=profile.max_height,
                        )
                        for profile in (approved.profiles if approved else ())
                        for stage in profile.allowed_stages
                    ),
                )
            )
        return tuple(workers)

    def _rate_samples(self) -> tuple[RateSample, ...]:
        """CX6: a sample's GPU model is the probe of the physical GPU its
        assignment held at claim, so a later card swap never relabels it; an
        attempt without a held GPU has no model and keys no rate."""
        rows = self._conn.execute(
            "SELECT s.stage,a.worker_id,a.started_at,a.ended_at,a.body_json,s.film_id,"
            "s.input_revision,p.ltx_model,g.physical_resource_id AS resource_id "
            "FROM attempts a JOIN stages s ON s.job_id=a.job_id "
            "JOIN assignments g ON g.attempt_id=a.attempt_id "
            "LEFT JOIN film_plans p ON p.film_id=s.film_id "
            "AND p.input_revision=s.input_revision "
            "WHERE a.status='succeeded' AND s.stage IN ('restore','asr') "
            "AND a.ended_at IS NOT NULL ORDER BY a.ended_at DESC,a.rowid DESC"
        ).fetchall()
        probes = self._probes()
        models = {  # the probed model per physical GPU, newest probe last
            resource: HardwareProbe.model_validate_json(body).gpu_model
            for resource, body in self._conn.execute(
                "SELECT physical_resource_id,body_json FROM capabilities "
                "WHERE kind='probed' AND physical_resource_id IS NOT NULL "
                "ORDER BY worker_id,revision"
            )
        }
        samples = []
        for row in rows:
            width, height, duration = probes.get(
                (row["film_id"], row["input_revision"]), (0, 0, None)
            )
            wall = _sample_wall(
                row["stage"], row["body_json"], row["started_at"], row["ended_at"]
            )
            if not duration or wall is None:
                continue
            samples.append(
                RateSample(
                    stage=row["stage"],
                    worker_id=row["worker_id"],
                    gpu_model=models.get(row["resource_id"]),
                    tier=row["ltx_model"] if row["stage"] == "restore" else None,
                    max_width=int(width),
                    max_height=int(height),
                    duration_ms=int(duration),
                    wall_seconds=wall,
                    avg_w=_sample_watts(row["body_json"]),
                    ended_at=parse_time(row["ended_at"]),
                )
            )
        return tuple(samples)
