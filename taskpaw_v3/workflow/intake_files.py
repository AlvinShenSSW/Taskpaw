"""Windows-only held source objects for the fixed intake helper.

All potentially blocking native I/O belongs inside the supervised helper process.
This module does not open a database, launch a process, or establish readiness.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import sys
from ctypes import wintypes as wt
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

from .intake_models import IntakePolicy
from .models import NativeIdentity


class IntakeFileError(OSError):
    """`path`: the package-relative member the failure is about, when one is."""

    def __init__(self, code: str, path: str | None = None):
        self.code = code
        self.path = path
        super().__init__(code)


# Windows writes these on its own and they carry no film data: Explorer marks
# Thumbs.db `:encryptable` (0 bytes) and a downloaded file gets the Mark of the
# Web `:Zone.Identifier`. Any other alternate stream is still refused.
_METADATA_STREAMS = frozenset(
    {"::$data", ":encryptable:$data", ":zone.identifier:$data"}
)


class _Basic(ctypes.Structure):
    _fields_ = [
        ("created", ctypes.c_longlong),
        ("accessed", ctypes.c_longlong),
        ("written", ctypes.c_longlong),
        ("changed", ctypes.c_longlong),
        ("attributes", wt.DWORD),
    ]


class _Standard(ctypes.Structure):
    _fields_ = [
        ("allocated", ctypes.c_longlong),
        ("size", ctypes.c_longlong),
        ("links", wt.DWORD),
        ("delete_pending", ctypes.c_ubyte),
        ("directory", ctypes.c_ubyte),
    ]


class _FileId(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_ulonglong), ("object", ctypes.c_ubyte * 16)]


class _Stream(ctypes.Structure):
    _fields_ = [("size", ctypes.c_longlong), ("name", wt.WCHAR * 296)]


def _parts(value: str) -> tuple[str, ...]:
    parts = tuple(value.replace("\\", "/").split("/"))
    if not parts or any(
        not part
        or part in (".", "..")
        or part[-1:] in (".", " ")
        or any(c in part for c in ':<>"|?*\x00')
        or any(ord(c) < 32 for c in part)
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
        for part in parts
    ):
        raise IntakeFileError("invalid_path")
    return parts


def _local(value: Path) -> Path:
    raw = str(value)
    path = PureWindowsPath(raw)
    if not re.fullmatch(r"[a-zA-Z]:", path.drive) or not path.root:
        raise IntakeFileError("invalid_path")
    _parts(raw[3:]) if len(raw) > 3 else None
    return Path(path)


def _key(value: NativeIdentity) -> tuple[str, str, str]:
    return value.scheme, value.volume_or_export_id, value.object_id


def _last_error() -> int:
    """``ctypes.get_last_error`` without a Windows-only name for other platforms."""
    return int(getattr(ctypes, "get_last_error")())


@dataclass(frozen=True)
class HeldEntry:
    relative_path: str
    identity: NativeIdentity
    directory: bool
    size_bytes: int
    mtime_ns: int
    change_filetime: int
    link_count: int
    temporary: bool
    handle: int

    @property
    def signature(self) -> tuple:
        return (
            self.relative_path,
            _key(self.identity),
            self.directory,
            self.size_bytes,
            self.mtime_ns,
            self.change_filetime,
            self.link_count,
            self.temporary,
        )


class WindowsFiles:
    """Narrow checked Win32 calls; handles never appear in persisted evidence."""

    kernel: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise IntakeFileError("platform_unsupported")
        self.kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        definitions = {
            "CreateFileW": (
                wt.HANDLE,
                [
                    wt.LPCWSTR,
                    wt.DWORD,
                    wt.DWORD,
                    wt.LPVOID,
                    wt.DWORD,
                    wt.DWORD,
                    wt.HANDLE,
                ],
            ),
            "CloseHandle": (wt.BOOL, [wt.HANDLE]),
            "GetFileType": (wt.DWORD, [wt.HANDLE]),
            "GetFileInformationByHandleEx": (
                wt.BOOL,
                [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD],
            ),
            "ReadFile": (
                wt.BOOL,
                [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID],
            ),
            "WriteFile": (
                wt.BOOL,
                [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID],
            ),
            "FlushFileBuffers": (wt.BOOL, [wt.HANDLE]),
            "SetFilePointerEx": (
                wt.BOOL,
                [
                    wt.HANDLE,
                    ctypes.c_longlong,
                    ctypes.POINTER(ctypes.c_longlong),
                    wt.DWORD,
                ],
            ),
            "FindFirstStreamW": (
                wt.HANDLE,
                [wt.LPCWSTR, ctypes.c_int, wt.LPVOID, wt.DWORD],
            ),
            "FindNextStreamW": (wt.BOOL, [wt.HANDLE, wt.LPVOID]),
            "FindClose": (wt.BOOL, [wt.HANDLE]),
            "GetDriveTypeW": (wt.UINT, [wt.LPCWSTR]),
            "GetVolumeInformationByHandleW": (
                wt.BOOL,
                [
                    wt.HANDLE,
                    wt.LPWSTR,
                    wt.DWORD,
                    ctypes.POINTER(wt.DWORD),
                    ctypes.POINTER(wt.DWORD),
                    ctypes.POINTER(wt.DWORD),
                    wt.LPWSTR,
                    wt.DWORD,
                ],
            ),
        }
        for name, (result, arguments) in definitions.items():
            function = getattr(self.kernel, name)
            function.restype, function.argtypes = result, arguments

    @staticmethod
    def check(success: object) -> None:
        if not success:
            error = _last_error()
            raise IntakeFileError(
                "writer_busy"
                if error in (32, 33)
                else "source_changed"
                if error in (2, 3)
                else "permission_denied"
                if error in (5, 1314)
                else "native_io_failed"
            )

    def open(self, path: Path, *, directory: bool) -> int:
        handle = self.kernel.CreateFileW(
            str(path),
            # Metadata-only directory access does not participate in Windows
            # delete sharing. LIST_DIRECTORY pins even an empty directory.
            0x81 if directory else 0x80000000,
            3 if directory else 1,
            None,
            3,
            0x00200000 | 0x02000000,
            None,
        )
        self.check(handle != ctypes.c_void_p(-1).value)
        return int(handle)

    def close(self, handle: int) -> None:
        if not self.kernel.CloseHandle(handle):
            raise IntakeFileError("close_failed")

    def create(self, path: Path) -> int:
        handle = self.kernel.CreateFileW(
            str(path), 0xC0000000, 1, None, 1, 0x00200000, None
        )
        self.check(handle != ctypes.c_void_p(-1).value)
        return int(handle)

    def write(self, handle: int, value: bytes) -> None:
        remaining = value
        while remaining:
            count = wt.DWORD()
            self.check(
                self.kernel.WriteFile(
                    handle, remaining, len(remaining), ctypes.byref(count), None
                )
            )
            if not 0 < count.value <= len(remaining):
                raise IntakeFileError("copy_failed")
            remaining = remaining[count.value :]

    def flush(self, handle: int) -> None:
        self.check(self.kernel.FlushFileBuffers(handle))

    def entry(self, handle: int, path: Path, relative: str, root_id: str) -> HeldEntry:
        basic, standard, file_id = _Basic(), _Standard(), _FileId()
        for kind, data in ((0, basic), (1, standard), (18, file_id)):
            self.check(
                self.kernel.GetFileInformationByHandleEx(
                    handle, kind, ctypes.byref(data), ctypes.sizeof(data)
                )
            )
        if basic.attributes & 0x400:
            raise IntakeFileError("reparse_point")
        if self.kernel.GetFileType(handle) != 1 or standard.delete_pending:
            raise IntakeFileError("unsupported_object")
        if standard.size < 0 or standard.links < 1:
            raise IntakeFileError("unknown_identity")
        directory = bool(standard.directory)
        if not directory:
            self.streams(path)
        return HeldEntry(
            relative,
            NativeIdentity(
                scheme="windows_file_id",
                root_id=root_id,
                volume_or_export_id=f"{file_id.volume:016x}",
                object_id=bytes(file_id.object).hex(),
                observed_at=datetime.now(timezone.utc),
                evidence_id=str(uuid4()),
            ),
            directory,
            0 if directory else standard.size,
            (basic.written - 116444736000000000) * 100,
            basic.changed,
            standard.links,
            bool(basic.attributes & 0x100)
            or path.suffix.casefold()
            in (".part", ".partial", ".tmp", ".download", ".crdownload"),
            handle,
        )

    def streams(self, path: Path) -> None:
        value = _Stream()
        handle = self.kernel.FindFirstStreamW(str(path), 0, ctypes.byref(value), 0)
        if handle == ctypes.c_void_p(-1).value:
            # A zero-length regular file can have no data stream enumeration.
            if _last_error() == 38:
                return
            self.check(False)
        try:
            while True:
                if value.name.casefold() not in _METADATA_STREAMS:
                    raise IntakeFileError("alternate_stream")
                if not self.kernel.FindNextStreamW(handle, ctypes.byref(value)):
                    if _last_error() != 38:
                        self.check(False)
                    break
        finally:
            if not self.kernel.FindClose(handle):
                raise IntakeFileError("close_failed")

    def local_filesystem(self, path: Path, handle: int) -> None:
        if self.kernel.GetDriveTypeW(path.anchor) != 3:
            raise IntakeFileError("unsupported_filesystem")
        name = ctypes.create_unicode_buffer(32)
        self.check(
            self.kernel.GetVolumeInformationByHandleW(
                handle, None, 0, None, None, None, name, len(name)
            )
        )
        if name.value != "NTFS":
            raise IntakeFileError("unsupported_filesystem")

    def rewind(self, handle: int) -> None:
        self.check(self.kernel.SetFilePointerEx(handle, 0, None, 0))

    def read(self, handle: int, maximum: int = 1024 * 1024) -> bytes:
        data, count = ctypes.create_string_buffer(maximum), wt.DWORD()
        self.check(
            self.kernel.ReadFile(handle, data, maximum, ctypes.byref(count), None)
        )
        return data.raw[: count.value]


class _Holds:
    def __init__(self, api: WindowsFiles, root_id: str):
        self.api, self.root_id = api, root_id
        self.handles: list[int] = []
        self.closed = False

    def acquire(self, path: Path, relative: str, *, directory: bool) -> HeldEntry:
        handle = self.api.open(path, directory=directory)
        self.handles.append(handle)
        value = self.api.entry(handle, path, relative, self.root_id)
        if value.directory != directory:
            raise IntakeFileError("source_changed")
        return value

    def chain(self, path: Path) -> list[HeldEntry]:
        result, current = [], Path(path.anchor)
        result.append(self.acquire(current, "", directory=True))
        for part in path.parts[1:]:
            current /= part
            result.append(self.acquire(current, "", directory=True))
        return result

    def close(self) -> None:
        failed = False
        for handle in reversed(self.handles[:]):
            try:
                self.api.close(handle)
                self.handles.remove(handle)
            except OSError:
                failed = True
        self.closed = not self.handles
        if failed:
            raise IntakeFileError("close_failed")


def inspect_root(path: Path, root_id: str) -> NativeIdentity:
    path = _local(path)
    holds = _Holds(WindowsFiles(), root_id)
    try:
        root = holds.chain(path)[-1]
        holds.api.local_filesystem(path, root.handle)
        return root.identity
    finally:
        holds.close()


class PinnedFile:
    """Verify the installed fixed probe bytes and retain no-write/delete access."""

    def __init__(self, path: Path, *, sha256: str, size_bytes: int):
        self.path, self.sha256, self.size_bytes = _local(path), sha256, size_bytes
        self._holds = _Holds(WindowsFiles(), str(uuid4()))
        self._entered = False

    def __enter__(self) -> PinnedFile:
        if self._entered or self._holds.closed:
            raise IntakeFileError("artifact_closed")
        try:
            parent = self._holds.chain(self.path.parent)[-1]
            self._holds.api.local_filesystem(self.path, parent.handle)
            entry = self._holds.acquire(self.path, self.path.name, directory=False)
            if entry.size_bytes != self.size_bytes:
                raise IntakeFileError("artifact_mismatch")
            digest, count = hashlib.sha256(), 0
            while chunk := self._holds.api.read(entry.handle):
                count += len(chunk)
                if count > self.size_bytes:
                    raise IntakeFileError("artifact_mismatch")
                digest.update(chunk)
            if count != self.size_bytes or digest.hexdigest() != self.sha256:
                raise IntakeFileError("artifact_mismatch")
            self._entered = True
            return self
        except BaseException:
            self._holds.close()
            raise

    def __exit__(self, *_: object) -> None:
        self._holds.close()


class HeldPackage:
    def __init__(
        self,
        root: Path,
        relative_path: str,
        *,
        root_id: str,
        expected_root: NativeIdentity,
        policy: IntakePolicy,
        expected_package: NativeIdentity | None = None,
        api: WindowsFiles | None = None,
    ):
        self.root = _local(root)
        self.parts = _parts(relative_path)
        self.path = self.root.joinpath(*self.parts)
        self.root_id, self.expected_root = root_id, expected_root
        self.expected_package, self.policy = expected_package, policy
        self.api = api if api is not None else WindowsFiles()
        self._holds = _Holds(self.api, root_id)
        self.entries: dict[str, HeldEntry] = {}
        self._linked_metadata: dict[tuple[str, str, str], tuple[int, int]] = {}
        self.package_identity: NativeIdentity | None = None
        self._entered = False

    @property
    def closed(self) -> bool:
        return self._holds.closed

    def __enter__(self) -> HeldPackage:
        if self._entered or self.closed:
            raise IntakeFileError("source_closed")
        try:
            root = self._holds.chain(self.root)[-1]
            self.api.local_filesystem(self.root, root.handle)
            if self.expected_root.root_id != self.root_id or _key(
                root.identity
            ) != _key(self.expected_root):
                raise IntakeFileError("identity_changed")
            path = self.root
            for part in self.parts:
                path /= part
                package = self._holds.acquire(path, "", directory=True)
            self.package_identity = package.identity
            if self.expected_package is not None and _key(package.identity) != _key(
                self.expected_package
            ):
                raise IntakeFileError("identity_changed")
            self._scan(self.path, (), retain=True)
            self._entered = True
            return self
        except BaseException:
            self.close()
            raise

    def _scan(self, path: Path, parents: tuple[str, ...], *, retain: bool) -> set[str]:
        found: set[str] = set()
        with os.scandir(path) as iterator:
            children = []
            for child in iterator:
                children.append(child)
                if (
                    len(children) + (len(self.entries) if retain else 0)
                    > self.policy.max_members
                ):
                    raise IntakeFileError(
                        "limit_exceeded" if retain else "source_changed"
                    )
            children.sort(key=lambda e: e.name.casefold())
        names: set[str] = set()
        for child in children:
            _parts(child.name)
            parts = (*parents, child.name)
            relative = "/".join(parts)
            if child.name.casefold() in names:
                raise IntakeFileError("case_conflict", relative)
            names.add(child.name.casefold())
            if (
                len(parts) > self.policy.max_depth
                or len(self.entries) >= self.policy.max_members
                and retain
            ):
                raise IntakeFileError("limit_exceeded")
            if not retain and relative not in self.entries:
                raise IntakeFileError("source_changed")
            directory = child.is_dir(follow_symlinks=False)
            try:
                entry = (
                    self._holds.acquire(Path(child.path), relative, directory=directory)
                    if retain
                    else self.entries[relative]
                )
            except IntakeFileError as exc:
                exc.path = exc.path or relative
                raise
            if retain:
                self.entries[relative] = entry
            found.add(relative)
            if entry.directory:
                found.update(self._scan(Path(child.path), parts, retain=retain))
        return found

    def recheck(self) -> None:
        self._require_open()
        if self._scan(self.path, (), retain=False) != set(self.entries):
            raise IntakeFileError("source_changed")
        for relative, entry in self.entries.items():
            current = self.api.entry(
                entry.handle,
                self.path.joinpath(*relative.split("/")),
                relative,
                self.root_id,
            )
            if current.signature != self._expected_entry(entry).signature:
                raise IntakeFileError("source_changed")

    def _expected_entry(self, entry: HeldEntry) -> HeldEntry:
        metadata = self._linked_metadata.get(_key(entry.identity))
        return (
            replace(entry, change_filetime=metadata[0], link_count=metadata[1])
            if metadata
            else entry
        )

    def record_link(self, relative: str) -> None:
        """Account only for our new name; keep admitted source observations intact."""
        self._require_open()
        entry = self.entries[relative]
        expected = self._expected_entry(entry)
        current = self.api.entry(
            entry.handle,
            self.path.joinpath(*relative.split("/")),
            relative,
            self.root_id,
        )
        if (
            current.signature
            != replace(
                expected,
                change_filetime=current.change_filetime,
                link_count=expected.link_count + 1,
            ).signature
        ):
            raise IntakeFileError("source_changed")
        # Other admitted names may refer to this same physical file.
        self._linked_metadata[_key(entry.identity)] = (
            current.change_filetime,
            current.link_count,
        )

    def linked_change_token(self, relative: str) -> str:
        return str(self._linked_metadata[_key(self.entries[relative].identity)][0])

    def _require_open(self) -> None:
        if not self._entered or self.closed:
            raise IntakeFileError("source_closed")

    def chunks(self, relative: str):
        self._require_open()
        entry = self.entries[relative]
        if entry.directory:
            raise IntakeFileError("not_a_file")
        self.api.rewind(entry.handle)
        total = 0
        while chunk := self.api.read(entry.handle):
            total += len(chunk)
            if total > entry.size_bytes:
                raise IntakeFileError("source_changed")
            yield chunk
        if total != entry.size_bytes:
            raise IntakeFileError("source_changed")

    def sha256(self, relative: str) -> str:
        value = hashlib.sha256()
        for chunk in self.chunks(relative):
            value.update(chunk)
        return value.hexdigest()

    def close(self) -> None:
        self._holds.close()

    def __exit__(self, *_: object) -> None:
        self.close()
