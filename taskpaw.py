"""
TaskPaw - Local AI Workflow Monitor
Monitors Lada / ComfyUI / download tasks etc., reports events via built-in HTTP API.
Windows system tray app with background running support.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
import subprocess
import uuid
import logging
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import shlex
import socket

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is required for fast CPU/RAM stats
    psutil = None

# ── GUI ──────────────────────────────────────────────
# Guarded so the module can be imported headless (tests/CI/service) where Tk is
# absent. The GUI only runs under `if __name__ == "__main__"`; tk is not used at
# module/class-definition level (annotations are strings via __future__).
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog, scrolledtext
except ImportError:  # pragma: no cover - headless import (no display/Tk)
    tk = None
    ttk = messagebox = filedialog = scrolledtext = None

# =====================================================
# Constants & Paths
# =====================================================

APP_NAME = "TaskPaw"
APP_VERSION = "2.7.1"
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "TaskPaw"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "taskpaw.log"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# Logging
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, encoding="utf-8", maxBytes=10_000_000, backupCount=5),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(APP_NAME)

# =====================================================
# Data Models
# =====================================================

class WatcherType(str, Enum):
    LADA = "lada"
    COMFYUI = "comfyui"
    FOLDER = "folder"  # Folder / download monitoring
    PROCESS = "process"  # Generic process monitoring
    CUSTOM_CMD = "custom_cmd"  # Custom command monitoring


WATCHER_LABELS = {
    WatcherType.LADA: "Lada (Process Monitor)",
    WatcherType.COMFYUI: "ComfyUI (Queue Monitor)",
    WatcherType.FOLDER: "Folder Monitor (Downloads etc.)",
    WatcherType.PROCESS: "Process Monitor (Generic)",
    WatcherType.CUSTOM_CMD: "Custom Command",
}


@dataclass
class WatcherConfig:
    id: str = ""
    name: str = ""
    watcher_type: str = "lada"
    enabled: bool = True
    task_description: str = ""  # What is currently being done, e.g. "Batch processing Q3 videos"
    # Lada / Process
    process_name: str = "lada-cli"
    lada_cli_path: str = ""       # Path to lada-cli.exe (empty = passive monitor only)
    lada_input_folder: str = ""   # Folder where videos are queued for Lada
    lada_output_folder: str = ""  # Folder where Lada writes processed videos
    lada_extra_args: str = ""     # Extra CLI arguments (e.g. --device cuda:1 --encoder h264_nvenc)
    lada_gpu_monitor: bool = True # Monitor GPU usage via nvidia-smi
    # When False (default): lada-cli runs in its own visible Windows CMD
    #   console (CREATE_NEW_CONSOLE). The native tqdm progress bar is
    #   visible there. TaskPaw's UI shows filename + queue + CPU/GPU
    #   only — it has no way to read lada's output in this mode.
    # When True (experimental): TaskPaw pipes lada's stdout/stderr,
    #   parses tqdm progress (percent, fps, ETA), and shows a Tk-based
    #   "Lada Output" window mirroring the raw stream. Caveat: many
    #   pyinstaller-windowed Python builds and tqdm itself misbehave
    #   when stderr isn't a TTY — you may see no output at all. If the
    #   output window stays blank, set this back to False.
    lada_capture_progress: bool = False
    # How often (seconds) to log a periodic status line to the activity
    # log while a watcher is running. 0 disables. Default 30s.
    lada_log_interval: int = 30
    # ComfyUI
    comfyui_host: str = "127.0.0.1"
    comfyui_port: int = 8188
    comfyui_log_path: str = ""  # Path to ComfyUI log file for error diagnostics
    idle_confirm_count: int = 3
    # Folder watcher
    watch_folder: str = ""
    stable_seconds: int = 30  # File considered complete after N seconds with no size change
    file_extensions: str = ""  # Comma-separated, leave empty to monitor all
    # Custom command
    custom_command: str = ""
    poll_interval: int = 10
    # Notification template
    notify_template: str = ""  # Leave empty for default template
    # Runtime state (not persisted)
    _running: bool = field(default=False, repr=False)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:8]


@dataclass
class AppConfig:
    watchers: list = field(default_factory=list)
    machine_name: str = ""  # Machine alias, e.g. "BlackGoldPig"
    start_minimized: bool = False
    auto_start: bool = False
    api_port: int = 5678
    # Optional bearer token. When non-empty, /status and /events require
    # Authorization: Bearer <token>. When empty (default), API is open
    # — same as the original behavior, suitable for trusted LANs / Tailscale.
    api_token: str = ""

    def to_dict(self):
        d = asdict(self)
        # Remove runtime fields
        for w in d.get("watchers", []):
            w.pop("_running", None)
        return d

    @classmethod
    def from_dict(cls, d):
        # Filter unknown keys so a config from a future/past version
        # with extra fields doesn't blow up dataclass construction
        # (Codex finding #8 — "Config loading is brittle for migrations").
        known_watcher_fields = {f.name for f in WatcherConfig.__dataclass_fields__.values()}
        watchers = []
        for w in d.get("watchers", []):
            unknown = set(w.keys()) - known_watcher_fields
            if unknown:
                log.info(f"Ignoring unknown watcher config keys: {sorted(unknown)}")
            filtered = {k: v for k, v in w.items() if k in known_watcher_fields}
            try:
                watchers.append(WatcherConfig(**filtered))
            except Exception as e:
                log.warning(f"Skipping invalid watcher config: {e}")
        return cls(
            watchers=watchers,
            machine_name=d.get("machine_name", ""),
            start_minimized=d.get("start_minimized", False),
            auto_start=d.get("auto_start", False),
            api_port=d.get("api_port", 5678),
            api_token=d.get("api_token", ""),
        )


def load_config() -> AppConfig:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return AppConfig.from_dict(json.load(f))
        except Exception as e:
            log.warning(f"Failed to load config, using defaults: {e}")
    return AppConfig()


def save_config(cfg: AppConfig):
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# =====================================================
# Events Queue (Thread-safe)
# =====================================================
#
# The Hub (taskpaw_hub.py) polls /events and dedupes by monotonic id —
# it expects each event to carry an integer "id" that increases over
# time, and a JSON envelope shaped {"events": [...]}. Previously this
# module produced events without ids and returned a raw list, so Hub
# silently dropped every event ("AttributeError: 'list' object has no
# attribute 'get'" caught at taskpaw_hub.py line 510 and ignored).
# That meant Lada/ComfyUI/folder completions never reached OpenClaw.
#
# We persist the next-id counter to STATE_FILE so it monotonically
# increases across app restarts. If macsubs.py or other components
# also restart, Hub's last_event_ids per server keeps them straight.

STATE_FILE = CONFIG_DIR / "state.json"

_events_lock = threading.Lock()
_events_queue: list[dict] = []
_next_event_id: int = 1
_app_start_time = datetime.now()

# Safety cap on the in-memory queue. With clear-on-ack the queue retains events
# until the Hub acks them, so a Hub that polls but never advances its ack (e.g.
# its DB persist keeps failing) could otherwise grow it without bound. The cap
# is far above any normal backlog; when exceeded we drop the OLDEST events (most
# likely already delivered-but-unacked / stale) and log loudly.
MAX_EVENTS_QUEUE = 10000


def _load_event_state():
    """Load the next event id from disk on startup so ids keep growing
    across app restarts (Hub dedupe relies on this)."""
    global _next_event_id
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _next_event_id = max(1, int(data.get("next_event_id", 1)))
        except Exception as e:
            log.warning(f"Failed to load event state, starting at 1: {e}")
            _next_event_id = 1


def _save_event_state():
    """Persist the next event id atomically so a crash mid-write doesn't
    corrupt the counter."""
    try:
        tmp = STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"next_event_id": _next_event_id}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.debug(f"Failed to persist event state: {e}")


def add_event(
    machine: str,
    monitor: str,
    message: str,
    level: Optional[str] = None,
    title: Optional[str] = None,
    data: Optional[dict] = None,
):
    """Add an event to the thread-safe queue with a monotonic id."""
    global _next_event_id
    if level is not None and level not in {"info", "warn", "alert", "done"}:
        raise ValueError("level must be one of: info, warn, alert, done")
    if data is not None and not isinstance(data, dict):
        raise ValueError("data must be a dict when provided")

    with _events_lock:
        evt = {
            "id": _next_event_id,
            "time": datetime.now().isoformat(),
            "machine": machine,
            "monitor": monitor,
            "message": message,
        }
        if level is not None:
            evt["level"] = level
        if title is not None:
            evt["title"] = title
        if data is not None:
            evt["data"] = dict(data)
        _events_queue.append(evt)
        _next_event_id += 1
        # Persist the counter INSIDE the lock, on purpose: the id must be durable
        # before the event becomes visible to a Hub poll (which also takes
        # _events_lock). Persisting after releasing would reopen a crash window
        # where a polled+acked id is reused after restart and then deduped away.
        # add_event is low-frequency (one call per monitor event), so the small
        # atomic write under the lock is negligible — correctness over micro-perf.
        _save_event_state()
        overflow = len(_events_queue) - MAX_EVENTS_QUEUE
        if overflow > 0:
            del _events_queue[:overflow]
            log.error(
                "Event queue exceeded %d (Hub not acking?); dropped %d oldest event(s)",
                MAX_EVENTS_QUEUE,
                overflow,
            )


def get_and_clear_events() -> list[dict]:
    """Get all events and clear the queue."""
    with _events_lock:
        events = list(_events_queue)
        _events_queue.clear()
        return events


def get_events_after_ack(ack_id: int) -> list[dict]:
    """Trim events acknowledged by the Hub and return the remaining queue."""
    with _events_lock:
        _events_queue[:] = [
            event for event in _events_queue if int(event.get("id", -1)) > ack_id
        ]
        return list(_events_queue)


def get_events_payload(ack_id: Optional[int] = None) -> dict:
    """Build the /events response payload."""
    if ack_id is None:
        events = get_and_clear_events()
    else:
        events = get_events_after_ack(ack_id)
    return {"events": events}


# Load persisted state at import time so the first event after launch
# already has the right id.
_load_event_state()


# =====================================================
# HTTP API Server
# =====================================================

class APIRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the API server."""

    # Class variable to store reference to app
    app_instance = None

    def do_GET(self):
        """Handle GET requests."""
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/status":
            if not self._check_auth():
                return
            self._handle_status()
        elif parsed.path == "/events":
            if not self._check_auth():
                return
            self._handle_events(parsed.query)
        elif parsed.path == "/ping":
            # /ping intentionally does NOT require auth — it's a trivial
            # reachability probe and the response carries no sensitive data.
            self._handle_ping()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())

    def _check_auth(self) -> bool:
        """Validate the Authorization header against the configured token.

        Empty config token means auth is disabled (default). When set, the
        client must send `Authorization: Bearer <token>`. On failure we
        send 401 and return False; callers must abort. Crucially, on a
        failed auth we do NOT touch the events queue, so an attacker
        can't drain pending notifications by spamming unauthenticated
        polls.
        """
        token = (self.app_instance.config.api_token
                 if self.app_instance else "").strip()
        if not token:
            return True  # auth disabled

        sent = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if sent == expected:
            return True

        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", 'Bearer realm="TaskPaw"')
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
        return False

    def _handle_ping(self):
        """Health check endpoint."""
        machine = self.app_instance.config.machine_name if self.app_instance else "Unknown"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "ok": True,
            "machine": machine,
        }).encode())

    def _handle_status(self):
        """Return current status of all monitors."""
        if not self.app_instance:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "App not ready"}).encode())
            return

        uptime_seconds = int((datetime.now() - _app_start_time).total_seconds())
        # One snapshot under the lock, then iterate over the copy. Avoids
        # holding the lock across the wfile.write() below and prevents
        # "dictionary changed size during iteration" with the UI thread.
        status_snapshot = self.app_instance.snapshot_watcher_status()
        monitors = []
        for wcfg in self.app_instance.config.watchers:
            status = status_snapshot.get(wcfg.id, "Stopped")
            monitors.append({
                "name": wcfg.name,
                "type": wcfg.watcher_type,
                "status": status,
                "enabled": wcfg.enabled,
            })

        response = {
            "machine": self.app_instance.config.machine_name or "Unnamed",
            "uptime_seconds": uptime_seconds,
            "api_version": APP_VERSION,
            "monitors": monitors,
        }

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def _handle_events(self, query: str = ""):
        """Return new events.

        Response shape MUST be {"events": [...]} — Hub does
        json.loads(...).get("events", []) and would silently drop a
        bare list (AttributeError caught and swallowed there). Each
        event carries a monotonic "id" so Hub can dedupe across polls.

        When ack is absent, preserve the legacy clear-on-read behavior
        for un-upgraded Hubs. When ack is present, trim only events at
        or below the acknowledged id, then return the rest without
        clearing them.
        """
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        if "ack" in params:
            try:
                ack = int(params["ack"][-1])
            except (TypeError, ValueError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid ack"}).encode())
                return
            payload = get_events_payload(ack)
        else:
            payload = get_events_payload()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        pass


class APIServer:
    """Simple HTTP API server running in a daemon thread."""

    def __init__(self, port: int = 5678, app_instance=None):
        self.port = port
        self.server = None
        self.thread = None
        self.running = False
        APIRequestHandler.app_instance = app_instance

    def start(self):
        """Start the HTTP server in a daemon thread."""
        try:
            # ThreadingHTTPServer handles each request in its own thread, so
            # a slow /status read from the Hub can't stall the watcher loop or
            # the next request behind it. (Was: single-threaded HTTPServer +
            # busy handle_request() poll, which was the dominant freeze cause.)
            self.server = ThreadingHTTPServer(("0.0.0.0", self.port), APIRequestHandler)
            self.server.daemon_threads = True  # don't block process exit
            self.running = True
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                name="taskpaw-api",
                daemon=True,
            )
            self.thread.start()
            log.info(f"API server started on port {self.port}")
        except Exception as e:
            log.error(f"Failed to start API server: {e}")
            self.running = False

    def stop(self):
        """Stop the HTTP server cleanly."""
        self.running = False
        if self.server:
            # serve_forever() blocks in its thread; shutdown() unblocks it.
            # Must be called from a *different* thread than serve_forever,
            # which is the case here (we're on the main/UI thread).
            try:
                self.server.shutdown()
            except Exception as e:
                log.debug(f"server.shutdown() failed: {e}")
            try:
                self.server.server_close()
            except Exception as e:
                log.debug(f"server.server_close() failed: {e}")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        log.info("API server stopped")

    @property
    def is_running(self):
        return self.running and self.thread and self.thread.is_alive()


