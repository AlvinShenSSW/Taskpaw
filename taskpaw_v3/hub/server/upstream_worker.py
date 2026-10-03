"""Bounded one-shot upstream GET; no application/config/DB startup.

The calling thread owns the deadline independently of blocking pipe I/O. Settings
travel through stdin, never argv. Both parent I/O owners are tracked/non-daemon.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import io
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, cast

from taskpaw_v3.core.control import without_control_env
from taskpaw_v3.core.http import NoRedirectHandler
from taskpaw_v3.core.llm import without_llm_env
from taskpaw_v3.core.state import MAX_EVENT_ID, StateError, integer, parse_cursor

STATUS_BYTES = 256 * 1024
EVENT_BYTES = 2 * 1024 * 1024
REQUEST_BYTES = 32 * 1024
REPLY_BYTES = 4 * 1024 * 1024
ITEM_BYTES = 64 * 1024
CHUNK_BYTES = 16 * 1024
METADATA_BYTES = 64 * 1024
LINE_BYTES = 8 * 1024
MAX_EVENTS = 10000
MAX_NODES = 200000
MAX_COUNTER = (1 << 63) - 1


class UpstreamError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Object(dict):
    duplicate = False


def _pairs(pairs: list[tuple[str, Any]]) -> _Object:
    result = _Object()
    for key, value in pairs:
        if key in result:
            result.duplicate = True
        result[key] = value
    return result


def _lexical(raw: str) -> None:
    depth = 0
    quoted = escaped = False
    number = 0
    for c in raw:
        if quoted:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                quoted = False
            continue
        if c == '"':
            quoted = True
        elif c in "[{":
            depth += 1
            if depth > 32:
                raise UpstreamError("json_depth")
        elif c in "]}":
            depth -= 1
        if c in "-+0123456789.eE":
            number += 1
            if number > 256:
                raise UpstreamError("json_number_size")
        else:
            number = 0


def _load(body: bytes | str, cap: int) -> Any:
    try:
        encoded = body.encode("utf-8") if isinstance(body, str) else body
        if not isinstance(encoded, bytes) or len(encoded) > cap:
            raise UpstreamError("body_oversize")
        raw = encoded.decode("utf-8")
        _lexical(raw)
        return json.loads(raw, object_pairs_hook=_pairs)
    except UpstreamError:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise UpstreamError("json_invalid") from None


def _safe(value: Any, nodes: int) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        nodes -= 1
        if nodes < 0:
            raise UpstreamError("json_nodes")
        if depth > 32:
            raise UpstreamError("json_depth")
        if isinstance(item, dict):
            if getattr(item, "duplicate", False):
                raise UpstreamError("duplicate_key")
            for key, val in item.items():
                if not isinstance(key, str):
                    raise UpstreamError("json_type")
                try:
                    key.encode("utf-8")
                except UnicodeError:
                    raise UpstreamError("json_encoding") from None
                pending.append((val, depth + 1))
        elif isinstance(item, list):
            pending.extend((val, depth + 1) for val in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise UpstreamError("json_encoding") from None
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise UpstreamError("json_nonfinite")
        elif item is not None and type(item) not in (bool, int):
            raise UpstreamError("json_type")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def decode_status(body: bytes | str) -> tuple[dict, str]:
    value = _load(body, STATUS_BYTES)
    _safe(value, 20000)
    if not isinstance(value, dict):
        raise UpstreamError("status_type")
    for name in ("machine", "server_id", "os", "version"):
        if name in value and not isinstance(value[name], str):
            raise UpstreamError("status_type")
    if "monitors" in value:
        monitors = value["monitors"]
        if isinstance(monitors, dict):
            snapshots = list(monitors.values())
        elif isinstance(monitors, list):
            snapshots = monitors
        else:
            raise UpstreamError("status_type")
        for snap in snapshots:
            if not isinstance(snap, dict):
                raise UpstreamError("status_type")
            if "metrics" in snap and not isinstance(snap["metrics"], dict):
                raise UpstreamError("status_type")
            for key in ("state", "status", "type_id"):
                if key in snap and not isinstance(snap[key], str):
                    raise UpstreamError("status_type")
    raw = canonical(value)
    if len(raw.encode("utf-8")) > STATUS_BYTES:
        raise UpstreamError("body_oversize")
    return value, raw


def evidence(value: Any, ordinal: int, reason: str) -> dict:
    # Even invalid Unicode/nonfinite values have bounded ASCII forensic hashes;
    # no source text/message is retained in this metadata or emitted to logs.
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    candidate = value.get("id") if isinstance(value, dict) else None
    return {
        "ordinal": ordinal,
        "reason": reason,
        "event_id": candidate
        if type(candidate) is int and 1 <= candidate <= MAX_EVENT_ID
        else None,
        "bytes": len(raw),
        "fingerprint": hashlib.sha256(raw).hexdigest(),
    }


def decode_events(body: bytes | str) -> dict:
    value = _load(body, EVENT_BYTES)
    if not isinstance(value, dict) or getattr(value, "duplicate", False):
        raise UpstreamError("invalid_cursor_response")
    try:
        cursor = value.get("event_cursor")
        _safe(cursor, 20000)
        proof = parse_cursor(cursor)
    except (StateError, UpstreamError):
        raise UpstreamError("invalid_cursor_response") from None
    events = value.get("events")
    if not isinstance(events, list):
        raise UpstreamError("invalid_cursor_response")
    if len(events) > MAX_EVENTS:
        raise UpstreamError("event_count")
    # A structural count bound without letting a bad item poison good neighbors.
    pending = [value]
    count = 0
    while pending:
        v = pending.pop()
        count += 1
        if count > MAX_NODES:
            raise UpstreamError("json_nodes")
        if isinstance(v, dict):
            pending.extend(v.values())
        elif isinstance(v, list):
            pending.extend(v)
    receipts: list[dict] = []
    valid: dict[int, dict] = {}
    prev = -1
    for ordinal, event in enumerate(events):
        try:
            if not isinstance(event, dict):
                raise UpstreamError("event_type")
            try:
                eid = integer(event.get("id"), 1, MAX_EVENT_ID)
            except StateError:
                raise UpstreamError("event_id") from None
            if eid > proof["offered_highwater"]:
                raise UpstreamError("event_unoffered")
            _safe(event, 4096)
            for key in ("time", "machine", "monitor", "message", "level", "title"):
                if key in event:
                    cap = 32768 if key in ("message", "title") else 4096
                    if not isinstance(event[key], str):
                        raise UpstreamError("event_field_type")
                    if len(event[key].encode("utf-8")) > cap:
                        raise UpstreamError("event_field_size")
            if "data" in event and not isinstance(event["data"], dict):
                raise UpstreamError("event_field_type")
            if len(canonical(event).encode("utf-8")) > ITEM_BYTES:
                raise UpstreamError("event_size")
            if eid in valid:
                reason = (
                    "event_duplicate"
                    if valid[eid] == event
                    else "event_conflicting_duplicate"
                )
                receipts.append(evidence(event, ordinal, reason))
                continue
            if eid < prev:
                receipts.append(evidence(event, ordinal, "event_out_of_order"))
            prev = eid
            valid[eid] = event
        except UpstreamError as exc:
            receipts.append(evidence(event, ordinal, exc.reason))
    return {
        "proof": proof,
        "events": [valid[i] for i in sorted(valid)],
        "receipts": receipts,
    }


class _MetadataReader:
    def __init__(self, fp: Any) -> None:
        self.fp = fp
        self.used = 0

    def readline(self, size: int = -1) -> bytes:
        size = min(
            size if size >= 0 else LINE_BYTES + 1,
            LINE_BYTES + 1,
            METADATA_BYTES - self.used + 1,
        )
        result = self.fp.readline(size)
        self.used += len(result)
        if len(result) > LINE_BYTES or self.used > METADATA_BYTES:
            raise UpstreamError("http_metadata_oversize")
        return result

    def read(self, size: int = -1) -> bytes:
        # read1() consumes body bytes separately. HTTPResponse's chunk parser
        # uses read(2) solely for the preceding chunk's CRLF terminator.
        result = self.fp.read(size)
        if size == 2:
            self.used += len(result)
            if self.used > METADATA_BYTES:
                raise UpstreamError("http_metadata_oversize")
        return result

    def __getattr__(self, key: str) -> Any:
        return getattr(self.fp, key)


class _Response(http.client.HTTPResponse):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fp = cast(Any, _MetadataReader(self.fp))


class _Connection(http.client.HTTPConnection):
    response_class = _Response


class _Handler(urllib.request.HTTPHandler):
    def http_open(self, req: Any) -> Any:
        return self.do_open(_Connection, req)


def handle_request(request: dict, opener: Any = None) -> dict:
    try:
        kind = request.get("kind")
        url = request.get("url")
        timeout = request.get("timeout")
        headers = request.get("headers")
        if (
            kind not in ("status", "events")
            or not isinstance(url, str)
            or not url.startswith("http://")
            or not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
            or not isinstance(headers, dict)
        ):
            raise UpstreamError("request_invalid")
        for key, val in headers.items():
            if (
                not isinstance(key, str)
                or not isinstance(val, str)
                or any(c in key + val for c in "\r\n\x00")
            ):
                raise UpstreamError("request_invalid")
        req = urllib.request.Request(url, headers=headers)
        transport = opener or urllib.request.build_opener(
            NoRedirectHandler(), _Handler()
        )
        cap = STATUS_BYTES if kind == "status" else EVENT_BYTES
        with transport.open(req, timeout=timeout) as resp:
            encoding = resp.headers.get("Content-Encoding", "identity")
            if encoding.lower() not in ("identity", ""):
                raise UpstreamError("http_encoding")
            length = resp.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > cap):
                raise UpstreamError("body_oversize")
            chunks = bytearray()
            while True:
                part = resp.read1(min(CHUNK_BYTES, cap + 1 - len(chunks)))
                if not part:
                    break
                chunks.extend(part)
                if len(chunks) > cap:
                    raise UpstreamError("body_oversize")
        if kind == "status":
            status, raw = decode_status(bytes(chunks))
            return {"ok": True, "status": status, "raw": raw}
        return {"ok": True, **decode_events(bytes(chunks))}
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return {
            "ok": False,
            "reason": "http_auth" if code in (401, 403) else "http_refused",
            "status_code": code,
        }
    except UpstreamError as exc:
        return {"ok": False, "reason": exc.reason}
    except Exception:
        return {"ok": False, "reason": "upstream_failed"}


def main() -> int:
    raw = sys.stdin.buffer.readline(REQUEST_BYTES + 1)
    if len(raw) > REQUEST_BYTES or not raw.endswith(b"\n"):
        return 2

    def eof_guard() -> None:
        try:
            os.read(sys.stdin.fileno(), 1)
        finally:
            os._exit(0)

    threading.Thread(target=eof_guard, daemon=True, name="upstream-eof").start()
    try:
        request = json.loads(raw)
        result = handle_request(request)
        reply = canonical(result).encode("utf-8")
        if len(reply) > REPLY_BYTES:
            reply = b'{"ok":false,"reason":"helper_output_oversize"}'
        sys.stdout.buffer.write(reply)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 2


def worker_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "upstream-http"]
    return [sys.executable, "-m", "taskpaw_v3.hub.server.upstream_worker"]


class _WindowsAPI:
    """Native operations for this one helper, loaded only on Windows."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes as w

        self.ctypes: Any = ctypes
        self.winapi = importlib.import_module("_winapi")
        self.msvcrt = importlib.import_module("msvcrt")
        kernel = self.ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel = kernel

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Limits),
                ("IoInfo", IO),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_longlong)
                for name in (
                    "TotalUserTime",
                    "TotalKernelTime",
                    "ThisPeriodTotalUserTime",
                    "ThisPeriodTotalKernelTime",
                )
            ] + [
                (name, w.DWORD)
                for name in (
                    "TotalPageFaultCount",
                    "TotalProcesses",
                    "ActiveProcesses",
                    "TotalTerminatedProcesses",
                )
            ]

        class ProcessList(ctypes.Structure):
            _fields_ = [
                ("NumberOfAssignedProcesses", w.DWORD),
                ("NumberOfProcessIdsInList", w.DWORD),
                ("ProcessIdList", ctypes.c_size_t * 16),
            ]

        self.extended, self.accounting_type = Extended, Accounting
        self.process_list = ProcessList
        self.filetime = w.FILETIME
        for name, args, result in (
            ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
            (
                "SetInformationJobObject",
                [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD],
                w.BOOL,
            ),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            (
                "QueryInformationJobObject",
                [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID],
                w.BOOL,
            ),
            ("ResumeThread", [w.HANDLE], w.DWORD),
            ("CloseHandle", [w.HANDLE], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("GetProcessId", [w.HANDLE], w.DWORD),
            ("IsProcessInJob", [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            ("GetHandleInformation", [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            ("GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        ):
            fn = getattr(kernel, name)
            fn.argtypes, fn.restype = args, result

    def checked(self, value: Any) -> Any:
        if not value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return value

    def create_job(self) -> int:
        return int(self.checked(self.kernel.CreateJobObjectW(None, None)))

    def configure_job(self, job: int) -> None:
        limits = self.extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only
        self.checked(
            self.kernel.SetInformationJobObject(
                job, 9, self.ctypes.byref(limits), self.ctypes.sizeof(limits)
            )
        )

    def pipe(self, size: int = 0) -> tuple[int, int]:
        return cast(tuple[int, int], self.winapi.CreatePipe(None, size))

    def fd(self, handle: int, flags: int) -> int:
        return int(self.msvcrt.open_osfhandle(handle, flags | cast(Any, os).O_BINARY))

    def oshandle(self, fd: int) -> int:
        return int(self.msvcrt.get_osfhandle(fd))

    def inherit(self, handle: int, value: bool) -> None:
        cast(Any, os).set_handle_inheritable(handle, value)

    def create_process(
        self, argv: list[str], env: dict, handles: list[int]
    ) -> tuple[int, int, int, int]:
        startup = cast(Any, subprocess).STARTUPINFO()
        startup.dwFlags |= self.winapi.STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
        startup.lpAttributeList = {"handle_list": handles}
        return cast(
            tuple[int, int, int, int],
            self.winapi.CreateProcess(
                None,
                subprocess.list2cmdline(argv),
                None,
                None,
                True,
                0x4,  # CREATE_SUSPENDED; _winapi adds EXTENDED_STARTUPINFO_PRESENT.
                env,
                None,
                startup,
            ),
        )

    def assign(self, job: int, process: int) -> None:
        self.checked(self.kernel.AssignProcessToJobObject(job, process))

    def resume(self, thread: int) -> None:
        # ResumeThread returns the previous suspend count, not a BOOL.
        if self.kernel.ResumeThread(thread) != 1:
            raise OSError("helper resume failed")

    def poll(self, process: int) -> int | None:
        result = self.winapi.WaitForSingleObject(process, 0)
        if result == self.winapi.WAIT_TIMEOUT:
            return None
        if result != self.winapi.WAIT_OBJECT_0:
            raise OSError("helper process wait failed")
        return int(self.winapi.GetExitCodeProcess(process))

    def wait(self, process: int, timeout: float | None) -> int:
        millis = (
            self.winapi.INFINITE
            if timeout is None
            else math.ceil(max(0, timeout) * 1000)
        )
        result = self.winapi.WaitForSingleObject(process, millis)
        if result == self.winapi.WAIT_TIMEOUT:
            assert timeout is not None
            raise subprocess.TimeoutExpired("upstream-http", timeout)
        if result != self.winapi.WAIT_OBJECT_0:
            raise OSError("helper process wait failed")
        return int(self.winapi.GetExitCodeProcess(process))

    def kill(self, process: int) -> None:
        self.winapi.TerminateProcess(process, 1)

    def terminate_job(self, job: int) -> None:
        self.checked(self.kernel.TerminateJobObject(job, 1))

    def accounting(self, job: int) -> tuple[int, int]:
        info = self.accounting_type()
        self.checked(
            self.kernel.QueryInformationJobObject(
                job, 1, self.ctypes.byref(info), self.ctypes.sizeof(info), None
            )
        )
        return int(info.TotalProcesses), int(info.ActiveProcesses)

    def active(self, job: int) -> int:
        return self.accounting(job)[1]

    def members(self, job: int) -> list[int]:
        info = self.process_list()
        self.checked(
            self.kernel.QueryInformationJobObject(
                job, 3, self.ctypes.byref(info), self.ctypes.sizeof(info), None
            )
        )
        count = int(info.NumberOfProcessIdsInList)
        if count != info.NumberOfAssignedProcesses or not 0 <= count <= 16:
            raise OSError("helper member list incomplete")
        return [int(info.ProcessIdList[i]) for i in range(count)]

    def open_process(self, pid: int) -> int:
        return int(self.checked(self.kernel.OpenProcess(0x101000, False, pid)))

    def identity(self, handle: int, job: int) -> tuple[int, int]:
        from ctypes import wintypes as w

        pid = int(self.checked(self.kernel.GetProcessId(handle)))
        times = [self.filetime() for _ in range(4)]
        self.checked(
            self.kernel.GetProcessTimes(handle, *(self.ctypes.byref(t) for t in times))
        )
        member, flags = w.BOOL(), w.DWORD()
        self.checked(self.kernel.IsProcessInJob(handle, job, self.ctypes.byref(member)))
        self.checked(self.kernel.GetHandleInformation(handle, self.ctypes.byref(flags)))
        if not member.value or flags.value & 1:
            raise OSError("helper member ownership invalid")
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return pid, created

    def close(self, handle: int) -> None:
        self.checked(self.kernel.CloseHandle(handle))


def _windows_stdin_pipe(api: _WindowsAPI) -> tuple[int, int]:
    return api.pipe()


@dataclass
class _WindowsMember:
    pid: int
    handle: int | None
    created: int | None = None
    valid: bool = False
    signaled: bool = False
    closed: bool = False
    launcher: bool = False


class _WindowsProcess:
    """Popen-compatible owned handle set, published before any native setup."""

    def __init__(self, api: Any = None) -> None:
        self.api = api
        self.stdin: io.FileIO | None = None
        self.stdout: io.FileIO | None = None
        self.pid = 0
        self.returncode: int | None = None
        self.process: int | None = None
        self.thread: int | None = None
        self.job: int | None = None
        self.assigned = False
        self.handles: set[int] = set()
        self.fds: set[int] = set()
        self.members: list[_WindowsMember] = []
        self.total_seen = 0
        self.final_accounting: tuple[int, int] | None = None
        self.exit_proven = False

    def _pipe_file(self, handle: int, mode: str) -> io.FileIO:
        fd = self.api.fd(handle, os.O_RDONLY if mode == "rb" else os.O_WRONLY)
        self.handles.remove(handle)  # CRT now owns this native handle.
        self.fds.add(fd)
        pipe = io.FileIO(fd, mode, closefd=True)
        self.fds.remove(fd)  # FileIO now owns the fd.
        return pipe

    def start(self, argv: list[str], env: dict) -> None:
        if self.api is None:
            self.api = _WindowsAPI()
        self.job = self.api.create_job()
        self.api.configure_job(self.job)
        read, write = _windows_stdin_pipe(self.api)
        self.handles.update((read, write))
        self.stdin = self._pipe_file(write, "wb")
        out_read, out_write = self.api.pipe()
        self.handles.update((out_read, out_write))
        self.stdout = self._pipe_file(out_read, "rb")
        null = os.open(os.devnull, os.O_WRONLY)
        self.fds.add(null)
        child = [read, out_write, self.api.oshandle(null)]
        for handle in child:
            self.api.inherit(handle, True)
        # Parent pipes, job and all other handles stay non-inheritable. The
        # explicit list allows only these child standard handles to transfer.
        self.process, self.thread, self.pid, _ = self.api.create_process(
            argv, env, child
        )
        self.api.assign(self.job, self.process)
        self.assigned = True
        self.capture_launcher()
        self.close_setup()

    def close_setup(self) -> None:
        for handle in tuple(self.handles):
            self.api.close(handle)
            self.handles.remove(handle)
        for fd in tuple(self.fds):
            os.close(fd)
            self.fds.remove(fd)

    def resume(self) -> None:
        assert self.thread is not None and self.assigned
        self.api.resume(self.thread)
        self.api.close(self.thread)
        self.thread = None

    def poll(self) -> int | None:
        if self.returncode is None and self.process is not None:
            self.returncode = self.api.poll(self.process)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = (
                self.api.wait(self.process, timeout) if self.process is not None else 0
            )
        return self.returncode

    def kill(self) -> None:
        if self.process is not None and self.poll() is None:
            self.api.kill(self.process)

    def terminate_owned(self) -> None:
        if self.assigned:
            if self.job is not None:
                self.api.terminate_job(self.job)
        else:
            self.kill()  # Assignment failure leaves a suspended unassigned child.

    def _validate_member(self, member: _WindowsMember) -> None:
        assert member.handle is not None and self.job is not None
        pid, created = self.api.identity(member.handle, self.job)
        if pid != member.pid or type(created) is not int or not 0 <= created < 1 << 64:
            raise OSError("helper member identity invalid")
        member.created = created
        for old in self.members:
            if (
                old is not member
                and old.valid
                and (old.pid, old.created) == (pid, created)
            ):
                self.api.close(member.handle)
                member.handle, member.closed = None, True
                self.members.remove(member)
                return
        member.valid = True

    def capture_launcher(self) -> None:
        if self.process is None or not self.assigned:
            return
        member = next((m for m in self.members if m.launcher), None)
        if member is None:
            member = _WindowsMember(self.pid, self.process, launcher=True)
            self.members.append(member)  # Alias the original creation handle once.
        if not member.valid:
            self._validate_member(member)

    def _accounting(self) -> tuple[int, int]:
        total, active = self.api.accounting(self.job)
        previous = self.total_seen
        if type(total) is int and 0 <= total <= 0xFFFFFFFF:
            self.total_seen = max(previous, total)  # Rejected totals still observed.
        if (
            type(total) is not int
            or type(active) is not int
            or not 1 <= total <= 16
            or not 0 <= active <= total
            or total < previous
        ):
            raise OSError("helper lifetime accounting invalid")
        return total, active

    def capture_members(self) -> None:
        if not self.assigned or self.job is None:
            return
        self.capture_launcher()
        pids = self.api.members(self.job)
        if len(pids) > 16 or len(set(pids)) != len(pids):
            raise OSError("helper member list invalid")
        for pid in pids:
            if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
                raise OSError("helper member PID invalid")
            member = next(
                (m for m in self.members if m.pid == pid and m.handle is not None), None
            )
            if member is not None:
                if not member.valid:
                    self._validate_member(member)
                continue
            if len(self.members) >= 16:
                raise OSError("helper member capacity exhausted")
            member = _WindowsMember(pid, None)
            self.members.append(member)  # Reserve before acquiring a native handle.
            try:
                member.handle = self.api.open_process(pid)
            except OSError:
                self.members.remove(member)
                raise
            self._validate_member(member)
        self._accounting()

    def close_pending(self) -> bool:
        cleaned = True
        for member in tuple(self.members):
            if member.valid or member.launcher:
                continue
            try:
                if member.handle is not None:
                    self.api.close(member.handle)
                self.members.remove(member)
            except OSError:
                cleaned = False
        return cleaned

    def drained(self, end: float) -> bool:
        if not self.assigned:
            # Never-resumed child was directly waited; still check the empty job.
            if self.job is not None:
                total, active = self.api.accounting(self.job)
                return 0 <= total <= 1 and active == 0
            return True
        if self.job is None:
            return self.exit_proven and all(m.closed for m in self.members)
        for member in self.members:
            if not member.valid:
                return False
            if not member.signaled:
                assert member.handle is not None
                try:
                    self.api.wait(member.handle, max(0, end - time.monotonic()))
                except subprocess.TimeoutExpired:
                    return False
                member.signaled = True
        self.final_accounting = self._accounting()  # One snapshot AFTER all signals.
        total, active = self.final_accounting
        if total != len(self.members) or active:
            return False
        self.exit_proven = True
        return True

    def close(self) -> None:
        self.close_setup()
        if not self.close_pending():
            raise OSError("helper pending handle closure failed")
        for member in self.members:
            if member.handle is not None and not member.launcher:
                self.api.close(member.handle)
                member.handle, member.closed = None, True
        for name in ("thread", "process", "job"):
            handle = getattr(self, name)
            if handle is not None:
                self.api.close(handle)
                setattr(self, name, None)
                if name == "process":
                    for member in self.members:
                        if member.launcher:
                            member.handle, member.closed = None, True


@dataclass
class _Owned:
    proc: subprocess.Popen | _WindowsProcess
    keeper: int
    cancel: threading.Event = field(default_factory=threading.Event)
    writer_done: threading.Event = field(default_factory=threading.Event)
    reader_done: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    output: bytearray = field(default_factory=bytearray)
    error: str | None = None


class Transport:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._owned: _Owned | None = None
        self._creating = False

    def cancel(self) -> None:
        with self._lock:
            self._stopped.set()
            if self._owned:
                self._owned.cancel.set()

    def admit_result(self) -> bool:
        """Order consumer admission against Stop without locking its I/O work.

        An admitted operation may finish after Stop; a later admission cannot
        start. The poll-thread join still owns completion of admitted work.
        """
        with self._lock:
            return not self._stopped.is_set()

    def clean(self) -> bool:
        with self._lock:
            return self._owned is None and not self._creating

    @staticmethod
    def _write(rec: _Owned, raw: bytes) -> None:
        pipe = rec.proc.stdin
        assert pipe is not None
        try:
            data = memoryview(raw)
            while data and not rec.cancel.is_set():
                n = pipe.write(data)
                if not n:
                    raise OSError()
                data = data[n:]
        except (OSError, ValueError):
            rec.error = rec.error or "helper_io_failed"
            rec.cancel.set()
        finally:
            try:
                pipe.close()
            except OSError:
                rec.error = rec.error or "helper_io_failed"
                rec.cancel.set()
            finally:
                rec.writer_done.set()

    @staticmethod
    def _read(rec: _Owned) -> None:
        pipe = rec.proc.stdout
        assert pipe is not None
        try:
            while True:
                part = pipe.read(min(CHUNK_BYTES, REPLY_BYTES + 1 - len(rec.output)))
                if not part:
                    break
                rec.output.extend(part)
                if len(rec.output) > REPLY_BYTES:
                    rec.error = "helper_output_oversize"
                    rec.cancel.set()
                    break
        except (OSError, ValueError):
            rec.error = rec.error or "helper_io_failed"
            rec.cancel.set()
        finally:
            try:
                pipe.close()
            except OSError:
                rec.error = rec.error or "helper_io_failed"
                rec.cancel.set()
            finally:
                rec.reader_done.set()

    def _cleanup(self, rec: _Owned, cancel: bool) -> bool:
        end = time.monotonic() + 1
        native = isinstance(rec.proc, _WindowsProcess)
        failed = False
        rec.cancel.set()
        if rec.keeper >= 0:
            os.close(rec.keeper)
            rec.keeper = -1
        try:
            live = cancel and rec.proc.poll() is None
        except OSError:
            if not native:
                raise
            failed, live = True, False
        if live:
            if rec.writer_done.is_set():
                try:
                    rec.proc.wait(timeout=min(0.25, max(0, end - time.monotonic())))
                except subprocess.TimeoutExpired:
                    pass
                except OSError:
                    if not native:
                        raise
                    failed = True
            if not native and rec.proc.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(rec.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    rec.proc.kill()
        if native:
            assert isinstance(rec.proc, _WindowsProcess)
            try:
                rec.proc.capture_members()  # This pass shares the cleanup end.
            except OSError:
                failed = True
            try:
                rec.proc.terminate_owned()
            except OSError:
                failed = True
        try:
            rec.proc.wait(timeout=max(0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            if not native:
                return False
            failed = True
        except OSError:
            if not native:
                raise
            failed = True
        for thread in rec.threads:
            if thread.ident is not None:
                thread.join(max(0, end - time.monotonic()))
        joined = not any(t.is_alive() for t in rec.threads)
        if joined:
            for pipe in (rec.proc.stdin, rec.proc.stdout):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        if native:
            assert isinstance(rec.proc, _WindowsProcess)
            if not rec.proc.close_pending():
                failed = True
            try:
                drained = rec.proc.drained(end)
            except OSError:
                drained = False
            if failed or not joined or not drained:
                return False
            rec.proc.close()
        elif not joined:
            return False
        return True

    def retry_cleanup(self) -> bool:
        """A retained record has exactly one cleanup owner; never replace it."""
        with self._lock:
            if self._creating:
                return False
            rec = self._owned
            if rec is None:
                return True
            self._creating = True
        cleaned = False
        try:
            cleaned = self._cleanup(rec, True)
            return cleaned
        except OSError:
            return False
        finally:
            with self._lock:
                self._creating = False
                if cleaned:
                    self._owned = None

    def request(self, request: dict, timeout: float) -> dict:
        try:
            raw = (canonical(request) + "\n").encode("utf-8")
            if len(raw) > REQUEST_BYTES:
                raise UpstreamError("request_oversize")
        except (ValueError, UnicodeError, UpstreamError):
            return {"ok": False, "reason": "request_invalid"}
        with self._lock:
            if self._stopped.is_set():
                return {"ok": False, "reason": "helper_cancelled"}
            if self._owned or self._creating:
                return {"ok": False, "reason": "helper_cleanup_failed"}
            self._creating = True
        deadline = time.monotonic() + timeout
        rec = None
        try:
            if os.name == "nt":
                native = _WindowsProcess()
                proc: subprocess.Popen | _WindowsProcess = native
                rec = _Owned(proc, -1)
                with self._lock:
                    self._owned = rec
                native.start(worker_argv(), without_control_env(without_llm_env()))
            else:
                proc = subprocess.Popen(
                    worker_argv(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    start_new_session=os.name == "posix",
                    env=without_control_env(without_llm_env()),
                )
                rec = _Owned(proc, -1)
                with self._lock:
                    self._owned = rec
            assert proc.stdin is not None
            rec.keeper = os.dup(proc.stdin.fileno())
            os.set_inheritable(rec.keeper, False)
            if self._stopped.is_set() or time.monotonic() >= deadline:
                if isinstance(proc, _WindowsProcess) and not self._stopped.is_set():
                    rec.error = "upstream_deadline"
                rec.cancel.set()
            else:
                if isinstance(proc, _WindowsProcess):
                    proc.resume()
                for fn, args, name in (
                    (self._read, (rec,), "upstream-reader"),
                    (self._write, (rec, raw), "upstream-writer"),
                ):
                    thread = threading.Thread(
                        target=fn, args=args, name=name, daemon=False
                    )
                    rec.threads.append(thread)
                    thread.start()
            while not rec.cancel.is_set():
                if isinstance(proc, _WindowsProcess):
                    proc.capture_members()
                if self._stopped.is_set():
                    rec.cancel.set()
                    break
                if (
                    rec.writer_done.is_set()
                    and rec.reader_done.is_set()
                    and proc.poll() is not None
                ):
                    break
                if time.monotonic() >= deadline:
                    rec.error = "upstream_deadline"
                    rec.cancel.set()
                    break
                rec.cancel.wait(min(0.01, max(0, deadline - time.monotonic())))
            if rec.cancel.is_set():
                return {"ok": False, "reason": rec.error or "helper_cancelled"}
            if proc.returncode != 0:
                return {"ok": False, "reason": "helper_failed"}
            try:
                result = json.loads(rec.output)
                if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                    raise ValueError()
                return result
            except (ValueError, UnicodeError, RecursionError):
                return {"ok": False, "reason": "helper_reply_invalid"}
        except Exception:
            return {"ok": False, "reason": "helper_failed"}
        finally:
            cleaned = rec is None
            if rec is not None:
                try:
                    cleaned = self._cleanup(
                        rec, rec.cancel.is_set() or rec.proc.poll() is None
                    )
                except OSError:
                    cleaned = False
            with self._lock:
                self._creating = False
                if cleaned:
                    self._owned = None
                stopped = self._stopped.is_set()
            if not cleaned:
                return {"ok": False, "reason": "helper_cleanup_failed"}
            if stopped:
                return {"ok": False, "reason": "helper_cancelled"}


if __name__ == "__main__":
    raise SystemExit(main())
