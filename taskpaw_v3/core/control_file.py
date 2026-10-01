"""Per-runtime local credential files, inspected on the same fd/HANDLE read.

Directories are anchored and never opened through symlinks/reparse points. The
Windows implementation uses owner-only DACLs at creation, not a later chmod.
"""

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

_LIMIT = 16 * 1024
_BOOT = re.compile(r"[0-9a-f]{32}\Z")
_FIELDS = {"version", "role", "base_url", "boot_id", "control_token"}

if sys.platform == "darwin":
    import ctypes

    _darwin = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

    def _acl_signature(name, arguments, result):
        function = getattr(_darwin, name)
        function.argtypes, function.restype = arguments, result
        return function

    _PTR = ctypes.c_void_p
    _acl_fd = _acl_signature("acl_get_fd_np", [ctypes.c_int, ctypes.c_int], _PTR)
    _acl_valid = _acl_signature("acl_valid", [_PTR], ctypes.c_int)
    _acl_entry = _acl_signature(
        "acl_get_entry", [_PTR, ctypes.c_int, ctypes.POINTER(_PTR)], ctypes.c_int
    )
    _acl_tag = _acl_signature(
        "acl_get_tag_type", [_PTR, ctypes.POINTER(ctypes.c_int)], ctypes.c_int
    )
    _acl_mask = _acl_signature(
        "acl_get_permset_mask_np", [_PTR, ctypes.POINTER(ctypes.c_uint64)], ctypes.c_int
    )
    _acl_flagset = _acl_signature(
        "acl_get_flagset_np", [_PTR, ctypes.POINTER(_PTR)], ctypes.c_int
    )
    _acl_flag = _acl_signature("acl_get_flag_np", [_PTR, ctypes.c_int], ctypes.c_int)
    _acl_free = _acl_signature("acl_free", [_PTR], ctypes.c_int)

    def _check_darwin_acl(fd: int, *, directory: bool, final: bool = False) -> None:
        """Darwin ACLs can grant access independently of 0700/0600 mode bits.

        Use only native public APIs on this already-open object. A conservative
        allow-rights policy avoids simulating principal membership or deny ordering.
        Ordinary deny entries (including HOME's deny-delete) remain compatible.
        """
        ctypes.set_errno(0)
        acl = _acl_fd(fd, 0x100)  # ACL_TYPE_EXTENDED
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:  # explicitly no extended ACL
                return
            raise ControlCredentialError("unsafe_control_acl")
        try:
            if _acl_valid(acl) != 0:
                raise ControlCredentialError("unsafe_control_acl")
            for index in range(129):  # native ACL_MAX_ENTRIES=128
                entry = _PTR()
                ctypes.set_errno(0)
                result = _acl_entry(acl, index, ctypes.byref(entry))
                if result == -1 and ctypes.get_errno() == errno.EINVAL:
                    return  # Darwin iteration ends with -1/EINVAL, not Linux's 0
                if result != 0 or not entry or index == 128:
                    raise ControlCredentialError("unsafe_control_acl")
                tag, mask, flagset = ctypes.c_int(), ctypes.c_uint64(), _PTR()
                if (
                    _acl_tag(entry, ctypes.byref(tag)) != 0
                    or _acl_mask(entry, ctypes.byref(mask)) != 0
                    or _acl_flagset(entry, ctypes.byref(flagset)) != 0
                    or not flagset
                ):
                    raise ControlCredentialError("unsafe_control_acl")
                flags = 0
                for bit in range(32):
                    value = _acl_flag(flagset, ctypes.c_int(1 << bit))
                    if value not in {0, 1}:
                        raise ControlCredentialError("unsafe_control_acl")
                    if value:
                        flags |= 1 << bit
                if tag.value not in {1, 2} or mask.value & ~0x103FFE or flags & ~0x1F0:
                    raise ControlCredentialError("unsafe_control_acl")
                if tag.value == 2:  # ACL_EXTENDED_DENY grants no access
                    continue
                # Reject unsafe inheritance even when the ACE is ONLY_INHERIT.
                if directory and flags & 0x60 and mask.value & 0x353E:
                    raise ControlCredentialError("unsafe_control_acl")
                unsafe = (0x3574 if final else 0x3550) if directory else 0x353E
                if not flags & 0x100 and mask.value & unsafe:
                    raise ControlCredentialError("unsafe_control_acl")
        finally:
            _acl_free(acl)