# =====================================================
# Watchers
# =====================================================

class BaseWatcher(threading.Thread):
    """Base class for all Watchers"""

    def __init__(self, watcher_cfg: WatcherConfig,
                 machine_name: str = "",
                 on_log=None, on_status_change=None, on_event=None,
                 on_raw_output=None):
        super().__init__(daemon=True)
        self.cfg = watcher_cfg
        self.machine_name = machine_name or "Unnamed Machine"
        self._stop_event = threading.Event()
        self._on_log = on_log or (lambda msg: None)
        self._on_status = on_status_change or (lambda wid, status: None)
        self._on_event = on_event or (lambda machine, monitor, msg: None)
        # Optional: for watchers that pipe a child process' stdout/stderr,
        # this callback receives each decoded chunk + its terminator
        # ('\r' / '\n' / '') so a viewer (e.g. LadaOutputWindow) can mirror
        # the raw stream. Currently used only by LadaWatcher.
        self._on_raw_output = on_raw_output

    def stop(self):
        self._stop_event.set()

    @property
    def stopped(self):
        return self._stop_event.is_set()

    def log(self, msg):
        full = f"[{self.cfg.name}] {msg}"
        log.info(full)
        self._on_log(full)

    def _format_header(self) -> str:
        """Build notification message header: machine alias + task description"""
        header = f"Machine: {self.machine_name}"
        if self.cfg.task_description.strip():
            header += f" | Task: {self.cfg.task_description}"
        return header

    def notify(self, message):
        template = self.cfg.notify_template.strip()
        if template:
            message = template.replace("{message}", message).replace(
                "{time}", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ).replace("{name}", self.cfg.name).replace(
                "{machine}", self.machine_name
            ).replace("{task}", self.cfg.task_description or "")
        else:
            # Default: prepend machine header to message
            message = f"{self._format_header()}\n{message}"

        # Add event to queue instead of sending to OpenClaw
        add_event(self.machine_name, self.cfg.name, message)
        self._on_event(self.machine_name, self.cfg.name, message)
        self._on_status(self.cfg.id, "Notified")

    # ── Shared system info methods ──

    def _get_cpu_memory(self) -> Optional[str]:
        """Get system CPU usage and memory usage.

        Uses psutil (sub-millisecond, cross-platform) instead of wmic, which
        is deprecated on Windows 11 and routinely hangs for seconds — the
        dominant cause of the watcher-thread stalls that made TaskPaw appear
        to freeze.
        """
        if psutil is None:
            # psutil not installed — fail soft, don't fall back to wmic.
            return None
        try:
            # interval=None returns the value since the last call without
            # blocking. The first call returns 0.0, but every subsequent
            # poll reflects real usage.
            cpu_pct = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            mem_used_gb = (mem.total - mem.available) / (1024 ** 3)
            mem_total_gb = mem.total / (1024 ** 3)
            return (f"CPU {cpu_pct:.0f}% | "
                    f"RAM {mem_used_gb:.1f}/{mem_total_gb:.1f}GB")
        except Exception as e:
            self.log(f"CPU/Memory query error: {e}")
            return None

    def _get_gpu_info(self) -> Optional[str]:
        """Get GPU utilization and VRAM usage via nvidia-smi."""
        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if result.returncode == 0:
                line = result.stdout.strip().split("\n")[0]
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 3:
                    gpu_util = parts[0]
                    mem_used = int(parts[1])
                    mem_total = int(parts[2])
                    mem_gb = f"{mem_used / 1024:.1f}/{mem_total / 1024:.1f}GB"
                    return f"GPU {gpu_util}% | VRAM {mem_gb}"
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"GPU query error: {e}")
        return None


