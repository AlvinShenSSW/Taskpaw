"""Local synchronous intake owner; public transactions surround native effects."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from . import intake_models as im
from . import intake_owner as native
from . import intake_protocol as wire
from . import models as m
from .auth import AuthProof
from .store import WorkflowStore


class SnapshotRunner:
    """A controller-owned worker thread may call run; never an async request loop.

    There is no implicit store, discovery, credential, service, or second owner.
    Native objects remain retained after uncertain closure or failed persistence.
    """

    def __init__(
        self,
        store: WorkflowStore,
        auth: AuthProof,
        *,
        meta: Callable[[], m.CommandMeta],
        owner_boot_id: str,
        ffprobe: wire.ProbeArtifact,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store, self.auth, self.meta = store, auth, meta
        self.owner_boot_id, self.ffprobe = owner_boot_id, ffprobe
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._native: native.OwnedIntakeSnapshot | None = None
        self._operation: str | None = None
        self._pending_result: im.IntakeSnapshotResult | None = None
        self._cancelled = threading.Event()

    @property
    def active_operation_id(self) -> str | None:
        # A status hint must not wait on an in-flight native launch. Actual
        # admission/stop and release still use the locks and durable store.
        return self._operation

    @property
    def has_pending_settlement(self) -> bool:
        return self._pending_result is not None

    def _persist_terminal(self) -> im.IntakeOperationView:
        """Two bounded public attempts; retain exact evidence on DB failure."""
        for attempt in range(2):
            assert self._pending_result is not None
            view = self._view()
            result = self._pending_result.model_copy(
                update={"expected_version": view.version}
            )
            try:
                if view.result is None:
                    self.store.finish_intake_snapshot(self.auth, self.meta(), result)
                    view = self._view()
                # A response can be lost after commit. Only the same persisted
                # exit proof closes this runner's retained terminal evidence.
                if view.exit_proof is None or view.exit_proof != result.exit_proof:
                    raise m.WorkflowError("owner_evidence_changed")
                with self._lock:
                    self._pending_result = None
                    if view.exit_proof.state != "unknown":
                        self._operation = None
                        self._native = None
                return view
            except m.WorkflowError as exc:
                if attempt:
                    raise
                if result.snapshot is not None and str(exc) in {
                    "precondition_failed",
                    "snapshot_binding_changed",
                    "invalid_request",
                }:
                    # Rejected candidate data never suppresses proven native
                    # closure. A separate public command records that failure.
                    self._pending_result = result.model_copy(
                        update={
                            "snapshot": None,
                            "outcome": "blocked",
                            "reason": "publication_blocked",
                        }
                    )
        raise AssertionError("bounded_terminal_attempts")

    def retry_settlement(self) -> im.IntakeOperationView | None:
        """Retry persistence only; never acquire or resume a native owner."""
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            if self._pending_result is None:
                return None
            return self._persist_terminal()
        finally:
            self._run_lock.release()

    def _view(self) -> im.IntakeOperationView:
        assert self._operation is not None
        return self.store.get_intake_operation(self.auth, self._operation)

    def _record(self, owned: native.OwnedIntakeSnapshot) -> int:
        view = self._view()
        result = self.store.record_intake_owner(
            self.auth,
            self.meta(),
            im.IntakeOwnerObservation(
                expected_version=view.version,
                operation_id=view.operation_id,
                source_generation=view.admission.source_generation,
                owner_boot_id=self.owner_boot_id,
                owner=owned.owner_observation(),
            ),
        )
        return result.version

    def stop(self) -> None:
        """Persist cancellation before native stop; no slot release here."""
        with self._lock:
            if self._operation is None:
                return
            view = self._view()
            if view.state not in {"completed", "cancelled", "failed"}:
                self.store.cancel_intake_snapshot(
                    self.auth,
                    self.meta(),
                    im.IntakeCancel(
                        expected_version=view.version,
                        operation_id=view.operation_id,
                        reason="cancelled",
                    ),
                )
            self._cancelled.set()
            if self._native is not None:
                self._native.request_stop()

    def run(self, package_id: str) -> im.IntakeOperationView:
        if not self._run_lock.acquire(blocking=False):
            raise m.WorkflowError("owner_unresolved")
        try:
            with self._lock:
                if self._operation is not None or self._native is not None:
                    raise m.WorkflowError("owner_unresolved")
                self._cancelled.clear()
                package_view = self.store.get_intake(self.auth, package_id)
                observed = self.store.get_intake_observation(self.auth, package_id)
                config = self.store.get_intake_configuration(
                    self.auth, package_view.root_id
                )
                if observed.grouping is None:
                    raise m.WorkflowError("intake_not_stable")
                admission = im.IntakeSnapshotAdmission(
                    expected_version=package_view.version,
                    package_id=package_id,
                    source_generation=package_view.source_generation,
                    root_config_revision=package_view.root_config_revision,
                    grouping_revision=observed.grouping.grouping_revision,
                    observation_digest=observed.observation_digest,
                    operation_id=str(uuid4()),
                    snapshot_id=str(uuid4()),
                    owner_boot_id=self.owner_boot_id,
                    policy_revision=config.policy.policy_revision,
                )
                self.store.begin_intake_snapshot(self.auth, self.meta(), admission)
                self._operation = admission.operation_id
                owner_id = str(uuid4())
                try:
                    request = self.store.get_intake_work(
                        self.auth,
                        self._operation,
                        owner_id=owner_id,
                        ffprobe=self.ffprobe,
                    )
                    wire.encode_frame(
                        wire.WorkRequestEvent(
                            operation_id=request.operation_id, request=request
                        )
                    )
                except (OSError, ValueError, RuntimeError):
                    # This scope ends BEFORE invoking native creation. Never
                    # infer no-process from an exception after that boundary.
                    self._pending_result = im.IntakeSnapshotResult(
                        expected_version=1,
                        operation_id=admission.operation_id,
                        source_generation=admission.source_generation,
                        outcome="cancelled"
                        if self._cancelled.is_set()
                        else "receiving",
                        reason="cancelled"
                        if self._cancelled.is_set()
                        else "source_changed",
                        snapshot=None,
                        exit_proof=im.IntakeExitProof(
                            operation_id=admission.operation_id,
                            owner_boot_id=self.owner_boot_id,
                            owner_id=owner_id,
                            source_generation=admission.source_generation,
                            state="no_process_created",
                            births=(),
                            job_active_zero=True,
                            io_joined=True,
                            owned_handles_closed=True,
                            failure_codes=(),
                            observed_at=self.clock(),
                        ),
                    )
                    return self._persist_terminal()
                self._native = native.start_snapshot_suspended(request)
                owned = self._native
            snapshot, reason = None, "copy_failed"
            try:
                version = self._record(owned)
                if not self._cancelled.is_set():
                    owned.resume_helper(persisted_owner_version=version)
                while not self._cancelled.is_set():
                    if not self._view().publication_allowed:
                        reason = "source_changed"
                        break
                    event = owned.next_event(
                        deadline=time.monotonic()
                        + config.policy.operation_deadline_seconds
                    )
                    if isinstance(event, wire.ProbeEvent):
                        owned.observe_probe(event.notice)
                        version = self._record(owned)
                        # stop() may commit while native observation/persistence runs.
                        with self._lock:
                            current = self._view()
                            if (
                                self._cancelled.is_set()
                                or not current.publication_allowed
                            ):
                                reason = (
                                    "cancelled"
                                    if self._cancelled.is_set()
                                    else "source_changed"
                                )
                                break
                            owned.ack_probe(
                                im.ProbeResumeAck(
                                    **event.notice.model_dump(), owner_version=version
                                )
                            )
                    elif isinstance(event, wire.ResultEvent):
                        snapshot, reason = event.snapshot, "snapshot_complete"
                        break
                    elif isinstance(event, wire.FailureEvent):
                        reason = event.reason
                        break
            except (OSError, ValueError, RuntimeError):
                # There is still a retained native owner; actual closure and
                # public persistence below decide whether any slot can release.
                reason = "cancelled" if self._cancelled.is_set() else "copy_failed"
            finally:
                end = time.monotonic() + 30
                proof = owned.settle(deadline=end)
                self._record(owned)
                # Final census may add known births. Persist those exact facts
                # first, then attest closure at/after that observation time.
                proof = owned.settle(deadline=end)
                view = self._view()
                if proof.state != "stopped" or self._cancelled.is_set():
                    snapshot = None
                outcome = (
                    "complete"
                    if snapshot is not None
                    else (
                        "cancelled"
                        if self._cancelled.is_set()
                        else "waiting_capacity"
                        if reason == "waiting_capacity"
                        else "receiving"
                        if reason == "source_changed"
                        else "failed"
                    )
                )
                result = im.IntakeSnapshotResult.model_validate(
                    dict(
                        expected_version=view.version,
                        operation_id=view.operation_id,
                        source_generation=view.admission.source_generation,
                        outcome=outcome,
                        reason="owner_unknown"
                        if proof.state == "unknown"
                        else "cancelled"
                        if self._cancelled.is_set()
                        else reason,
                        exit_proof=proof,
                        snapshot=snapshot,
                    )
                )
                self._pending_result = result
                final = self._persist_terminal()
            return final
        finally:
            self._run_lock.release()