else:

    def _check_darwin_acl(fd: int, *, directory: bool, final: bool = False) -> None:
        """Other platforms retain their native mode or HANDLE/DACL policy."""
        return


class ControlCredentialError(ValueError):
    """Only a fixed code: no credential contents or unsafe exception text."""


def validate_control_token(token: str) -> None:
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 1024
        or any(not 0x21 <= ord(ch) <= 0x7E for ch in token)
    ):
        raise ControlCredentialError("invalid_control_token")


def validate_control_base(base: str) -> None:
    try:
        parsed = urlsplit(base)
        host, port = parsed.hostname, parsed.port
        bracketed = "[::1]" if host == "::1" else host
        valid = (
            parsed.scheme in {"http", "https"}
            and host in {"127.0.0.1", "::1"}
            and port is not None
            and 1 <= port <= 65535
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and base == f"{parsed.scheme}://{bracketed}:{port}"
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ControlCredentialError("invalid_control_base")


@dataclass(frozen=True)
class ControlDescriptor:
    version: int
    role: str
    base_url: str
    boot_id: str
    control_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != 1
            or self.role not in {"agent", "hub"}
            or not isinstance(self.boot_id, str)
            or not _BOOT.fullmatch(self.boot_id)
        ):
            raise ControlCredentialError("invalid_control_descriptor")
        validate_control_base(self.base_url)
        validate_control_token(self.control_token)

    def _bytes(self) -> bytes:
        return json.dumps(
            {
                "version": self.version,
                "role": self.role,
                "base_url": self.base_url,
                "boot_id": self.boot_id,
                "control_token": self.control_token,
            },
            separators=(",", ":"),
        ).encode("ascii")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ControlCredentialError("invalid_control_descriptor")
        data[key] = value
    return data


def _decode(data: bytes) -> ControlDescriptor:
    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=_pairs)
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError
        return ControlDescriptor(**value)
    except (ValueError, TypeError, UnicodeError):
        raise ControlCredentialError("invalid_control_descriptor") from None


def _path(path: Path) -> Path:
    raw = Path(path)
    if ".." in raw.parts:
        raise ControlCredentialError("unsafe_control_path")
    return Path(os.path.abspath(raw))