class LadaWatcher(BaseWatcher):
    """
    Lada monitor with two modes:
    - Managed mode (lada_cli_path set): TaskPaw launches lada-cli, captures stderr
      for real-time progress (filename, %, fps, ETA), manages process lifecycle.
    - Passive mode (lada_cli_path empty): detects running lada process externally.

    Both modes show: CPU, memory, GPU/VRAM usage, file queue counting.
    """

    VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".ts", ".m4v"}

    # ── Regex patterns for parsing lada-cli progress output ──────────
    # Sample input from lada-cli (Chinese tqdm-style):
    #   MIDE-852-C.mp4:
    #   正在处理视频： 27%|█████▉    |已处理： 26:58 (84163帧) | 剩余： 1:45:32 (230599帧) | 速度：36.4 帧/秒
    # Filenames appear on their own line ending in ':' (or full-width '：').
    _RE_FILENAME = re.compile(
        r'^(.+\.(?:mp4|mkv|avi|mov|wmv|flv|webm|ts|m4v))\s*[:：]\s*$',
        re.IGNORECASE,
    )
    _RE_PCT = re.compile(r'(\d+)\s*%')
    _RE_ELAPSED = re.compile(r'已处理[:：]\s*([\d:]+)\s*\((\d+)\s*帧\)')
    _RE_REMAINING = re.compile(r'剩余[:：]\s*([\d:]+)\s*\((\d+)\s*帧\)')
    _RE_FPS = re.compile(r'速度[:：]\s*([\d.]+)')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._process: Optional[subprocess.Popen] = None
        self._progress: dict = {}
        self._progress_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._last_logged_file: Optional[str] = None
        # Snapshot of the input folder taken when the watcher starts.
        # Used by _detect_current_file() to identify which file lada-cli is
        # working on by indexing this list with the count of files already
        # written to the output folder. This is robust to lada renaming
        # outputs (suffix like "_restored") and to the user manually
        # cleaning processed files out of the input folder mid-run — both
        # of which broke the previous stem-match heuristic and caused the
        # "queue of 20 always shows file #1" bug. Kept in sync with user
        # removals by _reconcile_snapshot().
        self._initial_inputs: list = []

    def run(self):
        if self.cfg.lada_cli_path:
            self._run_managed()
        else:
            self._run_passive()

    # ── Managed mode ──────────────────────────────────

    def _run_managed(self):
        """Launch lada-cli and monitor it.

        Two display modes (chosen via cfg.lada_capture_progress):

          - False (default): CREATE_NEW_CONSOLE. lada-cli gets its own
            visible Windows CMD window with the native tqdm progress
            bar. TaskPaw can NOT see lada's output in this mode (Windows
            won't let us both pipe and show a console for the same
            process), so TaskPaw's status line shows only filename +
            queue + CPU/GPU. This is the safe default.

          - True (experimental): CREATE_NO_WINDOW + PIPE. TaskPaw
            captures stdout/stderr, parses tqdm output for percent /
            fps / ETA, and forwards each chunk to a Tk-based
            "Lada Output" window via on_raw_output. The catch: many
            programs (pyinstaller-windowed Python apps especially)
            write nothing useful when stdout/stderr aren't a real
            console. If the output window stays empty, this mode just
            doesn't work for that build of lada-cli — set it back to
            False.
        """
        capture = bool(self.cfg.lada_capture_progress)
        mode_label = "managed, capture progress" if capture else "managed, separate console"
        self.log(f"Starting Lada ({mode_label})")
        self._process = None
        self._last_logged_file = None
        self._reader_thread = None
        with self._progress_lock:
            self._progress = {}

        # Snapshot input list NOW — before lada starts and possibly
        # mutates the folder. Used by _detect_current_file() in both modes.
        self._initial_inputs = self._list_input_files()
        if self._initial_inputs:
            self.log(f"Found {len(self._initial_inputs)} input file(s) in queue")

        # Build command
        cmd = [self.cfg.lada_cli_path]
        if self.cfg.lada_input_folder:
            cmd.extend(["--input", self.cfg.lada_input_folder])
        if self.cfg.lada_output_folder:
            cmd.extend(["--output", self.cfg.lada_output_folder])
        if self.cfg.lada_extra_args:
            try:
                cmd.extend(shlex.split(self.cfg.lada_extra_args))
            except ValueError:
                cmd.extend(self.cfg.lada_extra_args.split())

        self.log(f"Launching: {' '.join(cmd)}")
        self._on_status(self.cfg.id, "Starting Lada...")

        try:
            if sys.platform == "win32":
                if capture:
                    creation_flags = subprocess.CREATE_NO_WINDOW
                    popen_kwargs = dict(
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=0,
                    )
                else:
                    creation_flags = subprocess.CREATE_NEW_CONSOLE
                    popen_kwargs = {}
            else:
                creation_flags = 0
                popen_kwargs = (
                    dict(stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, bufsize=0)
                    if capture else {}
                )
            self._process = subprocess.Popen(
                cmd, creationflags=creation_flags, **popen_kwargs
            )
        except FileNotFoundError:
            self.log(f"ERROR: lada-cli not found at {self.cfg.lada_cli_path}")
            self._on_status(self.cfg.id, "ERROR: lada-cli not found")
            self.notify(f"Lada ERROR on {self.cfg.name}: lada-cli not found at {self.cfg.lada_cli_path}")
            return
        except Exception as e:
            self.log(f"ERROR: Failed to launch Lada: {e}")
            self._on_status(self.cfg.id, f"ERROR: {e}")
            return

        self.log(f"Lada process started (PID: {self._process.pid})")
        self._last_output_snapshot = self._snapshot_output_folder()

        # Reader thread only runs in capture mode (when we actually have a
        # pipe). In native-console mode there's nothing to read.
        if capture and self._process.stdout is not None:
            self._reader_thread = threading.Thread(
                target=self._progress_reader_loop,
                name=f"lada-reader-{self.cfg.id}",
                daemon=True,
            )
            self._reader_thread.start()

        # Monitor loop
        last_periodic_log = time.monotonic()
        log_interval = max(0, int(self.cfg.lada_log_interval))

        while not self.stopped:
            retcode = self._process.poll()

            if retcode is not None:
                # Process exited
                if retcode == 0:
                    self.log("Lada processing complete")
                    queue_info = self._get_queue_info()
                    queue_str = f" | {queue_info}" if queue_info else ""
                    self.notify(
                        f"Lada processing complete | "
                        f"Task: {self.cfg.name}{queue_str} | "
                        f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}"
                    )
                    self._on_status(self.cfg.id, f"Idle{queue_str}")
                else:
                    self.log(f"Lada exited with error code {retcode}")
                    self._on_status(self.cfg.id, f"ERROR: Exit code {retcode}")
                    self.notify(f"Lada ERROR on {self.cfg.name}: Exit code {retcode}")
                self._process = None
                break

            status_parts = self._build_managed_status_parts()

            # Check for newly completed files (still useful for the activity log)
            new_snapshot = self._snapshot_output_folder()
            newly_done = new_snapshot - self._last_output_snapshot
            if newly_done:
                for f in newly_done:
                    self.log(f"Completed: {f}")
                self._last_output_snapshot = new_snapshot

            status_line = " | ".join(status_parts)
            self._on_status(self.cfg.id, status_line)

            # Periodic progress log: keeps the activity log alive while
            # processing so the user sees ongoing state, not just the
            # one-off "Processing: <file>" line.
            if log_interval > 0 and (time.monotonic() - last_periodic_log) >= log_interval:
                self.log(f"Status: {status_line}")
                last_periodic_log = time.monotonic()

            self._stop_event.wait(self.cfg.poll_interval)

        # User clicked Stop while process is running
        if self._process and self._process.poll() is None:
            self.log("Terminating Lada process (user stop)")
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self.log(f"Force-killed Lada process {self._process.pid}")
            self._on_status(self.cfg.id, "Stopped")
            self._process = None

        # Drain reader thread (it exits on EOF when the pipe closes).
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)
            if self._reader_thread.is_alive():
                log.debug("Lada reader thread did not exit within 2s")

        self.log("Lada monitor stopped")

    def _build_managed_status_parts(self) -> list:
        """Combine real-time progress (parsed from lada-cli output) with queue
        and system stats. Falls back to the folder-based file detector if
        lada-cli hasn't produced parseable output yet."""
        parts = []
        with self._progress_lock:
            pg = dict(self._progress)

        # Reconcile snapshot before reading queue stats so user-initiated
        # removals from the input folder are reflected in this cycle's total.
        self._reconcile_snapshot()

        current_file = pg.get("current_file") or self._detect_current_file()
        if current_file:
            parts.append(f"Running: {current_file}")
            if current_file != self._last_logged_file:
                self.log(f"Processing: {current_file}")
                self._last_logged_file = current_file
        else:
            parts.append("Running")

        pct = pg.get("percent")
        if pct is not None:
            parts.append(f"{pct}%")
        fps = pg.get("fps")
        if fps is not None:
            parts.append(f"{fps:.1f} fps")
        eta = pg.get("eta")
        if eta:
            parts.append(f"ETA {eta}")

        queue_info = self._get_queue_info()
        if queue_info:
            parts.append(queue_info)

        cpu_mem = self._get_cpu_memory()
        if cpu_mem:
            parts.append(cpu_mem)

        if self.cfg.lada_gpu_monitor:
            gpu_info = self._get_gpu_info()
            if gpu_info:
                parts.append(gpu_info)

        return parts

    def _progress_reader_loop(self):
        """Read merged stdout/stderr from lada-cli, parse tqdm progress, and
        mirror each chunk to the output window.

        tqdm uses '\\r' (carriage return) to update its progress bar in place
        instead of writing '\\n' each tick. A standard text-mode iterator
        only yields a "line" on '\\n', so it would only see updates between
        files. We read raw bytes and split on either delimiter so each
        progress refresh is captured. The terminator (\\r or \\n) is
        forwarded to the viewer so it can replace-vs-append correctly.
        """
        proc = self._process
        if not proc or not proc.stdout:
            return

        buf = bytearray()

        def emit(terminator: str):
            text = buf.decode("utf-8", errors="replace") if buf else ""
            buf.clear()
            if text or terminator:
                # 1) feed the parser (status-line metrics)
                if text:
                    self._parse_progress_line(text)
                # 2) feed the visible output window (raw mirror)
                if self._on_raw_output:
                    try:
                        self._on_raw_output(text, terminator)
                    except Exception as e:
                        log.debug(f"on_raw_output callback failed: {e}")

        try:
            while not self.stopped:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break  # EOF — process has exited or pipe closed
                for b in chunk:
                    if b == 0x0a:  # '\n'
                        emit("\n")
                    elif b == 0x0d:  # '\r'
                        emit("\r")
                    else:
                        buf.append(b)
            # Flush trailing partial line (e.g. process exited without final \n)
            if buf:
                emit("")
        except (OSError, ValueError) as e:
            log.debug(f"Lada reader exiting: {e}")

    def _parse_progress_line(self, line: str):
        """Update self._progress from one decoded line of lada-cli output."""
        line = line.strip()
        if not line:
            return

        # Filename header line: "MIDE-852-C.mp4:"
        m = self._RE_FILENAME.match(line)
        if m:
            new_file = m.group(1)
            with self._progress_lock:
                # Reset progress when the file changes so stale percent/fps
                # from the previous file don't stick.
                if self._progress.get("current_file") != new_file:
                    self._progress = {"current_file": new_file}
            return

        # Progress line — must contain a percentage to be parseable.
        m_pct = self._RE_PCT.search(line)
        if not m_pct:
            return

        update: dict = {"percent": int(m_pct.group(1))}
        if m := self._RE_ELAPSED.search(line):
            update["elapsed"] = m.group(1)
            try:
                update["processed_frames"] = int(m.group(2))
            except ValueError:
                pass
        if m := self._RE_REMAINING.search(line):
            update["eta"] = m.group(1)
            try:
                update["remaining_frames"] = int(m.group(2))
            except ValueError:
                pass
        if m := self._RE_FPS.search(line):
            try:
                update["fps"] = float(m.group(1))
            except ValueError:
                pass

        with self._progress_lock:
            self._progress.update(update)

    def _snapshot_output_folder(self) -> set:
        """Get a set of video filenames currently in the output folder."""
        output_folder = self.cfg.lada_output_folder
        if not output_folder:
            return set()
        try:
            output_path = Path(output_folder)
            if output_path.exists():
                return {
                    f.name for f in output_path.iterdir()
                    if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
                }
        except Exception:
            pass
        return set()

    def _list_input_files(self) -> list:
        """Return the alphabetically-sorted list of video filenames currently
        in the configured input folder. Used once at watcher start to take
        a snapshot of what lada-cli will process, so we can identify the
        active file later without depending on output filename conventions.
        """
        input_folder = self.cfg.lada_input_folder
        if not input_folder:
            return []
        try:
            input_path = Path(input_folder)
            if not input_path.exists():
                return []
            return sorted(
                f.name for f in input_path.iterdir()
                if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
            )
        except Exception:
            return []

    def _reconcile_snapshot(self):
        """Drop pending entries from the input snapshot that the user has
        manually removed from the input folder mid-run.

        Context: lada-cli does not delete its own input files. Users
        sometimes clean up mid-run by moving processed videos out of the
        output folder and deleting the matching files from the input
        folder (to keep the two in sync). Without reconciling, the
        snapshot-based total stays stuck at the initial count and the
        displayed queue size doesn't shrink with the user's cleanup —
        the bug this method fixes.

        Assumes lada-cli processes files in the order returned by
        _list_input_files (alphabetical). Entries at indices
        [0, output_count) are treated as already processed and always
        preserved — they may have been moved out by the user as part of
        a cleanup. Entries beyond that index that are no longer in the
        input folder must have been removed by the user (lada hasn't
        reached them yet, and lada doesn't delete inputs on its own).
        """
        if not self._initial_inputs or not self.cfg.lada_input_folder:
            return

        input_folder = self.cfg.lada_input_folder
        try:
            input_path = Path(input_folder)
            if not input_path.exists():
                return
            current_input = {
                f.name for f in input_path.iterdir()
                if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
            }
        except Exception:
            return

        output_count = 0
        output_folder = self.cfg.lada_output_folder
        if output_folder:
            try:
                output_path = Path(output_folder)
                if output_path.exists():
                    output_count = sum(
                        1 for f in output_path.iterdir()
                        if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
                    )
            except Exception:
                return

        output_count = min(output_count, len(self._initial_inputs))
        processed = self._initial_inputs[:output_count]
        pending = [
            f for f in self._initial_inputs[output_count:]
            if f in current_input
        ]
        new_snapshot = processed + pending
        removed = len(self._initial_inputs) - len(new_snapshot)
        if removed > 0:
            self.log(
                f"Detected {removed} pending input file(s) removed by user; "
                f"queue total now {len(new_snapshot)}"
            )
            self._initial_inputs = new_snapshot

    def _detect_current_file(self) -> Optional[str]:
        """Identify which input file lada-cli is currently processing.

        Strategy: index the start-of-run input snapshot by the count of
        files in the output folder. Robust to:
          - lada renaming outputs (e.g., adding "_restored" suffix)
          - user cleanup of processed input/output files mid-run
            (_reconcile_snapshot keeps the snapshot in sync)
          - lada writing to a different folder layout

        The previous implementation matched input stems against output
        stems and returned the first input not present in the output —
        which silently broke whenever lada's output names didn't match,
        causing the "queue of 20 always shows file #1" bug.
        """
        if not self._initial_inputs:
            # Lazy snapshot if a watcher is somehow running without one
            self._initial_inputs = self._list_input_files()
            if not self._initial_inputs:
                return None

        output_folder = self.cfg.lada_output_folder
        output_count = 0
        if output_folder:
            try:
                output_path = Path(output_folder)
                if output_path.exists():
                    output_count = sum(
                        1 for f in output_path.iterdir()
                        if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
                    )
            except Exception:
                pass

        # Clamp to last index — once everything is done, keep showing the
        # last file rather than overflowing.
        idx = min(output_count, len(self._initial_inputs) - 1)
        return self._initial_inputs[idx]

    # ── Passive mode (original behavior) ──────────────

    def _run_passive(self):
        """Detect externally-running lada process, no process management."""
        self.log("Starting Lada monitor (passive mode)")
        self._on_status(self.cfg.id, "Listening")
        proc_name = self.cfg.process_name or "lada-cli"
        was_running = False

        while not self.stopped:
            is_running = self._check_process(proc_name)
            status_parts = []

            if is_running:
                if not was_running:
                    self.log(f"Detected {proc_name} started running")
                    was_running = True
                status_parts.append("Running")
            else:
                if was_running:
                    self.log(f"{proc_name} has exited, processing complete")
                    queue_info = self._get_queue_info()
                    queue_str = f" | {queue_info}" if queue_info else ""
                    self.notify(
                        f"Lada processing complete | "
                        f"Task: {self.cfg.name}{queue_str} | "
                        f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}"
                    )
                    was_running = False
                status_parts.append("Idle")

            queue_info = self._get_queue_info()
            if queue_info:
                status_parts.append(queue_info)

            cpu_mem = self._get_cpu_memory()
            if cpu_mem:
                status_parts.append(cpu_mem)

            if self.cfg.lada_gpu_monitor:
                gpu_info = self._get_gpu_info()
                if gpu_info:
                    status_parts.append(gpu_info)

            self._on_status(self.cfg.id, " | ".join(status_parts))
            self._stop_event.wait(self.cfg.poll_interval)

        self.log("Lada monitor stopped")

    # ── Shared helpers ────────────────────────────────

    def _check_process(self, name: str) -> bool:
        try:
            if psutil is not None:
                for proc in psutil.process_iter(['name']):
                    proc_name = proc.info.get('name')
                    if proc_name and proc_name.lower() == name.lower():
                        return True
                return False
            # Fallback without psutil
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split('","')
                    if len(parts) >= 2:
                        image_name = parts[0].replace('"', '').lower()
                        if image_name == name.lower():
                            return True
                return False
            else:
                result = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True, text=True, timeout=5,
                )
                return result.returncode == 0
        except Exception:
            return False

    def _get_queue_info(self) -> Optional[str]:
        """Count video files in input folder vs output folder.

        Total is taken from the input snapshot (kept in sync with user
        removals by _reconcile_snapshot), so the displayed total stays
        stable as users clean processed files out of the input folder
        mid-run — and shrinks only when they actually remove pending
        files. Without the snapshot, a naive live-count of input would
        produce nonsense like "Queue: 20/5 done" once processed files
        get moved out.
        """
        input_folder = self.cfg.lada_input_folder
        output_folder = self.cfg.lada_output_folder
        if not input_folder or not output_folder:
            return None

        try:
            input_path = Path(input_folder)
            output_path = Path(output_folder)

            output_count = 0
            if output_path.exists():
                output_count = sum(
                    1 for f in output_path.iterdir()
                    if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
                )

            # Prefer the start-of-run snapshot for the total
            total = len(self._initial_inputs)
            if total == 0:
                # Passive mode or pre-run: fall back to live count
                if not input_path.exists():
                    return None
                total = sum(
                    1 for f in input_path.iterdir()
                    if f.is_file() and f.suffix.lower() in self.VIDEO_EXTENSIONS
                )

            completed = min(output_count, total)
            remaining = max(0, total - completed)
            if total > 0:
                return f"Queue: {completed}/{total} done ({remaining} left)"
        except Exception as e:
            self.log(f"Queue count error: {e}")
        return None


