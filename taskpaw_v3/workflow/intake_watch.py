"""Bounded Windows notifications. A notification never proves completeness."""

from __future__ import annotations

import ctypes
import math
import struct
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path

from .intake_files import IntakeFileError, WindowsFiles, _Holds, _key, _local, _parts
from .models import NativeIdentity


@dataclass(frozen=True)
class DirtyBatch:
    paths: tuple[str, ...]
    full_rescan: bool


class DirtyHints:
    def __init__(self, *, limit: int = 256):
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError("invalid_hint_limit")
        self.limit = limit
        self._paths: set[str] = set()
        self._full = False
        self._lock = threading.Lock()

    def add(self, paths: tuple[str, ...], *, overflow: bool = False) -> None:
        with self._lock:
            self._full |= overflow
            for path in paths:
                if path not in self._paths and len(self._paths) >= self.limit:
                    self._full = True
                    break
                self._paths.add(path)

    def drain(self) -> DirtyBatch:
        with self._lock:
            result = DirtyBatch(tuple(sorted(self._paths)), self._full)
            self._paths.clear()
            self._full = False
            return result


def decode_notifications(value: bytes) -> tuple[str, ...]:
    if not value or len(value) > 65536:
        raise ValueError("notification_overflow")
    paths, offset = [], 0
    while True:
        if offset + 12 > len(value):
            raise ValueError("invalid_notification")
        next_offset, action, length = struct.unpack_from("<III", value, offset)
        if (
            action not in (1, 2, 3, 4, 5)
            or length < 2
            or length % 2
            or offset + 12 + length > len(value)
        ):
            raise ValueError("invalid_notification")
        try:
            name = value[offset + 12 : offset + 12 + length].decode(
                "utf-16-le", errors="strict"
            )
            paths.append("/".join(_parts(name)))
        except (UnicodeError, IntakeFileError):
            raise ValueError("invalid_notification") from None
        if not next_offset:
            if len(value) - (offset + 12 + length) > 3:
                raise ValueError("invalid_notification")
            return tuple(paths)
        if (
            next_offset < 12 + length
            or next_offset % 4
            or offset + next_offset >= len(value)
        ):
            raise ValueError("invalid_notification")
        offset += next_offset


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", wt.DWORD),
        ("offset_high", wt.DWORD),
        ("event", wt.HANDLE),
    ]