class _PosixDirectory:
    def __init__(self, path: Path) -> None:
        self.path = _path(path)
        self._fds: list[int] = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            self._fds.append(os.open(self.path.anchor, flags))
            self._check(self._fds[-1], final=not self.path.parts[1:])
            for index, part in enumerate(self.path.parts[1:]):
                fd = os.open(part, flags, dir_fd=self._fds[-1])
                self._fds.append(fd)
                self._check(fd, final=index == len(self.path.parts[1:]) - 1)
        except (OSError, ControlCredentialError):
            self.close()
            raise ControlCredentialError("unsafe_control_directory") from None

    @property
    def fd(self) -> int:
        return self._fds[-1]

    @staticmethod
    def _check(fd: int, *, final: bool) -> None:
        info = os.fstat(fd)
        uid = os.getuid()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {uid, 0}:
            raise ControlCredentialError("unsafe_control_directory")
        writable = bool(info.st_mode & 0o022)
        sticky_ancestor = not final and bool(info.st_mode & stat.S_ISVTX)
        if (final and info.st_uid != uid) or (writable and not sticky_ancestor):
            raise ControlCredentialError("unsafe_control_directory")
        _check_darwin_acl(fd, directory=True, final=final)

    def _open(self, name: str) -> int:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > _LIMIT
            ):
                raise ControlCredentialError("unsafe_control_file")
            _check_darwin_acl(fd, directory=False)
        except Exception:
            os.close(fd)
            raise
        return fd

    def read(self, name: str) -> ControlDescriptor:
        fd = self._open(name)
        try:
            chunks, size = [], 0
            while size <= _LIMIT:
                chunk = os.read(fd, min(4096, _LIMIT + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > _LIMIT:
                raise ControlCredentialError("invalid_control_descriptor")
            return _decode(b"".join(chunks))
        finally:
            os.close(fd)

    def publish(self, name: str, descriptor: ControlDescriptor) -> None:
        try:
            self.read(name)
        except FileNotFoundError:
            pass
        tmp = f".{name}.{secrets.token_hex(12)}.tmp"
        fd: Optional[int] = None
        published = False
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.fd,
            )
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise ControlCredentialError("unsafe_control_file")
            _check_darwin_acl(fd, directory=False)
            data = descriptor._bytes()
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise ControlCredentialError("control_publish_failed")
                offset += written
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            published = True
            os.fsync(self.fd)
        except Exception:
            if published:
                self.revoke(name, descriptor.boot_id, descriptor.role)
            raise
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(tmp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def revoke(self, name: str, boot: str, role: str) -> None:
        try:
            current = self.read(name)
        except FileNotFoundError:
            return
        if current.boot_id == boot and current.role == role:
            os.unlink(name, dir_fd=self.fd)
            os.fsync(self.fd)

    def close(self) -> None:
        while self._fds:
            os.close(self._fds.pop())


# Windows functions are loaded only on Windows; no optional runtime dependency.
if os.name == "nt":  # pragma: no cover - executed by the Windows CI job
    import ctypes
    from ctypes import wintypes as wt

    _kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    _advapi = getattr(ctypes, "WinDLL")("advapi32", use_last_error=True)
    _HANDLE = wt.HANDLE
    _PVOID = ctypes.c_void_p
    _INVALID = ctypes.c_void_p(-1).value
    _READ_CONTROL = 0x20000
    _DELETE = 0x10000
    _DIR_UNSAFE = 0x10 | 0x40 | 0x100 | _DELETE | 0x40000 | 0x80000 | 0x50000000
    _PARENT_UNSAFE = _DIR_UNSAFE | 0x2 | 0x4

    class _SA(ctypes.Structure):
        _fields_ = [
            ("nLength", wt.DWORD),
            ("lpSecurityDescriptor", _PVOID),
            ("bInheritHandle", wt.BOOL),
        ]

    class _Tag(ctypes.Structure):
        _fields_ = [("FileAttributes", wt.DWORD), ("ReparseTag", wt.DWORD)]

    class _Standard(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wt.DWORD),
            ("DeletePending", wt.BYTE),
            ("Directory", wt.BYTE),
        ]

    class _ACL(ctypes.Structure):
        _fields_ = [
            ("revision", wt.BYTE),
            ("padding", wt.BYTE),
            ("size", wt.WORD),
            ("count", wt.WORD),
            ("padding2", wt.WORD),
        ]

    class _ACE(ctypes.Structure):
        _fields_ = [
            ("kind", wt.BYTE),
            ("flags", wt.BYTE),
            ("size", wt.WORD),
            ("mask", wt.DWORD),
        ]

    class _Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", wt.BYTE)]

    def _signature(lib, name: str, arguments, result):
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = arguments, result
        return fn

    _create = _signature(
        _kernel,
        "CreateFileW",
        [
            wt.LPCWSTR,
            wt.DWORD,
            wt.DWORD,
            ctypes.POINTER(_SA),
            wt.DWORD,
            wt.DWORD,
            _HANDLE,
        ],
        _HANDLE,
    )
    _close = _signature(_kernel, "CloseHandle", [_HANDLE], wt.BOOL)
    _current = _signature(_kernel, "GetCurrentProcess", [], _HANDLE)
    _local_free = _signature(_kernel, "LocalFree", [_PVOID], _PVOID)
    _open_token = _signature(
        _advapi,
        "OpenProcessToken",
        [_HANDLE, wt.DWORD, ctypes.POINTER(_HANDLE)],
        wt.BOOL,
    )
    _token_info = _signature(
        _advapi,
        "GetTokenInformation",
        [_HANDLE, wt.DWORD, _PVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)],
        wt.BOOL,
    )
    _sid_length = _signature(_advapi, "GetLengthSid", [_PVOID], wt.DWORD)
    _sid_valid = _signature(_advapi, "IsValidSid", [_PVOID], wt.BOOL)
    _sid_string = _signature(
        _advapi, "ConvertSidToStringSidW", [_PVOID, ctypes.POINTER(wt.LPWSTR)], wt.BOOL
    )
    _string_sid = _signature(
        _advapi, "ConvertStringSidToSidW", [wt.LPCWSTR, ctypes.POINTER(_PVOID)], wt.BOOL
    )
    _get_security = _signature(
        _advapi,
        "GetSecurityInfo",
        [
            _HANDLE,
            wt.DWORD,
            wt.DWORD,
            ctypes.POINTER(_PVOID),
            ctypes.POINTER(_PVOID),
            ctypes.POINTER(_PVOID),
            ctypes.POINTER(_PVOID),
            ctypes.POINTER(_PVOID),
        ],
        wt.DWORD,
    )
    _get_control = _signature(
        _advapi,
        "GetSecurityDescriptorControl",
        [_PVOID, ctypes.POINTER(wt.WORD), ctypes.POINTER(wt.DWORD)],
        wt.BOOL,
    )
    _get_ace = _signature(
        _advapi, "GetAce", [_PVOID, wt.DWORD, ctypes.POINTER(_PVOID)], wt.BOOL
    )
    _sddl = _signature(
        _advapi,
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        [wt.LPCWSTR, wt.DWORD, ctypes.POINTER(_PVOID), ctypes.POINTER(wt.DWORD)],
        wt.BOOL,
    )
    _get_tag = _signature(
        _kernel,
        "GetFileInformationByHandleEx",
        [_HANDLE, ctypes.c_int, _PVOID, wt.DWORD],
        wt.BOOL,
    )
    _file_type = _signature(_kernel, "GetFileType", [_HANDLE], wt.DWORD)
    _read = _signature(
        _kernel,
        "ReadFile",
        [_HANDLE, _PVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), _PVOID],
        wt.BOOL,
    )
    _write = _signature(
        _kernel,
        "WriteFile",
        [_HANDLE, _PVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), _PVOID],
        wt.BOOL,
    )
    _flush = _signature(_kernel, "FlushFileBuffers", [_HANDLE], wt.BOOL)
    _move = _signature(
        _kernel, "MoveFileExW", [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD], wt.BOOL
    )
    _set_info = _signature(
        _kernel,
        "SetFileInformationByHandle",
        [_HANDLE, ctypes.c_int, _PVOID, wt.DWORD],
        wt.BOOL,
    )

    def _win_ok(ok, code: str = "unsafe_control_file") -> None:
        if not ok:
            raise ControlCredentialError(code)

    def _sid_bytes(sid) -> bytes:
        _win_ok(_sid_valid(sid))
        return ctypes.string_at(sid, _sid_length(sid))

    def _named_sid(name: str) -> bytes:
        sid = _PVOID()
        _win_ok(_string_sid(name, ctypes.byref(sid)))
        try:
            return _sid_bytes(sid)
        finally:
            _local_free(sid)

    def _user_sid() -> tuple[bytes, str]:
        token = _HANDLE()
        _win_ok(_open_token(_current(), 8, ctypes.byref(token)))
        try:
            size = wt.DWORD()
            _token_info(token, 1, None, 0, ctypes.byref(size))
            _win_ok(size.value > 0)
            data = ctypes.create_string_buffer(size.value)
            _win_ok(_token_info(token, 1, data, size, ctypes.byref(size)))
            sid = ctypes.cast(data, ctypes.POINTER(_PVOID)).contents
            text = wt.LPWSTR()
            _win_ok(_sid_string(sid, ctypes.byref(text)))
            try:
                return _sid_bytes(sid), str(text.value)
            finally:
                _local_free(ctypes.cast(text, _PVOID))
        finally:
            _close(token)

    def _security(handle, user: bytes, *, directory: bool, final: bool = False) -> None:
        owner, dacl, sd = _PVOID(), _PVOID(), _PVOID()
        result = _get_security(
            handle,
            1,
            1 | 4,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(sd),
        )
        _win_ok(result == 0)
        try:
            system, admins = _named_sid("S-1-5-18"), _named_sid("S-1-5-32-544")
            trusted = {user, system, admins} if directory else {user, system}
            if _sid_bytes(owner) not in (trusted if directory else {user}):
                raise ControlCredentialError("unsafe_control_owner")
            control, revision = wt.WORD(), wt.DWORD()
            _win_ok(_get_control(sd, ctypes.byref(control), ctypes.byref(revision)))
            if not dacl or (not directory and not control.value & 0x1000):
                raise ControlCredentialError("unsafe_control_acl")
            acl = ctypes.cast(dacl, ctypes.POINTER(_ACL)).contents
            for index in range(acl.count):
                address = _PVOID()
                _win_ok(_get_ace(dacl, index, ctypes.byref(address)))
                ace = ctypes.cast(address, ctypes.POINTER(_ACE)).contents
                if ace.kind not in {0, 1} or ace.size < 16:
                    raise ControlCredentialError("unsafe_control_acl")
                if directory and ace.flags & 0x08:  # inherit-only doesn't apply here
                    continue
                if address.value is None:
                    raise ControlCredentialError("unsafe_control_acl")
                principal = _sid_bytes(ctypes.c_void_p(address.value + 8))
                if not directory:
                    if ace.kind != 0 or ace.flags & 0x10 or principal not in trusted:
                        raise ControlCredentialError("unsafe_control_acl")
                elif ace.kind == 0 and principal not in trusted:
                    mask = _PARENT_UNSAFE if final else _DIR_UNSAFE
                    if ace.mask & mask:
                        raise ControlCredentialError("unsafe_control_directory")
        finally:
            if sd:
                _local_free(sd)

    def _win_open(
        path: Path, *, directory: bool = False, delete: bool = False, create_sd=None
    ):
        access = _READ_CONTROL | (0x80 if directory else 0x80000000)
        if delete:
            access |= _DELETE
        creating = create_sd is not None
        if creating:
            access |= 0x40000000 | _DELETE
        sa = _SA(ctypes.sizeof(_SA), create_sd, False) if creating else None
        handle = _create(
            str(path),
            access,
            3 if directory else 7,
            ctypes.byref(sa) if sa else None,
            1 if creating else 3,
            0x00200000 | (0x02000000 if directory else 0x80),
            None,
        )
        if handle == _INVALID:
            if getattr(ctypes, "get_last_error")() in {2, 3} and not creating:
                raise FileNotFoundError
            raise ControlCredentialError("unsafe_control_file")
        try:
            tag = _Tag()
            _win_ok(_get_tag(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)))
            if (
                tag.FileAttributes & 0x400
                or bool(tag.FileAttributes & 0x10) != directory
                or _file_type(handle) != 1
            ):
                raise ControlCredentialError("unsafe_control_file")
            if not directory:
                info = _Standard()
                _win_ok(_get_tag(handle, 1, ctypes.byref(info), ctypes.sizeof(info)))
                if info.NumberOfLinks != 1 or info.EndOfFile > _LIMIT:
                    raise ControlCredentialError("unsafe_control_file")
            return handle
        except Exception:
            _close(handle)
            raise

    def _win_read_handle(handle) -> ControlDescriptor:
        chunks, size = [], 0
        while size <= _LIMIT:
            data = ctypes.create_string_buffer(min(4096, _LIMIT + 1 - size))
            count = wt.DWORD()
            _win_ok(_read(handle, data, len(data), ctypes.byref(count), None))
            if not count.value:
                break
            chunks.append(data.raw[: count.value])
            size += count.value
        if size > _LIMIT:
            raise ControlCredentialError("invalid_control_descriptor")
        return _decode(b"".join(chunks))

    class _WindowsDirectory:
        def __init__(self, path: Path) -> None:
            self.path = _path(path)
            self._handles: list[Any] = []
            self.user, self.user_text = _user_sid()
            if not self.path.drive or self.path.drive.startswith("\\\\"):
                raise ControlCredentialError("unsafe_control_directory")
            try:
                current = Path(self.path.anchor)
                components = [current]
                for part in self.path.parts[1:]:
                    current /= part
                    components.append(current)
                for index, component in enumerate(components):
                    handle = _win_open(component, directory=True)
                    self._handles.append(handle)
                    _security(
                        handle,
                        self.user,
                        directory=True,
                        final=index == len(components) - 1,
                    )
            except Exception:
                self.close()
                raise ControlCredentialError("unsafe_control_directory") from None

        def read(self, name: str) -> ControlDescriptor:
            handle = _win_open(self.path / name)
            try:
                _security(handle, self.user, directory=False)
                return _win_read_handle(handle)
            finally:
                _close(handle)

        def publish(self, name: str, descriptor: ControlDescriptor) -> None:
            try:
                self.read(name)
            except FileNotFoundError:
                pass
            sd = _PVOID()
            _win_ok(
                _sddl(
                    f"O:{self.user_text}D:P(A;;FA;;;{self.user_text})(A;;FA;;;SY)",
                    1,
                    ctypes.byref(sd),
                    None,
                )
            )
            tmp = self.path / f".{name}.{secrets.token_hex(12)}.tmp"
            handle = None
            published = False
            try:
                handle = _win_open(tmp, create_sd=sd)
                _security(handle, self.user, directory=False)
                data = descriptor._bytes()
                offset = 0
                while offset < len(data):
                    chunk = ctypes.create_string_buffer(data[offset:])
                    count = wt.DWORD()
                    _win_ok(
                        _write(
                            handle, chunk, len(data) - offset, ctypes.byref(count), None
                        ),
                        "control_publish_failed",
                    )
                    if not count.value:
                        raise ControlCredentialError("control_publish_failed")
                    offset += count.value
                _win_ok(_flush(handle), "control_publish_failed")
                _close(handle)
                handle = None
                _win_ok(
                    _move(str(tmp), str(self.path / name), 1 | 8),
                    "control_publish_failed",
                )
                published = True
            except Exception:
                if published:
                    self.revoke(name, descriptor.boot_id, descriptor.role)
                raise
            finally:
                if handle is not None:
                    _close(handle)
                _local_free(sd)
                if tmp.exists():
                    # The trusted directory handles pin this path until cleanup.
                    tmp.unlink()

        def revoke(self, name: str, boot: str, role: str) -> None:
            try:
                handle = _win_open(self.path / name, delete=True)
            except FileNotFoundError:
                return
            try:
                _security(handle, self.user, directory=False)
                current = _win_read_handle(handle)
                if current.boot_id == boot and current.role == role:
                    info = _Disposition(True)
                    _win_ok(
                        _set_info(handle, 4, ctypes.byref(info), ctypes.sizeof(info)),
                        "control_revoke_failed",
                    )
            finally:
                _close(handle)

        def close(self) -> None:
            while self._handles:
                _close(self._handles.pop())


