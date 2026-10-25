"""Verified private snapshot names; same-volume files may share NTFS data."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from .intake_files import (
    HeldEntry,
    HeldPackage,
    IntakeFileError,
    WindowsFiles,
    _Holds,
    _key,
    _local,
)
from .models import NativeIdentity


@dataclass(frozen=True)
class CopiedEntry:
    source: HeldEntry
    copy: HeldEntry
    sha256: str | None
    linked: bool = False


class PrivateCopy:
    """Must live inside HeldPackage's context, including probes and publication."""

    def __init__(
        self,
        source: HeldPackage,
        work_root: Path,
        *,
        root_id: str,
        expected_root: NativeIdentity,
        operation_id: str,
    ):
        source._require_open()
        try:
            if str(UUID(operation_id)) != operation_id:
                raise ValueError()
        except ValueError:
            raise IntakeFileError("invalid_operation_id") from None
        self.source, self.work_root = source, _local(work_root)
        if self.work_root.is_relative_to(source.root) or source.root.is_relative_to(
            self.work_root
        ):
            raise IntakeFileError("overlapping_roots")
        if root_id == source.root_id or expected_root.root_id != root_id:
            raise IntakeFileError("identity_changed")
        self.root_id, self.expected_root = root_id, expected_root
        self.operation_path = self.work_root / "intake" / operation_id
        self.partial_path = self.operation_path / "partial"
        self.snapshot_path = self.operation_path / "snapshot"
        self.api = WindowsFiles()
        self._root_holds = _Holds(self.api, root_id)
        self._tree_holds = _Holds(self.api, root_id)
        self._location = self.partial_path
        self._entered = self._copied = self._copy_attempted = self._published = False
        self.entries: dict[str, CopiedEntry] = {}
        self._verified_bytes = 0

    @property
    def closed(self) -> bool:
        return self._root_holds.closed and self._tree_holds.closed

    def __enter__(self) -> PrivateCopy:
        self.source._require_open()
        if self._entered or self.closed:
            raise IntakeFileError("copy_closed")
        try:
            root = self._root_holds.chain(self.work_root)[-1]
            self.api.local_filesystem(self.work_root, root.handle)
            if _key(root.identity) != _key(self.expected_root) or _key(
                root.identity
            ) == _key(self.source.expected_root):
                raise IntakeFileError("identity_changed")
            intake = self.operation_path.parent
            intake.mkdir(exist_ok=True)
            self._root_holds.acquire(intake, "", directory=True)
            try:
                self.operation_path.mkdir()
            except FileExistsError:
                raise IntakeFileError("operation_path_exists") from None
            self._root_holds.acquire(self.operation_path, "", directory=True)
            self.partial_path.mkdir()
            self._tree_holds.acquire(self.partial_path, "", directory=True)
            self._entered = True
            return self
        except BaseException:
            self.close()
            raise

    def _require_open(self) -> None:
        self.source._require_open()
        if not self._entered or self.closed:
            raise IntakeFileError("copy_closed")

    def copy_members(
        self,
        *,
        on_progress: Callable[[str, str, int], None] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> tuple[CopiedEntry, ...]:
        self._require_open()
        if self._copy_attempted:
            raise IntakeFileError("copy_already_attempted")
        self._copy_attempted = True
        total = sum(entry.size_bytes for entry in self.source.entries.values())
        if total > self.source.policy.max_package_bytes:
            raise IntakeFileError("limit_exceeded")
        same_volume = (
            self.expected_root.volume_or_export_id
            == self.source.expected_root.volume_or_export_id
        )
        copy_capacity_checked = not same_volume
        reserve = self.source.policy.min_free_bytes
        if shutil.disk_usage(self.work_root).free < reserve + (
            0 if same_volume else total
        ):
            raise IntakeFileError("waiting_capacity")
        copied = 0
        for relative, entry in self.source.entries.items():
            if check_cancelled:
                check_cancelled()
            path = self.partial_path.joinpath(*relative.split("/"))
            if entry.directory:
                path.mkdir()
                target = self._tree_holds.acquire(path, relative, directory=True)
                self.entries[relative] = CopiedEntry(entry, target, None)
                continue
            if same_volume:
                try:
                    os.link(self.source.path.joinpath(*relative.split("/")), path)
                except OSError as exc:
                    if exc.errno not in {
                        errno.EXDEV,
                        errno.ENOTSUP,
                        errno.EOPNOTSUPP,
                    } and getattr(exc, "winerror", None) not in {1, 17, 50}:
                        raise
                else:
                    self.source.record_link(relative)
                    pinned = self._tree_holds.acquire(path, relative, directory=False)
                    if (
                        _key(pinned.identity) != _key(entry.identity)
                        or pinned.size_bytes != entry.size_bytes
                    ):
                        raise IntakeFileError("copy_verification_failed")
                    digest = hashlib.sha256()
                    for chunk in self.source.chunks(relative):
                        if check_cancelled:
                            check_cancelled()
                        digest.update(chunk)
                        self._verified_bytes += len(chunk)
                        if on_progress:
                            on_progress("verify", relative, self._verified_bytes)
                    self.entries[relative] = CopiedEntry(
                        entry, pinned, digest.hexdigest(), True
                    )
                    continue
            if not copy_capacity_checked:
                if shutil.disk_usage(self.work_root).free < total + reserve:
                    raise IntakeFileError("waiting_capacity")
                copy_capacity_checked = True
            handle = self.api.create(path)
            # Register ownership before the first potentially failing write.
            self._tree_holds.handles.append(handle)
            digest = hashlib.sha256()
            for chunk in self.source.chunks(relative):
                if check_cancelled:
                    check_cancelled()
                self.api.write(handle, chunk)
                digest.update(chunk)
                copied += len(chunk)
                if on_progress:
                    on_progress("copy", relative, copied)
            self.api.flush(handle)
            target = self.api.entry(handle, path, relative, self.root_id)
            self.api.close(handle)
            self._tree_holds.handles.remove(handle)
            pinned = self._tree_holds.acquire(path, relative, directory=False)
            if _key(pinned.identity) != _key(target.identity):
                raise IntakeFileError("copy_verification_failed")
            value = CopiedEntry(entry, pinned, digest.hexdigest())
            self._verify_file(
                value, on_progress=on_progress, check_cancelled=check_cancelled
            )
            self.entries[relative] = value
        self.source.recheck()
        self._copied = True
        return tuple(self.entries.values())

    def _verify_file(
        self,
        value: CopiedEntry,
        *,
        on_progress: Callable[[str, str, int], None] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> None:
        if value.copy.directory or value.linked:
            # The linked bytes were hashed once under the retained source's
            # no-write/no-delete handle. Reading the other name adds no proof.
            return
        self.api.rewind(value.copy.handle)
        digest, count = hashlib.sha256(), 0
        while chunk := self.api.read(value.copy.handle):
            if check_cancelled:
                check_cancelled()
            count += len(chunk)
            if count > value.source.size_bytes:
                raise IntakeFileError("copy_verification_failed")
            digest.update(chunk)
            self._verified_bytes += len(chunk)
            if on_progress:
                on_progress("verify", value.source.relative_path, self._verified_bytes)
        if count != value.source.size_bytes or digest.hexdigest() != value.sha256:
            raise IntakeFileError("copy_verification_failed")

    def private_path(self, relative: str) -> Path:
        self._require_open()
        if not self._copied:
            raise IntakeFileError("copy_incomplete")
        if relative not in self.entries or self.entries[relative].copy.directory:
            raise IntakeFileError("unknown_member")
        return self._location.joinpath(*relative.split("/"))

    def publish(
        self,
        completion: bytes,
        *,
        on_progress: Callable[[str, str, int], None] | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> Path:
        self._require_open()
        if not self._copied:
            raise IntakeFileError("copy_incomplete")
        if self._published or self._location != self.partial_path:
            raise IntakeFileError("copy_already_published")
        if not isinstance(completion, bytes) or len(completion) > 64 * 1024 * 1024:
            raise IntakeFileError("limit_exceeded")
        self.source.recheck()
        for entry in self.entries.values():
            self._verify_file(
                entry, on_progress=on_progress, check_cancelled=check_cancelled
            )
        if check_cancelled:
            check_cancelled()
        # Windows cannot rename a directory while its non-delete-shared child
        # handles are held. Retain its operation/root guards, close the tree,
        # then verify the exact renamed objects before writing completion.
        self._tree_holds.close()
        os.rename(self.partial_path, self.snapshot_path)
        self._location = self.snapshot_path
        self._tree_holds = _Holds(self.api, self.root_id)
        self._tree_holds.acquire(self.snapshot_path, "", directory=True)
        for relative, old in tuple(self.entries.items()):
            pinned = self._tree_holds.acquire(
                self.snapshot_path.joinpath(*relative.split("/")),
                relative,
                directory=old.copy.directory,
            )
            if _key(pinned.identity) != _key(old.copy.identity):
                raise IntakeFileError("copy_verification_failed")
            value = CopiedEntry(old.source, pinned, old.sha256, old.linked)
            self._verify_file(
                value, on_progress=on_progress, check_cancelled=check_cancelled
            )
            self.entries[relative] = value
        self.source.recheck()
        if check_cancelled:
            check_cancelled()
        temporary = self.operation_path / "completion.tmp"
        handle = self.api.create(temporary)
        self._root_holds.handles.append(handle)
        self.api.write(handle, completion)
        self.api.flush(handle)
        self.api.close(handle)
        self._root_holds.handles.remove(handle)
        os.rename(temporary, self.operation_path / "completion.json")
        self._published = True
        return self.snapshot_path

    def close(self) -> None:
        failed = False
        for owner in (self._tree_holds, self._root_holds):
            try:
                owner.close()
            except OSError:
                failed = True
        if failed:
            raise IntakeFileError("close_failed")

    def __exit__(self, *_: object) -> None:
        self.close()