class ComfyUIWatcher(BaseWatcher):
    """
    Monitors ComfyUI queue. When queue becomes empty (after being non-empty),
    triggers a notification. Detects stuck/errored jobs via:
    1. History API check when same prompt_id runs > STUCK_TIMEOUT_SECONDS
    2. Stalled queue detection (0 running, N pending = queue halted after error)
    3. Recent history scan for errored prompts
    4. Log file tailing for CUDA/OOM/RuntimeError patterns
    """

    STUCK_TIMEOUT_SECONDS = 1800  # 30 minutes
    STALL_DETECT_SECONDS = 60     # 0-running+N-pending for 60s = stalled
    LOG_TAIL_LINES = 50           # How many lines to read from end of log
    LOG_ERROR_PATTERNS = re.compile(
        r"(CUDA out of memory|RuntimeError|torch\.cuda\.OutOfMemoryError"
        r"|CUDA error|Traceback \(most recent|MemoryError"
        r"|allocation on device|out of memory)",
        re.IGNORECASE,
    )

    def run(self):
        self.log("Starting ComfyUI queue monitor")
        self._on_status(self.cfg.id, "Listening")
        idle_count = 0
        was_processing = False
        last_running_prompt_id = None
        running_since = None
        stall_since = None           # When we first saw 0-running + N-pending
        self._error_notified = False
        self._last_log_position = 0  # For incremental log reading

        while not self.stopped:
            try:
                running, pending, running_prompt_id = self._get_queue_detail()
                queue_size = running + pending if running is not None else None

                # Build system info suffix
                sys_parts = []
                cpu_mem = self._get_cpu_memory()
                if cpu_mem:
                    sys_parts.append(cpu_mem)
                gpu_info = self._get_gpu_info()
                if gpu_info:
                    sys_parts.append(gpu_info)
                sys_suffix = " | " + " | ".join(sys_parts) if sys_parts else ""

                # Check log file for errors (always, as supplementary info)
                log_error = self._tail_log_for_errors()

                if queue_size is None:
                    self._on_status(self.cfg.id, f"Connection error{sys_suffix}")
                    last_running_prompt_id = None
                    running_since = None
                    stall_since = None

                elif queue_size == 0:
                    last_running_prompt_id = None
                    running_since = None
                    stall_since = None
                    if was_processing:
                        if idle_count < self.cfg.idle_confirm_count:
                            idle_count += 1
                        else:
                            self.log("ComfyUI queue is empty, processing complete")
                            self.notify(
                                f"ComfyUI processing complete | "
                                f"Task: {self.cfg.name} | "
                                f"Completed: {datetime.now():%Y-%m-%d %H:%M:%S}"
                            )
                            was_processing = False
                            self._error_notified = False
                            idle_count = 0
                    else:
                        idle_count = 0
                    self._on_status(self.cfg.id, f"Listening{sys_suffix}")

                else:
                    # queue_size > 0
                    idle_count = 0
                    was_processing = True

                    # ── Case A: 0 running + N pending = queue stalled ──
                    if running == 0 and pending > 0:
                        if stall_since is None:
                            stall_since = time.time()
                        stall_elapsed = time.time() - stall_since

                        if stall_elapsed > self.STALL_DETECT_SECONDS:
                            # Queue halted — check recent history for the error
                            error_msg = self._check_recent_history_errors()
                            if log_error and not error_msg:
                                error_msg = log_error

                            if error_msg:
                                self.log(f"ComfyUI ERROR (queue halted): {error_msg}")
                                self._on_status(
                                    self.cfg.id,
                                    f"ERROR: {error_msg} | {pending} pending{sys_suffix}"
                                )
                                if not self._error_notified:
                                    self.notify(
                                        f"ComfyUI ERROR on {self.cfg.name}: {error_msg} | "
                                        f"Queue halted with {pending} pending"
                                    )
                                    self._error_notified = True
                            else:
                                minutes = int(stall_elapsed // 60)
                                self._on_status(
                                    self.cfg.id,
                                    f"STUCK? (queue halted {minutes}m, {pending} pending){sys_suffix}"
                                )
                                if not self._error_notified and stall_elapsed > self.STALL_DETECT_SECONDS * 5:
                                    self.notify(
                                        f"ComfyUI WARNING on {self.cfg.name}: "
                                        f"Queue halted for {minutes}m with {pending} pending"
                                    )
                                    self._error_notified = True
                            continue

                        # Brief stall, might be transitioning between prompts
                        self._on_status(
                            self.cfg.id,
                            f"Processing (waiting, {pending} pending){sys_suffix}"
                        )
                        continue

                    # ── Case B: actively running ──
                    stall_since = None  # Reset stall timer when something is running

                    if running_prompt_id and running_prompt_id != last_running_prompt_id:
                        # New prompt started — check if previous prompt errored
                        if last_running_prompt_id:
                            prev_error = self._check_history_error(last_running_prompt_id)
                            if prev_error:
                                self.log(f"ComfyUI previous prompt errored: {prev_error}")

                        last_running_prompt_id = running_prompt_id
                        running_since = time.time()
                        self._error_notified = False

                    elif running_prompt_id and running_since:
                        elapsed = time.time() - running_since
                        if elapsed > self.STUCK_TIMEOUT_SECONDS:
                            # Same prompt running too long — check history + log
                            error_msg = self._check_history_error(running_prompt_id)
                            if not error_msg and log_error:
                                error_msg = log_error

                            minutes = int(elapsed // 60)
                            if error_msg:
                                self.log(f"ComfyUI ERROR detected: {error_msg}")
                                self._on_status(
                                    self.cfg.id,
                                    f"ERROR: {error_msg} ({minutes}m) | {pending} pending{sys_suffix}"
                                )
                                if not self._error_notified:
                                    self.notify(
                                        f"ComfyUI ERROR on {self.cfg.name}: {error_msg} | "
                                        f"Queue stuck for {minutes}m with {pending} pending"
                                    )
                                    self._error_notified = True
                            else:
                                self.log(f"ComfyUI queue may be stuck ({minutes}m on same job)")
                                self._on_status(
                                    self.cfg.id,
                                    f"STUCK? ({minutes}m on same job, {pending} pending){sys_suffix}"
                                )
                            continue

                    else:
                        if not running_prompt_id and running > 0:
                            if running_since is None:
                                running_since = time.time()

                    self.log(f"ComfyUI queue: {running} running, {pending} pending")
                    self._on_status(self.cfg.id, f"Processing ({running} running, {pending} pending){sys_suffix}")

            except Exception as e:
                self.log(f"Error checking queue: {e}")
                self._on_status(self.cfg.id, "Error")

            self._stop_event.wait(self.cfg.poll_interval)

        self.log("ComfyUI monitor stopped")

    def _get_queue_detail(self) -> tuple:
        """Returns (running_count, pending_count, running_prompt_id) or (None, None, None)."""
        try:
            url = f"http://{self.cfg.comfyui_host}:{self.cfg.comfyui_port}/queue"
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode()
                data = json.loads(body)
                queue_running = data.get("queue_running", [])
                queue_pending = data.get("queue_pending", [])
                running_count = len(queue_running)
                pending_count = len(queue_pending)

                running_prompt_id = None
                if queue_running:
                    try:
                        running_prompt_id = str(queue_running[0][1])
                    except (IndexError, TypeError):
                        pass

                return running_count, pending_count, running_prompt_id
        except json.JSONDecodeError:
            self.log(f"ComfyUI /queue returned non-JSON: {body[:200]}")
            return None, None, None
        except Exception:
            return None, None, None

    def _check_history_error(self, prompt_id: str) -> Optional[str]:
        """Check ComfyUI /history for error on a specific prompt. Returns error message or None."""
        try:
            url = f"http://{self.cfg.comfyui_host}:{self.cfg.comfyui_port}/history/{prompt_id}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode()
                data = json.loads(body)
                return self._extract_error_from_history_entry(data.get(prompt_id))
        except json.JSONDecodeError:
            self.log(f"ComfyUI /history returned non-JSON: {body[:200]}")
            return None
        except Exception:
            return None

    def _check_recent_history_errors(self) -> Optional[str]:
        """Scan recent ComfyUI /history for any errored prompts. Returns first error or None."""
        try:
            url = f"http://{self.cfg.comfyui_host}:{self.cfg.comfyui_port}/history?max_items=5"
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode()
                data = json.loads(body)
                # History returns {prompt_id: {...}, ...} — check each for errors
                for pid, entry in data.items():
                    err = self._extract_error_from_history_entry(entry)
                    if err:
                        return err
        except json.JSONDecodeError:
            self.log(f"ComfyUI /history returned non-JSON: {body[:200]}")
            return None
        except Exception:
            return None

    def _extract_error_from_history_entry(self, entry) -> Optional[str]:
        """Extract error message from a single history entry dict. Returns error string or None."""
        if not entry or not isinstance(entry, dict):
            return None
        status_info = entry.get("status", {})
        completed = status_info.get("completed", True)
        status_str = status_info.get("status_str", "")

        if not completed or "error" in status_str.lower():
            messages = status_info.get("messages", [])
            for msg in messages:
                if isinstance(msg, list) and len(msg) >= 2:
                    if msg[0] == "execution_error":
                        error_detail = msg[1]
                        if isinstance(error_detail, dict):
                            exc_msg = error_detail.get("exception_message", "")
                            if exc_msg:
                                if len(exc_msg) > 80:
                                    exc_msg = exc_msg[:77] + "..."
                                return exc_msg
            return status_str or "Unknown error"
        return None

    def _tail_log_for_errors(self) -> Optional[str]:
        """Read ComfyUI log file tail for error patterns. Returns last matched error or None."""
        log_path = self.cfg.comfyui_log_path
        if not log_path:
            return None
        try:
            path = Path(log_path)
            if not path.exists() or not path.is_file():
                return None

            file_size = path.stat().st_size
            if file_size == 0:
                return None

            # Read last chunk of the file (approx last N lines)
            read_size = min(file_size, 8192)  # Read last 8KB
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if file_size > read_size:
                    f.seek(file_size - read_size)
                    f.readline()  # Skip partial first line
                lines = f.readlines()

            # Only check lines we haven't seen before (approximate via file size)
            if file_size <= self._last_log_position:
                return None  # No new content
            self._last_log_position = file_size

            # Search from bottom up for error patterns
            last_error = None
            for line in reversed(lines[-self.LOG_TAIL_LINES:]):
                match = self.LOG_ERROR_PATTERNS.search(line)
                if match:
                    # Get the error line, cleaned up
                    error_line = line.strip()
                    if len(error_line) > 80:
                        error_line = error_line[:77] + "..."
                    last_error = error_line
                    break

            return last_error
        except Exception:
            return None


class FolderWatcher(BaseWatcher):
    """
    Monitors a folder for new files. When a file stabilizes (no size change for N seconds),
    triggers a notification.
    """

    def run(self):
        self.log("Starting folder monitor")
        self._on_status(self.cfg.id, "Listening")
        folder = Path(self.cfg.watch_folder)

        if not folder.exists():
            self.log(f"Folder does not exist: {folder}")
            self._on_status(self.cfg.id, "Folder not found")
            return

        file_sizes = {}
        stable_count = {}

        while not self.stopped:
            try:
                exts = [e.strip() for e in self.cfg.file_extensions.split(",") if e.strip()]
                if exts:
                    files = [f for f in folder.iterdir() if f.is_file() and f.suffix in exts]
                else:
                    files = [f for f in folder.iterdir() if f.is_file()]

                current_files = {f.name: f.stat().st_size for f in files}

                # Check for new files and stabilized files
                for fname, size in current_files.items():
                    # Skip 0-byte files: a freshly-created placeholder or a
                    # failed/empty download would otherwise be tracked, see
                    # its size remain unchanged at 0, and trip the
                    # stable_count threshold — sending a false "complete"
                    # notification for a file that's actually broken.
                    if size == 0:
                        continue
                    if fname not in file_sizes:
                        # New file
                        file_sizes[fname] = size
                        stable_count[fname] = 0
                        self.log(f"New file detected: {fname}")
                        self._on_status(self.cfg.id, f"New file: {fname}")
                    elif file_sizes[fname] == size:
                        # File size unchanged
                        stable_count[fname] = stable_count.get(fname, 0) + 1
                        if stable_count[fname] >= self.cfg.stable_seconds:
                            if stable_count[fname] == self.cfg.stable_seconds:
                                # Just became stable
                                self.log(f"File stabilized: {fname}")
                                self.notify(
                                    f"✅ New file detected and complete\n"
                                    f"📁 File: {fname}\n"
                                    f"📊 Size: {size / (1024*1024):.2f} MB\n"
                                    f"🕐 Detected: {datetime.now():%Y-%m-%d %H:%M:%S}"
                                )
                    else:
                        # File size changed
                        file_sizes[fname] = size
                        stable_count[fname] = 0
                        self.log(f"File modified: {fname}")
                        self._on_status(self.cfg.id, f"Monitoring: {fname}")

                # Remove deleted files
                file_sizes = {f: s for f, s in file_sizes.items() if f in current_files}
                stable_count = {f: c for f, c in stable_count.items() if f in current_files}

                if not current_files:
                    self._on_status(self.cfg.id, "Listening")

            except Exception as e:
                self.log(f"Error monitoring folder: {e}")
                self._on_status(self.cfg.id, "Error")

            self._stop_event.wait(1)

        self.log("Folder monitor stopped")


class ProcessWatcher(BaseWatcher):
    """
    Generic process monitor: watches for a process and notifies when it starts/stops.
    """

    def run(self):
        self.log("Starting process monitor")
        self._on_status(self.cfg.id, "Listening")
        proc_name = self.cfg.process_name
        was_running = False

        while not self.stopped:
            is_running = self._check_process(proc_name)

            if is_running and not was_running:
                self.log(f"Process started: {proc_name}")
                self._on_status(self.cfg.id, "Running")
                self.notify(
                    f"✅ Process started\n"
                    f"📋 Process: {proc_name}\n"
                    f"🕐 Started: {datetime.now():%Y-%m-%d %H:%M:%S}"
                )
                was_running = True
            elif not is_running and was_running:
                self.log(f"Process exited: {proc_name}")
                self._on_status(self.cfg.id, "Listening")
                self.notify(
                    f"✅ Process exited\n"
                    f"📋 Process: {proc_name}\n"
                    f"🕐 Exited: {datetime.now():%Y-%m-%d %H:%M:%S}"
                )
                was_running = False

            self._stop_event.wait(self.cfg.poll_interval)

        self.log("Process monitor stopped")

    def _check_process(self, name: str) -> bool:
        try:
            if psutil is not None:
                for proc in psutil.process_iter(['name']):
                    proc_name = proc.info.get('name')
                    if proc_name and proc_name.lower() == name.lower():
                        return True
                return False
            # Fallback without psutil
            if sys.platform == "win32":
                result = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split('","')
                    if len(parts) >= 2:
                        image_name = parts[0].replace('"', '').lower()
                        if image_name == name.lower():
                            return True
                return False
            else:
                result = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True, text=True, timeout=5,
                )
                return result.returncode == 0
        except Exception:
            return False


class CustomCmdWatcher(BaseWatcher):
    """
    Runs a custom command periodically and notifies based on exit code.
    Exit code 0 = success, non-zero = failure.
    """

    def run(self):
        self.log("Starting custom command monitor")
        self._on_status(self.cfg.id, "Listening")

        while not self.stopped:
            try:
                cmd = shlex.split(self.cfg.custom_command)
                result = subprocess.run(
                    cmd,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )

                if result.returncode == 0:
                    self.log(f"Command succeeded")
                    self._on_status(self.cfg.id, "Success")
                    self.notify(
                        f"✅ Command completed successfully\n"
                        f"📋 Task: {self.cfg.name}\n"
                        f"📝 Output: {result.stdout[:200]}\n"
                        f"🕐 Completed: {datetime.now():%Y-%m-%d %H:%M:%S}"
                    )
                else:
                    self.log(f"Command failed with code {result.returncode}")
                    self._on_status(self.cfg.id, f"Failed ({result.returncode})")
                    self.notify(
                        f"❌ Command failed\n"
                        f"📋 Task: {self.cfg.name}\n"
                        f"Error: {result.stderr[:200]}\n"
                        f"🕐 Failed: {datetime.now():%Y-%m-%d %H:%M:%S}"
                    )

            except subprocess.TimeoutExpired:
                self.log(f"Command timed out")
                self._on_status(self.cfg.id, "Timeout")
                self.notify(
                    f"⏱️ Command timed out\n"
                    f"📋 Task: {self.cfg.name}\n"
                    f"🕐 Timeout: {datetime.now():%Y-%m-%d %H:%M:%S}"
                )
            except Exception as e:
                self.log(f"Error running command: {e}")
                self._on_status(self.cfg.id, "Error")

            self._stop_event.wait(self.cfg.poll_interval)

        self.log("Custom command monitor stopped")


WATCHER_CLASS_MAP = {
    WatcherType.LADA: LadaWatcher,
    WatcherType.COMFYUI: ComfyUIWatcher,
    WatcherType.FOLDER: FolderWatcher,
    WatcherType.PROCESS: ProcessWatcher,
    WatcherType.CUSTOM_CMD: CustomCmdWatcher,
}


# =====================================================
# GUI - Main Window
# =====================================================

# Light color theme: sky blue, grey, white
COLORS = {
    "bg": "#f0f4f8",
    "surface": "#ffffff",
    "surface2": "#e8edf2",
    "border": "#c8d1dc",
    "accent": "#4a9ede",
    "accent_light": "#6bb5f0",
    "success": "#2eaa6f",
    "warning": "#e6a817",
    "error": "#d94f3d",
    "stuck": "#FF8C00",
    "text": "#2d3748",
    "text_dim": "#718096",
    "text_bright": "#1a202c",
}


class LadaOutputWindow:
    """Tk Toplevel that mirrors lada-cli's raw output, including in-place
    tqdm progress bar updates (via carriage return).

    Why this exists: on Windows you can either give a child process its own
    visible CMD window (CREATE_NEW_CONSOLE) OR pipe its stdout/stderr to the
    parent — not both. Previously TaskPaw used the OS console, which meant
    it could not parse fps/percent/ETA out of lada's output. Now TaskPaw
    captures the output stream and renders it in this Tk window, which:
      - Looks like a console (dark bg, monospace, no wrap)
      - Honors '\r' refresh so the tqdm progress bar updates in place
      - Lets TaskPaw also parse the same stream for its status line
    Owned by TaskPawApp; closing the window only hides it (the watcher
    keeps running). A "Show output" button on the watcher row re-displays
    a hidden window.
    """

    MAX_LINES = 5000  # cap buffer to bound memory on long runs

    def __init__(self, parent: tk.Tk, watcher_name: str):
        self.win = tk.Toplevel(parent)
        self.win.title(f"Lada Output — {watcher_name}")
        self.win.geometry("1000x500")
        self.win.minsize(600, 200)
        # Hide instead of destroy on close, so the watcher keeps running and
        # the user can re-open the same window from the row's "Output" btn.
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        self.text = scrolledtext.ScrolledText(
            self.win,
            bg="#1e1e1e", fg="#e0e0e0",
            insertbackground="#e0e0e0",
            font=("Consolas", 10),
            wrap=tk.NONE,
            borderwidth=0,
            highlightthickness=0,
        )
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.configure(state=tk.DISABLED)

        # Track whether the most recent chunk ended in '\r' (a tqdm refresh).
        # If so, the next chunk should overwrite the open last line rather
        # than appending below it.
        self._last_was_cr = False

    def append(self, text: str, terminator: str):
        """Append a chunk decoded from lada's output stream.

        terminator: '\\r' = the chunk was a tqdm refresh (next chunk
        replaces this line), '\\n' = the chunk is a complete line (next
        chunk goes on a new line below), '' = mid-buffer flush at EOF.
        """
        if not self.win.winfo_exists():
            return
        self.text.configure(state=tk.NORMAL)
        try:
            if self._last_was_cr:
                # Delete the open last line so the new chunk overwrites it.
                self.text.delete("end-1l linestart", "end-1c")
            self.text.insert(tk.END, text)
            if terminator == "\n":
                self.text.insert(tk.END, "\n")
                self._last_was_cr = False
            elif terminator == "\r":
                self._last_was_cr = True
            # else: empty terminator means "leave open"; rare flush path.

            # Trim oldest content if we've exceeded the line budget.
            try:
                line_count = int(self.text.index("end-1c").split(".")[0])
                if line_count > self.MAX_LINES:
                    excess = line_count - self.MAX_LINES
                    self.text.delete("1.0", f"{excess + 1}.0")
            except (tk.TclError, ValueError):
                pass

            self.text.see(tk.END)
        finally:
            self.text.configure(state=tk.DISABLED)

    def clear(self):
        if not self.win.winfo_exists():
            return
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)
        self._last_was_cr = False

    def show(self):
        if self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()

    def hide(self):
        if self.win.winfo_exists():
            self.win.withdraw()

    def is_alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def destroy(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class TaskPawApp:
    def __init__(self):
        self.config = load_config()
        self.watchers: dict[str, BaseWatcher] = {}
        self.watcher_status: dict[str, str] = {}
        # One LadaOutputWindow per Lada watcher_id. Created on first start,
        # reused on subsequent starts (cleared between runs). Hidden when
        # the user closes the window; reopened from the row's "Output" btn.
        self._lada_output_windows: dict[str, "LadaOutputWindow"] = {}
        # Re-entrant: same UI thread may call start_watcher → _refresh_watcher_list,
        # both of which acquire the lock.
        self._state_lock = threading.RLock()
        self.api_server: Optional[APIServer] = None

        self.root = tk.Tk()
        mn = self.config.machine_name
        title_suffix = f" — {mn}" if mn else ""
        self.root.title(f"{APP_NAME} v{APP_VERSION}{title_suffix}")
        self.root.geometry("920x650")
        self.root.minsize(850, 580)
        self.root.configure(bg=COLORS["bg"])

        # Try to set icon
        try:
            self.root.iconbitmap(default="")
        except (tk.TclError, OSError):
            pass

        self._build_ui()
        self._refresh_watcher_list()

        # Window close -> minimize to background (if pystray available) or exit
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Start API server and all enabled watchers
        self.root.after(500, self._auto_start)

    # ── Thread-safe accessors for shared watcher state ───────────────
    # The watchers / watcher_status dicts are touched from worker threads
    # (status callbacks), the UI thread (_refresh_watcher_list, start/stop),
    # and the HTTP API thread (Hub polling /status). Without locking, the
    # UI thread occasionally hit "RuntimeError: dictionary changed size
    # during iteration" and Tk would visually freeze.

    def snapshot_watcher_status(self) -> dict:
        """Return a defensive copy of the watcher_status dict."""
        with self._state_lock:
            return dict(self.watcher_status)

    def get_watcher_status(self, watcher_id: str, default: str = "Stopped") -> str:
        with self._state_lock:
            return self.watcher_status.get(watcher_id, default)

    # ── UI Construction ──────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Configure ttk styles
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"],
                        font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"),
                        foreground=COLORS["text_bright"])
        style.configure("Subtitle.TLabel", font=("Segoe UI", 9),
                        foreground=COLORS["text_dim"])
        style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"),
                        foreground=COLORS["accent"])
        style.configure("Status.TLabel", font=("Segoe UI", 9),
                        foreground=COLORS["success"])

        style.configure("Accent.TButton", background=COLORS["accent"],
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        padding=(16, 8))
        style.map("Accent.TButton",
                  background=[("active", COLORS["accent_light"])])

        style.configure("TButton", background=COLORS["surface2"],
                        foreground=COLORS["text"], font=("Segoe UI", 9),
                        padding=(12, 6))
        style.map("TButton",
                  background=[("active", COLORS["border"])])

        style.configure("TEntry", fieldbackground=COLORS["surface"],
                        foreground=COLORS["text"], insertcolor=COLORS["text"])

        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["surface2"],
                        foreground=COLORS["text_dim"], padding=(16, 8),
                        font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", COLORS["accent"])],
                  foreground=[("selected", "#ffffff")])

        # Main container
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # Header
        header = ttk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 12))

        # Left: paw icon + title + machine alias
        left_header = ttk.Frame(header)
        left_header.pack(side=tk.LEFT)

        paw_canvas = tk.Canvas(
            left_header, width=28, height=28,
            bg=COLORS["bg"], highlightthickness=0,
        )
        paw_canvas.pack(side=tk.LEFT, padx=(0, 8))
        paw_canvas.create_oval(6, 12, 22, 26, fill=COLORS["accent"], outline="")
        paw_canvas.create_oval(3, 5, 10, 12, fill=COLORS["accent"], outline="")
        paw_canvas.create_oval(11, 2, 18, 9, fill=COLORS["accent"], outline="")
        paw_canvas.create_oval(18, 5, 25, 12, fill=COLORS["accent"], outline="")

        ttk.Label(left_header, text="TaskPaw", style="Title.TLabel").pack(side=tk.LEFT)

        mn = self.config.machine_name or "No alias set"
        self.machine_label = ttk.Label(left_header, text=f"{mn}",
                  style="Subtitle.TLabel")
        self.machine_label.pack(side=tk.LEFT, padx=(12, 0), pady=(4, 0))

        # Right: version + API status
        right_header = ttk.Frame(header)
        right_header.pack(side=tk.RIGHT)

        self.status_label = ttk.Label(right_header, text="API: Starting",
                  style="Subtitle.TLabel")
        self.status_label.pack(side=tk.RIGHT)

        ttk.Label(right_header, text=f"v{APP_VERSION}",
                  font=("Segoe UI", 9), foreground="#888888",
                  background=COLORS["bg"]).pack(side=tk.RIGHT, padx=(0, 12))

        # Notebook (tabs)
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: Monitor Tasks
        self.tab_watchers = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_watchers, text="  Monitors  ")
        self._build_watchers_tab()

        # Tab 2: Settings
        self.tab_settings = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_settings, text="  Settings  ")
        self._build_settings_tab()

        # Tab 3: Log
        self.tab_log = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_log, text="  Activity Log  ")
        self._build_log_tab()

    # ── Tab: Monitor Tasks ───────────────────────────

    def _build_watchers_tab(self):
        # Toolbar
        toolbar = ttk.Frame(self.tab_watchers)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(toolbar, text="Active Monitors", style="Section.TLabel").pack(side=tk.LEFT)

        btn_frame = ttk.Frame(toolbar)
        btn_frame.pack(side=tk.RIGHT)

        ttk.Button(btn_frame, text="+ Add Monitor", style="Accent.TButton",
                   command=self._add_watcher_dialog).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Start All",
                   command=self._start_all).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Stop All",
                   command=self._stop_all).pack(side=tk.LEFT, padx=4)

        # List area (Canvas for scrolling)
        list_frame = ttk.Frame(self.tab_watchers)
        list_frame.pack(fill=tk.BOTH, expand=True)

        self.watcher_canvas = tk.Canvas(list_frame, bg=COLORS["bg"],
                                         highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                   command=self.watcher_canvas.yview)
        self.watcher_inner = ttk.Frame(self.watcher_canvas)

        self.watcher_inner.bind("<Configure>",
            lambda e: self.watcher_canvas.configure(scrollregion=self.watcher_canvas.bbox("all")))

        self.watcher_canvas.create_window((0, 0), window=self.watcher_inner,
                                           anchor="nw", tags="inner")
        self.watcher_canvas.configure(yscrollcommand=scrollbar.set)
        self.watcher_canvas.bind("<Configure>",
            lambda e: self.watcher_canvas.itemconfig("inner", width=e.width))

        self.watcher_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            self.watcher_canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        def _bind_mousewheel(event):
            self.watcher_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(event):
            self.watcher_canvas.unbind_all("<MouseWheel>")

        self.watcher_canvas.bind("<Enter>", _bind_mousewheel)
        self.watcher_canvas.bind("<Leave>", _unbind_mousewheel)

    def _refresh_watcher_list(self):
        """Rebuild the watcher list UI."""
        for child in self.watcher_inner.winfo_children():
            child.destroy()

        self._status_labels = {}

        if not self.config.watchers:
            ttk.Label(self.watcher_inner, text="No monitors configured",
                      style="Subtitle.TLabel").pack(anchor=tk.W, padx=8, pady=8)
            return

        for wcfg in self.config.watchers:
            # Card container with subtle border
            card = tk.Frame(self.watcher_inner, bg=COLORS["surface"],
                           relief=tk.RIDGE, bd=1)
            card.pack(fill=tk.X, padx=4, pady=3)

            # Row 1: Name + type on left, status indicator on right
            row1 = tk.Frame(card, bg=COLORS["surface"])
            row1.pack(fill=tk.X, padx=10, pady=(8, 2))

            name_text = wcfg.name or f"[{wcfg.watcher_type}]"
            tk.Label(row1, text=name_text,
                     font=("Segoe UI", 10, "bold"),
                     bg=COLORS["surface"], fg=COLORS["text_bright"]
                     ).pack(side=tk.LEFT)

            desc = f"{WATCHER_LABELS.get(WatcherType(wcfg.watcher_type), wcfg.watcher_type)}"
            if wcfg.task_description:
                desc += f" | {wcfg.task_description}"
            tk.Label(row1, text=f"  {desc}",
                     font=("Segoe UI", 9),
                     bg=COLORS["surface"], fg=COLORS["text_dim"]
                     ).pack(side=tk.LEFT, padx=(8, 0))

            # Status indicator on right of row 1
            status = self.get_watcher_status(wcfg.id, "Stopped")
            sl = status.lower()
            status_color = (COLORS["success"] if "processing" in sl or "running" in sl
                           else COLORS["stuck"] if "stuck" in sl
                           else COLORS["error"] if "error" in sl or "failed" in sl
                           else COLORS["warning"] if status in ("Listening", "Notified", "Stopped", "Idle")
                           else COLORS["text_dim"])
            lbl = tk.Label(row1, text=f"● {status}",
                          font=("Segoe UI", 9),
                          bg=COLORS["surface"], fg=status_color)
            lbl.pack(side=tk.RIGHT, padx=4)
            self._status_labels[wcfg.id] = lbl

            # Row 2: Buttons row
            row2 = tk.Frame(card, bg=COLORS["surface"])
            row2.pack(fill=tk.X, padx=10, pady=(2, 8))

            # Enable/disable checkbox
            enabled_var = tk.BooleanVar(value=wcfg.enabled)
            def _toggle_enabled(wid=wcfg.id, var=enabled_var):
                wcfg = next((w for w in self.config.watchers if w.id == wid), None)
                if wcfg:
                    wcfg.enabled = var.get()
                    save_config(self.config)

            ttk.Checkbutton(row2, text="Enabled", variable=enabled_var,
                           command=_toggle_enabled).pack(side=tk.LEFT, padx=(0, 12))

            # Action buttons
            def _start(wid=wcfg.id):
                self._start_watcher(wid)

            def _stop(wid=wcfg.id):
                self._stop_watcher(wid)

            def _edit(wid=wcfg.id):
                self._edit_watcher_dialog(wid)

            def _delete(wid=wcfg.id):
                self._delete_watcher(wid)

            ttk.Button(row2, text="Start", command=_start, width=8).pack(side=tk.LEFT, padx=3)
            ttk.Button(row2, text="Stop", command=_stop, width=8).pack(side=tk.LEFT, padx=3)

            # Lada watchers in capture mode get an extra "Output" button to
            # raise the Tk-based output window. In native-console mode the
            # button is hidden because lada's output goes to its own CMD
            # window, which we don't own.
            if (wcfg.watcher_type == WatcherType.LADA.value
                    and getattr(wcfg, "lada_capture_progress", False)):
                def _show_output(wid=wcfg.id, name=wcfg.name):
                    self._show_lada_output_window(wid, name)
                ttk.Button(row2, text="Output", command=_show_output, width=8
                           ).pack(side=tk.LEFT, padx=3)

            ttk.Button(row2, text="Edit", command=_edit, width=8).pack(side=tk.LEFT, padx=3)
            ttk.Button(row2, text="Delete", command=_delete, width=8).pack(side=tk.LEFT, padx=3)

    # ── Tab: Settings ────────────────────────────────

    def _build_settings_tab(self):
        # Scrollable canvas
        settings_frame = ttk.Frame(self.tab_settings)
        settings_frame.pack(fill=tk.BOTH, expand=True)

        settings_canvas = tk.Canvas(settings_frame, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(settings_frame, orient=tk.VERTICAL,
                                   command=settings_canvas.yview)
        container = ttk.Frame(settings_canvas)

        container.bind("<Configure>",
            lambda e: settings_canvas.configure(scrollregion=settings_canvas.bbox("all")))

        settings_canvas.create_window((0, 0), window=container, anchor="nw")
        settings_canvas.configure(yscrollcommand=scrollbar.set)

        settings_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Mouse wheel scrolling
        def _on_mousewheel(event):
            settings_canvas.yview_scroll(int(-1*(event.delta/120)), "units")

        def _bind_mousewheel(event):
            settings_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(event):
            settings_canvas.unbind_all("<MouseWheel>")

        settings_canvas.bind("<Enter>", _bind_mousewheel)
        settings_canvas.bind("<Leave>", _unbind_mousewheel)

        # ── Machine Settings ──
        ttk.Label(container, text="Machine Settings", style="Section.TLabel").pack(
            anchor=tk.W, pady=(0, 8))

        row_mn = ttk.Frame(container)
        row_mn.pack(fill=tk.X, pady=4)
        ttk.Label(row_mn, text="Machine Alias:", width=14).pack(side=tk.LEFT)
        self.machine_name_entry = ttk.Entry(row_mn, width=30)
        self.machine_name_entry.insert(0, self.config.machine_name)
        self.machine_name_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row_mn, text="e.g. BlackGoldPig, WorkStation-01",
                  style="Subtitle.TLabel").pack(side=tk.LEFT, padx=8)

        ttk.Separator(container, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)

        # ── HTTP API Server ──
        ttk.Label(container, text="HTTP API Server", style="Section.TLabel").pack(
            anchor=tk.W, pady=(0, 8))

        # API Port
        row_api1 = ttk.Frame(container)
        row_api1.pack(fill=tk.X, pady=4)
        ttk.Label(row_api1, text="API Port:", width=14).pack(side=tk.LEFT)
        self.api_port_entry = ttk.Entry(row_api1, width=8)
        self.api_port_entry.insert(0, str(self.config.api_port))
        self.api_port_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row_api1, text="(default 5678, 1-65535)",
                  style="Subtitle.TLabel").pack(side=tk.LEFT, padx=8)

        # API Token (optional bearer auth)
        row_api2 = ttk.Frame(container)
        row_api2.pack(fill=tk.X, pady=4)
        ttk.Label(row_api2, text="API Token:", width=14).pack(side=tk.LEFT)
        self.api_token_entry = ttk.Entry(row_api2, width=40, show="*")
        self.api_token_entry.insert(0, self.config.api_token)
        self.api_token_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row_api2,
                  text="(optional — empty = no auth, set to require Bearer)",
                  style="Subtitle.TLabel").pack(side=tk.LEFT, padx=8)

        # Server status and URL
        self.api_status_label = ttk.Label(container, text="", style="Subtitle.TLabel")
        self.api_status_label.pack(anchor=tk.W, pady=4)

        self.api_url_label = ttk.Label(container, text="", style="Subtitle.TLabel")
        self.api_url_label.pack(anchor=tk.W, pady=2)

        ttk.Separator(container, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=12)

        # ── App Settings ──
        ttk.Label(container, text="App Settings", style="Section.TLabel").pack(
            anchor=tk.W, pady=(0, 8))

        self.start_minimized_var = tk.BooleanVar(value=self.config.start_minimized)
        ttk.Checkbutton(container, text="Start minimized to background",
                        variable=self.start_minimized_var).pack(anchor=tk.W, pady=2)

        ttk.Separator(container, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=16)

        # ── Save Button (prominent, at bottom) ──
        save_row = ttk.Frame(container)
        save_row.pack(fill=tk.X, pady=(4, 8))
        ttk.Button(save_row, text="Save All Settings", style="Accent.TButton",
                   command=self._save_settings).pack(side=tk.LEFT, padx=4)
        self.save_result = ttk.Label(save_row, text="", style="Subtitle.TLabel")
        self.save_result.pack(side=tk.LEFT, padx=8)

        # Config file path
        ttk.Label(container, text=f"Config file: {CONFIG_FILE}",
                  style="Subtitle.TLabel").pack(anchor=tk.W, pady=(16, 0))
        ttk.Label(container, text=f"Log file: {LOG_FILE}",
                  style="Subtitle.TLabel").pack(anchor=tk.W, pady=2)

    def _save_settings(self):
        self.config.machine_name = self.machine_name_entry.get().strip()
        self.config.start_minimized = self.start_minimized_var.get()

        # API port — clamp to a valid TCP port (Codex finding #7).
        try:
            port = int(self.api_port_entry.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError(f"port {port} out of range 1..65535")
            self.config.api_port = port
        except ValueError as e:
            log.warning(f"Invalid API port '{self.api_port_entry.get()}': {e}")
            self.save_result.configure(
                text=f"Invalid port — must be 1..65535. Reverted to {self.config.api_port}.",
                foreground=COLORS["error"])
            # Reflect the actual value in the entry so the user sees what was kept.
            self.api_port_entry.delete(0, tk.END)
            self.api_port_entry.insert(0, str(self.config.api_port))
            return

        # API token — optional. Empty disables auth.
        self.config.api_token = self.api_token_entry.get().strip()

        save_config(self.config)

        # Restart API server so port and token changes take effect.
        self._start_api_server()

        # Update title bar machine alias
        mn = self.config.machine_name or "No alias set"
        self.machine_label.configure(text=mn)
        self.root.title(f"{APP_NAME} v{APP_VERSION} — {mn}")
        token_msg = " (auth ON)" if self.config.api_token else " (auth OFF)"
        self.save_result.configure(text=f"Settings saved!{token_msg}",
                                   foreground=COLORS["success"])

    # ── Tab: Activity Log ────────────────────────────

    def _build_log_tab(self):
        toolbar = ttk.Frame(self.tab_log)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(toolbar, text="Activity Log", style="Section.TLabel").pack(side=tk.LEFT)

        ttk.Button(toolbar, text="Clear", command=self._clear_log).pack(side=tk.RIGHT, padx=4)

        # Log text area
        self.log_text = scrolledtext.ScrolledText(
            self.tab_log, state=tk.DISABLED, height=20, wrap=tk.WORD,
            bg=COLORS["surface"], fg=COLORS["text"],
            font=("Courier New", 9)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _append_log(self, msg):
        """Thread-safe log append"""
        def _do():
            self.log_text.configure(state=tk.NORMAL)
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        try:
            self.root.after(0, _do)
        except (tk.TclError, RuntimeError):
            pass

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ── Watcher Management ────────────────────────────

    def _on_watcher_status(self, watcher_id: str, status: str):
        """Watcher status change callback (thread-safe)"""
        with self._state_lock:
            self.watcher_status[watcher_id] = status
        # Tk is not thread-safe; marshal back to the UI thread.
        try:
            if self.root and self.root.winfo_exists():
                self.root.after(0, lambda: self._update_status_label(watcher_id, status))
        except (tk.TclError, RuntimeError) as e:
            log.debug(f"after() during shutdown: {e}")

    def _on_watcher_event(self, machine: str, monitor: str, message: str):
        """Watcher event callback (thread-safe)"""
        self._append_log(f"[{monitor}] {message.split(chr(10))[0]}")

    def _update_status_label(self, watcher_id, status):
        if hasattr(self, '_status_labels') and watcher_id in self._status_labels:
            lbl = self._status_labels[watcher_id]
            if lbl.winfo_exists():
                sl = status.lower()
                color = COLORS["success"] if "processing" in sl or "running" in sl \
                    else COLORS["stuck"] if "stuck" in sl \
                    else COLORS["error"] if "error" in sl or "failed" in sl \
                    else COLORS["warning"] if status in ("Listening", "Notified", "Stopped", "Idle") \
                    else COLORS["text_dim"]
                lbl.configure(text=f"● {status}", foreground=color)

    def _start_watcher(self, watcher_id: str):
        wcfg = next((w for w in self.config.watchers if w.id == watcher_id), None)
        if not wcfg:
            return

        with self._state_lock:
            existing = self.watchers.get(watcher_id)
            if existing and existing.is_alive():
                return

        wtype = WatcherType(wcfg.watcher_type)
        cls = WATCHER_CLASS_MAP.get(wtype)
        if not cls:
            self._append_log(f"Unknown monitor type: {wcfg.watcher_type}")
            return

        # For Lada in capture mode, set up the live output window before
        # the watcher starts so the very first chunk has somewhere to land.
        # In native-console mode we don't pipe lada's output, so the Tk
        # window would just stay blank — skip it.
        on_raw_output = None
        if wtype == WatcherType.LADA and getattr(wcfg, "lada_capture_progress", False):
            output_window = self._ensure_lada_output_window(watcher_id, wcfg.name)
            output_window.clear()
            output_window.show()
            on_raw_output = self._make_lada_output_callback(watcher_id)

        watcher = cls(
            wcfg,
            machine_name=self.config.machine_name,
            on_log=self._append_log,
            on_status_change=self._on_watcher_status,
            on_event=self._on_watcher_event,
            on_raw_output=on_raw_output,
        )
        with self._state_lock:
            self.watchers[watcher_id] = watcher
        watcher.start()
        self._append_log(f"Started: {wcfg.name}")
        self._refresh_watcher_list()

    # ── Lada output windows ──────────────────────────

    def _ensure_lada_output_window(self, watcher_id: str, watcher_name: str) -> LadaOutputWindow:
        """Get an existing output window for this watcher, or create one."""
        win = self._lada_output_windows.get(watcher_id)
        if win is not None and win.is_alive():
            return win
        win = LadaOutputWindow(self.root, watcher_name)
        self._lada_output_windows[watcher_id] = win
        return win

    def _make_lada_output_callback(self, watcher_id: str):
        """Build the on_raw_output callback for a specific Lada watcher.

        The reader thread calls this on every chunk from lada-cli's stdout.
        Tk is not thread-safe, so we marshal the actual text-widget update
        onto the UI thread via root.after(0, ...).
        """
        def _cb(text: str, terminator: str):
            try:
                if not (self.root and self.root.winfo_exists()):
                    return
            except (tk.TclError, RuntimeError):
                return

            def _apply():
                win = self._lada_output_windows.get(watcher_id)
                if win is not None and win.is_alive():
                    win.append(text, terminator)

            try:
                self.root.after(0, _apply)
            except (tk.TclError, RuntimeError):
                pass
        return _cb

    def _show_lada_output_window(self, watcher_id: str, watcher_name: str):
        """Wired up to the row's "Output" button."""
        win = self._ensure_lada_output_window(watcher_id, watcher_name)
        win.show()

    def _stop_watcher(self, watcher_id: str):
        with self._state_lock:
            watcher = self.watchers.pop(watcher_id, None)
            if watcher is not None:
                self.watcher_status[watcher_id] = "Stopped"

        if watcher is None:
            return

        # Signal the worker to exit, then wait for it. Without the join the
        # thread can outlive Tk shutdown and leave a zombie that has to be
        # killed via Task Manager — exactly the "force-close only" symptom.
        watcher.stop()
        watcher.join(timeout=5)
        if watcher.is_alive():
            log.warning(f"Watcher {watcher_id} did not exit within 5s")

        wcfg = next((w for w in self.config.watchers if w.id == watcher_id), None)
        if wcfg:
            self._append_log(f"Stopped: {wcfg.name}")
        self._refresh_watcher_list()

    def _start_all(self):
        for wcfg in self.config.watchers:
            if wcfg.enabled:
                self._start_watcher(wcfg.id)

    def _stop_all(self):
        with self._state_lock:
            wids = list(self.watchers.keys())
        for wid in wids:
            self._stop_watcher(wid)

    def _start_api_server(self):
        """Start or restart the API server."""
        if self.api_server and self.api_server.is_running:
            self.api_server.stop()

        self.api_server = APIServer(port=self.config.api_port, app_instance=self)
        self.api_server.start()

        # Update status label
        if self.api_server.is_running:
            self._update_api_status()
        else:
            self.status_label.configure(text="API: Failed", foreground=COLORS["error"])

    def _update_api_status(self):
        """Update the API server status display."""
        if self.api_server and self.api_server.is_running:
            self.status_label.configure(text="API: Running", foreground=COLORS["success"])

            # Get local IP for display
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except:
                local_ip = "127.0.0.1"

            url = f"http://{local_ip}:{self.config.api_port}"
            self.api_url_label.configure(text=f"API: {url}")
            self.api_status_label.configure(text="Status: Running", foreground=COLORS["success"])
        else:
            self.status_label.configure(text="API: Stopped", foreground=COLORS["error"])
            self.api_status_label.configure(text="Status: Stopped", foreground=COLORS["error"])

    def _auto_start(self):
        """Start the API server, and watchers only if auto_start is enabled.

        Previously this method ignored AppConfig.auto_start and always
        started every enabled watcher on launch — so Lada would kick off
        immediately when the user opened the app, with no chance to click
        Start. Now it respects the flag (default False = manual start).
        The API server is always started so the Hub can poll us.
        """
        self._append_log("Initializing TaskPaw...")
        self._start_api_server()

        if not self.config.auto_start:
            self._append_log(
                "Watchers not auto-started. Click Start on a monitor to launch it. "
                "(Set \"auto_start\": true in %APPDATA%\\TaskPaw\\config.json to "
                "auto-start on launch.)"
            )
            return

        for wcfg in self.config.watchers:
            if wcfg.enabled:
                self._start_watcher(wcfg.id)

    def _delete_watcher(self, watcher_id: str):
        if not messagebox.askyesno("Confirm Delete", "Are you sure you want to delete this monitor?"):
            return
        self._stop_watcher(watcher_id)
        self.config.watchers = [w for w in self.config.watchers if w.id != watcher_id]
        save_config(self.config)
        self._refresh_watcher_list()

    # ── Add/Edit Dialog ───────────────────────────────

    def _add_watcher_dialog(self):
        self._open_watcher_editor(None)

    def _edit_watcher_dialog(self, watcher_id: str):
        wcfg = next((w for w in self.config.watchers if w.id == watcher_id), None)
        if wcfg:
            self._open_watcher_editor(wcfg)

    def _open_watcher_editor(self, wcfg: Optional[WatcherConfig]):
        """Open dialog to add/edit a watcher."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Monitor Configuration")
        dialog.geometry("700x700")
        dialog.resizable(False, False)

        # Create new config if adding
        if wcfg is None:
            wcfg = WatcherConfig()
            is_new = True
        else:
            is_new = False

        # Scrollable frame
        canvas = tk.Canvas(dialog, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(dialog, orient=tk.VERTICAL, command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Name
        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Monitor Name:", width=14).pack(side=tk.LEFT)
        name_entry = ttk.Entry(row, width=40)
        name_entry.insert(0, wcfg.name)
        name_entry.pack(side=tk.LEFT, padx=4)

        # Type
        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Type:", width=14).pack(side=tk.LEFT)
        type_var = tk.StringVar(value=wcfg.watcher_type)
        type_combo = ttk.Combobox(row, textvariable=type_var, width=30,
                                   values=[str(t.value) for t in WatcherType],
                                   state="readonly")
        type_combo.pack(side=tk.LEFT, padx=4)

        # Task Description
        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Task Description:", width=14).pack(side=tk.LEFT)
        task_entry = ttk.Entry(row, width=40)
        task_entry.insert(0, wcfg.task_description)
        task_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="(optional)", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        # ── Lada Settings ──
        ttk.Label(scrollable_frame, text="Lada / Process Settings", style="Section.TLabel").pack(
            anchor=tk.W, padx=8, pady=(12, 4))

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="CLI Path:", width=14).pack(side=tk.LEFT)
        lada_cli_entry = ttk.Entry(row, width=36)
        lada_cli_entry.insert(0, wcfg.lada_cli_path)
        lada_cli_entry.pack(side=tk.LEFT, padx=4)
        def _browse_lada_cli():
            path = filedialog.askopenfilename(
                title="Select lada-cli Executable",
                filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
            )
            if path:
                lada_cli_entry.delete(0, tk.END)
                lada_cli_entry.insert(0, path)
        ttk.Button(row, text="Browse", command=_browse_lada_cli, width=8).pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(row, text="", width=14).pack(side=tk.LEFT)
        ttk.Label(row, text="(set path = managed mode, empty = passive monitor only)",
                  style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Process Name:", width=14).pack(side=tk.LEFT)
        proc_entry = ttk.Entry(row, width=30)
        proc_entry.insert(0, wcfg.process_name)
        proc_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="(passive mode only)", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Input Folder:", width=14).pack(side=tk.LEFT)
        lada_input_entry = ttk.Entry(row, width=36)
        lada_input_entry.insert(0, wcfg.lada_input_folder)
        lada_input_entry.pack(side=tk.LEFT, padx=4)
        def _browse_lada_input():
            path = filedialog.askdirectory(title="Select Lada Input Folder")
            if path:
                lada_input_entry.delete(0, tk.END)
                lada_input_entry.insert(0, path)
        ttk.Button(row, text="Browse", command=_browse_lada_input, width=8).pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Output Folder:", width=14).pack(side=tk.LEFT)
        lada_output_entry = ttk.Entry(row, width=36)
        lada_output_entry.insert(0, wcfg.lada_output_folder)
        lada_output_entry.pack(side=tk.LEFT, padx=4)
        def _browse_lada_output():
            path = filedialog.askdirectory(title="Select Lada Output Folder")
            if path:
                lada_output_entry.delete(0, tk.END)
                lada_output_entry.insert(0, path)
        ttk.Button(row, text="Browse", command=_browse_lada_output, width=8).pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Extra Args:", width=14).pack(side=tk.LEFT)
        lada_args_entry = ttk.Entry(row, width=40)
        lada_args_entry.insert(0, wcfg.lada_extra_args)
        lada_args_entry.pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(row, text="", width=14).pack(side=tk.LEFT)
        ttk.Label(row, text="(e.g. --device cuda:1 --encoder h264_nvenc)",
                  style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        gpu_var = tk.BooleanVar(value=wcfg.lada_gpu_monitor)
        ttk.Checkbutton(row, text="Monitor GPU usage (nvidia-smi)", variable=gpu_var).pack(
            side=tk.LEFT, padx=8)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Poll Interval:", width=14).pack(side=tk.LEFT)
        poll_entry = ttk.Entry(row, width=8)
        poll_entry.insert(0, str(wcfg.poll_interval))
        poll_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="seconds", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        # ── ComfyUI Settings ──
        ttk.Label(scrollable_frame, text="ComfyUI Settings", style="Section.TLabel").pack(
            anchor=tk.W, padx=8, pady=(12, 4))

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Host:", width=14).pack(side=tk.LEFT)
        comfyui_host = ttk.Entry(row, width=20)
        comfyui_host.insert(0, wcfg.comfyui_host)
        comfyui_host.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="Port:", width=6).pack(side=tk.LEFT)
        comfyui_port = ttk.Entry(row, width=8)
        comfyui_port.insert(0, str(wcfg.comfyui_port))
        comfyui_port.pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Log File Path:", width=14).pack(side=tk.LEFT)
        comfyui_log_entry = ttk.Entry(row, width=36)
        comfyui_log_entry.insert(0, wcfg.comfyui_log_path)
        comfyui_log_entry.pack(side=tk.LEFT, padx=4)
        def _browse_log():
            path = filedialog.askopenfilename(
                title="Select ComfyUI Log File",
                filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")],
            )
            if path:
                comfyui_log_entry.delete(0, tk.END)
                comfyui_log_entry.insert(0, path)
        ttk.Button(row, text="Browse", command=_browse_log, width=8).pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Idle Confirms:", width=14).pack(side=tk.LEFT)
        idle_entry = ttk.Entry(row, width=8)
        idle_entry.insert(0, str(wcfg.idle_confirm_count))
        idle_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="(queue empty N times = complete)", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        # ── Folder Settings ──
        ttk.Label(scrollable_frame, text="Folder Monitor Settings", style="Section.TLabel").pack(
            anchor=tk.W, padx=8, pady=(12, 4))

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Folder Path:", width=14).pack(side=tk.LEFT)
        folder_entry = ttk.Entry(row, width=40)
        folder_entry.insert(0, wcfg.watch_folder)
        folder_entry.pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="File Extensions:", width=14).pack(side=tk.LEFT)
        ext_entry = ttk.Entry(row, width=40)
        ext_entry.insert(0, wcfg.file_extensions)
        ext_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="(comma-separated, empty = all)", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Stable Seconds:", width=14).pack(side=tk.LEFT)
        stable_entry = ttk.Entry(row, width=8)
        stable_entry.insert(0, str(wcfg.stable_seconds))
        stable_entry.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="(no size change = complete)", style="Subtitle.TLabel").pack(side=tk.LEFT, padx=4)

        # ── Custom Command Settings ──
        ttk.Label(scrollable_frame, text="Custom Command Settings", style="Section.TLabel").pack(
            anchor=tk.W, padx=8, pady=(12, 4))

        row = ttk.Frame(scrollable_frame)
        row.pack(fill=tk.X, padx=8, pady=4)
        ttk.Label(row, text="Command:", width=14).pack(side=tk.LEFT)
        cmd_entry = ttk.Entry(row, width=50)
        cmd_entry.insert(0, wcfg.custom_command)
        cmd_entry.pack(side=tk.LEFT, padx=4)

        # ── Notification Template ──
        ttk.Label(scrollable_frame, text="Notification Template", style="Section.TLabel").pack(
            anchor=tk.W, padx=8, pady=(12, 4))

        ttk.Label(scrollable_frame, text="Template variables: {message}, {time}, {name}, {machine}, {task}",
                  style="Subtitle.TLabel").pack(anchor=tk.W, padx=8, pady=2)

        template_entry = tk.Text(scrollable_frame, height=5, width=60, bg=COLORS["surface"],
                                fg=COLORS["text"], font=("Courier New", 9))
        template_entry.pack(padx=8, pady=4)
        template_entry.insert("1.0", wcfg.notify_template)

        # Buttons
        btn_frame = ttk.Frame(scrollable_frame)
        btn_frame.pack(fill=tk.X, padx=8, pady=16)

        def _save():
            wcfg.name = name_entry.get().strip()
            wcfg.watcher_type = type_var.get()
            wcfg.task_description = task_entry.get().strip()
            wcfg.lada_cli_path = lada_cli_entry.get().strip()
            wcfg.process_name = proc_entry.get().strip()
            wcfg.lada_input_folder = lada_input_entry.get().strip()
            wcfg.lada_output_folder = lada_output_entry.get().strip()
            wcfg.lada_extra_args = lada_args_entry.get().strip()
            wcfg.lada_gpu_monitor = gpu_var.get()
            try:
                wcfg.poll_interval = max(1, int(poll_entry.get().strip()))
            except ValueError:
                wcfg.poll_interval = 10

            wcfg.comfyui_host = comfyui_host.get().strip()
            try:
                wcfg.comfyui_port = int(comfyui_port.get().strip())
            except ValueError:
                wcfg.comfyui_port = 8188
            wcfg.comfyui_log_path = comfyui_log_entry.get().strip()
            try:
                wcfg.idle_confirm_count = max(1, int(idle_entry.get().strip()))
            except ValueError:
                wcfg.idle_confirm_count = 3

            wcfg.watch_folder = folder_entry.get().strip()
            wcfg.file_extensions = ext_entry.get().strip()
            try:
                wcfg.stable_seconds = max(1, int(stable_entry.get().strip()))
            except ValueError:
                wcfg.stable_seconds = 30

            wcfg.custom_command = cmd_entry.get().strip()
            wcfg.notify_template = template_entry.get("1.0", tk.END).strip()

            if is_new:
                self.config.watchers.append(wcfg)

            save_config(self.config)
            self._refresh_watcher_list()
            dialog.destroy()

        ttk.Button(btn_frame, text="Save", style="Accent.TButton", command=_save).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side=tk.LEFT, padx=4)

    # ── Window Management ────────────────────────────

    def _on_close(self):
        """Handle window close."""
        try:
            import pystray
            # Try to minimize to tray
            self.root.withdraw()
            self._create_tray_icon()
        except ImportError:
            # No pystray, just exit
            self._quit()

    def _create_tray_icon(self):
        """Create system tray icon"""
        try:
            import pystray
            from PIL import Image, ImageDraw

            # Generate a simple icon
            img = Image.new("RGBA", (64, 64), (74, 158, 222, 255))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 56, 56], fill=(255, 255, 255, 200))

            menu = pystray.Menu(
                pystray.MenuItem("Show Window", self._show_window),
                pystray.MenuItem("Exit", self._quit_from_tray),
            )
            self.tray_icon = pystray.Icon(APP_NAME, img, APP_NAME, menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except ImportError:
            # No pystray/PIL, fallback
            self._quit()

    def _show_window(self, *args):
        self.root.after(0, self.root.deiconify)
        if hasattr(self, 'tray_icon'):
            try:
                self.tray_icon.stop()
            except Exception:
                pass

    def _quit_from_tray(self, *args):
        if hasattr(self, 'tray_icon'):
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.root.after(0, self._quit)

    def _quit(self):
        """Quit the application and clean up."""
        self._stop_all()
        if self.api_server:
            self.api_server.stop()
        # Tear down any Lada output windows so they don't keep the Tk loop
        # alive after root.destroy().
        for win in list(self._lada_output_windows.values()):
            try:
                win.destroy()
            except Exception:
                pass
        self._lada_output_windows.clear()
        save_config(self.config)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# =====================================================
# Entry Point
# =====================================================

# Per-user single-instance lock. Without this, double-clicking the .exe
# (or having both a Startup-folder shortcut AND a manual launcher) starts
# a second TaskPaw — but only one can bind port 5678. The losing instance
# silently runs with an empty watcher_status dict, and the Hub then sees
# stale "Stopped" status for whichever watchers were started in the
# *other* instance. We hit this on SnowLeopard and lost an hour to it.
#
# Implementation: a Windows named mutex via ctypes (no extra deps,
# stdlib only). On non-Windows this is a no-op — the Hub side runs on
# Mac via taskpaw_hub.py which doesn't have this problem.
_SINGLE_INSTANCE_MUTEX_NAME = "TaskPaw_SingleInstance_a8f3b2e1"


def _acquire_single_instance_lock():
    """Try to acquire a Windows named mutex. Returns the handle on success,
    or None if another TaskPaw instance is already running.

    The mutex is per-user (no "Global\\" prefix) so two different Windows
    users on the same machine can each run their own TaskPaw — that's
    almost certainly what you want.
    """
    if sys.platform != "win32":
        return "non-windows"  # noop sentinel; truthy

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183

        # CreateMutexW(lpMutexAttributes, bInitialOwner, lpName)
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            # Couldn't create the mutex at all (e.g. unusual security
            # context). Fail open — let the launch proceed rather than
            # blocking it. Worst case is we're back to the old behavior.
            log.warning("CreateMutexW failed; skipping single-instance check")
            return "create-failed"

        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return None
        return handle
    except Exception as e:
        log.debug(f"Single-instance check skipped: {e}")
        return "exception"


def _show_already_running_message():
    """Show a brief native message box on Windows; otherwise just log."""
    msg = ("TaskPaw is already running.\n\n"
           "Check the system tray (or look for a TaskPaw window in the "
           "taskbar). Only one instance can run at a time.")
    if sys.platform == "win32":
        try:
            import ctypes
            MB_OK = 0x00000000
            MB_ICONINFORMATION = 0x00000040
            MB_TOPMOST = 0x00040000
            ctypes.windll.user32.MessageBoxW(
                None, msg, APP_NAME, MB_OK | MB_ICONINFORMATION | MB_TOPMOST
            )
        except Exception as e:
            log.warning(f"MessageBox failed: {e}")
    log.info("Another TaskPaw instance is already running — exiting")


def main():
    # Acquire the single-instance lock BEFORE we touch Tk or sockets, so
    # a second launch doesn't briefly grab port 5678 or flash a window.
    lock = _acquire_single_instance_lock()
    if lock is None:
        _show_already_running_message()
        return  # exit cleanly; OS releases the (not-acquired) mutex

    log.info(f"TaskPaw v{APP_VERSION} started")
    try:
        app = TaskPawApp()
        app.run()
    finally:
        # Explicit close so the mutex is released the moment we exit,
        # not whenever the OS gets around to it. Important for fast
        # restart scenarios (kill + relaunch).
        if sys.platform == "win32" and isinstance(lock, int):
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(lock)
            except Exception:
                pass


if __name__ == "__main__":
    main()
