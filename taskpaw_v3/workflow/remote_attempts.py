"""P2-3 (#294) §2, §5: the controller's side of a remote attempt.

`claim`, `renew` and `finish` turn the worker API's bodies into the store's
commands; `attempt_view` answers `GET /attempts/{a}`. The boot settlement
resolves what an earlier worker boot left (§5) and the heartbeat producers
name what to cancel (R9) and what to release (R10). The store stays the only
writer; nothing here trusts the worker beyond its authenticated identity:
the owner-stopped evidence and every ledger check are the controller's own.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from . import models as m
from .auth import AuthProof
from .exec_common import write_text_atomic
from .executors import ErrorClass, ExecOutcome
from .local_dispatch import resolution, retry_if_due
from .stage_results import RESULT_NAME
from .transfer import TransferError, Transfers, validate_finish_refs

if TYPE_CHECKING:
    from .fleet_live import FleetLive
    from .local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.remote")

ATTESTED = "operator_attested_stopped"  # 判定已停止 (P2-1 R7)
REMOTE_STAGES = ("restore", "asr")
_FINISHED = ("succeeded", "skipped")
Verdict = Literal["interrupted", "finish", "held"]
Resolved = Callable[[m.PendingComputeJob, ExecOutcome], None]


class AttemptRefused(Exception):
    """An HTTP-shaped refusal (409/404/422) with a stable code."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def _stale() -> AttemptRefused:
    return AttemptRefused(409, "stale_attempt")


# ── reads ───────────────────────────────────────────────────────────────
def assignment_of(
    rt: LocalRuntime, worker_id: str, attempt_id: str
) -> m.AssignmentRecord | None:
    """The worker's assignment of `attempt_id` (resolved ones included)."""
    view = rt.store.get_worker(rt.operator, worker_id)
    return next((a for a in view.assignments if a.attempt_id == attempt_id), None)


def _attempt(rt: LocalRuntime, assignment: m.AssignmentRecord) -> m.AttemptRecord:
    return next(
        a
        for a in rt.store.list_attempts(rt.operator, assignment.job_id)
        if a.attempt_id == assignment.attempt_id
    )


def attempt_view(rt: LocalRuntime, worker_id: str, attempt_id: str) -> dict[str, Any]:
    """`GET /attempts/{a}`: 404 when absent or another worker's (S-4)."""
    assignment = assignment_of(rt, worker_id, attempt_id)
    if assignment is None:
        raise AttemptRefused(404, "not_found")
    stage = rt.store.get_stage(rt.operator, assignment.job_id)
    value: dict[str, Any] = {
        "state": "resolved"
        if assignment.status == "resolved"
        else "unknown"
        if assignment.status == "unknown"
        else "running",
        "current": stage.attempt_id == attempt_id,
    }
    if assignment.status == "resolved":
        value["outcome"] = _attempt(rt, assignment).status
    return value