class CredentialLease:
    """Pin safe directories through publication/revocation; never reuse a key."""

    def __init__(self, directory: Path, name: str) -> None:
        if name not in {"agent.control.json", "hub.control.json"}:
            raise ControlCredentialError("unsafe_control_path")
        try:
            self._directory = (
                _WindowsDirectory(directory)
                if os.name == "nt"
                else _PosixDirectory(directory)
            )
        except (OSError, ControlCredentialError):
            raise ControlCredentialError("unsafe_control_directory") from None
        self.path = self._directory.path / name

    def publish(self, descriptor: ControlDescriptor) -> None:
        try:
            self._directory.publish(self.path.name, descriptor)
        except (OSError, ControlCredentialError):
            raise ControlCredentialError("control_publish_failed") from None

    def revoke(self, boot_id: str, role: str) -> None:
        try:
            self._directory.revoke(self.path.name, boot_id, role)
        except (OSError, ControlCredentialError):
            raise ControlCredentialError("control_revoke_failed") from None

    def close(self) -> None:
        self._directory.close()


def read_control_descriptor(path: Path) -> ControlDescriptor:
    """Read exactly the object inspected; no path check followed by reopen."""
    parent = None
    try:
        target = _path(path)
        parent = (
            _WindowsDirectory(target.parent)
            if os.name == "nt"
            else _PosixDirectory(target.parent)
        )
        return parent.read(target.name)
    except (OSError, ControlCredentialError):
        raise ControlCredentialError("control_read_failed") from None
    finally:
        if parent is not None:
            parent.close()
