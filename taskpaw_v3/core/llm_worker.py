"""`llm-worker`: a terminable child process that runs `chat()` (#178, AC3).

Why a process: in-thread HTTP cannot be cancelled during DNS/connect, and a
slow-drip body defeats any between-reads deadline (Codex rounds 3–4), yet #177's
translator must stop inside the supervisor's shared 5 s budget (constitution §4).
A child process is cancellable in every phase.

Protocol — JSON lines, UTF-8, `\\n`-terminated:
- request on stdin: `{"id", "messages", "temperature"?, "max_tokens"?,
  "json_mode"?, "timeout"?, "api_base"?, "model"?}` (`api_base`/`model` override
  the environment for that request — live-apply; the key can NOT be overridden);
- reply on stdout: `{"id", "ok": true, "content", "finish_reason", "model",
  "latency_ms"}` or `{"id", "ok": false, "kind", "status", "message"}`.

Settings come from the child's environment (`worker_env()`), never argv — the key
must not appear in a process listing (constitution §2).

Cancel contract (D5/D6), consumed by #177's parent side:
1. close the worker's stdin → the stdin watcher calls `os._exit(0)` at once,
   whatever the main thread is doing (this reaches the REAL Python process even
   in the PyInstaller onefile build, where the spawned handle is the bootloader);
2. wait up to 1 s for exit;
3. still alive → tree kill (`taskkill /PID <pid> /T /F` on Windows, `kill()`
   elsewhere);
4. close the `JobKeeper` (`assign_kill_on_close_job`, a best-effort extra layer).
Writing to a dead worker's stdin raises OSError (errno 22 on Windows,
BrokenPipeError elsewhere) — treat both as "worker gone".
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from typing import Any, BinaryIO, Callable, Mapping, Optional, TextIO

from taskpaw_v3.core.llm import (
    DEFAULT_LLM_API_BASE,
    DEFAULT_LLM_MODEL,
    LLM_KEY_ENV,
    LLMError,
    LLMSettings,
    chat,
    resolve_llm_settings,
)

log = logging.getLogger("taskpaw.llm_worker")

ENV_BASE = "TASKPAW_LLM_API_BASE"
ENV_MODEL = "TASKPAW_LLM_MODEL"
ENV_KEY = LLM_KEY_ENV

_MODULE = "taskpaw_v3.core.llm_worker"


def worker_argv() -> list[str]:
    """How to launch the worker: the bundled backend's `llm-worker` role when
    frozen (PyInstaller, A6), else this module under the current interpreter.
    Carries no settings — they travel in the environment."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "llm-worker"]
    return [sys.executable, "-m", _MODULE]


def worker_env(
    settings: LLMSettings, base: Optional[Mapping[str, str]] = None
) -> dict[str, str]:
    """A COPY of `base` (default `os.environ`) carrying the settings. The key is
    set only when non-empty and otherwise removed, so a stale inherited key can't
    leak into a keyless (e.g. local Ollama) worker. Never mutates `os.environ`."""
    env = dict(os.environ if base is None else base)
    env[ENV_BASE] = settings.api_base
    env[ENV_MODEL] = settings.model
    if settings.api_key:
        env[ENV_KEY] = settings.api_key
    else:
        env.pop(ENV_KEY, None)
    return env


def settings_from_env(environ: Mapping[str, str]) -> LLMSettings:
    """The worker's settings (the inverse of `worker_env`)."""
    return resolve_llm_settings(
        environ.get(ENV_BASE, DEFAULT_LLM_API_BASE),
        environ.get(ENV_MODEL, DEFAULT_LLM_MODEL),
        "",
        environ=environ,
    )


class _InvalidRequest(Exception):
    pass


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_request(req: Any, settings: LLMSettings) -> tuple[LLMSettings, dict]:
    """Validate one request object → (effective settings, chat kwargs)."""
    if not isinstance(req, dict) or not isinstance(req.get("messages"), list):
        raise _InvalidRequest
    kwargs: dict[str, Any] = {
        "temperature": req.get("temperature", 0.3),
        "max_tokens": req.get("max_tokens"),
        "json_mode": req.get("json_mode", False),
        "timeout": req.get("timeout", 30.0),
    }
    if not _is_number(kwargs["temperature"]):
        raise _InvalidRequest
    mt = kwargs["max_tokens"]
    if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool)):
        raise _InvalidRequest
    if not isinstance(kwargs["json_mode"], bool):
        raise _InvalidRequest
    if not _is_number(kwargs["timeout"]) or kwargs["timeout"] <= 0:
        raise _InvalidRequest
    api_base = _override(req, "api_base").rstrip("/") or settings.api_base
    model = _override(req, "model") or settings.model
    # The key is deliberately NOT overridable per request (it lives only in the
    # worker's environment); any "api_key" field in the request is ignored.
    return dataclasses.replace(settings, api_base=api_base, model=model), kwargs


def _override(req: dict, field: str) -> str:
    """A per-request string override (live-apply), stripped; "" = none."""
    value = req.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _InvalidRequest
    return value.strip()


def _error_reply(rid: Any, kind: str, status: Optional[int], message: str) -> str:
    return json.dumps(
        {"id": rid, "ok": False, "kind": kind, "status": status, "message": message}
    )