# ── claim and renew ─────────────────────────────────────────────────────
def claim(
    rt: LocalRuntime,
    auth: AuthProof,
    worker: m.WorkerRecord,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """The worker's exact claim body under the key `claim:<a>`; the server
    fills the worker, its physical GPU and the profile fields, so a resend of
    the same bytes replays the stored result."""
    binding = worker.physical_binding
    if binding is None:
        raise m.WorkflowError("precondition_failed")
    tools = m.ToolVersions.model_validate(body["tool_versions"])
    result = rt.store.claim(
        auth,
        rt.meta(f"claim:{body['attempt_id']}"),
        m.ClaimRequest(
            job_id=body["job_id"],
            expected_version=body["expected_version"],
            attempt_id=body["attempt_id"],
            operation_id=body["operation_id"],
            worker_id=worker.worker_id,
            boot_id=body["boot_id"],
            profile_revision=tools.profile_revision,
            config_digest=tools.config_digest,
            resource_id=binding.resource_id,
            tool_versions=tools,
        ),
    )
    return {
        "assignment_id": result.assignment_id,
        "attempt_id": result.attempt_id,
        "version": result.version,
    }


def renew(
    rt: LocalRuntime,
    auth: AuthProof,
    worker_id: str,
    attempt_id: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """C5: the one renew after a claim; it records the worker's owner with
    evidence the controller makes (`native_owner`)."""
    current = next(
        (
            o.assignment
            for o in rt.store.list_open_assignments(rt.operator)
            if o.assignment.attempt_id == attempt_id
            and o.assignment.worker_id == worker_id
        ),
        None,
    )
    if current is None:
        raise _stale()
    result = rt.store.renew(
        auth,
        rt.meta(),
        m.AssignmentRenewal(
            assignment_id=current.assignment_id,
            expected_version=current.version,
            worker_id=worker_id,
            boot_id=body["boot_id"],
            attempt_id=attempt_id,
            authority_epoch=current.authority_epoch,
            fencing_token=current.fencing_token,
            local_owner=m.LocalOwnerEvidence(
                owner_id=body["owner_id"],
                generation=body["generation"],
                boot_id=body["boot_id"],
                process_identity=body["process_identity"],
                state="running",
                evidence=rt.evidence("native_owner"),
            ),
        ),
    )
    return {"version": result.version}


# ── finish ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FinishBody:
    outcome: Literal["succeeded", "failed"]
    message: str = ""
    error_class: ErrorClass | None = None
    ledger: str | None = None
    payload_digest: str | None = None
    timing: m.RestoreTiming | None = None
    energy: m.AttemptEnergy | None = None


# finishes are rare and short (no hashing, C13): one at a time keeps a
# replayed finish from racing its own first send
_finish_lock = threading.Lock()


def _stored(rt: LocalRuntime, assignment: m.AssignmentRecord) -> dict[str, Any]:
    """(1) Already resolved: the stored outcome. After 判定已停止 the worker's
    finish is stale (crash matrix 9): it discards the attempt."""
    attempt = _attempt(rt, assignment)
    if any(e.kind == ATTESTED for e in attempt.evidence):
        raise _stale()
    return {"state": "resolved", "outcome": attempt.status}


def stopped_owner(
    rt: LocalRuntime, assignment: m.AssignmentRecord
) -> m.LocalOwnerEvidence:
    """The owner the single renew recorded (its identity must match), or one
    for the claiming boot when that renew never landed; `resolution` marks it
    stopped with the controller's own evidence."""
    return assignment.local_owner or m.LocalOwnerEvidence(
        owner_id=str(uuid.uuid4()),
        generation=1,
        boot_id=assignment.boot_id,
        process_identity="remote",
        state="stopped",
        evidence=rt.evidence("native_owner"),
    )


def finish(
    rt: LocalRuntime,
    transfers: Transfers | None,
    auth: AuthProof,
    worker_id: str,
    attempt_id: str,
    body: FinishBody,
    *,
    on_resolved: Resolved | None = None,
) -> dict[str, Any]:
    """§2 finish, idempotent by state; sent only after the worker's Job
    settled, so the controller's stopped-owner evidence is the worker's word
    on its own process plus the credential it signed with."""
    with _finish_lock:
        assignment = assignment_of(rt, worker_id, attempt_id)
        if assignment is None:
            raise _stale()
        if assignment.status == "resolved":
            return _stored(rt, assignment)
        try:
            owned = rt.store.worker_attempt(auth, attempt_id)  # (2) C7
        except m.WorkflowError:
            raise _stale() from None
        manifest = rt.store.get_input_manifest(
            rt.operator, owned.film_id, owned.input_revision
        )
        if body.outcome == "succeeded":
            outcome = _verify(rt, transfers, owned, manifest, body)
        else:
            outcome = ExecOutcome.failed(
                body.error_class or "needs_attention", body.message or "失败"
            )
        operation_id = _attempt(rt, assignment).operation_id
        try:
            rt.store.resolve_assignment(
                rt.operator,
                rt.meta(),
                resolution(
                    rt,
                    assignment,
                    operation_id,
                    outcome,
                    stopped_owner(rt, assignment),
                    body.energy,
                ),
            )
        except m.WorkflowError as exc:
            again = assignment_of(rt, worker_id, attempt_id)
            if again is not None and again.status == "resolved":
                return _stored(rt, again)
            raise AttemptRefused(409, exc.code) from None
        _after(rt, transfers, owned, outcome, on_resolved)
        return {"state": "resolved", "outcome": "succeeded" if outcome.ok else "failed"}


def _verify(
    rt: LocalRuntime,
    transfers: Transfers | None,
    owned: m.WorkerAttempt,
    manifest: m.InputManifest,
    body: FinishBody,
) -> ExecOutcome:
    """(3) The ledger's digest, then every ref (C3), then the ledger is
    written where the controller's later stages read it."""
    raw = (body.ledger or "").encode("utf-8")
    if not raw:
        raise TransferError(422, "invalid_ledger")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != body.payload_digest:
        raise TransferError(422, "digest_mismatch")
    validate_finish_refs(
        rt.work_dir,
        raw,
        attempt_id=owned.attempt_id,
        film_id=owned.film_id,
        input_revision=owned.input_revision,
        stage=owned.stage,
        manifest=manifest,
        committed=transfers.committed(owned.attempt_id) if transfers else {},
    )
    folder = rt.work_dir / owned.attempt_id
    folder.mkdir(parents=True, exist_ok=True)
    write_text_atomic(folder / RESULT_NAME, raw.decode("utf-8"))
    return ExecOutcome.succeeded(body.message, digest, body.timing)


def _after(
    rt: LocalRuntime,
    transfers: Transfers | None,
    owned: m.WorkerAttempt,
    outcome: ExecOutcome,
    on_resolved: Resolved | None,
) -> None:
    """(4)/(5) after the resolve: the retry, the plan sweep, superseded work
    folders (E4), then the host's log, events and wakes. None of these can
    undo the resolution; a failure is logged."""
    try:
        if not outcome.ok:
            retry_if_due(rt, owned.job_id)
        else:
            for other in rt.store.list_attempts(rt.operator, owned.job_id):
                if other.attempt_id != owned.attempt_id:
                    shutil.rmtree(rt.work_dir / other.attempt_id, ignore_errors=True)
        if transfers is not None:
            transfers.sweep()
    except Exception:
        log.exception("after a remote finish of %s", owned.attempt_id)
    if on_resolved is None:
        return
    try:
        view = rt.store.get_film(rt.operator, owned.film_id)
        stage = rt.store.get_stage(rt.operator, owned.job_id)
        on_resolved(
            m.PendingComputeJob(
                job_id=owned.job_id,
                film_id=owned.film_id,
                display_name=view.film.display_name,
                stage=owned.stage,
                input_revision=owned.input_revision,
                version=stage.version,
                film_created_at=view.film.created_at,
            ),
            outcome,
        )
    except Exception:
        log.exception("remote resolve hook failed")


# ── boot settlement (§5) ────────────────────────────────────────────────
@dataclass(frozen=True)
class AttemptReport:
    attempt_id: str
    children_alive: bool
    ledger: bool
    outputs_intact: bool
    outcome: str | None = None


def settle_boot(
    rt: LocalRuntime,
    worker_id: str,
    boot_id: str,
    reports: Iterable[AttemptReport],
) -> dict[str, Verdict]:
    """Per unresolved assignment of an earlier boot of this worker: a live
    child stays unknown (`held`); a ledger with intact outputs, or a settled
    outcome, stays unknown for the worker's finish (`finish`); anything else
    (not reported, children gone, nothing to show) is resolved failed
    `interrupted`, a free retry. A lost reply is never proof of a stop: only
    the worker's own report of its dead children is."""
    by_attempt = {r.attempt_id: r for r in reports}
    verdicts: dict[str, Verdict] = {}
    for open_ in rt.store.list_open_assignments(rt.operator):
        assignment = open_.assignment
        if assignment.worker_id != worker_id or assignment.boot_id == boot_id:
            continue
        report = by_attempt.get(assignment.attempt_id)
        if report is not None and report.children_alive:
            verdicts[assignment.attempt_id] = "held"
        elif report is not None and (
            (report.ledger and report.outputs_intact) or report.outcome
        ):
            verdicts[assignment.attempt_id] = "finish"
        else:
            rt.resolve_interrupted(assignment)
            verdicts[assignment.attempt_id] = "interrupted"
    return verdicts


def keep_list(rt: LocalRuntime, worker_id: str) -> list[dict[str, Any]]:
    """R10 at boot: the staged revisions the worker keeps — its open routes
    and the revisions its unresolved assignments hold; the rest goes."""
    kept = {
        (r.film_id, r.input_revision)
        for r in rt.store.list_routes(rt.operator)
        if r.worker_id == worker_id
    } | held_revisions(rt, worker_id)
    return [{"film": film, "rev": rev} for film, rev in sorted(kept)]


def held_revisions(rt: LocalRuntime, worker_id: str) -> set[tuple[str, int]]:
    held = set()
    for open_ in rt.store.list_open_assignments(rt.operator):
        if open_.assignment.worker_id == worker_id:
            stage = rt.store.get_stage(rt.operator, open_.assignment.job_id)
            held.add((stage.film_id, stage.input_revision))
    return held


# ── heartbeat producers ─────────────────────────────────────────────────
def release_list(
    rt: LocalRuntime,
    worker_id: str,
    staged: Iterable[tuple[str, int]],
) -> list[dict[str, Any]]:
    """R10: a staged revision is released only with no open route to this
    worker and no unresolved assignment of it, and once its GPU work is done,
    the revision is no longer current or the film went to another worker."""
    staged = list(staged)
    if not staged:
        return []
    routes = {
        (r.film_id, r.input_revision): r for r in rt.store.list_routes(rt.operator)
    }
    held = held_revisions(rt, worker_id)
    released = []
    for film_id, rev in staged:
        route = routes.get((film_id, rev))
        if (route is not None and route.worker_id == worker_id) or (
            film_id,
            rev,
        ) in held:
            continue
        if route is not None or _done_or_stale(rt, film_id, rev):
            released.append({"film": film_id, "rev": rev})
    return released


def _done_or_stale(rt: LocalRuntime, film_id: str, rev: int) -> bool:
    try:
        view = rt.store.get_film(rt.operator, film_id)
    except m.WorkflowError:
        return True  # an unknown film keeps nothing
    if view.film.current_input_revision != rev:
        return True
    return all(
        s.status in _FINISHED
        for s in view.stages
        if s.stage in REMOTE_STAGES and s.input_revision == rev
    )


def cancel_list(
    rt: LocalRuntime,
    auth: AuthProof,
    worker_id: str,
    requeued: set[str],
    current: str | None,
) -> list[str]:
    """R9: (a) attempts the operator stopped with 停止并重新排队 that this
    worker still runs, (b) a reported current attempt that is no longer the
    worker's current, unresolved one (stale, D4)."""
    live = {
        o.assignment.attempt_id
        for o in rt.store.list_open_assignments(rt.operator)
        if o.assignment.worker_id == worker_id
    }
    cancel = sorted(a for a in requeued if a in live)
    if current is not None and current not in cancel:
        try:
            rt.store.worker_attempt(auth, current)
        except m.WorkflowError:
            cancel.append(current)
    return cancel


SETTLED_PHASES = ("uploading", "finishing")  # §11 CX5: the GPU work is done


def requeue(
    rt: LocalRuntime,
    cancels: set[str],
    film_id: str,
    attempt_id: str,
    *,
    fleet: FleetLive | None = None,
) -> None:
    """停止并重新排队 (OD3, R9a): only the film's current, unresolved remote
    restore or ASR attempt; it goes into the heartbeat's `cancel` until it
    resolves (in memory: a controller restart forgets the request). §11 CX5:
    once the worker reports the attempt uploading or finishing its stage
    settled ok, and finished GPU work is never discarded: `already_settled`."""
    view = rt.store.get_film(rt.operator, film_id)
    stage = next(
        (
            s
            for s in view.stages
            if s.stage in REMOTE_STAGES
            and s.attempt_id == attempt_id
            and s.input_revision == view.film.current_input_revision
        ),
        None,
    )
    running = next(
        (
            o.assignment
            for o in rt.store.list_open_assignments(rt.operator)
            if o.assignment.attempt_id == attempt_id
        ),
        None,
    )
    if (
        stage is None
        or running is None
        or running.worker_id in (None, rt.identity.worker_id)
    ):
        raise m.WorkflowError("precondition_failed")
    if worker_phase(fleet, running.worker_id, attempt_id) in SETTLED_PHASES:
        raise m.WorkflowError("already_settled")
    cancels.add(attempt_id)


def worker_phase(
    fleet: FleetLive | None, worker_id: str | None, attempt_id: str
) -> str | None:
    """The phase the worker last reported for `attempt_id` (None: unknown)."""
    entry = fleet.entry(worker_id) if fleet is not None and worker_id else None
    current = entry.beat.current if entry is not None else None
    if current is None or current.attempt_id != attempt_id:
        return None
    return current.phase
