"""Approval-bound archive and exact cleanup intents; never performs file I/O."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from uuid import uuid4

from . import models as m
from .auth import AuthProof
from .film_store import FilmCommands, native_identity_key
from .store_core import Mutation, parse_time, timestamp


class ApprovalCommands(FilmCommands):
    def _binding_manifests(
        self, binding: m.ApprovalBinding
    ) -> tuple[m.InputManifest, m.FinalManifest]:
        film = self._film_row(binding.film_id)
        if (
            film["current_input_revision"] != binding.input_revision
            or film["current_final_revision"] != binding.final_revision
            or film["policy_revision"] != binding.policy_revision
        ):
            raise m.WorkflowError("binding_changed")
        source = m.InputManifest.model_validate_json(
            self._one(
                "SELECT body_json FROM manifests WHERE film_id=? AND kind='input' AND revision=?",
                (binding.film_id, binding.input_revision),
            )[0]
        )
        final = m.FinalManifest.model_validate_json(
            self._one(
                "SELECT body_json FROM manifests WHERE film_id=? AND kind='final' AND revision=?",
                (binding.film_id, binding.final_revision),
            )[0]
        )
        if (
            source.manifest_hash != binding.input_hash
            or final.manifest_hash != binding.final_hash
            or m.digest(final.postprocess) != binding.postprocess_digest
            or final.input_hash != binding.input_hash
        ):
            raise m.WorkflowError("binding_changed")
        for cleanup, manifest in (
            (binding.input_cleanup, source),
            (binding.workcopy_cleanup, final),
        ):
            members = {item.item_id: item for item in manifest.items}
            for item in cleanup.members:
                expected = members.get(item.item_id)
                if (
                    expected is None
                    or expected.native_identity is None
                    or (
                        item.root_id,
                        item.relative_path,
                        item.size_bytes,
                        item.sha256,
                        native_identity_key(item.native_identity),
                    )
                    != (
                        expected.source_root_id,
                        expected.relative_path,
                        expected.size_bytes,
                        expected.sha256,
                        native_identity_key(expected.native_identity),
                    )
                ):
                    raise m.WorkflowError("binding_changed")
        return source, final

    def _observation_producer(self, evidence: m.EvidenceRef) -> None:
        row = self._one(
            "SELECT role FROM principals WHERE principal_id=?",
            (evidence.producer_id,),
            code="precondition_failed",
        )
        if row[0] in ("compute_worker", "observer"):
            raise m.WorkflowError("precondition_failed")

    def put_approval_context(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.ApprovalContextPut
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if (
                film["version"] != command.expected_version
                or (film["current_context_revision"] or 0)
                != command.expected_context_revision
            ):
                raise m.WorkflowError("cas_conflict")
            if self._conn.execute(
                "SELECT 1 FROM operations WHERE film_id=? AND kind='archive' AND resolved_at IS NULL",
                (command.film_id,),
            ).fetchone():
                # An open archive must settle (receipt or failure) before the
                # operator can re-confirm (F02-P1 D02/N05).
                raise m.WorkflowError("precondition_failed")
            self._require_source_admissible(film)
            self._binding_manifests(command.binding)
            observations = command.observations
            proofs = (
                observations.metadata,
                observations.candidate,
                observations.target,
                observations.input_cleanup,
                observations.workcopy_cleanup,
            )
            for proof in proofs:
                self._observation_producer(proof)
            state = (
                "ready"
                if all(proof.status == "passed" for proof in proofs)
                and command.binding.target.expected_absent
                and command.binding.actor_ids
                else "blocked"
            )
            revision = command.expected_context_revision + 1
            context = m.ApprovalContext(
                film_id=command.film_id,
                context_revision=revision,
                binding=command.binding,
                observations=observations,
                context_hash=m.context_digest(command.binding, observations),
            )
            self._insert(
                "approval_contexts",
                film_id=command.film_id,
                context_revision=revision,
                context_hash=context.context_hash,
                binding_hash=m.digest(command.binding),
                state=state,
                created_at=timestamp(now),
                body_json=m.canonical_json(context),
            )
            version = self._set_film(
                film, current_context_revision=revision, context_state=state
            )
            return Mutation(
                command.film_id,
                version,
                state,
                context_revision=revision,
                context_hash=context.context_hash,
            )

        return self._execute(auth, meta, command, "put_approval_context", apply)

    def _context(self, film: sqlite3.Row) -> m.ApprovalContext:
        if film["current_context_revision"] is None or film["context_state"] != "ready":
            raise m.WorkflowError("binding_changed")
        row = self._one(
            "SELECT * FROM approval_contexts WHERE film_id=? AND context_revision=?",
            (film["film_id"], film["current_context_revision"]),
            code="binding_changed",
        )
        if row["state"] != "ready":
            raise m.WorkflowError("binding_changed")
        context = m.ApprovalContext.model_validate_json(row["body_json"])
        self._binding_manifests(context.binding)
        return context

    def approve(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.ApprovalCreate
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            self._require_source_admissible(film)
            context = self._context(film)
            if (
                context.context_revision != command.context_revision
                or context.context_hash != command.context_hash
                or context.binding != command.binding
            ):
                raise m.WorkflowError("binding_changed")
            if command.expires_at <= now:
                raise m.WorkflowError("approval_expired")
            scrape = self._one(
                "SELECT status FROM stages WHERE film_id=? AND input_revision=? AND stage='scrape'",
                (command.film_id, command.binding.input_revision),
            )
            if scrape[0] != "succeeded":
                raise m.WorkflowError("precondition_failed")
            record = m.ApprovalRecord(
                approval_id=command.approval_id,
                context_revision=context.context_revision,
                context_hash=context.context_hash,
                binding=command.binding,
                binding_hash=m.digest(command.binding),
                operator_principal_id=principal["principal_id"],
                issued_at=now,
                expires_at=command.expires_at,
                authority_epoch=meta.authority_epoch,
                revoked_at=None,
                revocation_reason=None,
            )
            self._insert(
                "approvals",
                approval_id=record.approval_id,
                film_id=command.film_id,
                context_revision=record.context_revision,
                context_hash=record.context_hash,
                binding_hash=record.binding_hash,
                operator_principal_id=record.operator_principal_id,
                epoch=meta.authority_epoch,
                issued_at=timestamp(now),
                expires_at=timestamp(command.expires_at),
                body_json=m.canonical_json(record),
            )
            version = self._set_film(film)
            return Mutation(command.film_id, version, "approved")

        return self._execute(auth, meta, command, "approve", apply)

    def revoke_approval(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.ApprovalRevocation
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._one(
                "SELECT * FROM approvals WHERE approval_id=?", (command.approval_id,)
            )
            record = m.ApprovalRecord.model_validate_json(row["body_json"]).model_copy(
                update={"revoked_at": now, "revocation_reason": command.reason}
            )
            version = self._update(
                "approvals",
                "approval_id",
                command.approval_id,
                command.expected_version,
                revoked_at=timestamp(now),
                body_json=m.canonical_json(record),
            )
            return Mutation(command.approval_id, version, "revoked")

        return self._execute(auth, meta, command, "revoke_approval", apply)

    def _approved(
        self,
        film_id: str,
        approval_id: str,
        now: datetime,
        *,
        allow_source_change: bool = False,
    ) -> m.ApprovalRecord:
        row = self._one(
            "SELECT * FROM approvals WHERE approval_id=?",
            (approval_id,),
            code="precondition_failed",
        )
        if row["revoked_at"] is not None:
            raise m.WorkflowError("approval_revoked")
        if parse_time(row["expires_at"]) <= now:
            raise m.WorkflowError("approval_expired")
        authority = self._one("SELECT * FROM authority WHERE singleton=1")
        record = m.ApprovalRecord.model_validate_json(row["body_json"])
        if (
            record.binding.film_id != film_id
            or record.authority_epoch != authority["epoch"]
        ):
            raise m.WorkflowError("binding_changed")
        film = self._film_row(film_id)
        source_invalidation = None
        if allow_source_change and film["context_state"] == "invalidated":
            source_invalidation = self._conn.execute(
                "SELECT 1 FROM intake_films WHERE film_id=? AND input_revision=? "
                "AND source_invalidated_context_revision=?",
                (
                    film_id,
                    film["current_input_revision"],
                    film["current_context_revision"],
                ),
            ).fetchone()
        if source_invalidation:
            context_row = self._one(
                "SELECT * FROM approval_contexts WHERE film_id=? AND context_revision=?",
                (film_id, film["current_context_revision"]),
                code="binding_changed",
            )
            if context_row["state"] != "ready":
                raise m.WorkflowError("binding_changed")
            context = m.ApprovalContext.model_validate_json(context_row["body_json"])
            self._binding_manifests(context.binding)
        else:
            context = self._context(film)
        if (
            context.context_revision != record.context_revision
            or context.context_hash != record.context_hash
            or record.binding != context.binding
        ):
            raise m.WorkflowError("binding_changed")
        return record

    def _adapter(self, principal_id: str, role: str, now: datetime) -> sqlite3.Row:
        row = self._one(
            "SELECT * FROM principals WHERE principal_id=? AND role=?",
            (principal_id, role),
            code="precondition_failed",
        )
        credentials = self._conn.execute(
            "SELECT expires_at FROM credentials WHERE principal_id=? AND revoked_at IS NULL",
            (principal_id,),
        ).fetchall()
        if not row["boot_id"] or not any(
            parse_time(credential[0]) > now for credential in credentials
        ):
            raise m.WorkflowError("precondition_failed")
        return row

    def _external_attempt(
        self,
        film: sqlite3.Row,
        kind: str,
        operation_id: str,
        adapter: sqlite3.Row,
        approval: m.ApprovalRecord,
        now: datetime,
    ) -> tuple[sqlite3.Row, m.AttemptRecord]:
        job = self._one(
            "SELECT * FROM stages WHERE film_id=? AND input_revision=? AND stage=?",
            (film["film_id"], film["current_input_revision"], kind),
        )
        if job["status"] not in ("pending", "failed", "blocked"):
            raise m.WorkflowError("precondition_failed")
        self._dependencies(job)
        if self._conn.execute(
            "SELECT 1 FROM operations WHERE film_id=? AND kind=? AND resolved_at IS NULL",
            (film["film_id"], kind),
        ).fetchone():
            raise m.WorkflowError("owner_unresolved")
        authority = self._one("SELECT * FROM authority WHERE singleton=1")
        fence = authority["next_fence"]
        self._conn.execute(
            "UPDATE authority SET next_fence=next_fence+1 WHERE singleton=1"
        )
        number = self._one(
            "SELECT coalesce(max(attempt_number),0)+1 FROM attempts WHERE job_id=?",
            (job["job_id"],),
        )[0]
        attempt = m.AttemptRecord(
            attempt_id=str(uuid4()),
            job_id=job["job_id"],
            attempt_number=number,
            tool_versions=m.ToolVersions(
                tool="workflow_" + kind,
                tool_version="1",
                model="none",
                model_version="none",
                profile="approved",
                profile_revision=1,
                config_digest=approval.binding_hash,
            ),
            input_hash=approval.binding.input_hash,
            owner_principal_id=adapter["principal_id"],
            worker_id=None,
            boot_id=adapter["boot_id"],
            authority_epoch=authority["epoch"],
            fencing_token=fence,
            status="running",
            started_at=now,
            ended_at=None,
            error_code=None,
            operation_id=operation_id,
            evidence=(),
        )
        self._insert(
            "attempts",
            attempt_id=attempt.attempt_id,
            job_id=attempt.job_id,
            attempt_number=number,
            owner_principal_id=attempt.owner_principal_id,
            boot_id=attempt.boot_id,
            epoch=attempt.authority_epoch,
            fence=fence,
            input_hash=attempt.input_hash,
            status="running",
            operation_id=operation_id,
            started_at=timestamp(now),
            body_json=m.canonical_json(attempt),
        )
        self._set_stage(job, "running", attempt_id=attempt.attempt_id)
        return job, attempt

    def begin_archive(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.ArchiveAdmission
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            self._require_source_admissible(film)
            approval = self._approved(command.film_id, command.approval_id, now)
            if approval.binding_hash != command.binding_hash:
                raise m.WorkflowError("binding_changed")
            adapter = self._adapter(command.adapter_id, "archive_adapter", now)
            _, final = self._binding_manifests(approval.binding)
            job, attempt = self._external_attempt(
                film, "archive", command.operation_id, adapter, approval, now
            )
            operation = m.ArchiveOperation(
                operation_id=command.operation_id,
                approval_id=approval.approval_id,
                binding_hash=approval.binding_hash,
                attempt_id=attempt.attempt_id,
                adapter_id=adapter["principal_id"],
                target=approval.binding.target,
                status="admitted",
                owned_items=tuple(
                    m.OwnedTargetItem(
                        item_id=item.item_id,
                        relative_path=item.relative_path,
                        native_identity=None,
                    )
                    for item in final.items
                ),
                error_code=None,
                evidence=(),
                version=1,
            )
            self._insert(
                "operations",
                operation_id=command.operation_id,
                film_id=command.film_id,
                kind="archive",
                job_id=job["job_id"],
                attempt_id=attempt.attempt_id,
                approval_id=approval.approval_id,
                owner_principal_id=adapter["principal_id"],
                binding_hash=approval.binding_hash,
                status="admitted",
                created_at=timestamp(now),
                updated_at=timestamp(now),
                body_json=m.canonical_json(operation),
            )
            self._set_film(film)
            return Mutation(
                command.operation_id,
                1,
                "admitted",
                operation_id=command.operation_id,
                attempt_id=attempt.attempt_id,
            )

        return self._execute(auth, meta, command, "begin_archive", apply)

    def _external_fact(
        self,
        operation: sqlite3.Row,
        now: datetime,
        evidence: tuple[m.EvidenceRef, ...],
        status: str,
        error_code: str | None = None,
    ) -> None:
        attempt = self._one(
            "SELECT * FROM attempts WHERE attempt_id=?", (operation["attempt_id"],)
        )
        self._set_attempt(attempt, status, now, evidence, error_code)
        job = self._one("SELECT * FROM stages WHERE job_id=?", (operation["job_id"],))
        film = self._film_row(operation["film_id"])
        approval = self._one(
            "SELECT context_revision FROM approvals WHERE approval_id=?",
            (operation["approval_id"],),
        )
        if (
            film["current_input_revision"] == job["input_revision"]
            and film["current_context_revision"] == approval[0]
        ):
            self._set_stage(job, status)
        # Different current revisions do not inherit this historical success.

    def accept_archive_receipt(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.ArchiveReceipt
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            operation = self._one(
                "SELECT * FROM operations WHERE operation_id=?", (command.operation_id,)
            )
            if (
                operation["kind"] != "archive"
                or operation["owner_principal_id"] != principal["principal_id"]
                or command.producer_id != principal["principal_id"]
            ):
                raise m.WorkflowError("forbidden")
            original = m.ArchiveOperation.model_validate_json(operation["body_json"])
            approval = m.ApprovalRecord.model_validate_json(
                self._one(
                    "SELECT body_json FROM approvals WHERE approval_id=?",
                    (original.approval_id,),
                )[0]
            )
            final = m.FinalManifest.model_validate_json(
                self._one(
                    "SELECT body_json FROM manifests WHERE film_id=? AND kind='final' AND revision=?",
                    (operation["film_id"], approval.binding.final_revision),
                )[0]
            )
            expected = {
                item.item_id: (item.size_bytes, item.sha256) for item in final.items
            }
            actual = {
                item.item_id: (item.size_bytes, item.sha256) for item in command.items
            }
            if (
                command.approval_id != original.approval_id
                or command.target != original.target
                or command.final_manifest_hash != final.manifest_hash
                or actual != expected
                or any(
                    item.evidence.producer_id != principal["principal_id"]
                    for item in command.items
                )
                or operation["status"] not in ("admitted", "running", "unknown")
            ):
                raise m.WorkflowError("precondition_failed")
            record = command.model_copy(update={"accepted_at": now})
            self._insert(
                "receipts",
                receipt_id=command.receipt_id,
                kind="archive",
                operation_id=command.operation_id,
                attempt_id=original.attempt_id,
                approval_id=original.approval_id,
                producer_id=command.producer_id,
                manifest_hash=final.manifest_hash,
                accepted_at=timestamp(now),
                body_json=m.canonical_json(record),
            )
            value = original.model_copy(
                update={
                    "status": "verified",
                    "version": operation["version"] + 1,
                    "evidence": tuple(item.evidence for item in command.items),
                }
            )
            version = self._update(
                "operations",
                "operation_id",
                command.operation_id,
                operation["version"],
                status="verified",
                updated_at=timestamp(now),
                resolved_at=timestamp(now),
                body_json=m.canonical_json(value),
            )
            self._external_fact(operation, now, value.evidence, "succeeded")
            return Mutation(
                command.operation_id,
                version,
                "verified",
                operation_id=command.operation_id,
            )

        return self._execute(auth, meta, command, "accept_archive_receipt", apply)

    def fail_archive(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.ArchiveFailure
    ) -> m.CommandResult:
        """Record a terminal archive failure; re-archiving needs a new approval (C8)."""

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            operation = self._one(
                "SELECT * FROM operations WHERE operation_id=?", (command.operation_id,)
            )
            if (
                operation["kind"] != "archive"
                or operation["owner_principal_id"] != principal["principal_id"]
                or command.evidence.producer_id != principal["principal_id"]
            ):
                raise m.WorkflowError("forbidden")
            if operation["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if operation["status"] not in ("admitted", "running", "unknown"):
                raise m.WorkflowError("precondition_failed")
            original = m.ArchiveOperation.model_validate_json(operation["body_json"])
            value = original.model_copy(
                update={
                    "status": "failed",
                    "error_code": command.error_code,
                    "version": operation["version"] + 1,
                    "evidence": (*original.evidence, command.evidence),
                }
            )
            version = self._update(
                "operations",
                "operation_id",
                command.operation_id,
                operation["version"],
                status="failed",
                updated_at=timestamp(now),
                resolved_at=timestamp(now),
                body_json=m.canonical_json(value),
            )
            self._external_fact(
                operation, now, (command.evidence,), "failed", command.error_code
            )
            return Mutation(
                command.operation_id,
                version,
                "failed",
                operation_id=command.operation_id,
            )

        return self._execute(auth, meta, command, "fail_archive", apply)

    @staticmethod
    def _cleanup_members_equal(
        left: tuple[m.CleanupMember, ...], right: tuple[m.CleanupMember, ...]
    ) -> bool:
        def member(item: m.CleanupMember) -> tuple[object, ...]:
            return (
                item.root_id,
                item.relative_path,
                item.size_bytes,
                item.sha256,
                native_identity_key(item.native_identity),
            )

        return {item.item_id: member(item) for item in left} == {
            item.item_id: member(item) for item in right
        } and len(left) == len(right)

    def _shared_sources(
        self, film_id: str, manifest: m.CleanupManifest, now: datetime
    ) -> None:
        for member in manifest.members:
            key = native_identity_key(member.native_identity)
            refs = self._conn.execute(
                "SELECT r.film_id,r.input_revision,r.native_identity_key,r.body_json "
                "FROM source_refs r JOIN manifest_items i ON "
                "i.film_id=r.film_id AND i.kind=r.manifest_kind "
                "AND i.revision=r.input_revision AND i.item_id=r.item_id "
                "WHERE r.source_root_id=? AND r.retained=1 AND r.film_id<>? "
                "AND (r.native_identity_key=? OR i.path_key=?)",
                (member.root_id, film_id, key, member.relative_path.casefold()),
            ).fetchall()
            for ref in refs:
                identity = m.ManifestItem.model_validate_json(
                    ref["body_json"]
                ).native_identity
                if (
                    ref["native_identity_key"] != key
                    and identity is not None
                    and identity.scheme == member.native_identity.scheme
                    and identity.volume_or_export_id
                    == member.native_identity.volume_or_export_id
                ):
                    # Comparable native identities prove different objects. A
                    # matching path alone must not merge them; NULL or another
                    # identity namespace cannot provide that exclusion proof.
                    continue
                other = self._film_row(ref["film_id"])
                approvals = self._conn.execute(
                    "SELECT approval_id FROM approvals WHERE film_id=? AND context_revision=? AND revoked_at IS NULL",
                    (ref["film_id"], other["current_context_revision"]),
                ).fetchall()
                releasable = False
                for row in approvals:
                    try:
                        approval = self._approved(ref["film_id"], row[0], now)
                    except m.WorkflowError:
                        continue
                    if approval.binding.input_revision != ref["input_revision"]:
                        continue
                    if not any(
                        native_identity_key(item.native_identity)
                        == native_identity_key(member.native_identity)
                        and item.sha256 == member.sha256
                        for item in approval.binding.input_cleanup.members
                    ):
                        continue
                    facts = self._conn.execute(
                        "SELECT r.body_json FROM receipts r JOIN operations o ON o.operation_id=r.operation_id WHERE r.kind='archive' AND o.film_id=?",
                        (ref["film_id"],),
                    ).fetchall()
                    releasable = any(
                        (proof := m.ArchiveReceipt.model_validate_json(fact[0])).target
                        == approval.binding.target
                        and proof.final_manifest_hash == approval.binding.final_hash
                        for fact in facts
                    )
                    if releasable:
                        break
                if not releasable:
                    raise m.WorkflowError("precondition_failed")

    def _workcopy_excludes_inputs(self, manifest: m.CleanupManifest) -> None:
        objects = {
            (
                member.native_identity.scheme,
                member.native_identity.volume_or_export_id,
                member.native_identity.object_id,
            )
            for member in manifest.members
        }
        locations = {
            (member.root_id, member.relative_path.casefold()): member.native_identity
            for member in manifest.members
        }
        # Include this film and references admitted after its approval. Root and
        # item labels cannot turn the same native object into a disposable copy.
        for row in self._conn.execute(
            "SELECT body_json FROM source_refs WHERE retained=1"
        ):
            source = m.ManifestItem.model_validate_json(row[0])
            identity = source.native_identity
            if (
                identity is not None
                and (
                    identity.scheme,
                    identity.volume_or_export_id,
                    identity.object_id,
                )
                in objects
            ):
                raise m.WorkflowError("precondition_failed")
            location = locations.get(
                (source.source_root_id, source.relative_path.casefold())
            )
            if location is not None and (
                identity is None
                or identity.scheme != location.scheme
                or identity.volume_or_export_id != location.volume_or_export_id
            ):
                # As with input sharing, only comparable native evidence can
                # prove that the same controlled path refers to another object.
                raise m.WorkflowError("precondition_failed")

    def begin_cleanup(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.CleanupAdmission
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            film = self._film_row(command.film_id)
            if film["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            # N04: once this approval's archive is accepted, a changed INBOX
            # must not strand input cleanup. Exactness moves to each member's
            # delete handle; binding, sharing and observation checks remain.
            archived_here = (
                self._conn.execute(
                    "SELECT 1 FROM receipts WHERE receipt_id=? AND kind='archive' AND approval_id=?",
                    (command.archive_receipt_id, command.approval_id),
                ).fetchone()
                is not None
            )
            if command.scope == "input" and not archived_here:
                self._require_source_admissible(film)
            approval = self._approved(
                command.film_id,
                command.approval_id,
                now,
                allow_source_change=command.scope == "workcopy" or archived_here,
            )
            expected = (
                approval.binding.input_cleanup
                if command.scope == "input"
                else approval.binding.workcopy_cleanup
            )
            if command.manifest != expected or command.manifest_hash != m.digest(
                expected
            ):
                raise m.WorkflowError("binding_changed")
            receipt_row = self._one(
                "SELECT * FROM receipts WHERE receipt_id=? AND kind='archive'",
                (command.archive_receipt_id,),
                code="precondition_failed",
            )
            receipt = m.ArchiveReceipt.model_validate_json(receipt_row["body_json"])
            archived = m.ApprovalRecord.model_validate_json(
                self._one(
                    "SELECT body_json FROM approvals WHERE approval_id=?",
                    (receipt.approval_id,),
                )[0]
            )
            if (
                archived.binding_hash != approval.binding_hash
                or receipt.final_manifest_hash != approval.binding.final_hash
                or receipt.target != approval.binding.target
            ):
                raise m.WorkflowError("binding_changed")
            observation = command.source_observation
            proofs = (
                observation.writer_check,
                observation.content_check,
                observation.identity_check,
                observation.membership_check,
                observation.shared_references_released,
            )
            for proof in proofs:
                self._observation_producer(proof)
            if (
                observation.manifest_hash != command.manifest_hash
                or not self._cleanup_members_equal(observation.items, expected.members)
                or observation.membership_digest
                != m.digest(
                    tuple(sorted(observation.items, key=lambda item: item.item_id))
                )
                or any(proof.status != "passed" for proof in proofs)
                or observation.writer_check.summary.active_writers is not False
            ):
                raise m.WorkflowError("precondition_failed")
            if command.scope == "input":
                self._shared_sources(command.film_id, expected, now)
            else:
                self._workcopy_excludes_inputs(expected)
            candidates = self._conn.execute(
                "SELECT principal_id FROM principals WHERE role='cleanup_adapter'"
            ).fetchall()
            adapters = []
            for candidate in candidates:
                try:
                    adapters.append(self._adapter(candidate[0], "cleanup_adapter", now))
                except m.WorkflowError:
                    continue
            if len(adapters) != 1:
                raise m.WorkflowError("precondition_failed")
            adapter = adapters[0]
            kind = "cleanup_" + command.scope
            job, attempt = self._external_attempt(
                film, kind, command.operation_id, adapter, approval, now
            )
            intent = m.CleanupIntent(
                operation_id=command.operation_id,
                approval_id=command.approval_id,
                archive_receipt_id=command.archive_receipt_id,
                scope=command.scope,
                manifest=command.manifest,
                manifest_hash=command.manifest_hash,
                source_observation=command.source_observation,
                version=1,
            )
            self._insert(
                "operations",
                operation_id=command.operation_id,
                film_id=command.film_id,
                kind=kind,
                job_id=job["job_id"],
                attempt_id=attempt.attempt_id,
                approval_id=command.approval_id,
                owner_principal_id=adapter["principal_id"],
                binding_hash=approval.binding_hash,
                scope=command.scope,
                status="admitted",
                created_at=timestamp(now),
                updated_at=timestamp(now),
                body_json=m.canonical_json(intent),
            )
            for item in command.manifest.members:
                self._insert(
                    "cleanup_items",
                    operation_id=command.operation_id,
                    item_id=item.item_id,
                    status="pending",
                    updated_at=timestamp(now),
                    body_json=m.canonical_json(item),
                )
            self._set_film(film)
            return Mutation(
                command.operation_id,
                1,
                "admitted",
                operation_id=command.operation_id,
                attempt_id=attempt.attempt_id,
            )

        return self._execute(auth, meta, command, "begin_cleanup", apply)

    def record_cleanup_result(
        self, auth: AuthProof, meta: m.CommandMeta, command: m.CleanupResult
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            operation = self._one(
                "SELECT * FROM operations WHERE operation_id=?", (command.operation_id,)
            )
            if (
                operation["kind"] not in ("cleanup_input", "cleanup_workcopy")
                or operation["owner_principal_id"] != principal["principal_id"]
            ):
                raise m.WorkflowError("forbidden")
            if operation["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            intent = m.CleanupIntent.model_validate_json(operation["body_json"])
            members = {item.item_id: item for item in intent.manifest.members}
            for item in command.items:
                if (
                    item.item_id not in members
                    or item.evidence.producer_id != principal["principal_id"]
                ):
                    raise m.WorkflowError("precondition_failed")
                if (
                    item.status in ("deleted", "already_absent")
                    and item.evidence.status != "passed"
                ):
                    raise m.WorkflowError("precondition_failed")
                old = self._one(
                    "SELECT * FROM cleanup_items WHERE operation_id=? AND item_id=?",
                    (command.operation_id, item.item_id),
                )
                if old["status"] in ("deleted", "already_absent"):
                    raise m.WorkflowError("precondition_failed")
                self._conn.execute(
                    "UPDATE cleanup_items SET status=?,updated_at=?,body_json=? WHERE operation_id=? AND item_id=?",
                    (
                        item.status,
                        timestamp(now),
                        m.canonical_json(item),
                        command.operation_id,
                        item.item_id,
                    ),
                )
                if intent.scope == "input" and item.status in (
                    "deleted",
                    "already_absent",
                ):
                    member = members[item.item_id]
                    self._conn.execute(
                        "UPDATE source_refs SET retained=0 WHERE source_root_id=? AND native_identity_key=?",
                        (member.root_id, native_identity_key(member.native_identity)),
                    )
            statuses = [
                row[0]
                for row in self._conn.execute(
                    "SELECT status FROM cleanup_items WHERE operation_id=?",
                    (command.operation_id,),
                )
            ]
            complete = all(
                status in ("deleted", "already_absent") for status in statuses
            )
            state = (
                "verified"
                if complete
                else "unknown"
                if "unknown" in statuses
                else "running"
            )
            value = intent.model_copy(update={"version": operation["version"] + 1})
            version = self._update(
                "operations",
                "operation_id",
                command.operation_id,
                command.expected_version,
                status=state,
                updated_at=timestamp(now),
                resolved_at=timestamp(now) if complete else None,
                body_json=m.canonical_json(value),
            )
            self._external_fact(
                operation,
                now,
                tuple(item.evidence for item in command.items),
                "succeeded" if complete else "unknown",
            )
            return Mutation(
                command.operation_id, version, state, operation_id=command.operation_id
            )

        return self._execute(auth, meta, command, "record_cleanup_result", apply)