def handle_request(
    line: str,
    settings: LLMSettings,
    chat_fn: Callable[..., Any] = chat,
) -> str:
    """One request line → one reply line (no trailing newline). Pure; NEVER
    raises (D2): a bad line or any non-LLMError failure becomes an error reply
    with a fixed message (never exception text — it could embed the key)."""
    rid: Any = None
    try:
        req = json.loads(line)
        if isinstance(req, dict):
            candidate = req.get("id")
            if candidate is None or isinstance(candidate, (str, int, float, bool)):
                rid = candidate
            else:
                raise _InvalidRequest
        effective, kwargs = _parse_request(req, settings)
    except (ValueError, _InvalidRequest):  # JSONDecodeError is a ValueError
        return _error_reply(rid, "bad_response", None, "invalid request")
    try:
        r = chat_fn(effective, req["messages"], **kwargs)
    except LLMError as e:
        return _error_reply(rid, e.kind, e.status, e.message)
    except Exception as e:  # catch-all (D2): the worker must never die mid-run
        log.warning("llm-worker: request failed: %s", type(e).__name__)
        return _error_reply(
            rid, "bad_response", None, f"unexpected error: {type(e).__name__}"
        )
    # ensure_ascii keeps every line pure ASCII: immune to pipe encodings.
    return json.dumps(
        {
            "id": rid,
            "ok": True,
            "content": r.content,
            "finish_reason": r.finish_reason,
            "model": r.model,
            "latency_ms": r.latency_ms,
        }
    )


def serve(
    stdin: BinaryIO,
    stdout: TextIO,
    settings: LLMSettings,
    chat_fn: Callable[..., Any] = chat,
    exit_fn: Callable[[int], Any] = os._exit,
) -> int:
    """Answer requests one at a time until stdin reaches EOF.

    A daemon watcher thread owns stdin. On EOF it queues a sentinel and then
    calls `exit_fn(0)` IMMEDIATELY and UNCONDITIONALLY (D5) — idle or mid-request
    — so closing stdin is the parent's cancel. In production `exit_fn` is
    `os._exit` and the process ends right there; the sentinel only lets an
    injected (recording) `exit_fn` in tests return 0 instead of blocking (D15)."""
    lines: queue.Queue[Optional[bytes]] = queue.Queue()

    def _watch() -> None:
        try:
            for raw in iter(stdin.readline, b""):
                lines.put(raw)
        except (OSError, ValueError) as e:  # a broken/closed pipe == EOF
            log.warning("llm-worker: stdin read failed (%s); exiting", type(e).__name__)
        lines.put(None)
        exit_fn(0)

    threading.Thread(target=_watch, name="llm-worker-stdin", daemon=True).start()
    while True:
        raw = lines.get()
        if raw is None:
            return 0
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.strip():
            continue
        stdout.write(handle_request(line, settings, chat_fn) + "\n")
        stdout.flush()


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point (`python -m taskpaw_v3.core.llm_worker` / backend role
    `llm-worker`). Takes no flags (`argv` is accepted for the role-dispatch
    signature and ignored): settings come from the environment only."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if isinstance(sys.stdout, io.TextIOWrapper):
        # Protocol lines are UTF-8 with bare "\n" (a Windows text pipe would
        # otherwise write "\r\n" in the locale code page).
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    return serve(sys.stdin.buffer, sys.stdout, settings_from_env(os.environ))


# ── parent-side helper: Windows kill-on-close Job Object (best-effort, D6) ──
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class JobKeeper:
    """Owns a Job Object handle; closing it kills every process in the job.
    The caller keeps it alive for as long as the worker should live."""

    def __init__(self, handle: int, close_handle: Callable[[int], Any]) -> None:
        self._handle = handle
        self._close_handle = close_handle  # kernel32.CloseHandle with argtypes set

    def close(self) -> None:
        """Idempotent; the first call closes the job (and kills its members)."""
        handle, self._handle = self._handle, 0
        if handle:
            self._close_handle(handle)

    def __del__(self) -> None:
        self.close()


def assign_kill_on_close_job(proc: subprocess.Popen) -> Optional[JobKeeper]:
    """Put `proc` in a KILL_ON_JOB_CLOSE Job Object (Windows only; else None).

    BEST-EFFORT, never the guarantee (D6): a grandchild that `proc` spawned
    BEFORE this assignment — e.g. the PyInstaller onefile bootloader's Python
    child, or the venv python.exe launcher's interpreter — is not in the job.
    The guarantee is the stdin-EOF watcher followed by a tree kill. Any failure
    logs one warning and returns None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
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

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = None
    try:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, int(getattr(proc, "_handle"))):
            raise ctypes.WinError(ctypes.get_last_error())
        return JobKeeper(job, kernel32.CloseHandle)
    except Exception as e:  # best-effort layer: never fatal (D6)
        log.warning(
            "llm-worker: kill-on-close job object unavailable (%s); relying on "
            "stdin close + tree kill",
            e,
        )
        if job:
            kernel32.CloseHandle(job)
        return None


if __name__ == "__main__":
    raise SystemExit(main())
