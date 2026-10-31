"""Transaction-owned intake observations and snapshot publication; no media I/O."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from typing_extensions import Self

from . import intake_models as im
from . import intake_protocol as wire
from . import models as m
from .approval_store import ApprovalCommands
from .auth import AuthProof
from .film_store import native_identity_key
from .intake_discovery_models import (
    IntakeLookup,
    IntakeRootScanBegin,
    IntakeRootScanFinish,
    IntakeRootScanState,
    IntakeScanSeen,
    IntakeSourceNotice,
)
from .intake_snapshot import validate_snapshot
from .manifest import observation_digest
from .store_core import Clock, Mutation, parse_time, timestamp


def _path_parts(value: str) -> tuple[str, ...]:
    path = PureWindowsPath(value)
    if not path.is_absolute() or path.drive.startswith("\\\\") or ".." in path.parts:
        raise m.WorkflowError("local_intake_path_required")
    return tuple(part.casefold() for part in path.parts)


def _overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return left[: len(right)] == right or right[: len(left)] == left


def _object_key(identity: m.NativeIdentity) -> tuple[str, str, str]:
    return identity.scheme, identity.volume_or_export_id, identity.object_id


class IntakeCommands(ApprovalCommands):
    @classmethod
    def open(cls, path: Path, *, clock: Clock | None = None) -> Self:
        store = super().open(path, clock=clock)
        try:
            store._conn.execute("BEGIN IMMEDIATE")
            store._conn.execute(
                "UPDATE intake_root_scans SET required=1,reason='recovery_required',version=version+1"
            )
            store._conn.execute(
                "UPDATE intake_operations SET state='unknown',version=version+1 WHERE unresolved=1"
            )
            store._conn.execute(
                "UPDATE intake_packages SET state='unknown_owner',reason='recovery_required',version=version+1 "
                "WHERE active_operation_id IN (SELECT operation_id FROM intake_operations WHERE unresolved=1)"
            )
            store._conn.execute(
                "UPDATE intake_packages SET stable_since=NULL,version=version+1 "
                "WHERE stable_since IS NOT NULL AND active_operation_id IS NULL "
                "AND state IN ('receiving','waiting_stability')"
            )
            store._conn.commit()
        except BaseException:
            store._conn.rollback()
            store.close()
            raise
        return store

    def _root(self, root_id: str) -> sqlite3.Row:
        return self._one(
            "SELECT * FROM intake_roots WHERE root_id=? AND current=1", (root_id,)
        )

    def _intake_archived(self, package_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM intake_films i JOIN operations o ON o.film_id=i.film_id "
                "JOIN receipts r ON r.operation_id=o.operation_id "
                "WHERE i.package_id=? AND r.kind='archive' LIMIT 1",
                (package_id,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _intake_access(
        principal: sqlite3.Row, root: sqlite3.Row, *, write: bool = False
    ) -> None:
        role = principal["role"]
        if not write and role in ("operator", "observer"):
            return
        if (
            role != "intake_adapter"
            or principal["component_id"] != root["component_id"]
        ):
            raise m.WorkflowError("forbidden")

    def _invalidate_intake_dependents(self, package_id: str) -> None:
        self._conn.execute(
            "UPDATE intake_operations SET publication_allowed=0 WHERE package_id=? AND unresolved=1",
            (package_id,),
        )
        for film in self._conn.execute(
            "SELECT f.* FROM films f JOIN intake_films i ON i.film_id=f.film_id "
            "AND i.input_revision=f.current_input_revision WHERE i.package_id=?",
            (package_id,),
        ).fetchall():
            if film["context_state"] == "ready":
                self._conn.execute(
                    "UPDATE intake_films SET source_invalidated_context_revision=? WHERE film_id=? AND input_revision=?",
                    (
                        film["current_context_revision"],
                        film["film_id"],
                        film["current_input_revision"],
                    ),
                )
                self._set_film(film, context_state="invalidated")

    def configure_intake_root(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeRootPut
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            old = self._conn.execute(
                "SELECT * FROM intake_roots WHERE root_id=? AND current=1",
                (command.root_id,),
            ).fetchone()
            if (old["config_revision"] if old else 0) != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if (
                command.enabled
                and self._conn.execute(
                    "SELECT 1 FROM intake_roots WHERE current=1 AND enabled=1 AND root_id!=?",
                    (command.root_id,),
                ).fetchone()
            ):
                raise m.WorkflowError("input_root_already_enabled")
            self._one(
                "SELECT 1 FROM principals WHERE component_id=? AND role='intake_adapter'",
                (command.component_id,),
                code="intake_component_required",
            )
            source, work = (
                _path_parts(command.private_path),
                _path_parts(command.private_work_path),
            )
            if _overlap(source, work):
                raise m.WorkflowError("overlapping_intake_roots")
            for other in self._conn.execute(
                "SELECT body_json FROM intake_roots WHERE current=1 AND root_id!=?",
                (command.root_id,),
            ):
                registered = im.IntakeRootPut.model_validate_json(other[0])
                for path, native in (
                    (registered.private_path, registered.native_identity),
                    (registered.private_work_path, registered.work_native_identity),
                ):
                    if any(
                        _overlap(candidate, _path_parts(path))
                        for candidate in (source, work)
                    ) or _object_key(native) in (
                        _object_key(command.native_identity),
                        _object_key(command.work_native_identity),
                    ):
                        raise m.WorkflowError("overlapping_intake_roots")
            revision = command.expected_version + 1
            self._conn.execute(
                "UPDATE intake_root_scans SET required=1,reason='changed',version=version+1 WHERE root_id=?",
                (command.root_id,),
            )
            if old:
                self._conn.execute(
                    "UPDATE intake_roots SET current=0 WHERE root_id=? AND current=1",
                    (command.root_id,),
                )
            self._insert(
                "intake_roots",
                root_id=command.root_id,
                config_revision=revision,
                current=1,
                component_id=command.component_id,
                work_root_id=command.work_root_id,
                enabled=int(command.enabled),
                created_at=timestamp(now),
                body_json=m.canonical_json(command),
            )
            for package in self._conn.execute(
                "SELECT * FROM intake_packages WHERE root_id=?", (command.root_id,)
            ).fetchall():
                self._invalidate_intake_dependents(package["package_id"])
                self._update(
                    "intake_packages",
                    "package_id",
                    package["package_id"],
                    package["version"],
                    root_config_revision=revision,
                    source_generation=package["source_generation"] + 1,
                    dirty=1,
                    stable_since=None,
                    state="blocked",
                    reason="changed" if command.enabled else "disabled",
                )
            return Mutation(
                command.root_id, revision, "enabled" if command.enabled else "disabled"
            )

        return self._execute(auth, meta, command, "configure_intake_root", apply)

    @staticmethod
    def _observation_state(
        command: im.IntakeObservation, config: im.IntakeRootPut
    ) -> tuple[im.IntakeState, im.Reason]:
        if command.coverage != "complete":
            if command.failure:
                return "needs_attention", command.failure
            return "receiving", "partial_observation"
        if command.package_native_identity is None or any(
            value.native_identity is None or value.entry_kind in ("unknown", "reparse")
            for value in command.members
        ):
            return "needs_attention", "unknown_identity"
        if command.writer_state != "clear":
            return (
                "receiving",
                "writer_busy" if command.writer_state == "busy" else "writer_unknown",
            )
        if not command.members:
            return "receiving", "empty_package"
        if any(value.temporary for value in command.members):
            return "receiving", "temporary_member"
        if len(command.members) > config.policy.max_members or any(
            len(value.relative_path.split("/")) > config.policy.max_depth
            for value in command.members
        ):
            return "blocked", "limit_exceeded"
        if (
            sum(value.size_bytes or 0 for value in command.members)
            > config.policy.max_package_bytes
        ):
            return "blocked", "limit_exceeded"
        if command.grouping is None or command.grouping.status != "ready":
            return "needs_attention", "ambiguous_grouping"
        return "waiting_stability", "observed"

    def _observation_target(
        self, principal: sqlite3.Row, command: im.IntakeObservation, now: datetime
    ) -> tuple[sqlite3.Row, sqlite3.Row | None]:
        root = self._root(command.root_id)
        self._intake_access(principal, root, write=True)
        if root["config_revision"] != command.root_config_revision:
            raise m.WorkflowError("binding_changed")
        if not root["enabled"]:
            raise m.WorkflowError("intake_disabled")
        old = self._conn.execute(
            "SELECT * FROM intake_packages WHERE package_id=?",
            (command.package_id,),
        ).fetchone()
        if (old["version"] if old else 0) != command.expected_package_version:
            raise m.WorkflowError("cas_conflict")
        if old and old["root_id"] != command.root_id:
            raise m.WorkflowError("binding_changed")
        if old and not old["current"]:
            raise m.WorkflowError("package_replaced")
        if command.observed_at > now or (
            old and command.observed_at < parse_time(old["observed_at"])
        ):
            raise m.WorkflowError("observation_time_invalid")
        if (
            observation_digest(
                command.members,
                parent_context=command.parent_context,
                grouping=command.grouping,
            )
            != command.observation_digest
        ):
            raise m.WorkflowError("observation_digest_mismatch")
        return root, old

    def _observation_values(
        self,
        command: im.IntakeObservation,
        root: sqlite3.Row,
        old: sqlite3.Row | None,
    ) -> tuple[dict[str, Any], bool]:
        """The row `observe_intake` writes, and whether its source changed."""
        if command.grouping:
            self._check_intake_grouping(command.grouping, command)
        config = im.IntakeRootPut.model_validate_json(root["body_json"])
        state, reason = self._observation_state(command, config)
        previous = (
            im.IntakeObservation.model_validate_json(old["observation_json"])
            if old
            else None
        )
        changed = bool(old) and (
            old["observation_digest"] != command.observation_digest
            or old["root_config_revision"] != command.root_config_revision
            or old["relative_path"] != command.relative_path
            or previous is not None
            and native_identity_key(previous.package_native_identity)
            != native_identity_key(command.package_native_identity)
            or state != "waiting_stability"
        )
        generation = old["source_generation"] + int(changed) if old else 1
        stable_since = old["stable_since"] if old and not changed else None
        if state == "waiting_stability" and stable_since is None:
            stable_since = timestamp(command.observed_at)
        if state != "waiting_stability":
            stable_since = None
        if (
            old
            and not changed
            and old["state"]
            in (
                "snapshotting",
                "snapshot_ready",
                "operational_ready",
                "unknown_owner",
            )
        ):
            state, reason = old["state"], old["reason"]
        restored_snapshot = None
        if (
            old
            and not changed
            and old["active_snapshot_id"]
            and old["active_operation_id"] is None
            and state != "unknown_owner"
        ):
            stored = self._one(
                "SELECT body_json FROM intake_snapshots WHERE snapshot_id=?",
                (old["active_snapshot_id"],),
            )
            candidate = im.PackageSnapshot.model_validate_json(stored[0])
            if (
                candidate.admission.source_generation == generation
                and candidate.admission.root_config_revision
                == command.root_config_revision
                and candidate.admission.observation_digest == command.observation_digest
                and candidate.grouping == command.grouping
            ):
                # A dirty notification alone does not create a new input.
                # Existing approvals invalidated by the hint stay invalid.
                restored_snapshot = candidate
                state = (
                    "operational_ready"
                    if candidate.eligibility.decision == "eligible"
                    else "blocked"
                )
                reason = candidate.eligibility.reason
                stable_since = None
        if (
            old
            and (changed or old["dirty"])
            and self._intake_archived(command.package_id)
            and restored_snapshot is None
        ):
            state, reason, stable_since = (
                "needs_attention",
                "recovery_required",
                None,
            )
        if old and not changed and old["reason"] == "publication_blocked":
            # A complete unchanged scan cannot make a rejected film policy
            # compatible, nor should it repeatedly copy the same package.
            state, reason, stable_since = "blocked", "publication_blocked", None
        values = dict(
            root_id=command.root_id,
            root_config_revision=command.root_config_revision,
            relative_path=command.relative_path,
            path_key=command.relative_path.casefold(),
            native_identity_key=native_identity_key(command.package_native_identity),
            parent_context=command.parent_context,
            source_generation=generation,
            state=state,
            reason=reason,
            dirty=0
            if restored_snapshot is not None
            else 1
            if changed or not old
            else old["dirty"],
            observation_digest=command.observation_digest,
            observed_at=timestamp(command.observed_at),
            last_complete_scan_at=timestamp(command.observed_at)
            if command.coverage == "complete"
            else old["last_complete_scan_at"]
            if old
            else None,
            stable_since=stable_since,
            observation_json=m.canonical_json(command),
            grouping_json=m.canonical_json(command.grouping)
            if command.grouping
            else None,
            grouping_revision=command.grouping.grouping_revision
            if command.grouping
            else None,
        )
        return values, changed

    def observe_intake(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeObservation
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            root, old = self._observation_target(principal, command, now)
            new_identity = native_identity_key(command.package_native_identity)
            if (
                old
                and old["native_identity_key"]
                and new_identity
                and old["native_identity_key"] != new_identity
            ):
                version = self._retire_intake_package(old)
                return Mutation(old["package_id"], version, "package_replaced")
            if old is None:
                aliases = self._conn.execute(
                    "SELECT * FROM intake_packages WHERE root_id=? AND current=1 AND "
                    "(path_key=? OR native_identity_key=?)",
                    (
                        command.root_id,
                        command.relative_path.casefold(),
                        native_identity_key(command.package_native_identity),
                    ),
                ).fetchall()
                if len(aliases) > 1:
                    raise m.WorkflowError("intake_identity_conflict")
                if (
                    aliases
                    and aliases[0]["native_identity_key"]
                    and new_identity
                    and aliases[0]["native_identity_key"] != new_identity
                ):
                    self._retire_intake_package(aliases[0])
                    aliases = []
                if aliases:
                    existing = aliases[0]
                    if command.observed_at < parse_time(existing["observed_at"]):
                        raise m.WorkflowError("observation_time_invalid")
                    changed = (
                        existing["observation_digest"] != command.observation_digest
                        or existing["relative_path"] != command.relative_path
                        or existing["native_identity_key"]
                        != native_identity_key(command.package_native_identity)
                        or command.coverage != "complete"
                        or command.writer_state != "clear"
                    )
                    version = existing["version"]
                    if changed:
                        self._invalidate_intake_dependents(existing["package_id"])
                        version = self._update(
                            "intake_packages",
                            "package_id",
                            existing["package_id"],
                            version,
                            source_generation=existing["source_generation"] + 1,
                            dirty=1,
                            stable_since=None,
                            state="receiving",
                            reason="source_changed",
                        )
                    # Discovery does not replace established film/member IDs. The
                    # service rereads this canonical package before its next CAS.
                    return Mutation(existing["package_id"], version, "existing_package")
            values, changed = self._observation_values(command, root, old)
            if changed:
                self._invalidate_intake_dependents(command.package_id)
            if old:
                version = self._update(
                    "intake_packages",
                    "package_id",
                    command.package_id,
                    old["version"],
                    **values,
                )
            else:
                version = 1
                self._insert(
                    "intake_packages",
                    package_id=command.package_id,
                    version=version,
                    **values,
                )
            return Mutation(command.package_id, version, values["state"])

        return self._execute(auth, meta, command, "observe_intake", apply)

    @staticmethod
    def _check_intake_grouping(
        grouping: im.PackageGrouping, observation: im.IntakeObservation
    ) -> None:
        assigned = {
            member.member_id for film in grouping.film_groups for member in film.members
        }
        assigned.update(member.member_id for member in grouping.shared_members)
        assigned.update(grouping.directory_member_ids)
        assigned.update(grouping.unassigned_member_ids)
        if (
            assigned != {member.member_id for member in observation.members}
            or set(grouping.directory_member_ids)
            != {
                member.member_id
                for member in observation.members
                if member.entry_kind == "directory"
            }
            or grouping.parent_context != observation.parent_context
        ):
            raise m.WorkflowError("grouping_coverage_mismatch")

    def resolve_intake_grouping(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeGroupingPut
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?",
                (command.package_id,),
            )
            if package["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if (
                not package["current"]
                or package["source_generation"] != command.source_generation
            ):
                raise m.WorkflowError("binding_changed")
            if (
                command.grouping.grouping_revision
                != (package["grouping_revision"] or 0) + 1
            ):
                raise m.WorkflowError("binding_changed")
            observation = im.IntakeObservation.model_validate_json(
                package["observation_json"]
            )
            self._check_intake_grouping(command.grouping, observation)
            for film in command.grouping.film_groups:
                if self._conn.execute(
                    "SELECT 1 FROM intake_films WHERE film_id=? AND package_id!=?",
                    (film.film_id, command.package_id),
                ).fetchone():
                    raise m.WorkflowError("binding_changed")
            changed = observation.model_copy(
                update={
                    "grouping": command.grouping,
                    "observation_digest": observation_digest(
                        observation.members,
                        parent_context=observation.parent_context,
                        grouping=command.grouping,
                    ),
                }
            )
            self._invalidate_intake_dependents(command.package_id)
            version = self._update(
                "intake_packages",
                "package_id",
                command.package_id,
                package["version"],
                source_generation=package["source_generation"] + 1,
                dirty=1,
                stable_since=None,
                state="waiting_stability"
                if command.grouping.status == "ready"
                else "needs_attention",
                reason="changed"
                if command.grouping.status == "ready"
                else "ambiguous_grouping",
                grouping_revision=command.grouping.grouping_revision,
                grouping_json=m.canonical_json(command.grouping),
                observation_json=m.canonical_json(changed),
                observation_digest=changed.observation_digest,
            )
            return Mutation(command.package_id, version, "grouping_changed")

        return self._execute(auth, meta, command, "resolve_intake_grouping", apply)

    def begin_intake_root_scan(
        self, auth: AuthProof, meta: m.CommandMeta, command: IntakeRootScanBegin
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            root = self._root(command.root_id)
            self._intake_access(principal, root, write=True)
            if (
                not root["enabled"]
                or root["config_revision"] != command.root_config_revision
            ):
                raise m.WorkflowError("binding_changed")
            old = self._conn.execute(
                "SELECT * FROM intake_root_scans WHERE root_id=?", (command.root_id,)
            ).fetchone()
            if (old["version"] if old else 0) != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if (
                command.observed_at > now
                or old
                and command.observed_at < parse_time(old["observed_at"])
            ):
                raise m.WorkflowError("observation_time_invalid")
            values = dict(
                root_config_revision=command.root_config_revision,
                scan_id=command.scan_id,
                required=1,
                reason="partial_observation",
                observed_at=timestamp(command.observed_at),
            )
            if old:
                version = self._update(
                    "intake_root_scans",
                    "root_id",
                    command.root_id,
                    old["version"],
                    **values,
                )
            else:
                version = 1
                self._insert(
                    "intake_root_scans",
                    root_id=command.root_id,
                    version=version,
                    **values,
                )
            return Mutation(command.root_id, version, "scan_required")

        return self._execute(auth, meta, command, "begin_intake_root_scan", apply)

    def finish_intake_root_scan(
        self, auth: AuthProof, meta: m.CommandMeta, command: IntakeRootScanFinish
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            root = self._root(command.root_id)
            self._intake_access(principal, root, write=True)
            row = self._one(
                "SELECT * FROM intake_root_scans WHERE root_id=?", (command.root_id,)
            )
            if row["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if (
                row["scan_id"] != command.scan_id
                or row["root_config_revision"] != command.root_config_revision
                or root["config_revision"] != command.root_config_revision
                or not root["enabled"]
            ):
                raise m.WorkflowError("binding_changed")
            if command.observed_at > now or command.observed_at < parse_time(
                row["observed_at"]
            ):
                raise m.WorkflowError("observation_time_invalid")
            version = self._update(
                "intake_root_scans",
                "root_id",
                command.root_id,
                row["version"],
                required=int(not command.complete),
                reason=command.reason,
                observed_at=timestamp(command.observed_at),
                last_complete_scan_at=timestamp(command.observed_at)
                if command.complete
                else row["last_complete_scan_at"],
            )
            return Mutation(
                command.root_id,
                version,
                "scanned" if command.complete else "scan_required",
            )

        return self._execute(auth, meta, command, "finish_intake_root_scan", apply)

    def get_intake_root_scan(
        self, auth: AuthProof, root_id: str
    ) -> IntakeRootScanState | None:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            self._intake_access(principal, self._root(root_id))
            row = self._conn.execute(
                "SELECT * FROM intake_root_scans WHERE root_id=?", (root_id,)
            ).fetchone()
            return (
                IntakeRootScanState(
                    root_id=root_id,
                    root_config_revision=row["root_config_revision"],
                    version=row["version"],
                    scan_id=row["scan_id"],
                    required=bool(row["required"]),
                    reason=row["reason"],
                    observed_at=parse_time(row["observed_at"]),
                    last_complete_scan_at=parse_time(row["last_complete_scan_at"])
                    if row["last_complete_scan_at"]
                    else None,
                )
                if row
                else None
            )

    def find_intake(self, auth: AuthProof, query: IntakeLookup) -> im.IntakeView | None:
        """Indexed local discovery lookup; identity takes precedence over a renamed path."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            self._intake_access(principal, self._root(query.root_id))
            native = native_identity_key(query.native_identity)
            row = None
            if native is not None:
                row = self._conn.execute(
                    "SELECT * FROM intake_packages WHERE root_id=? AND current=1 AND native_identity_key=?",
                    (query.root_id, native),
                ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM intake_packages WHERE root_id=? AND current=1 AND path_key=?",
                    (query.root_id, query.relative_path.casefold()),
                ).fetchone()
            return self._intake_view(principal, row) if row else None

    def mark_intake_scan_seen(
        self, auth: AuthProof, meta: m.CommandMeta, command: IntakeScanSeen
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            root = self._root(command.root_id)
            self._intake_access(principal, root, write=True)
            scan = self._one(
                "SELECT * FROM intake_root_scans WHERE root_id=?", (command.root_id,)
            )
            if (
                scan["scan_id"] != command.scan_id
                or not scan["required"]
                or root["config_revision"] != command.root_config_revision
                or scan["root_config_revision"] != command.root_config_revision
            ):
                raise m.WorkflowError("binding_changed")
            row = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?",
                (command.package_id,),
            )
            if row["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if row["root_id"] != command.root_id or not row["current"]:
                raise m.WorkflowError("binding_changed")
            version = self._update(
                "intake_packages",
                "package_id",
                command.package_id,
                row["version"],
                last_seen_scan_id=command.scan_id,
            )
            return Mutation(command.package_id, version, "scan_seen")

        return self._execute(auth, meta, command, "mark_intake_scan_seen", apply)

    def list_intake_unseen(
        self,
        auth: AuthProof,
        root_id: str,
        scan_id: str,
        *,
        after_package_id: str | None = None,
        limit: int = 200,
    ) -> im.IntakePage:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise m.WorkflowError("invalid_page_limit")
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            root = self._root(root_id)
            self._intake_access(principal, root, write=True)
            scan = self._one(
                "SELECT * FROM intake_root_scans WHERE root_id=?", (root_id,)
            )
            if (
                scan["scan_id"] != scan_id
                or not scan["required"]
                or scan["root_config_revision"] != root["config_revision"]
            ):
                raise m.WorkflowError("binding_changed")
            rows = self._conn.execute(
                "SELECT * FROM intake_packages WHERE root_id=? AND current=1 AND (last_seen_scan_id IS NULL OR last_seen_scan_id!=?) AND (? IS NULL OR package_id>?) ORDER BY package_id LIMIT ?",
                (root_id, scan_id, after_package_id, after_package_id, limit + 1),
            ).fetchall()
            return im.IntakePage(
                items=tuple(self._intake_view(principal, row) for row in rows[:limit]),
                next_package_id=rows[limit - 1]["package_id"]
                if len(rows) > limit
                else None,
            )

    def record_intake_source_notice(
        self, auth: AuthProof, meta: m.CommandMeta, command: IntakeSourceNotice
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?",
                (command.package_id,),
            )
            root = self._root(package["root_id"])
            self._intake_access(principal, root, write=True)
            if package["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if (
                not package["current"]
                or not root["enabled"]
                or command.source_generation != package["source_generation"]
                or command.root_config_revision != package["root_config_revision"]
                or command.root_config_revision != root["config_revision"]
            ):
                raise m.WorkflowError("binding_changed")
            if command.observed_at > now or command.observed_at < parse_time(
                package["observed_at"]
            ):
                raise m.WorkflowError("observation_time_invalid")
            self._invalidate_intake_dependents(command.package_id)
            state = (
                "unknown_owner"
                if package["state"] == "unknown_owner"
                else (
                    "needs_attention"
                    if self._intake_archived(command.package_id)
                    else "missing"
                    # A dirty hint cannot prove that a missing package returned.
                    # Keep that fact until discovery can regroup a deleted film.
                    if command.kind == "missing" or package["state"] == "missing"
                    else "receiving"
                )
            )
            version = self._update(
                "intake_packages",
                "package_id",
                command.package_id,
                package["version"],
                source_generation=package["source_generation"]
                + int(command.kind == "missing"),
                dirty=1,
                stable_since=None,
                state=state,
                reason="recovery_required"
                if state in {"unknown_owner", "needs_attention"}
                else "source_changed",
                observed_at=timestamp(command.observed_at),
            )
            return Mutation(command.package_id, version, state)

        return self._execute(auth, meta, command, "record_intake_source_notice", apply)

    def _retire_intake_package(self, package: sqlite3.Row) -> int:
        self._invalidate_intake_dependents(package["package_id"])
        return self._update(
            "intake_packages",
            "package_id",
            package["package_id"],
            package["version"],
            current=0,
            source_generation=package["source_generation"] + 1,
            dirty=1,
            stable_since=None,
            state="missing",
            reason="source_changed",
        )

    def retire_archived_package(
        self,
        auth: AuthProof,
        meta: m.CommandMeta,
        command: m.ArchivedPackageRetirement,
    ) -> m.CommandResult:
        """Retire a package after every linked film cleaned its input (N03/V3-3)."""

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?",
                (command.package_id,),
            )
            if package["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            films = self._package_films(command.package_id)
            if (
                not package["current"]
                or not films
                or any(film.cleanup_input_status != "succeeded" for film in films)
            ):
                raise m.WorkflowError("precondition_failed")
            version = self._retire_intake_package(package)
            return Mutation(command.package_id, version, "retired")

        return self._execute(auth, meta, command, "retire_archived_package", apply)

    def begin_intake_snapshot(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeSnapshotAdmission
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?",
                (command.package_id,),
            )
            root = self._root(package["root_id"])
            self._intake_access(principal, root, write=True)
            config = im.IntakeRootPut.model_validate_json(root["body_json"])
            if package["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if not root["enabled"]:
                raise m.WorkflowError("intake_disabled")
            scan = self._conn.execute(
                "SELECT required FROM intake_root_scans WHERE root_id=?",
                (package["root_id"],),
            ).fetchone()
            if scan and scan[0]:
                raise m.WorkflowError("source_rescan_required")
            if self._intake_archived(command.package_id):
                raise m.WorkflowError("archived_source_changed")
            if (
                command.source_generation != package["source_generation"]
                or command.root_config_revision != package["root_config_revision"]
                or command.root_config_revision != root["config_revision"]
                or command.grouping_revision != package["grouping_revision"]
                or command.observation_digest != package["observation_digest"]
                or command.policy_revision != config.policy.policy_revision
                or command.owner_boot_id != principal["boot_id"]
            ):
                raise m.WorkflowError("binding_changed")
            if self._conn.execute(
                "SELECT 1 FROM intake_operations WHERE unresolved=1"
            ).fetchone():
                raise m.WorkflowError("owner_unresolved")
            if (
                package["state"] != "waiting_stability"
                or package["stable_since"] is None
                or package["last_complete_scan_at"] is None
                or (
                    parse_time(package["last_complete_scan_at"])
                    - parse_time(package["stable_since"])
                ).total_seconds()
                < config.policy.stable_seconds
                or (now - parse_time(package["last_complete_scan_at"])).total_seconds()
                > config.policy.rescan_seconds
            ):
                raise m.WorkflowError("intake_not_stable")
            self._insert(
                "intake_operations",
                operation_id=command.operation_id,
                package_id=command.package_id,
                owner_principal_id=principal["principal_id"],
                root_id=root["root_id"],
                root_config_revision=root["config_revision"],
                source_generation=command.source_generation,
                owner_boot_id=command.owner_boot_id,
                state="admitted",
                unresolved=1,
                publication_allowed=1,
                version=1,
                admission_json=m.canonical_json(command),
                observation_json=package["observation_json"],
                config_json=root["body_json"],
                created_at=timestamp(now),
                updated_at=timestamp(now),
            )
            self._update(
                "intake_packages",
                "package_id",
                command.package_id,
                package["version"],
                active_operation_id=command.operation_id,
                state="snapshotting",
                reason="stable",
            )
            return Mutation(
                command.operation_id, 1, "admitted", operation_id=command.operation_id
            )

        return self._execute(auth, meta, command, "begin_intake_snapshot", apply)

    def _intake_operation(
        self, principal: sqlite3.Row, operation_id: str, *, operator: bool = False
    ) -> sqlite3.Row:
        row = self._one(
            "SELECT * FROM intake_operations WHERE operation_id=?", (operation_id,)
        )
        if not (operator and principal["role"] == "operator") and (
            principal["role"] != "intake_adapter"
            or principal["principal_id"] != row["owner_principal_id"]
        ):
            raise m.WorkflowError("forbidden")
        return row

    def list_unresolved_intake_operations(
        self, auth: AuthProof
    ) -> tuple[im.UnresolvedIntakeOperation, ...]:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise m.WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT operation_id,package_id,owner_boot_id,version FROM intake_operations "
                "WHERE unresolved=1 ORDER BY created_at,operation_id"
            ).fetchall()
            return tuple(
                im.UnresolvedIntakeOperation(
                    operation_id=row["operation_id"],
                    package_id=row["package_id"],
                    owner_boot_id=row["owner_boot_id"],
                    version=row["version"],
                )
                for row in rows
            )

    def settle_abandoned_intake(
        self,
        auth: AuthProof,
        meta: m.CommandMeta,
        command: im.IntakeAbandonedSettlement,
    ) -> m.CommandResult:
        """Cancel an operation left by an exited controller boot (T03-P1 D01).

        Its helper ran in a KILL_ON_JOB_CLOSE Job held by that controller, so the
        controller's exit ended it. Same-boot unknowns keep the T02 blocking rule.
        """

        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._intake_operation(principal, command.operation_id, operator=True)
            if row["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if not row["unresolved"]:
                return Mutation(
                    command.operation_id,
                    row["version"],
                    row["state"],
                    operation_id=command.operation_id,
                )
            if row["owner_boot_id"] == command.current_owner_boot_id or not (
                self._conn.execute(
                    "SELECT 1 FROM principals WHERE role='intake_adapter' AND boot_id=?",
                    (command.current_owner_boot_id,),
                ).fetchone()
            ):
                raise m.WorkflowError("precondition_failed")
            version = self._update(
                "intake_operations",
                "operation_id",
                command.operation_id,
                row["version"],
                state="cancelled",
                unresolved=0,
                publication_allowed=0,
                updated_at=timestamp(now),
            )
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?", (row["package_id"],)
            )
            if package["active_operation_id"] == command.operation_id:
                self._update(
                    "intake_packages",
                    "package_id",
                    package["package_id"],
                    package["version"],
                    state="waiting_stability",
                    reason="cancelled",
                    active_operation_id=None,
                    stable_since=None,
                )
            return Mutation(
                command.operation_id,
                version,
                "cancelled",
                operation_id=command.operation_id,
            )

        return self._execute(auth, meta, command, "settle_abandoned_intake", apply)

    def record_intake_owner(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeOwnerObservation
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._intake_operation(principal, command.operation_id)
            if row["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if not row["unresolved"]:
                raise m.WorkflowError("intake_operation_closed")
            if (
                row["source_generation"] != command.source_generation
                or row["owner_boot_id"] != command.owner_boot_id
            ):
                raise m.WorkflowError("binding_changed")
            value = command.owner
            old = (
                im.IntakeOwner.model_validate_json(row["owner_json"])
                if row["owner_json"]
                else None
            )
            if value.observed_at > now or value.observed_at < parse_time(
                row["created_at"]
            ):
                raise m.WorkflowError("observation_time_invalid")
            if old and (
                value.owner_id != old.owner_id
                or value.observed_at < old.observed_at
                or value.observed_total < old.observed_total
                or any(birth not in value.births for birth in old.births)
                or old.identity_uncertain
                and not value.identity_uncertain
                or not set(old.failure_codes).issubset(value.failure_codes)
            ):
                raise m.WorkflowError("owner_evidence_changed")
            state = (
                "unknown"
                if value.identity_uncertain
                else "running"
                if row["state"] in ("admitted", "running")
                else row["state"]
            )
            version = self._update(
                "intake_operations",
                "operation_id",
                command.operation_id,
                row["version"],
                owner_id=value.owner_id,
                owner_json=m.canonical_json(value),
                state=state,
                updated_at=timestamp(now),
            )
            return Mutation(
                command.operation_id, version, state, operation_id=command.operation_id
            )

        return self._execute(auth, meta, command, "record_intake_owner", apply)

    def cancel_intake_snapshot(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeCancel
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._intake_operation(principal, command.operation_id, operator=True)
            if row["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if not row["unresolved"]:
                return Mutation(
                    command.operation_id,
                    row["version"],
                    row["state"],
                    operation_id=command.operation_id,
                )
            state = "unknown" if row["state"] == "unknown" else "stopping"
            version = self._update(
                "intake_operations",
                "operation_id",
                command.operation_id,
                row["version"],
                state=state,
                publication_allowed=0,
                updated_at=timestamp(now),
            )
            return Mutation(
                command.operation_id, version, state, operation_id=command.operation_id
            )

        return self._execute(auth, meta, command, "cancel_intake_snapshot", apply)

    def finish_intake_snapshot(
        self, auth: AuthProof, meta: m.CommandMeta, command: im.IntakeSnapshotResult
    ) -> m.CommandResult:
        def apply(principal: sqlite3.Row, now: datetime) -> Mutation:
            row = self._intake_operation(principal, command.operation_id)
            if row["version"] != command.expected_version:
                raise m.WorkflowError("cas_conflict")
            if not row["unresolved"]:
                raise m.WorkflowError("intake_operation_closed")
            proof = command.exit_proof
            if (
                row["source_generation"] != command.source_generation
                or row["owner_boot_id"] != proof.owner_boot_id
            ):
                raise m.WorkflowError("binding_changed")
            old = (
                im.IntakeOwner.model_validate_json(row["owner_json"])
                if row["owner_json"]
                else None
            )
            if proof.observed_at > now or proof.observed_at < (
                old.observed_at if old else parse_time(row["created_at"])
            ):
                raise m.WorkflowError("observation_time_invalid")
            if old and (
                old.owner_id != proof.owner_id
                or any(
                    b not in tuple(v.birth for v in proof.births) for b in old.births
                )
            ):
                raise m.WorkflowError("owner_evidence_changed")
            if proof.state != "unknown" and (
                old
                and old.identity_uncertain
                or proof.state == "stopped"
                and (not old or tuple(v.birth for v in proof.births) != old.births)
                or proof.state == "no_process_created"
                and old
                and old.observed_total
            ):
                raise m.WorkflowError("owner_unresolved")
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?", (row["package_id"],)
            )
            snapshot = command.snapshot
            current_snapshot = False
            publication_blocked = False
            if snapshot is not None:
                admission = im.IntakeSnapshotAdmission.model_validate_json(
                    row["admission_json"]
                )
                config = im.IntakeRootPut.model_validate_json(row["config_json"])
                validate_snapshot(
                    snapshot,
                    admission,
                    im.IntakeObservation.model_validate_json(row["observation_json"]),
                    config,
                    producer_id=principal["principal_id"],
                    now=now,
                )
                self._insert(
                    "intake_snapshots",
                    snapshot_id=admission.snapshot_id,
                    operation_id=command.operation_id,
                    package_id=row["package_id"],
                    source_generation=command.source_generation,
                    grouping_revision=admission.grouping_revision,
                    completion_digest=snapshot.completion_digest,
                    created_at=timestamp(now),
                    body_json=m.canonical_json(snapshot),
                )
                current_snapshot = bool(row["publication_allowed"]) and (
                    package["source_generation"] == command.source_generation
                    and package["root_config_revision"]
                    == admission.root_config_revision
                    and package["observation_digest"] == admission.observation_digest
                    and package["active_operation_id"] == command.operation_id
                )
                if current_snapshot and snapshot.eligibility.decision == "eligible":
                    # Publication is all-film atomic, but valid snapshot/exit
                    # facts must survive a routine policy change. Check every
                    # existing film before creating or publishing any film.
                    for manifest in snapshot.manifests:
                        existing = self._conn.execute(
                            "SELECT body_json FROM films WHERE film_id=?",
                            (manifest.film_id,),
                        ).fetchone()
                        if existing is None:
                            continue
                        record = m.FilmRecord.model_validate_json(existing[0])
                        if record.policy.policy_revision != manifest.policy_revision:
                            publication_blocked = True
                        try:
                            self._check_attachments(record, manifest)
                        except m.WorkflowError:
                            publication_blocked = True
                if (
                    current_snapshot
                    and snapshot.eligibility.decision == "eligible"
                    and not publication_blocked
                ):
                    for manifest in snapshot.manifests:
                        film = self._conn.execute(
                            "SELECT * FROM films WHERE film_id=?", (manifest.film_id,)
                        ).fetchone()
                        if film is None:
                            group = next(
                                g
                                for g in snapshot.grouping.film_groups
                                if g.film_id == manifest.film_id
                            )
                            self._create_film_sql(
                                m.FilmCreate(
                                    film_id=manifest.film_id,
                                    intake_session_id=row["package_id"],
                                    display_name=group.group_key,
                                    policy=config.film_policy,
                                ),
                                now,
                            )
                            film = self._film_row(manifest.film_id)
                        elif not self._conn.execute(
                            "SELECT 1 FROM intake_films WHERE film_id=? AND package_id=?",
                            (manifest.film_id, row["package_id"]),
                        ).fetchone():
                            raise m.WorkflowError("snapshot_binding_changed")
                        self._put_input_sql(
                            m.InputManifestPut(
                                expected_version=film["version"],
                                film_id=manifest.film_id,
                                manifest=manifest,
                            ),
                            now,
                        )
                        self._insert(
                            "intake_films",
                            film_id=manifest.film_id,
                            input_revision=manifest.revision,
                            package_id=row["package_id"],
                            snapshot_id=admission.snapshot_id,
                            source_generation=command.source_generation,
                            grouping_revision=admission.grouping_revision,
                            created_at=timestamp(now),
                        )
            unresolved = proof.state == "unknown"
            state = (
                "unknown"
                if unresolved
                else "completed"
                if snapshot is not None
                else "cancelled"
                if command.outcome == "cancelled"
                else "failed"
            )
            version = self._update(
                "intake_operations",
                "operation_id",
                command.operation_id,
                row["version"],
                state=state,
                unresolved=int(unresolved),
                exit_json=m.canonical_json(proof),
                result_json=m.canonical_json(command),
                updated_at=timestamp(now),
            )
            if package["active_operation_id"] == command.operation_id:
                same = package["source_generation"] == command.source_generation
                snapshot_values = {}
                if (
                    current_snapshot
                    and snapshot is not None
                    and not publication_blocked
                ):
                    snapshot_values = dict(
                        active_snapshot_id=snapshot.admission.snapshot_id,
                        eligibility_json=m.canonical_json(snapshot.eligibility),
                        dirty=0,
                    )
                self._update(
                    "intake_packages",
                    "package_id",
                    row["package_id"],
                    package["version"],
                    active_operation_id=command.operation_id if unresolved else None,
                    state="unknown_owner"
                    if unresolved
                    else "blocked"
                    if publication_blocked
                    else "operational_ready"
                    if current_snapshot
                    and snapshot is not None
                    and snapshot.eligibility.decision == "eligible"
                    else "blocked"
                    if current_snapshot
                    else (
                        command.outcome
                        if command.outcome
                        in (
                            "receiving",
                            "needs_attention",
                            "blocked",
                            "waiting_capacity",
                        )
                        else "waiting_stability"
                    )
                    if same
                    else package["state"],
                    reason="owner_unknown"
                    if unresolved
                    else "publication_blocked"
                    if publication_blocked
                    else snapshot.eligibility.reason
                    if current_snapshot and snapshot is not None
                    else command.reason
                    if same
                    else package["reason"],
                    **snapshot_values,
                )
            return Mutation(
                command.operation_id, version, state, operation_id=command.operation_id
            )

        return self._execute(auth, meta, command, "finish_intake_snapshot", apply)

    def get_intake_operation(
        self, auth: AuthProof, operation_id: str
    ) -> im.IntakeOperationView:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            row = self._intake_operation(principal, operation_id, operator=True)
            return im.IntakeOperationView(
                operation_id=operation_id,
                version=row["version"],
                admission=im.IntakeSnapshotAdmission.model_validate_json(
                    row["admission_json"]
                ),
                owner=im.IntakeOwner.model_validate_json(row["owner_json"])
                if row["owner_json"]
                else None,
                exit_proof=im.IntakeExitProof.model_validate_json(row["exit_json"])
                if row["exit_json"]
                else None,
                publication_allowed=bool(row["publication_allowed"]),
                state=row["state"],
                result=im.IntakeSnapshotResult.model_validate_json(row["result_json"])
                if row["result_json"]
                else None,
            )

    def get_intake_configuration(
        self, auth: AuthProof, root_id: str
    ) -> im.IntakeRootPut:
        """Local assembly only; never mounted on a monitoring or LAN API."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            root = self._root(root_id)
            if principal["role"] != "operator":
                self._intake_access(principal, root, write=True)
            return im.IntakeRootPut.model_validate_json(root["body_json"])

    def get_intake_observation(
        self, auth: AuthProof, package_id: str
    ) -> im.IntakeObservation:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?", (package_id,)
            )
            if principal["role"] != "operator":
                self._intake_access(
                    principal, self._root(package["root_id"]), write=True
                )
            return im.IntakeObservation.model_validate_json(package["observation_json"])

    def get_intake_snapshot(
        self, auth: AuthProof, package_id: str
    ) -> im.PackageSnapshot | None:
        """The current validated snapshot, for native linked-source observations."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?", (package_id,)
            )
            if principal["role"] != "operator":
                self._intake_access(
                    principal, self._root(package["root_id"]), write=True
                )
            row = self._conn.execute(
                "SELECT body_json FROM intake_snapshots WHERE snapshot_id=? AND source_generation=?",
                (package["active_snapshot_id"], package["source_generation"]),
            ).fetchone()
            if row is None:
                return None
            snapshot = im.PackageSnapshot.model_validate_json(row[0])
            return (
                snapshot
                if snapshot.admission.root_config_revision
                == package["root_config_revision"]
                else None
            )

    def removed_intake_films(self, auth: AuthProof, package_id: str) -> frozenset[str]:
        """Films of the package the operator deleted (#304): discovery treats
        their ids as absent, so a re-dropped package becomes a new film."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?", (package_id,)
            )
            if principal["role"] != "operator":
                self._intake_access(
                    principal, self._root(package["root_id"]), write=True
                )
            return frozenset(
                row[0]
                for row in self._conn.execute(
                    "SELECT f.film_id FROM intake_films i "
                    "JOIN films f ON f.film_id=i.film_id "
                    "WHERE i.package_id=? "
                    "AND json_extract(f.body_json,'$.removed_at') IS NOT NULL",
                    (package_id,),
                )
            )

    def get_intake_work(
        self,
        auth: AuthProof,
        operation_id: str,
        *,
        owner_id: str,
        ffprobe: wire.ProbeArtifact,
    ) -> wire.IntakeWorkRequest:
        """Read the admitted inputs before creating the one fixed native helper."""
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            operation = self._intake_operation(principal, operation_id)
            package = self._one(
                "SELECT * FROM intake_packages WHERE package_id=?",
                (operation["package_id"],),
            )
            root = self._root(operation["root_id"])
            if (
                operation["state"] != "admitted"
                or operation["owner_json"]
                or not operation["publication_allowed"]
                or not root["enabled"]
                or not package["current"]
                or not package["stable_since"]
                or package["source_generation"] != operation["source_generation"]
                or root["config_revision"] != operation["root_config_revision"]
                or package["active_operation_id"] != operation_id
            ):
                raise m.WorkflowError("intake_not_launchable")
            observation = im.IntakeObservation.model_validate_json(
                operation["observation_json"]
            )
            assert observation.grouping is not None
            revisions = []
            for group in observation.grouping.film_groups:
                film = self._conn.execute(
                    "SELECT current_input_revision FROM films WHERE film_id=?",
                    (group.film_id,),
                ).fetchone()
                revisions.append(
                    wire.FilmInputRevision(
                        film_id=group.film_id,
                        input_revision=(film[0] or 0) + 1 if film else 1,
                    )
                )
            return wire.IntakeWorkRequest(
                owner_id=owner_id,
                producer_id=principal["principal_id"],
                admission=im.IntakeSnapshotAdmission.model_validate_json(
                    operation["admission_json"]
                ),
                root=im.IntakeRootPut.model_validate_json(operation["config_json"]),
                observation=observation,
                stable_started_at=parse_time(package["stable_since"]),
                input_revisions=tuple(revisions),
                ffprobe=ffprobe,
            )

    def get_input_manifest(
        self, auth: AuthProof, film_id: str, revision: int
    ) -> m.InputManifest:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            permitted = principal["role"] == "operator"
            if principal["role"] == "intake_adapter":
                permitted = bool(
                    self._conn.execute(
                        "SELECT 1 FROM intake_films i JOIN intake_packages p ON p.package_id=i.package_id "
                        "JOIN intake_roots r ON r.root_id=p.root_id AND r.current=1 "
                        "WHERE i.film_id=? AND i.input_revision=? AND r.component_id=?",
                        (film_id, revision, principal["component_id"]),
                    ).fetchone()
                )
            elif principal["role"] not in ("operator", "observer"):
                permitted = bool(
                    self._conn.execute(
                        "SELECT 1 FROM operations o JOIN stages s ON s.job_id=o.job_id "
                        "WHERE o.film_id=? AND s.input_revision=? AND o.owner_principal_id=?",
                        (film_id, revision, principal["principal_id"]),
                    ).fetchone()
                )
            if not permitted:
                raise m.WorkflowError("forbidden")
            return m.InputManifest.model_validate_json(
                self._one(
                    "SELECT body_json FROM manifests WHERE film_id=? AND kind='input' AND revision=?",
                    (film_id, revision),
                )[0]
            )

    def _intake_view(self, principal: sqlite3.Row, row: sqlite3.Row) -> im.IntakeView:
        self._intake_access(principal, self._root(row["root_id"]))
        observer = principal["role"] == "observer"
        snapshot = (
            self._conn.execute(
                "SELECT body_json FROM intake_snapshots WHERE snapshot_id=?",
                (row["active_snapshot_id"],),
            ).fetchone()
            if row["active_snapshot_id"] and not observer
            else None
        )
        return im.IntakeView(
            package_id=row["package_id"],
            root_id=row["root_id"],
            root_config_revision=row["root_config_revision"],
            version=row["version"],
            source_generation=row["source_generation"],
            relative_path=row["relative_path"],
            parent_context=row["parent_context"],
            state=row["state"],
            reason=row["reason"],
            observed_at=parse_time(row["observed_at"]),
            last_complete_scan_at=parse_time(row["last_complete_scan_at"])
            if row["last_complete_scan_at"]
            else None,
            grouping=im.PackageGrouping.model_validate_json(row["grouping_json"])
            if row["grouping_json"] and not observer
            else None,
            media_probes=im.PackageSnapshot.model_validate_json(
                snapshot[0]
            ).media_probes
            if snapshot
            else (),
            eligibility=im.ProcessingEligibility.model_validate_json(
                row["eligibility_json"]
            )
            if row["eligibility_json"] and not observer
            else None,
            operation_id=row["active_operation_id"],
        )

    def get_intake(self, auth: AuthProof, package_id: str) -> im.IntakeView:
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            return self._intake_view(
                principal,
                self._one(
                    "SELECT * FROM intake_packages WHERE package_id=?", (package_id,)
                ),
            )

    def list_intake_board(self, auth: AuthProof) -> tuple[im.IntakeBoardPackage, ...]:
        """Current packages that have not become films, for the operator's page (H2 R2).

        `missing` is hidden (D07) and `operational_ready` has become films.
        """
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] != "operator":
                raise m.WorkflowError("forbidden")
            rows = self._conn.execute(
                "SELECT p.package_id,p.relative_path,p.state,p.reason,p.observation_json "
                "FROM intake_packages p WHERE p.current=1 "
                "AND p.state NOT IN ('missing','operational_ready') "
                # a removed film no longer holds its package off the board (#304 IR3)
                "AND NOT EXISTS (SELECT 1 FROM intake_films i "
                "LEFT JOIN films f ON f.film_id=i.film_id "
                "WHERE i.package_id=p.package_id "
                "AND json_extract(f.body_json,'$.removed_at') IS NULL) "
                "ORDER BY p.path_key,p.package_id"
            ).fetchall()
        packages = []
        for row in rows:
            observation = im.IntakeObservation.model_validate_json(
                row["observation_json"]
            )
            paths = {v.member_id: v.relative_path for v in observation.members}
            grouping = observation.grouping
            failed = observation.failure  # current, ahead of an earlier grouping
            packages.append(
                im.IntakeBoardPackage(
                    package_id=row["package_id"],
                    relative_path=row["relative_path"],
                    state=row["state"],
                    reason=row["reason"],
                    grouping_reasons=(failed,)
                    if failed
                    else grouping.reasons
                    if grouping
                    else (),
                    unassigned_paths=(
                        (observation.failure_path,) if observation.failure_path else ()
                    )
                    if failed
                    else tuple(
                        paths[member]
                        for member in (
                            grouping.unassigned_member_ids if grouping else ()
                        )
                        if member in paths
                    ),
                )
            )
        return tuple(packages)

    def list_intakes(
        self, auth: AuthProof, query: im.IntakeListQuery, *, current_only: bool = False
    ) -> im.IntakePage:
        query = im.IntakeListQuery.model_validate(query)
        if type(current_only) is not bool:
            raise m.WorkflowError("invalid_current_filter")
        with self._lock:
            principal = self._check_auth(auth, self._observe_time())
            if principal["role"] not in ("operator", "observer", "intake_adapter"):
                raise m.WorkflowError("forbidden")
            filters, params = ["r.current=1"], []
            if current_only:
                filters.append("p.current=1")
            if principal["role"] == "intake_adapter":
                filters.append("r.component_id=?")
                params.append(principal["component_id"])
            if query.root_id:
                filters.append("p.root_id=?")
                params.append(query.root_id)
            if query.states:
                filters.append(
                    "p.state IN (" + ",".join("?" for _ in query.states) + ")"
                )
                params.extend(query.states)
            if query.after_package_id:
                filters.append("p.package_id>?")
                params.append(query.after_package_id)
            rows = self._conn.execute(
                "SELECT p.* FROM intake_packages p JOIN intake_roots r ON r.root_id=p.root_id WHERE "
                + " AND ".join(filters)
                + " ORDER BY p.package_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
            return im.IntakePage(
                items=tuple(
                    self._intake_view(principal, row) for row in rows[: query.limit]
                ),
                next_package_id=rows[query.limit - 1]["package_id"]
                if len(rows) > query.limit
                else None,
            )
