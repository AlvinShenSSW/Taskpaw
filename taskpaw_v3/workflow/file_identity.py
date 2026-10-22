"""One native file identity for manifests, approval observations and C6 deletes.

``native`` records the same identity the intake helper records: on Windows the
``FILE_ID_INFO`` of a handle opened without following reparse points
(``windows_file_id``, ``{volume:016x}`` + 16-byte object hex), elsewhere the
``lstat`` device/inode pair (``posix_inode``, development and tests only).

``check`` compares identity and size without reading content (D12).
``delete_exact`` (C6) opens the member once with read-only sharing, checks
identity, size and SHA-256 on that same handle and then marks that handle for
deletion. It never unlinks by path on Windows.
"""

from __future__ import annotations

import ctypes
import functools
import hashlib
import os
import stat
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes as wt
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from . import models as m
from .intake_files import (
    IntakeFileError,
    WindowsFiles,
    _Basic,
    _FileId,
    _key,
    _Standard,
)

CheckStatus = Literal[
    "ok",
    "already_absent",
    "blocked_identity",
    "blocked_changed",
    "blocked_writing",
    "failed",
]
# Exactly the T01 ``CleanupItemResult`` status strings a delete can produce.
DeleteStatus = Literal[
    "deleted",
    "already_absent",
    "blocked_identity",
    "blocked_changed",
    "blocked_writing",
    "failed",
]
Interrupted = Literal["interrupted"]

_DELETE = 0x00010000
_GENERIC_READ = 0x80000000
_SHARE_READ = 0x1
_OPEN_EXISTING = 3
_OPEN_REPARSE_POINT = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_FILE_DISPOSITION_INFO = 4
_CHUNK = 1024 * 1024


class _Disposition(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _Files(WindowsFiles):
    """Intake's checked Win32 surface plus same-handle delete disposition."""

    def __init__(self) -> None:
        super().__init__()
        function = self.kernel.SetFileInformationByHandle
        function.restype = wt.BOOL
        function.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]

    def open_exact(self, path: Path, access: int) -> int:
        # Read-only sharing: any existing writer/deleter/renamer makes this a
        # sharing violation, and none can start while the handle is held.
        handle = self.kernel.CreateFileW(
            str(path),
            access,
            _SHARE_READ,
            None,
            _OPEN_EXISTING,
            _OPEN_REPARSE_POINT | _BACKUP_SEMANTICS,
            None,
        )
        self.check(handle != ctypes.c_void_p(-1).value)
        return int(handle)

    def facts(self, handle: int) -> tuple[tuple[str, str, str] | None, int]:
        """(identity key or None when not a plain regular file, size)."""
        basic, standard, file_id = _Basic(), _Standard(), _FileId()
        for kind, data in ((0, basic), (1, standard), (18, file_id)):
            self.check(
                self.kernel.GetFileInformationByHandleEx(
                    handle, kind, ctypes.byref(data), ctypes.sizeof(data)
                )
            )
        if (
            basic.attributes & 0x400
            or standard.directory
            or standard.delete_pending
            or self.kernel.GetFileType(handle) != 1
        ):
            return None, standard.size
        key = (
            "windows_file_id",
            f"{file_id.volume:016x}",
            bytes(file_id.object).hex(),
        )
        return key, standard.size

    def dispose(self, handle: int) -> None:
        info = _Disposition(1)
        self.check(
            self.kernel.SetFileInformationByHandle(
                handle, _FILE_DISPOSITION_INFO, ctypes.byref(info), ctypes.sizeof(info)
            )
        )


@functools.cache
def _api() -> _Files:
    return _Files()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def native(path: Path, root_id: str) -> m.NativeIdentity:
    """The identity intake would record for this regular file (D08).

    Symlinks, reparse points, directories and (on Windows) alternate data
    streams are rejected with ``IntakeFileError`` exactly as intake rejects them.
    """
    if sys.platform == "win32":
        api = _api()
        handle = api.open(path, directory=False)
        try:
            entry = api.entry(handle, path, "", root_id)
        finally:
            api.close(handle)
        if entry.directory:
            raise IntakeFileError("not_a_file")
        return entry.identity
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise IntakeFileError("reparse_point")
    if not stat.S_ISREG(info.st_mode):
        raise IntakeFileError("unsupported_object")
    return m.NativeIdentity(
        scheme="posix_inode",
        root_id=root_id,
        volume_or_export_id=str(info.st_dev),
        object_id=str(info.st_ino),
        observed_at=_now(),
        evidence_id=str(uuid4()),
    )