class DirectoryWatcher:
    """Explicit one-thread owner; an unjoined/cancel-uncertain owner stays held."""

    def __init__(self, root: Path, *, expected_root: NativeIdentity, limit: int = 256):
        self.root, self.expected_root = _local(root), expected_root
        self.hints = DirtyHints(limit=limit)
        self._stop, self._ready = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._api: WindowsFiles | None = None
        self._holds: _Holds | None = None
        self._watch: int | None = None
        self._event: int | None = None
        self.failure: str | None = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def closed(self) -> bool:
        return (
            not self.alive
            and self._watch is None
            and self._event is None
            and (self._holds is None or self._holds.closed)
        )

    def start(self) -> None:
        if self._thread is not None or self._api is not None or self._stop.is_set():
            raise IntakeFileError("watcher_already_started")
        self._api = api = WindowsFiles()
        self._holds = _Holds(api, self.expected_root.root_id)
        definitions = {
            "CreateEventW": (wt.HANDLE, [wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]),
            "ResetEvent": (wt.BOOL, [wt.HANDLE]),
            "ReadDirectoryChangesW": (
                wt.BOOL,
                [
                    wt.HANDLE,
                    wt.LPVOID,
                    wt.DWORD,
                    wt.BOOL,
                    wt.DWORD,
                    ctypes.POINTER(wt.DWORD),
                    wt.LPVOID,
                    wt.LPVOID,
                ],
            ),
            "CancelIoEx": (wt.BOOL, [wt.HANDLE, wt.LPVOID]),
            "GetOverlappedResult": (
                wt.BOOL,
                [wt.HANDLE, wt.LPVOID, ctypes.POINTER(wt.DWORD), wt.BOOL],
            ),
            "WaitForSingleObject": (wt.DWORD, [wt.HANDLE, wt.DWORD]),
        }
        for name, (result, arguments) in definitions.items():
            function = getattr(api.kernel, name)
            function.restype, function.argtypes = result, arguments
        try:
            root = self._holds.chain(self.root)[-1]
            api.local_filesystem(self.root, root.handle)
            if _key(root.identity) != _key(self.expected_root):
                raise IntakeFileError("identity_changed")
            handle = api.kernel.CreateFileW(
                str(self.root), 1, 7, None, 3, 0x42000000 | 0x00200000, None
            )
            api.check(handle != ctypes.c_void_p(-1).value)
            self._watch = int(handle)
            if _key(
                api.entry(
                    self._watch, self.root, "", self.expected_root.root_id
                ).identity
            ) != _key(root.identity):
                raise IntakeFileError("identity_changed")
            handle = api.kernel.CreateEventW(None, True, False, None)
            api.check(handle)
            self._event = int(handle)
            self.hints.add((), overflow=True)
            self._thread = threading.Thread(
                target=self._run, name="taskpaw-intake-watch", daemon=False
            )
            self._thread.start()
        except BaseException:
            self._close_handles()
            raise
        if not self._ready.wait(5) or self.failure:
            self.stop(timeout=5)
            raise IntakeFileError("watcher_start_failed")

    def _cancel(self, overlap: _Overlapped) -> None:
        assert self._api is not None
        if (
            not self._api.kernel.CancelIoEx(self._watch, ctypes.byref(overlap))
            and ctypes.get_last_error() != 1168
        ):
            self.failure = self.failure or "watcher_cancel_failed"

    def _read(self, buffer, overlap: _Overlapped) -> bytes | None:
        assert self._api is not None
        api, count = self._api, wt.DWORD()
        api.check(api.kernel.ResetEvent(self._event))
        api.check(
            api.kernel.ReadDirectoryChangesW(
                self._watch,
                buffer,
                ctypes.sizeof(buffer),
                True,
                0x15F,
                None,
                ctypes.byref(overlap),
                None,
            )
        )
        self._ready.set()
        cancelled = False
        # Keep buffer/OVERLAPPED/handles alive until this particular read has
        # actually completed. stop() may return unknown while this remains held.
        while True:
            if self._stop.is_set() and not cancelled:
                self._cancel(overlap)
                cancelled = True
            signaled = api.kernel.WaitForSingleObject(self._event, 50)
            if signaled not in (0, 258):
                self.failure = self.failure or "watcher_wait_failed"
                self._cancel(overlap)
                cancelled = True
            if api.kernel.GetOverlappedResult(
                self._watch, ctypes.byref(overlap), ctypes.byref(count), False
            ):
                return bytes(buffer)[: count.value]
            error = ctypes.get_last_error()
            if error == 996:  # ERROR_IO_INCOMPLETE, still owns this buffer.
                continue
            if error == 995 and cancelled:
                return None
            if error == 1022:  # ERROR_NOTIFY_ENUM_DIR, require full enumeration.
                return b""
            raise IntakeFileError("watcher_read_failed")

    def _run(self) -> None:
        try:
            # DWORD-aligned storage as required by ReadDirectoryChangesW.
            buffer = (ctypes.c_uint32 * 16384)()
            while not self._stop.is_set():
                overlap = _Overlapped()
                overlap.event = self._event
                value = self._read(buffer, overlap)
                if value is None:
                    break
                try:
                    self.hints.add(decode_notifications(value))
                except ValueError:
                    self.hints.add((), overflow=True)
                if self.failure:
                    break
        except Exception:
            self.failure = self.failure or "watcher_failed"
            self.hints.add((), overflow=True)
        finally:
            self._ready.set()
            self._close_handles()

    def _close_handles(self) -> None:
        if self._api is not None:
            for name in ("_watch", "_event"):
                handle = getattr(self, name)
                if handle is not None:
                    try:
                        self._api.close(handle)
                        setattr(self, name, None)
                    except OSError:
                        self.failure = self.failure or "watcher_close_failed"
        if self._holds is not None:
            try:
                self._holds.close()
            except OSError:
                self.failure = self.failure or "watcher_close_failed"

    def drain(self) -> DirtyBatch:
        value = self.hints.drain()
        return DirtyBatch(value.paths, value.full_rescan or self.failure is not None)

    def stop(self, *, timeout: float) -> bool:
        if not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("invalid_stop_timeout")
        end = time.monotonic() + timeout
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0, end - time.monotonic()))
        if self.alive:
            return False
        return self.closed and self.failure is None
