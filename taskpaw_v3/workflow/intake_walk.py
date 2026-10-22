"""Bounded local package-frontier enumeration, without media reads or DB access.

Complete means that the supported root/actor/film frontier was observed and
rechecked, not that any package is complete or ready. Returned identities are
point observations: all handles have closed, and consumers must reopen packages.
Cancellation/deadlines are checked around synchronous native calls; the caller
must retain its supervised owner until an in-flight native call actually returns.
An over-limit root stays partial. This function does not claim paged full-root
coverage or use a truncated result as evidence that an existing package is gone.
"""

from __future__ import annotations

import math
import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from .intake_files import (
    HeldEntry,
    IntakeFileError,
    WindowsFiles,
    _Holds,
    _key,
    _local,
    _parts,
)
from .intake_models import IntakeRootPut
from .manifest import _VIDEO
from .models import NativeIdentity

_VOLUME = re.compile(r"cd[ _-]*[1-9][0-9]*", re.IGNORECASE)
_STOP = {"cancelled", "timeout", "limit_exceeded"}


@dataclass(frozen=True)
class WalkLimits:
    max_directories: int = 4096
    max_entries: int = 65536

    def __post_init__(self) -> None:
        for value, ceiling in ((self.max_directories, 4096), (self.max_entries, 65536)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("invalid_walk_limit")


@dataclass(frozen=True)
class PackageCandidate:
    relative_path: str
    native_identity: NativeIdentity


@dataclass(frozen=True)
class DirectoryObservation:
    relative_path: str | None
    native_identity: NativeIdentity
    file_names: tuple[str, ...] = ()
    directory_names: tuple[str, ...] = ()
    enumerated: bool = False


@dataclass(frozen=True)
class WalkResult:
    root_id: str
    root_config_revision: int
    root_native_identity: NativeIdentity | None
    coverage: Literal["complete", "partial"]
    reasons: tuple[str, ...]
    candidates: tuple[PackageCandidate, ...]
    directories: tuple[DirectoryObservation, ...]
    observed_from: datetime
    observed_until: datetime


@dataclass(frozen=True)
class _Child:
    name: str
    directory: bool
    reparse: bool


class _DirectoryIterator(Protocol):
    def __iter__(self) -> Iterator[os.DirEntry[str]]: ...

    def close(self) -> None: ...


class WalkUnresolved(IntakeFileError):
    def __init__(self, owner: _WalkOwner, result: WalkResult):
        super().__init__("close_failed")
        self.pending_owner = owner
        self.result = result


def _not_cancelled() -> bool:
    return False


def _reason(error: OSError) -> str:
    if isinstance(error, IntakeFileError):
        return error.code
    native = getattr(error, "winerror", None)
    if native in (5, 1314) or isinstance(error, PermissionError):
        return "permission_denied"
    if native in (2, 3) or isinstance(error, FileNotFoundError):
        return "source_changed"
    if native in (32, 33):
        return "writer_busy"
    return "native_io_failed"


class _WalkOwner:
    def __init__(
        self,
        config: IntakeRootPut,
        limits: WalkLimits,
        deadline: float,
        cancelled: Callable[[], bool],
        clock: Callable[[], float],
    ):
        self.config, self.limits = config, limits
        self.deadline, self.cancelled, self.clock = deadline, cancelled, clock
        self.root = _local(Path(config.private_path))
        self._holds = _Holds(WindowsFiles(), config.root_id)
        self._iterators: list[_DirectoryIterator] = []
        self._entries: dict[str, HeldEntry] = {}
        self._directories: dict[str, DirectoryObservation] = {}
        self._snapshots: dict[str, tuple[_Child, ...]] = {}
        self._candidates: dict[str, PackageCandidate] = {}
        self._reasons: list[str] = []
        self._count = 0
        self._directory_count = 0
        self._started = datetime.now(timezone.utc)

    @property
    def closed(self) -> bool:
        return self._holds.closed and not self._iterators

    def _admit(self) -> None:
        if self.cancelled():
            raise IntakeFileError("cancelled")
        if self.clock() >= self.deadline:
            raise IntakeFileError("timeout")

    def _record(self, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)

    def _path(self, relative: str) -> Path:
        return self.root.joinpath(*relative.split("/")) if relative else self.root

    def _acquire(self, path: Path, relative: str) -> HeldEntry:
        self._admit()
        entry = self._holds.acquire(path, relative, directory=True)
        self._admit()
        return entry

    def _directory(self, relative: str) -> HeldEntry:
        if self._directory_count >= self.limits.max_directories:
            raise IntakeFileError("limit_exceeded")
        # Failed identity reads still retain the opened handle until closure.
        self._directory_count += 1
        entry = self._acquire(self._path(relative), relative)
        if any(
            _key(entry.identity) == _key(old.identity) for old in self._entries.values()
        ):
            raise IntakeFileError("identity_changed")
        self._entries[relative] = entry
        self._directories[relative] = DirectoryObservation(
            relative or None, entry.identity
        )
        return entry

    def _read(self, relative: str, *, recheck: bool = False) -> tuple[_Child, ...]:
        self._admit()
        iterator = os.scandir(self._path(relative))
        self._iterators.append(iterator)
        children: list[_Child] = []
        names: set[str] = set()
        ceiling = (
            len(self._snapshots[relative])
            if recheck
            else self.limits.max_entries - self._count
        )
        try:
            self._admit()
            for item in iterator:
                self._admit()
                if len(children) >= ceiling:
                    raise IntakeFileError(
                        "source_changed" if recheck else "limit_exceeded"
                    )
                if not recheck:
                    self._count += 1
                _parts(item.name)
                folded = item.name.casefold()
                if folded in names:
                    raise IntakeFileError("case_conflict")
                names.add(folded)
                info = item.stat(follow_symlinks=False)
                self._admit()
                children.append(
                    _Child(
                        item.name,
                        stat.S_ISDIR(info.st_mode),
                        bool(getattr(info, "st_file_attributes") & 0x400),
                    )
                )
            self._admit()
        finally:
            iterator.close()
            self._iterators.remove(iterator)
        children.sort(key=lambda value: (value.name.casefold(), value.name))
        return tuple(children)

    def _observe(self, relative: str) -> tuple[_Child, ...]:
        children = self._read(relative)
        self._snapshots[relative] = children
        self._directories[relative] = replace(
            self._directories[relative],
            file_names=tuple(value.name for value in children if not value.directory),
            directory_names=tuple(value.name for value in children if value.directory),
            enumerated=True,
        )
        if any(value.reparse for value in children):
            self._record("reparse_point")
        return children

    def _candidate(self, relative: str) -> None:
        entry = self._entries.get(relative)
        if entry is None:
            entry = self._directory(relative)
        self._candidates[relative] = PackageCandidate(relative, entry.identity)

    def _actor_or_package(self, relative: str) -> None:
        self._directory(relative)
        children = self._observe(relative)
        directories = tuple(
            value for value in children if value.directory and not value.reparse
        )
        direct_video = any(
            not value.directory
            and not value.reparse
            and Path(value.name).suffix.lower() in _VIDEO
            for value in children
        )
        volumes_only = bool(directories) and all(
            _VOLUME.fullmatch(value.name) for value in directories
        )
        if direct_video or volumes_only:
            if direct_video and directories and not volumes_only:
                self._record("ambiguous_layout")
                return
            self._candidate(relative)
            return
        # The fixed conventional frontier is Actor/Film. Film internals, shared
        # attachments and multiple videos are handled by package grouping later.
        for child in directories:
            try:
                self._candidate(relative + "/" + child.name)
            except OSError as error:
                reason = _reason(error)
                self._record(reason)
                if reason in _STOP:
                    raise

    def collect(self) -> None:
        self._admit()
        # Bound configured ancestors as well as the discovered frontier.
        if len(self.root.parts) > 64:
            raise IntakeFileError("limit_exceeded")
        path = Path(self.root.anchor)
        ancestors = [self._acquire(path, "")]
        for part in self.root.parts[1:-1]:
            path /= part
            ancestors.append(self._acquire(path, ""))
        root = self._directory("")
        self._holds.api.local_filesystem(self.root, root.handle)
        self._admit()
        if self.config.native_identity.root_id != self.config.root_id or _key(
            root.identity
        ) != _key(self.config.native_identity):
            raise IntakeFileError("identity_changed")
        children = self._observe("")
        if any(not child.directory for child in children):
            self._record("root_package_required")
        for child in children:
            if not child.directory or child.reparse:
                continue
            try:
                self._actor_or_package(child.name)
            except OSError as error:
                reason = _reason(error)
                self._record(reason)
                if reason in _STOP:
                    raise
        # Coverage can be positive only after every inspected frontier listing
        # agrees while all observed directory objects still deny rename/delete.
        if not self._reasons:
            for relative, previous in self._snapshots.items():
                if self._read(relative, recheck=True) != previous:
                    raise IntakeFileError("source_changed")
            for relative, entry in self._entries.items():
                self._admit()
                current = self._holds.api.entry(
                    entry.handle, self._path(relative), relative, self.config.root_id
                )
                self._admit()
                if current.signature != entry.signature:
                    raise IntakeFileError("source_changed")

    def result(self) -> WalkResult:
        root = self._entries.get("")
        return WalkResult(
            self.config.root_id,
            self.config.expected_version + 1,
            root.identity if root else None,
            "partial" if self._reasons else "complete",
            tuple(self._reasons),
            tuple(
                self._candidates[key]
                for key in sorted(self._candidates, key=str.casefold)
            ),
            tuple(self._directories.values()),
            self._started,
            datetime.now(timezone.utc),
        )

    def close(self) -> None:
        failed = False
        for iterator in self._iterators[:]:
            try:
                iterator.close()
                self._iterators.remove(iterator)
            except OSError:
                failed = True
        try:
            self._holds.close()
        except OSError:
            failed = True
        if failed:
            raise IntakeFileError("close_failed")


def walk_root(
    config: IntakeRootPut,
    *,
    limits: WalkLimits = WalkLimits(),
    deadline: float,
    cancelled: Callable[[], bool] = _not_cancelled,
    clock: Callable[[], float] = time.monotonic,
) -> WalkResult:
    """Observe the supported frontier; partial results never prove absence.

    An unresolved close transfers the exact owner to the caller. The caller must
    retain it and block replacement scans until ``pending_owner.close()`` proves
    closure. Neither this function nor the result is a readiness authorization.
    """
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise ValueError("invalid_walk_deadline")
    owner = _WalkOwner(config, limits, deadline, cancelled, clock)
    try:
        try:
            owner.collect()
            owner._admit()
        except OSError as error:
            owner._record(_reason(error))
    finally:
        try:
            owner.close()
        except OSError:
            owner._record("close_failed")
            raise WalkUnresolved(owner, owner.result()) from None
    try:
        owner._admit()
    except OSError as error:
        owner._record(_reason(error))
    return owner.result()
