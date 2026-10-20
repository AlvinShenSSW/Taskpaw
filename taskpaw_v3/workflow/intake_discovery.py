"""Bounded native observations; these are discovery facts, never readiness proof."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from . import intake_models as im
from .intake_files import HeldPackage, IntakeFileError, _key
from .manifest import group_keys, group_package, observation_digest, parse_nfo_hint

Trigger = Literal[
    "startup", "notification", "rescan", "overflow", "reconnect", "manual"
]


class DiscoveryUnresolved(IntakeFileError):
    def __init__(self, code: str, owner: HeldPackage):
        super().__init__(code)
        self.pending_owner = owner


def _uid() -> str:
    return str(uuid4())


def _members(
    source: HeldPackage, previous: im.IntakeObservation | None
) -> tuple[im.ObservedMember, ...]:
    old = previous.members if previous else ()
    used: set[str] = set()
    result = []
    for relative, entry in source.entries.items():
        same = [
            v
            for v in old
            if v.native_identity
            and _key(v.native_identity) == _key(entry.identity)
            and v.member_id not in used
        ]
        exact = [v for v in same if v.relative_path.casefold() == relative.casefold()]
        chosen = exact[0] if exact else same[0] if len(same) == 1 else None
        member_id = chosen.member_id if chosen else _uid()
        used.add(member_id)
        result.append(
            im.ObservedMember(
                member_id=member_id,
                relative_path=relative,
                entry_kind="directory" if entry.directory else "file",
                native_identity=entry.identity,
                size_bytes=entry.size_bytes,
                mtime_ns=entry.mtime_ns,
                change_token=str(entry.change_filetime),
                temporary=entry.temporary,
            )
        )
    return tuple(result)


def _group(
    source: HeldPackage,
    members: tuple[im.ObservedMember, ...],
    parent: str | None,
    previous: im.IntakeObservation | None,
    root_config_revision: int,
) -> im.PackageGrouping:
    old = previous.grouping if previous else None
    if (
        old
        and previous
        # A new root configuration (e.g. more allowed attachments) re-groups;
        # film ids still come from the previous keys below (H2 R3, D01).
        and previous.root_config_revision == root_config_revision
        and old.parent_context == parent
        and observation_digest(members, parent_context=parent, grouping=old)
        == previous.observation_digest
    ):
        return old  # Includes a prior explicit operator grouping correction.
    hints = []
    for value in members:
        if (
            value.entry_kind == "file"
            and PurePosixPath(value.relative_path).suffix.lower() == ".nfo"
        ):
            payload = (
                b""
                if value.size_bytes is None or value.size_bytes > 262144
                else b"".join(source.chunks(value.relative_path))
            )
            hints.append(parse_nfo_hint(payload, member_id=value.member_id))
    keys = group_keys(members, nfo_hints=tuple(hints))
    previous_keys = {g.group_key: g.film_id for g in old.film_groups} if old else {}
    grouping = group_package(
        members,
        film_ids=tuple(
            im.FilmIdentity(group_key=k, film_id=previous_keys.get(k, _uid()))
            for k in keys
        ),
        parent_context=parent,
        grouping_revision=old.grouping_revision if old else 1,
        nfo_hints=tuple(hints),
        allowed_attachment_extensions=source.policy.allowed_attachment_extensions,
    )
    if old:
        # A renamed video has the same held native object/member identity. Match
        # the complete video set only; never infer identity from title alone.
        identities = {frozenset(g.video_member_ids): g.film_id for g in old.film_groups}
        changes = {
            g.film_id: identities[frozenset(g.video_member_ids)]
            for g in grouping.film_groups
            if g.group_key not in previous_keys
            and frozenset(g.video_member_ids) in identities
        }
        all_ids = [changes.get(g.film_id, g.film_id) for g in grouping.film_groups]
        if len(all_ids) == len(set(all_ids)):
            grouping = grouping.model_copy(
                update={
                    "film_groups": tuple(
                        g.model_copy(
                            update={"film_id": changes.get(g.film_id, g.film_id)}
                        )
                        for g in grouping.film_groups
                    ),
                    "shared_members": tuple(
                        v.model_copy(
                            update={
                                "film_ids": tuple(
                                    changes.get(fid, fid) for fid in v.film_ids
                                )
                            }
                        )
                        for v in grouping.shared_members
                    ),
                }
            )
        if grouping != old:
            grouping = grouping.model_copy(
                update={"grouping_revision": old.grouping_revision + 1}
            )
    return grouping


def observe_package(
    config: im.IntakeRootPut,
    relative_path: str,
    *,
    previous: im.IntakeObservation | None = None,
    package_version: int = 0,
    observed_at: datetime,
    trigger: Trigger,
) -> im.IntakeObservation:
    """The caller supplies persisted previous IDs; no database or default path."""
    source = HeldPackage(
        Path(config.private_path),
        relative_path,
        root_id=config.root_id,
        expected_root=config.native_identity,
        policy=config.policy,
    )
    parent: str | None = str(PurePosixPath(relative_path).parent)
    parent = None if parent == "." else parent
    failure = None
    compatible = previous
    grouping: im.PackageGrouping | None
    try:
        with source:
            if previous and (
                previous.package_native_identity is None
                or source.package_identity is None
                or _key(previous.package_native_identity)
                != _key(source.package_identity)
            ):
                compatible = None
            members = _members(source, compatible)
            grouping = _group(
                source, members, parent, compatible, config.expected_version + 1
            )
            source.recheck()
    except IntakeFileError as exc:
        if exc.code in {"close_failed", "native_io_failed"}:
            # The owning service must retain this object; it cannot turn an
            # unresolved native observation into a complete empty directory.
            raise DiscoveryUnresolved(exc.code, source) from None
        failure = exc.code
        members = compatible.members if compatible else ()
        grouping = (
            compatible.grouping
            if compatible and compatible.parent_context == parent
            else None
        )
    return im.IntakeObservation(
        root_id=config.root_id,
        root_config_revision=config.expected_version + 1,
        package_id=compatible.package_id if compatible else _uid(),
        expected_package_version=package_version if compatible else 0,
        package_native_identity=source.package_identity,
        relative_path=relative_path,
        parent_context=parent,
        observed_at=observed_at,
        coverage="partial" if failure else "complete",
        trigger=trigger,
        members=members,
        observation_digest=observation_digest(
            members, parent_context=parent, grouping=grouping
        ),
        grouping=grouping,
        writer_state="busy"
        if failure == "writer_busy"
        else "unknown"
        if failure
        else "clear",
    )
