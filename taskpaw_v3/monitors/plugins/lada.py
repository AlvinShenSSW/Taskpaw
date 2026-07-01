"""`lada` monitor — full port of V2's LadaWatcher (#59).

Two modes, switched by `lada_cli_path`:
  - **managed** (path set): the agent LAUNCHES lada-cli, owns its lifecycle, and
    (in capture mode) parses its tqdm progress (current file, %, fps, ETA).
  - **passive** (path empty): detects an externally-running lada process.

Both report a file queue count (input vs output folder), CPU/RAM, and GPU/VRAM.
Faithful to V2 (`taskpaw.py:649-1270`) but reshaped to the V3 plugin contract:
the subprocess + reader thread are owned by `start()`/`stop()`, and `check()` is
a single non-blocking observation the supervisor schedules. The managed child is
terminated in `stop()`, so it dies with the agent — preserving the #40 no-orphan
guarantee (the Tauri Job Object is the backstop). No `shell=True`.

Events match V2 exactly (only these emit): cli-not-found, managed completion
(exit 0), managed non-zero exit, passive completion. "Starting", per-file, and
"recovered" are status/detail only — never events.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator

from taskpaw_v3.monitors.base import (
    BaseMonitorConfig,
    EventEmitter,
    MonitorInstance,
    MonitorPlugin,
    MonitorStatus,
)
from taskpaw_v3.monitors.plugins.host_metrics import read_gpu

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v"}

# tqdm-style progress regexes. lada-cli localizes its labels via gettext but ships
# no zh translation, so its DEFAULT output is English; the labels are matched
# bilingually (Chinese kept for back-compat) while %/frames("f"/"帧")/fps are literal.
# Samples:
#   ABF-346.mp4:
#   Processing video:  15%|███ |Processed: 06:09 (36703f) | Remaining: 30:47 (207454f) | Speed: 112.3fps
#   正在处理视频： 27%|███ |已处理： 26:58 (84163帧) | 剩余： 1:45:32 (230599帧) | 速度：36.4 帧/秒
_RE_FILENAME = re.compile(r"^(.+\.(?:mp4|mkv|avi|mov|wmv|flv|webm|ts|m4v))\s*[:：]\s*$", re.IGNORECASE)
_RE_PCT = re.compile(r"(\d+)\s*%")
_RE_ELAPSED = re.compile(r"(?:已处理|Processed)[:：]\s*([\d:]+)\s*\((\d+)\s*(?:帧|f)\)", re.IGNORECASE)
_RE_REMAINING = re.compile(r"(?:剩余|Remaining)[:：]\s*([\d:]+)\s*\((\d+)\s*(?:帧|f)\)", re.IGNORECASE)
_RE_FPS = re.compile(r"(?:速度|Speed)[:：]\s*([\d.]+)", re.IGNORECASE)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
_RECENT_OUTPUT_LINES = 20
_CRASH_DETAIL_LINES = 10
_CRASH_DETAIL_CHARS = 800


def parse_progress_line(line: str, prev: dict) -> dict:
    """Pure: return the updated progress dict for one decoded output line.
    A filename header resets progress; a percent line merges fields (V2:961)."""
    line = line.strip()
    if not line:
        return prev
    m = _RE_FILENAME.match(line)
    if m:
        new_file = m.group(1)
        if prev.get("current_file") != new_file:
            return {"current_file": new_file}   # reset stale %/fps on a new file
        return prev
    m_pct = _RE_PCT.search(line)
    if not m_pct:
        return prev
    out = dict(prev)
    out["percent"] = int(m_pct.group(1))
    if m := _RE_ELAPSED.search(line):
        out["elapsed"] = m.group(1)
        try:
            out["processed_frames"] = int(m.group(2))
        except ValueError:
            pass
    if m := _RE_REMAINING.search(line):
        out["eta"] = m.group(1)
        try:
            out["remaining_frames"] = int(m.group(2))
        except ValueError:
            pass
    if m := _RE_FPS.search(line):
        try:
            out["fps"] = float(m.group(1))
        except ValueError:
            pass
    return out


def _proc_name_eq(a: str, b: str) -> bool:
    """Process names equal ignoring case AND an optional Windows '.exe' suffix —
    so a passive `process_name` of "lada-cli" matches the actual "lada-cli.exe"
    (the common Windows case the default would otherwise miss) (#70)."""
    def norm(s: str) -> str:
        s = s.strip().lower()
        return s[:-4] if s.endswith(".exe") else s
    return norm(a) == norm(b)


def process_alive(name: str) -> bool:
    """True if a process named `name` is running (psutil → tasklist/pgrep
    fallback). Case-insensitive, '.exe'-tolerant (V2 taskpaw.py:1194; #70)."""
    try:
        if psutil is not None:
            for proc in psutil.process_iter(["name"]):
                pn = proc.info.get("name")
                if pn and _proc_name_eq(pn, name):
                    return True
            return False
        if sys.platform == "win32":
            # No IMAGENAME filter (it requires an exact name incl. .exe) — list all
            # and match '.exe'-tolerantly.
            out = subprocess.run(
                ["tasklist", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW,
            )
            for line in out.stdout.strip().splitlines():
                parts = line.split('","')
                if parts and _proc_name_eq(parts[0].replace('"', ""), name):
                    return True
            return False
        out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except Exception:
        # Defensive liveness scan: ANY failure (psutil error, subprocess hiccup,
        # timeout) → treat as "not running". A safe degraded default, not a
        # swallowed bug (V2 parity).
        return False


def _count_videos(folder: str) -> Optional[int]:
    if not folder:
        return None
    try:
        p = Path(folder)
        if not p.exists():
            return None
        return sum(1 for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
    except OSError:
        return None


def _list_videos(folder: str) -> list[str]:
    if not folder:
        return []
    try:
        p = Path(folder)
        if not p.exists():
            return []
        return sorted(f.name for f in p.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS)
    except OSError:
        return []


def args_supply_io(extra_args: str) -> tuple[bool, bool]:
    """Whether `lada_extra_args` explicitly passes --input / --output WITH A VALUE.
    Tokenize and match the EXACT option — a substring test would wrongly accept
    `--input-size` / `--output-format` (Codex), and a bare `--input` or empty
    `--output=` supplies no path so it must NOT count either (Codex). Requires
    `--opt VALUE` (a following non-flag token) or a non-empty `--opt=VALUE`.
    Returns (has_input, has_output). Shared by the managed-mode validator AND the
    migrator so they agree on what counts as "I/O supplied"."""
    try:
        toks = shlex.split(extra_args)
    except ValueError:
        toks = extra_args.split()

    def has(opt: str) -> bool:
        for i, t in enumerate(toks):
            if t == opt:
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                if nxt and not nxt.startswith("-"):   # a real value follows
                    return True
            elif t.startswith(opt + "=") and len(t) > len(opt) + 1:  # non-empty =value
                return True
        return False

    return has("--input"), has("--output")


def _cpu_mem() -> dict:
    if psutil is None:
        return {}
    try:
        return {"cpu_pct": round(psutil.cpu_percent(interval=None), 1),
                "mem_pct": round(psutil.virtual_memory().percent, 1)}
    except Exception:
        # Best-effort metrics: a psutil failure shouldn't sink the status update.
        return {}


class LadaConfig(BaseMonitorConfig):
    lada_cli_path: str = Field(
        "", description="Full path to the lada-cli EXECUTABLE FILE (e.g. "
        r"C:\Lada\lada-cli.exe) — NOT the folder. Set it → MANAGED mode (TaskPaw "
        "launches lada-cli, needs the input/output folders below). Leave empty → "
        "PASSIVE mode (just watch an already-running lada-cli).")
    process_name: str = Field(
        "lada-cli", description="Passive mode only: the process to detect. "
        "Matches with or without a trailing '.exe' (Windows: lada-cli.exe).")
    lada_input_folder: str = Field(
        "", description="Folder of videos to process (lada-cli --input). Required in "
        "managed mode.")
    lada_output_folder: str = Field(
        "", description="Folder where lada-cli writes results (--output). Required in "
        "managed mode; also drives the queue count and completion notice.")
    lada_extra_args: str = Field(
        "", description="Extra lada-cli flags passed verbatim, e.g. "
        "--device cuda:1 --encoder h264_nvenc")
    lada_gpu_monitor: bool = Field(
        True, description="Report GPU% / VRAM via nvidia-smi (turn off on a machine "
        "without an NVIDIA GPU).")
    lada_capture_progress: bool = Field(
        False, description="Advanced. Off (default): lada-cli opens its OWN console "
        "window with its progress bar. On: capture lada's output into TaskPaw "
        "(no separate window) to show file/%/fps/ETA in the status pane.")

    @model_validator(mode="after")
    def _managed_needs_folders(self) -> "LadaConfig":
        # Managed mode (a CLI path) can't process without an input AND an output —
        # require them up front rather than launch a no-op lada (#70). But the
        # operator may instead pass them through extra args (e.g.
        # `--input X --output Y`), so accept those too (Codex). Passive mode (no
        # CLI path) needs neither.
        if self.lada_cli_path.strip():
            args_in, args_out = args_supply_io(self.lada_extra_args)
            missing = []
            if not self.lada_input_folder.strip() and not args_in:
                missing.append("lada_input_folder")
            if not self.lada_output_folder.strip() and not args_out:
                missing.append("lada_output_folder")
            if missing:
                raise ValueError(
                    "managed Lada (lada_cli_path set) needs an input AND output "
                    f"folder (or --input/--output in extra args); missing: "
                    f"{', '.join(missing)}")
        return self


class LadaInstance(MonitorInstance):
    def __init__(self, instance_id: str, config: LadaConfig) -> None:
        super().__init__(instance_id, config)
        self._process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._progress: dict = {}
        self._recent_output: deque[str] = deque(maxlen=_RECENT_OUTPUT_LINES)
        self._lock = threading.Lock()
        self._inputs: list[str] = []          # start-of-run input snapshot
        self._launch_error: Optional[str] = None
        self._done_emitted = False
        self._prev_running: Optional[bool] = None   # passive transition

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self, emit: EventEmitter) -> None:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        # Idempotent restart (supervisor stop→start, or a watchdog respawn of
        # this same instance): terminate any process/reader a prior run left so we
        # never leak a duplicate lada-cli, then RESET per-run state so the new run
        # starts clean — a stale _stop would kill the new reader, a stale
        # _done_emitted would suppress the next completion, _prev_running could
        # emit a phantom completion, _launch_error would mask a healthy relaunch
        # (Codex #59).
        if self._process is not None:
            self.stop()
        self._stop.clear()
        self._done_emitted = False
        self._launch_error = None
        self._prev_running = None
        self._process = None
        self._reader = None
        with self._lock:
            self._progress = {}
            self._recent_output.clear()
        self._inputs = _list_videos(cfg.lada_input_folder)
        if cfg.lada_cli_path:
            self._start_managed(emit)

    def _emit_launch_error(self, emit: EventEmitter, msg: str) -> None:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        self._launch_error = msg
        emit("alert", f"{cfg.name} error", msg, dedupe_key=f"{self.instance_id}:launch")

    def _start_managed(self, emit: EventEmitter) -> None:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        # Pre-flight the CLI path. The #1 real-world misconfig is pointing it at
        # the install FOLDER instead of the executable (e.g. "C:\Lada" rather than
        # "C:\Lada\lada-cli.exe"); subprocess would fail that with a cryptic
        # "[WinError 5] access denied", so catch it here with an actionable message.
        cli = Path(cfg.lada_cli_path)
        if cli.is_dir():
            self._emit_launch_error(emit,
                f"lada_cli_path is a folder ({cfg.lada_cli_path}); point it at the "
                f"lada-cli executable, e.g. {cli / 'lada-cli.exe'}")
            return
        # A non-existent path is left to Popen → FileNotFoundError below (one
        # "not found" path; also keeps mocked-Popen tests exercising the argv).
        cmd = [cfg.lada_cli_path]
        if cfg.lada_input_folder:
            cmd += ["--input", cfg.lada_input_folder]
        if cfg.lada_output_folder:
            cmd += ["--output", cfg.lada_output_folder]
        if cfg.lada_extra_args:
            try:
                cmd += shlex.split(cfg.lada_extra_args)
            except ValueError:
                cmd += cfg.lada_extra_args.split()

        capture = cfg.lada_capture_progress
        if sys.platform == "win32":
            flags = _NO_WINDOW if capture else _NEW_CONSOLE
        else:
            flags = 0
        kw: dict = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0) if capture else {}
        try:
            # shell=False (list argv) — constitution §2.
            self._process = subprocess.Popen(cmd, creationflags=flags, **kw)
        except FileNotFoundError:
            # The ONLY start-time event V2 emits. start() must NOT raise (else the
            # supervisor's watchdog would restart-spin on a permanently missing
            # binary) — record the error so check() reports it.
            self._emit_launch_error(emit, f"lada-cli not found at {cfg.lada_cli_path}")
            return
        except PermissionError as e:
            # WinError 5 — usually a non-executable target or an AV/permission block.
            self._emit_launch_error(emit,
                f"access denied launching {cfg.lada_cli_path} ({e}); make sure it's "
                f"the lada-cli executable and isn't blocked by antivirus")
            return
        except Exception as e:
            self._emit_launch_error(emit, f"failed to launch lada-cli: {e}")
            return

        if capture and self._process.stdout is not None:
            self._reader = threading.Thread(target=self._reader_loop,
                                            name=f"lada-reader-{self.instance_id}", daemon=True)
            self._reader.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        p = self._process
        if p is not None and p.poll() is None:
            try:
                p.terminate()                       # graceful first
                try:
                    p.wait(timeout=max(0.1, timeout))
                except subprocess.TimeoutExpired:
                    p.kill()                        # then force
                    try:
                        p.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            except OSError:
                # The child may have exited between poll() and terminate()
                # (ProcessLookupError etc.) — already gone, nothing to clean up.
                pass
        if self._reader is not None:
            self._reader.join(timeout=2)

    # ── reader thread (capture mode) ───────────────────────────────────────
    def _reader_loop(self) -> None:
        stdout = self._process.stdout if self._process else None
        if stdout is None:
            return
        buf = b""
        try:
            while not self._stop.is_set():
                ch = stdout.read(1)        # byte at a time: lada uses \r in-place updates
                if ch == b"":
                    break                  # EOF — process exited
                if ch in (b"\r", b"\n"):
                    self._consume_output(buf)
                    buf = b""
                else:
                    buf += ch
            # A crash line printed right before exit may arrive without a trailing
            # \r/\n before the pipe closes — flush it so the reason isn't lost.
            self._consume_output(buf)
        except (OSError, ValueError):
            # Pipe closed / read on a terminated process as the child exits —
            # expected; the reader simply ends.
            pass

    def _consume_output(self, buf: bytes) -> None:
        """Classify one decoded output line: a recognized progress update advances
        `_progress`; any other non-empty line is retained as recent output (so a
        non-zero exit can report the crash reason)."""
        if not buf:
            return
        line = buf.decode("utf-8", "replace")
        with self._lock:
            new_progress = parse_progress_line(line, self._progress)
            # A new dict means a recognized progress update, even when repeated
            # tqdm values are content-equal.
            if new_progress is not self._progress:
                self._progress = new_progress
            elif line.strip():
                self._recent_output.append(line.strip())

    # ── check (one observation) ────────────────────────────────────────────
    def check(self, emit: EventEmitter) -> MonitorStatus:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        if cfg.lada_cli_path:
            return self._check_managed(emit)
        return self._check_passive(emit)

    def _check_managed(self, emit: EventEmitter) -> MonitorStatus:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        if self._launch_error is not None:
            return MonitorStatus(state="error", detail=self._launch_error)
        if self._process is None:
            return MonitorStatus(state="error", detail="not started")
        retcode = self._process.poll()
        if retcode is None:
            return self._build_status("running")
        # exited — emit completion/failure once (the V2 events).
        detail = self._managed_exit_detail(retcode)
        if not self._done_emitted:
            self._done_emitted = True
            q = self._queue_str()
            if retcode == 0:
                emit("done", f"{cfg.name} complete",
                     f"Lada processing complete{(' | ' + q) if q else ''}"
                     f" | {datetime.now():%Y-%m-%d %H:%M:%S}")
            else:
                emit("alert", f"{cfg.name} failed", detail)
        return self._build_status("idle" if retcode == 0 else "error",
                                  detail=None if retcode == 0 else detail)

    def _managed_exit_detail(self, retcode: int) -> str:
        detail = f"Lada exited with code {retcode}"
        if retcode == 0:
            return detail
        with self._lock:
            tail = "\n".join(list(self._recent_output)[-_CRASH_DETAIL_LINES:]).strip()
        if not tail:
            return detail
        if len(tail) > _CRASH_DETAIL_CHARS:
            tail = tail[-_CRASH_DETAIL_CHARS:].lstrip()
        return f"{detail}\n{tail}"

    def _check_passive(self, emit: EventEmitter) -> MonitorStatus:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        running = process_alive(cfg.process_name or "lada-cli")
        if self._prev_running is True and not running:   # exited → completion
            q = self._queue_str()
            emit("done", f"{cfg.name} complete",
                 f"Lada processing complete{(' | ' + q) if q else ''}"
                 f" | {datetime.now():%Y-%m-%d %H:%M:%S}")
        self._prev_running = running
        return self._build_status("running" if running else "idle")

    # ── status assembly ────────────────────────────────────────────────────
    def _build_status(self, state: str, detail: Optional[str] = None) -> MonitorStatus:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        metrics: dict = {}
        # "Processing file X" (parsed progress + current-file detection) is only
        # meaningful while RUNNING — reporting it when idle/exited would tell the
        # UI/Hub a file is being processed when it isn't (Codex #59).
        if state == "running":
            with self._lock:
                metrics.update(self._progress)
            if "current_file" not in metrics:
                cf = self._detect_current_file()
                if cf:
                    metrics["current_file"] = cf
        # Queue counts (folder state) + CPU/GPU are facts at any state.
        completed, total = self._queue_counts()
        if total is not None:
            metrics["queue_completed"] = completed
            metrics["queue_total"] = total
            metrics["queue_remaining"] = max(0, total - completed)
        metrics.update(_cpu_mem())
        if cfg.lada_gpu_monitor:
            gpu = read_gpu()
            if gpu:
                metrics["gpu_pct"] = gpu["util_pct"]
                metrics["gpu_mem_used_mb"] = gpu["mem_used_mb"]
                metrics["gpu_mem_total_mb"] = gpu["mem_total_mb"]
        return MonitorStatus(state=state, detail=detail or self._detail(state, metrics),
                             metrics=metrics)

    def _detail(self, state: str, m: dict) -> str:
        # A clean one-line summary (the rich view is the UI metrics dashboard). Use
        # "·" separators and a bare "N/M done" — not "error | Queue 2/49 done".
        parts = []
        if m.get("current_file"):
            parts.append(f"{state}: {m['current_file']}")
        else:
            parts.append(state)
        if "percent" in m:
            parts.append(f"{m['percent']}%")
        if "fps" in m:
            parts.append(f"{m['fps']:.1f} fps")
        if m.get("eta"):
            parts.append(f"ETA {m['eta']}")
        if "queue_total" in m:
            parts.append(f"{m['queue_completed']}/{m['queue_total']} done")
        return " · ".join(parts)

    # ── snapshot / queue (V2:1021-1269) ────────────────────────────────────
    def _reconcile_snapshot(self) -> None:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        if not self._inputs or not cfg.lada_input_folder:
            return
        try:
            cur = {f.name for f in Path(cfg.lada_input_folder).iterdir()
                   if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS}
        except OSError:
            return
        out = _count_videos(cfg.lada_output_folder) or 0
        out = min(out, len(self._inputs))
        processed = self._inputs[:out]
        pending = [f for f in self._inputs[out:] if f in cur]
        self._inputs = processed + pending

    def _detect_current_file(self) -> Optional[str]:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        if not self._inputs:
            self._inputs = _list_videos(cfg.lada_input_folder)
            if not self._inputs:
                return None
        self._reconcile_snapshot()
        out = _count_videos(cfg.lada_output_folder) or 0
        idx = min(out, len(self._inputs) - 1)
        if idx < 0:
            return None
        return self._inputs[idx]

    def _queue_counts(self) -> tuple[int, Optional[int]]:
        cfg: LadaConfig = self.config  # type: ignore[assignment]
        if not cfg.lada_input_folder or not cfg.lada_output_folder:
            return 0, None
        out = _count_videos(cfg.lada_output_folder) or 0
        total = len(self._inputs) or (_count_videos(cfg.lada_input_folder) or 0)
        if total == 0:
            return 0, None
        return min(out, total), total

    def _queue_str(self) -> str:
        completed, total = self._queue_counts()
        if total is None:
            return ""
        return f"Queue: {completed}/{total} done ({max(0, total - completed)} left)"


class LadaPlugin(MonitorPlugin):
    type_id = "lada"
    display_name = "Lada (video restore)"
    category = "task"
    config_version = 1

    @classmethod
    def config_model(cls) -> type[BaseMonitorConfig]:
        return LadaConfig

    @classmethod
    def ui_schema(cls) -> dict:
        # Lead with the fields that matter; path fields are flagged for the
        # file/folder picker widget (#71). Help text comes from the field
        # descriptions (rjsf renders them) — not a ui:help key.
        return {
            "ui:order": [
                "name", "lada_cli_path", "lada_input_folder", "lada_output_folder",
                "process_name", "lada_extra_args", "lada_gpu_monitor",
                "lada_capture_progress", "poll_interval", "timeout", "*",
            ],
            "lada_cli_path": {"ui:options": {"taskpawPath": "file"}},
            "lada_input_folder": {"ui:options": {"taskpawPath": "directory"}},
            "lada_output_folder": {"ui:options": {"taskpawPath": "directory"}},
        }

    def manual_start(self, config: BaseMonitorConfig) -> bool:
        # Managed Lada (a CLI path) LAUNCHES lada-cli on start, so add it STOPPED
        # and let the operator click Start (V2 parity — don't begin video
        # processing the instant the form is saved). Passive Lada (no CLI path)
        # only watches an external process, so auto-start on add is fine.
        return bool(getattr(config, "lada_cli_path", "").strip())

    def create(self, instance_id: str, config: BaseMonitorConfig) -> MonitorInstance:
        return LadaInstance(instance_id, config)  # type: ignore[arg-type]