def _open_status(
    exc: IntakeFileError,
) -> Literal["blocked_writing", "already_absent", "failed"]:
    if exc.code == "writer_busy":  # ERROR_SHARING/LOCK_VIOLATION
        return "blocked_writing"
    if exc.code == "source_changed":  # ERROR_FILE/PATH_NOT_FOUND
        return "already_absent"
    return "failed"


class _Interrupted(Exception):
    """Shutdown was requested while a member was being hashed."""


def _hash_matches(
    read: Callable[[], bytes],
    size: int,
    sha256: str,
    cancel: threading.Event | None = None,
) -> bool:
    digest, total = hashlib.sha256(), 0
    while chunk := read():
        if cancel is not None and cancel.is_set():
            raise _Interrupted
        total += len(chunk)
        if total > size:
            return False
        digest.update(chunk)
    return total == size and digest.hexdigest() == sha256


def check(path: Path, member: m.CleanupMember) -> CheckStatus:
    """Identity + size under read-only sharing; content is never read (D12)."""
    expected = _key(member.native_identity)
    if sys.platform != "win32":
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return "already_absent"
        except OSError:
            return "failed"
        if not stat.S_ISREG(info.st_mode) or expected != _posix_key(info):
            return "blocked_identity"
        return "ok" if info.st_size == member.size_bytes else "blocked_changed"
    api = _api()
    try:
        handle = api.open_exact(path, _GENERIC_READ)
    except IntakeFileError as exc:
        return _open_status(exc)
    except OSError:
        return "failed"
    try:
        key, size = api.facts(handle)
        if key != expected:
            return "blocked_identity"
        if size != member.size_bytes or not _single_stream(api, path):
            return "blocked_changed"
        return "ok"
    except OSError:
        return "failed"
    finally:
        try:
            api.close(handle)
        except OSError:
            pass


def _single_stream(api: WindowsFiles, path: Path) -> bool:
    """False when an alternate data stream exists: its bytes were never archived."""
    try:
        api.streams(path)
    except IntakeFileError as exc:
        if exc.code == "alternate_stream":
            return False
        raise OSError(exc.code) from exc
    return True


def delete_exact(
    path: Path, member: m.CleanupMember, cancel: threading.Event | None = None
) -> DeleteStatus | Interrupted:
    """C6: verify identity, size, single stream and SHA-256 on one handle, then delete it.

    "interrupted" (shutdown during hashing) deletes nothing and is not a result: retry later.
    """
    if sys.platform != "win32":
        return _posix_delete(path, member, cancel)
    api = _api()
    try:
        handle = api.open_exact(path, _DELETE | _GENERIC_READ)
    except IntakeFileError as exc:
        return _open_status(exc)
    except OSError:
        return "failed"
    disposed = False
    try:
        key, size = api.facts(handle)
        if key != _key(member.native_identity):
            return "blocked_identity"
        if (
            size != member.size_bytes
            or not _single_stream(api, path)
            or not _hash_matches(
                lambda: api.read(handle, _CHUNK),
                member.size_bytes,
                member.sha256,
                cancel,
            )
        ):
            return "blocked_changed"
        api.dispose(handle)
        disposed = True
    except _Interrupted:
        return "interrupted"
    except OSError:
        return "failed"
    finally:
        try:
            api.close(handle)
        except OSError:
            # A disposed handle that fails to close is not a confirmed delete.
            disposed = False
    return "deleted" if disposed else "failed"


def _posix_key(info: os.stat_result) -> tuple[str, str, str]:
    return "posix_inode", str(info.st_dev), str(info.st_ino)


def _posix_delete(
    path: Path, member: m.CleanupMember, cancel: threading.Event | None = None
) -> DeleteStatus | Interrupted:
    """Development/test fallback; POSIX has no read-only share lock."""
    expected = _key(member.native_identity)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "already_absent"
    except OSError:
        return "failed"
    if not stat.S_ISREG(info.st_mode) or _posix_key(info) != expected:
        return "blocked_identity"
    if info.st_size != member.size_bytes:
        return "blocked_changed"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "already_absent"
    except OSError:
        return "failed"
    try:
        if _posix_key(os.fstat(fd)) != expected:
            return "blocked_identity"
        if not _hash_matches(
            lambda: os.read(fd, _CHUNK), member.size_bytes, member.sha256, cancel
        ):
            return "blocked_changed"
        if _posix_key(os.lstat(path)) != expected:
            return "blocked_identity"
        os.unlink(path)
    except _Interrupted:
        return "interrupted"
    except FileNotFoundError:
        return "already_absent"
    except OSError:
        return "failed"
    finally:
        os.close(fd)
    return "deleted"
