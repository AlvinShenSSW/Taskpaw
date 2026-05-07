#!/usr/bin/env python3
"""
TaskPaw Hub v2.0.0
A macOS GUI application that polls Windows servers running TaskPaw V2,
collects their status and events, stores in SQLite, and sends reports to OpenClaw.
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import sqlite3
import threading
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import time
from collections import deque
import ipaddress
from logging.handlers import RotatingFileHandler

# App Constants
APP_NAME = "TaskPaw Hub"
APP_VERSION = "2.0.0"
HUB_DIR = Path.home() / ".taskpaw-hub"
DB_FILE = HUB_DIR / "hub.db"
LOG_FILE = HUB_DIR / "hub.log"
STATUS_FILE = HUB_DIR / "status.md"

# Colors matching TaskPaw theme
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

# Setup logging
HUB_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=5),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


@dataclass
class Server:
    """Server configuration."""
    id: Optional[int] = None
    name: str = ""
    ip: str = ""
    port: int = 5678
    enabled: bool = True


class DatabaseManager:
    """Manages all SQLite operations with thread-safe single connection."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, timeout=10
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.init_db()

    def init_db(self):
        """Initialize database tables."""
        with self._lock:
            cursor = self._conn.cursor()

            # Servers table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 5678,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            # Status log table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS status_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status_json TEXT NOT NULL,
                    FOREIGN KEY (server_id) REFERENCES servers(id)
                )
                """
            )

            # Events table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id INTEGER NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    machine TEXT,
                    monitor TEXT,
                    message TEXT,
                    FOREIGN KEY (server_id) REFERENCES servers(id)
                )
                """
            )

            # Config table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

            self._conn.commit()
            logger.info(f"Database initialized at {self.db_path}")

    def get_servers(self) -> List[Server]:
        """Get all registered servers."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT id, name, ip, port, enabled FROM servers ORDER BY name"
            )
            return [
                Server(
                    id=row[0],
                    name=row[1],
                    ip=row[2],
                    port=row[3],
                    enabled=bool(row[4]),
                )
                for row in cursor.fetchall()
            ]

    def add_server(self, server: Server) -> int:
        """Add a new server. Returns server id."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO servers (name, ip, port, enabled) VALUES (?, ?, ?, ?)",
                    (server.name, server.ip, server.port, int(server.enabled)),
                )
                self._conn.commit()
                return cursor.lastrowid
            except Exception:
                self._conn.rollback()
                raise

    def update_server(self, server: Server):
        """Update an existing server."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "UPDATE servers SET name=?, ip=?, port=?, enabled=? WHERE id=?",
                    (server.name, server.ip, server.port, int(server.enabled), server.id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def delete_server(self, server_id: int):
        """Delete a server and its associated data."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("DELETE FROM status_log WHERE server_id=?", (server_id,))
                cursor.execute("DELETE FROM events WHERE server_id=?", (server_id,))
                cursor.execute("DELETE FROM servers WHERE id=?", (server_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def store_status(self, server_id: int, status_json: str):
        """Store status for a server (using local time, not UTC)."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO status_log (server_id, status_json, timestamp) VALUES (?, ?, datetime('now', 'localtime'))",
                    (server_id, status_json),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def store_event(
        self, server_id: int, machine: str, monitor: str, message: str
    ):
        """Store an event (using local time, not UTC)."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "INSERT INTO events (server_id, machine, monitor, message, timestamp) VALUES (?, ?, ?, ?, datetime('now', 'localtime'))",
                    (server_id, machine, monitor, message),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent events from all servers."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT s.name, e.machine, e.monitor, e.message, e.timestamp
                FROM events e
                JOIN servers s ON e.server_id = s.id
                ORDER BY e.timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                {
                    "server": row[0],
                    "machine": row[1],
                    "monitor": row[2],
                    "message": row[3],
                    "timestamp": row[4],
                }
                for row in cursor.fetchall()
            ]

    def get_latest_status(self, server_id: int) -> Optional[Dict[str, Any]]:
        """Get the latest status for a server."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT status_json, timestamp FROM status_log
                WHERE server_id=?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (server_id,),
            )
            row = cursor.fetchone()
            if row:
                return {
                    "json": json.loads(row[0]),
                    "timestamp": datetime.fromisoformat(row[1]),
                }
            return None

    def get_recent_status_logs(self, limit: int = 4) -> List[Dict[str, Any]]:
        """Get the most recent status log entries across all servers."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                """
                SELECT s.name, sl.status_json, sl.timestamp
                FROM status_log sl
                JOIN servers s ON sl.server_id = s.id
                ORDER BY sl.timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            results = []
            for row in cursor.fetchall():
                try:
                    status_data = json.loads(row[1])
                    monitors = status_data.get("monitors", [])
                    if isinstance(monitors, list):
                        summary = ", ".join(
                            f"{m.get('name', '?')}: {m.get('status', '?')}"
                            for m in monitors
                        )
                    elif isinstance(monitors, dict):
                        summary = ", ".join(
                            f"{k}: {v}" for k, v in monitors.items()
                        )
                    else:
                        summary = str(monitors)
                    results.append({
                        "server": row[0],
                        "summary": summary,
                        "timestamp": row[2],
                    })
                except Exception as e:
                    logger.debug(f"Failed to parse status log row: {e}")
            return results

    def prune_old_status_logs(self, days: int = 7):
        """Delete status logs older than N days."""
        pruned = False
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    f"DELETE FROM status_log WHERE timestamp < datetime('now', '-{days} days', 'localtime')"
                )
                pruned = cursor.rowcount > 0
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        # VACUUM periodically to reclaim space (every ~10 prunes with actual deletions)
        if pruned:
            self._vacuum_counter = getattr(self, '_vacuum_counter', 0) + 1
            if self._vacuum_counter >= 10:
                try:
                    with self._lock:
                        self._conn.execute("VACUUM")
                    self._vacuum_counter = 0
                    logger.info("Database VACUUM completed")
                except Exception as e:
                    logger.warning(f"VACUUM failed: {e}")

    def get_config(self, key: str, default: str = "") -> str:
        """Get a config value."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key=?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    def set_config(self, key: str, value: str):
        """Set a config value."""
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute(
                    "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                    (key, value),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


class PollingEngine(threading.Thread):
    """Background thread that polls all servers."""

    def __init__(self, db_manager: DatabaseManager, callback):
        super().__init__(daemon=True)
        self.db_manager = db_manager
        self.callback = callback  # Called to update GUI
        self.running = True
        self.poll_count = 0
        self.last_event_ids = {}  # Track last seen events per server
        self._load_event_ids()

    def run(self):
        """Main polling loop."""
        logger.info("Polling engine started")
        next_poll = time.monotonic()
        while self.running:
            now = time.monotonic()
            if now >= next_poll:
                try:
                    self.poll_all_servers()
                except Exception as e:
                    logger.error(f"Error in polling loop: {e}")
                poll_interval = int(
                    self.db_manager.get_config("poll_interval", "60")
                )
                next_poll = now + poll_interval
            time.sleep(1)

    def poll_all_servers(self):
        """Poll all enabled servers."""
        report_every_n = int(
            self.db_manager.get_config("report_every_n_polls", "5")
        )

        self.poll_count += 1

        servers = self.db_manager.get_servers()
        server_statuses = {}

        for server in servers:
            if not server.enabled:
                continue

            status = self.poll_server(server)
            server_statuses[server.name] = status

            # Check for new events
            new_events = self.get_new_events(server)
            if new_events:
                for event in new_events:
                    self.db_manager.store_event(
                        server.id,
                        event.get("machine"),
                        event.get("monitor"),
                        event.get("message"),
                    )
                # Send immediate notification for new events
                if self.db_manager.get_config("openclaw_enabled") == "1":
                    for event in new_events:
                        self.send_event_to_openclaw(server.name, event)

        # Prune old logs
        try:
            self.db_manager.prune_old_status_logs()
        except Exception as e:
            logger.error(f"Prune failed: {e}")

        # Send periodic summary to OpenClaw
        if (
            self.poll_count % report_every_n == 0
            and self.db_manager.get_config("openclaw_enabled") == "1"
        ):
            self.send_summary_to_openclaw(server_statuses)

        # Write status file for OpenClaw
        self.write_status_file(server_statuses)

        # Update GUI
        self.callback(server_statuses)

    def _auth_headers(self) -> Dict[str, str]:
        """Build Authorization header(s) for outbound polls.

        Reads polling_token from the Hub config table. When set, all
        polls to TaskPaw / MacSubs include `Authorization: Bearer <token>`
        so those agents (when configured with a matching token) accept
        the request. Empty token = no header sent, preserving the
        original unauthenticated behavior.
        """
        token = self.db_manager.get_config("polling_token", "").strip()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def poll_server(self, server: Server) -> Dict[str, Any]:
        """Poll a single server."""
        url_base = f"http://{server.ip}:{server.port}"
        status = {
            "reachable": False,
            "last_seen": None,
            "monitors": [],
        }
        auth_headers = self._auth_headers()

        try:
            # Check ping (no auth required by agents — it's a reachability
            # probe with no sensitive payload)
            req = urllib.request.Request(f"{url_base}/ping")
            urllib.request.urlopen(req, timeout=5)

            # Get status
            req = urllib.request.Request(f"{url_base}/status", headers=auth_headers)
            resp = urllib.request.urlopen(req, timeout=5)
            status_data = json.loads(resp.read().decode("utf-8"))

            status["reachable"] = True
            status["last_seen"] = datetime.now()
            status["monitors"] = status_data.get("monitors", [])

            # Store in database
            self.db_manager.store_status(
                server.id, json.dumps(status_data)
            )
            logger.info(f"Successfully polled {server.name}")

        except Exception as e:
            logger.warning(f"Failed to poll {server.name}: {e}")
            status["last_seen"] = self.get_last_seen_time(server.id)

        return status

    def get_new_events(self, server: Server) -> List[Dict[str, Any]]:
        """Get new events from a server."""
        url_base = f"http://{server.ip}:{server.port}"
        try:
            req = urllib.request.Request(
                f"{url_base}/events", headers=self._auth_headers()
            )
            resp = urllib.request.urlopen(req, timeout=5)
            events = json.loads(resp.read().decode("utf-8")).get("events", [])

            # Filter for new events (simple: by ID)
            last_id = self.last_event_ids.get(server.id, -1)
            new_events = [
                e for e in events if e.get("id", -1) > last_id
            ]

            if new_events:
                self.last_event_ids[server.id] = max(
                    e.get("id", last_id) for e in new_events
                )
                self._persist_event_ids()

            return new_events

        except Exception as e:
            logger.debug(f"Failed to get events from {server.name}: {e}")
            return []

    def get_last_seen_time(self, server_id: int) -> Optional[datetime]:
        """Get the last recorded status time for a server."""
        status = self.db_manager.get_latest_status(server_id)
        return status["timestamp"] if status else None

    def send_event_to_openclaw(self, server_name: str, event: Dict[str, Any]):
        """Send event immediately to OpenClaw."""
        token = self.db_manager.get_config("openclaw_token", "")
        if not token:
            return

        message = f"TaskPaw Event | {server_name}: {event.get('message', 'Unknown event')}"
        payload = json.dumps({"text": message}).encode("utf-8")

        try:
            req = urllib.request.Request(
                "http://127.0.0.1:18789/hooks/wake",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info(f"Event sent to OpenClaw: {message}")
        except Exception as e:
            logger.error(f"Failed to send event to OpenClaw: {e}")

    def send_summary_to_openclaw(
        self, server_statuses: Dict[str, Dict[str, Any]]
    ):
        """Send periodic summary to OpenClaw."""
        token = self.db_manager.get_config("openclaw_token", "")
        if not token:
            return

        # Build summary string (SINGLE LINE)
        parts = ["TaskPaw Hub Status"]
        for server_name, status in server_statuses.items():
            if status["reachable"]:
                monitors = status.get("monitors", [])
                if isinstance(monitors, list):
                    monitors_str = ", ".join(
                        f"{m.get('name', '?')}: {m.get('status', '?')}"
                        for m in monitors
                    )
                else:
                    monitors_str = ", ".join(
                        f"{m}: {s}" for m, s in monitors.items()
                    )
                parts.append(f"{server_name}: {monitors_str}")
            else:
                parts.append(f"{server_name}: offline")

        message = " | ".join(parts)
        payload = json.dumps({"text": message}).encode("utf-8")

        try:
            req = urllib.request.Request(
                "http://127.0.0.1:18789/hooks/wake",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            logger.info(f"Summary sent to OpenClaw")
        except Exception as e:
            logger.error(f"Failed to send summary to OpenClaw: {e}")

    def write_status_file(self, server_statuses: Dict[str, Dict[str, Any]]):
        """Write a markdown status file that OpenClaw can read."""
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines = [
                f"# TaskPaw Hub Status",
                f"",
                f"Last updated: {now}",
                f"",
            ]

            for server_name, status in server_statuses.items():
                if status["reachable"]:
                    lines.append(f"## {server_name}: ONLINE")
                    monitors = status.get("monitors", [])
                    if isinstance(monitors, list):
                        for m in monitors:
                            name = m.get("name", "Unknown")
                            mstatus = m.get("status", "Unknown")
                            enabled = m.get("enabled", True)
                            if enabled:
                                lines.append(f"- {name}: {mstatus}")
                            else:
                                lines.append(f"- {name}: disabled")
                    elif isinstance(monitors, dict):
                        for m_name, m_status in monitors.items():
                            lines.append(f"- {m_name}: {m_status}")
                else:
                    last_seen = status.get("last_seen")
                    if last_seen:
                        ts = last_seen.strftime("%H:%M:%S") if isinstance(last_seen, datetime) else str(last_seen)
                        lines.append(f"## {server_name}: OFFLINE (last seen {ts})")
                    else:
                        lines.append(f"## {server_name}: OFFLINE")

                lines.append("")

            tmp = STATUS_FILE.with_suffix(".md.tmp")
            tmp.write_text("\n".join(lines), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
            logger.debug("Status file updated")
        except Exception as e:
            logger.error(f"Failed to write status file: {e}")

    def _load_event_ids(self):
        """Load persisted last_event_ids from database."""
        try:
            raw = self.db_manager.get_config("last_event_ids", "")
            if raw:
                self.last_event_ids = {int(k): int(v) for k, v in json.loads(raw).items()}
        except Exception as e:
            logger.debug(f"Failed to load event ids: {e}")
            self.last_event_ids = {}

    def _persist_event_ids(self):
        """Persist last_event_ids to database."""
        try:
            self.db_manager.set_config("last_event_ids", json.dumps(self.last_event_ids))
        except Exception as e:
            logger.debug(f"Failed to persist event ids: {e}")

    def stop(self):
        """Stop the polling engine."""
        self._persist_event_ids()
        self.running = False


class TaskPawHub:
    """Main GUI application."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry("780x650")
        self.root.configure(bg=COLORS["bg"])

        self.db_manager = DatabaseManager(DB_FILE)
        self.polling_engine = None
        self.server_statuses = {}

        self._setup_styles()
        self._create_widgets()
        self._start_polling()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _bind_mousewheel(self, widget, target):
        """Bind mousewheel scrolling to a widget that scrolls target.
        Works on macOS (MouseWheel) and Linux (Button-4/5)."""
        import sys

        def _on_mousewheel(event):
            if sys.platform == "darwin":
                # macOS: delta is ±1..±3, scroll 3 units per tick
                target.yview_scroll(-3 * event.delta, "units")
            else:
                # Windows/Linux: delta is ±120
                target.yview_scroll(-1 * (event.delta // 120), "units")

        def _on_mousewheel_linux_up(event):
            target.yview_scroll(-3, "units")

        def _on_mousewheel_linux_down(event):
            target.yview_scroll(3, "units")

        widget.bind("<MouseWheel>", _on_mousewheel)
        widget.bind("<Button-4>", _on_mousewheel_linux_up)
        widget.bind("<Button-5>", _on_mousewheel_linux_down)

    def _setup_styles(self):
        """Configure ttk styles."""
        style = ttk.Style()
        style.theme_use("aqua")  # macOS native theme

        # Configure colors for various elements
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure(
            "TButton", background=COLORS["surface"], foreground=COLORS["text"]
        )
        style.configure(
            "Accent.TButton",
            background=COLORS["accent"],
            foreground=COLORS["surface"],
        )

    def _create_widgets(self):
        """Create main UI elements."""
        # Title bar: logo + name on left, version on right
        title_frame = tk.Frame(self.root, bg=COLORS["bg"])
        title_frame.pack(fill=tk.X, padx=20, pady=15)

        # Left side: paw logo + app name
        left_frame = tk.Frame(title_frame, bg=COLORS["bg"])
        left_frame.pack(side=tk.LEFT)

        # Draw a small paw icon
        paw_canvas = tk.Canvas(
            left_frame, width=28, height=28,
            bg=COLORS["bg"], highlightthickness=0,
        )
        paw_canvas.pack(side=tk.LEFT, padx=(0, 8))
        # Paw pad (large oval)
        paw_canvas.create_oval(6, 12, 22, 26, fill=COLORS["accent"], outline="")
        # Toe beans (small circles)
        paw_canvas.create_oval(3, 5, 10, 12, fill=COLORS["accent"], outline="")
        paw_canvas.create_oval(11, 2, 18, 9, fill=COLORS["accent"], outline="")
        paw_canvas.create_oval(18, 5, 25, 12, fill=COLORS["accent"], outline="")

        title_label = tk.Label(
            left_frame,
            text=APP_NAME,
            font=("Helvetica", 20, "bold"),
            bg=COLORS["bg"],
            fg=COLORS["text_bright"],
        )
        title_label.pack(side=tk.LEFT)

        # Right side: version number (small, dark grey)
        version_label = tk.Label(
            title_frame,
            text=f"v{APP_VERSION}",
            font=("Helvetica", 10),
            bg=COLORS["bg"],
            fg="#666666",
        )
        version_label.pack(side=tk.RIGHT, anchor=tk.E, pady=(8, 0))

        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=15, pady=(0, 15))

        self.dashboard_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.servers_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.settings_tab = tk.Frame(self.notebook, bg=COLORS["bg"])

        self.notebook.add(self.dashboard_tab, text="Dashboard")
        self.notebook.add(self.servers_tab, text="Servers")
        self.notebook.add(self.settings_tab, text="Settings")

        self._create_dashboard_tab()
        self._create_servers_tab()
        self._create_settings_tab()

    def _create_dashboard_tab(self):
        """Create the Dashboard tab."""
        # Server cards area (scrollable)
        cards_frame = tk.Frame(self.dashboard_tab, bg=COLORS["bg"])
        cards_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Canvas for scrolling
        self.cards_canvas = tk.Canvas(
            cards_frame, bg=COLORS["bg"], highlightthickness=0
        )
        scrollbar = ttk.Scrollbar(
            cards_frame, orient=tk.VERTICAL, command=self.cards_canvas.yview
        )
        self.cards_scrollable_frame = tk.Frame(self.cards_canvas, bg=COLORS["bg"])

        self.cards_scrollable_frame.bind(
            "<Configure>",
            lambda e: self.cards_canvas.configure(
                scrollregion=self.cards_canvas.bbox("all")
            ),
        )

        self.cards_canvas.create_window(
            (0, 0), window=self.cards_scrollable_frame, anchor="nw"
        )
        self.cards_canvas.configure(yscrollcommand=scrollbar.set)

        self.cards_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Enable mousewheel scrolling on dashboard
        self._bind_mousewheel(self.cards_canvas, self.cards_canvas)
        self._bind_mousewheel(self.cards_scrollable_frame, self.cards_canvas)

        # Recent Updates area
        events_label = tk.Label(
            self.dashboard_tab,
            text="Recent Updates",
            font=("Helvetica", 12, "bold"),
            bg=COLORS["bg"],
            fg=COLORS["text"],
        )
        events_label.pack(anchor=tk.W, padx=10, pady=(5, 0))

        events_frame = tk.Frame(self.dashboard_tab, bg=COLORS["surface"])
        events_frame.pack(fill=tk.BOTH, padx=10, pady=5)

        # Updates listbox (last 4 entries)
        self.events_listbox = tk.Listbox(
            events_frame,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            border=1,
            height=4,
            font=("Helvetica", 10),
        )
        self.events_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def _create_servers_tab(self):
        """Create the Servers tab."""
        # List frame
        list_frame = tk.Frame(self.servers_tab, bg=COLORS["surface"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Servers listbox header
        header_frame = tk.Frame(list_frame, bg=COLORS["surface2"])
        header_frame.pack(fill=tk.X, padx=5, pady=(5, 0))

        for text in ["Name", "IP", "Port", "Enabled"]:
            tk.Label(
                header_frame,
                text=text,
                bg=COLORS["surface2"],
                fg=COLORS["text_bright"],
                font=("Helvetica", 10, "bold"),
                width=15,
            ).pack(side=tk.LEFT, padx=5, pady=5)

        # Servers listbox
        self.servers_listbox = tk.Listbox(
            list_frame,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            border=1,
            height=10,
        )
        self.servers_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.servers_listbox.bind("<<ListboxSelect>>", self._on_server_selected)
        self._bind_mousewheel(self.servers_listbox, self.servers_listbox)

        # Buttons frame
        buttons_frame = tk.Frame(self.servers_tab, bg=COLORS["bg"])
        buttons_frame.pack(fill=tk.X, padx=10, pady=10)

        tk.Button(
            buttons_frame,
            text="Add Server",
            command=self._show_add_server_dialog,
            bg=COLORS["accent"],
            fg=COLORS["text_bright"],
            padx=10,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)

        self.edit_button = tk.Button(
            buttons_frame,
            text="Edit",
            command=self._show_edit_server_dialog,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            padx=10,
            pady=5,
            state=tk.DISABLED,
        )
        self.edit_button.pack(side=tk.LEFT, padx=5)

        self.remove_button = tk.Button(
            buttons_frame,
            text="Remove",
            command=self._remove_server,
            bg=COLORS["error"],
            fg=COLORS["text_bright"],
            padx=10,
            pady=5,
            state=tk.DISABLED,
        )
        self.remove_button.pack(side=tk.LEFT, padx=5)

        self._refresh_servers_list()

    def _create_settings_tab(self):
        """Create the Settings tab."""
        # Scroll area
        canvas = tk.Canvas(self.settings_tab, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            self.settings_tab, orient=tk.VERTICAL, command=canvas.yview
        )
        scrollable_frame = tk.Frame(canvas, bg=COLORS["bg"])

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Enable mousewheel scrolling on settings
        self._bind_mousewheel(canvas, canvas)
        self._bind_mousewheel(scrollable_frame, canvas)

        # Poll interval
        poll_frame = tk.Frame(scrollable_frame, bg=COLORS["surface"])
        poll_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(
            poll_frame,
            text="Poll Interval (seconds):",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica", 11),
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))

        self.poll_interval_var = tk.StringVar(
            value=self.db_manager.get_config("poll_interval", "60")
        )
        poll_entry = tk.Entry(
            poll_frame,
            textvariable=self.poll_interval_var,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            width=10,
        )
        poll_entry.pack(anchor=tk.W, padx=10, pady=(0, 10))

        # Report frequency
        report_frame = tk.Frame(scrollable_frame, bg=COLORS["surface"])
        report_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(
            report_frame,
            text="Report to OpenClaw Every N Polls:",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica", 11),
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))

        self.report_every_var = tk.StringVar(
            value=self.db_manager.get_config("report_every_n_polls", "5")
        )
        report_entry = tk.Entry(
            report_frame,
            textvariable=self.report_every_var,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            width=10,
        )
        report_entry.pack(anchor=tk.W, padx=10, pady=(0, 10))

        # ── Polling Auth (Hub → TaskPaw / MacSubs) ──
        # Optional bearer token sent on every /status and /events poll.
        # Empty = no auth (current default), suitable for trusted LAN /
        # Tailscale. Each Windows agent and MacSubs must set the same
        # token in its own config for auth to succeed.
        polling_frame = tk.Frame(scrollable_frame, bg=COLORS["surface"])
        polling_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(
            polling_frame,
            text="Polling Auth (sent to Windows agents and MacSubs)",
            bg=COLORS["surface"],
            fg=COLORS["text_bright"],
            font=("Helvetica", 12, "bold"),
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))

        tk.Label(
            polling_frame,
            text="Token:  (leave empty to disable auth)",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=10)

        self.polling_token_var = tk.StringVar(
            value=self.db_manager.get_config("polling_token", "")
        )
        polling_token_entry = tk.Entry(
            polling_frame,
            textvariable=self.polling_token_var,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            show="*",
            width=50,
        )
        polling_token_entry.pack(anchor=tk.W, padx=10, pady=(0, 10), fill=tk.X)

        # OpenClaw section
        openclaw_frame = tk.Frame(scrollable_frame, bg=COLORS["surface"])
        openclaw_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(
            openclaw_frame,
            text="OpenClaw Integration",
            bg=COLORS["surface"],
            fg=COLORS["text_bright"],
            font=("Helvetica", 12, "bold"),
        ).pack(anchor=tk.W, padx=10, pady=(10, 5))

        # Token field
        tk.Label(
            openclaw_frame,
            text="Token:",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=10)

        self.openclaw_token_var = tk.StringVar(
            value=self.db_manager.get_config("openclaw_token", "")
        )
        token_entry = tk.Entry(
            openclaw_frame,
            textvariable=self.openclaw_token_var,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            show="*",
            width=50,
        )
        token_entry.pack(anchor=tk.W, padx=10, pady=(0, 10), fill=tk.X)

        # Enable checkbox
        self.openclaw_enabled_var = tk.BooleanVar(
            value=self.db_manager.get_config("openclaw_enabled") == "1"
        )
        tk.Checkbutton(
            openclaw_frame,
            text="Enable OpenClaw Integration",
            variable=self.openclaw_enabled_var,
            bg=COLORS["surface"],
            fg=COLORS["text"],
        ).pack(anchor=tk.W, padx=10, pady=5)

        # Test button
        tk.Button(
            openclaw_frame,
            text="Test Connection",
            command=self._test_openclaw,
            bg=COLORS["accent_light"],
            fg=COLORS["text_bright"],
            padx=10,
            pady=5,
        ).pack(anchor=tk.W, padx=10, pady=5)

        # Config path
        config_path_text = f"Config: {self.db_manager.db_path}"
        tk.Label(
            scrollable_frame,
            text=config_path_text,
            bg=COLORS["bg"],
            fg=COLORS["text_dim"],
            font=("Helvetica", 9),
        ).pack(anchor=tk.W, padx=10, pady=(20, 5))

        # Save button
        tk.Button(
            scrollable_frame,
            text="Save Settings",
            command=self._save_settings,
            bg=COLORS["success"],
            fg=COLORS["text_bright"],
            padx=15,
            pady=5,
            font=("Helvetica", 11, "bold"),
        ).pack(anchor=tk.W, padx=10, pady=10)

    def _refresh_servers_list(self):
        """Refresh the servers listbox."""
        self.servers_listbox.delete(0, tk.END)
        servers = self.db_manager.get_servers()

        for server in servers:
            enabled_text = "Yes" if server.enabled else "No"
            text = f"{server.name:<15} {server.ip:<15} {server.port:<8} {enabled_text}"
            self.servers_listbox.insert(tk.END, text)

    def _on_server_selected(self, event):
        """Enable Edit/Remove buttons when a server is selected."""
        if self.servers_listbox.curselection():
            self.edit_button.config(state=tk.NORMAL)
            self.remove_button.config(state=tk.NORMAL)
        else:
            self.edit_button.config(state=tk.DISABLED)
            self.remove_button.config(state=tk.DISABLED)

    def _show_add_server_dialog(self):
        """Show dialog to add a new server."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Add Server")
        dialog.geometry("400x250")
        dialog.configure(bg=COLORS["bg"])

        tk.Label(
            dialog,
            text="Server Name:",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=15, pady=(15, 5))
        name_entry = tk.Entry(
            dialog, bg=COLORS["surface"], fg=COLORS["text"], width=30
        )
        name_entry.pack(padx=15, pady=(0, 10), fill=tk.X)

        tk.Label(
            dialog,
            text="IP Address:",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=15, pady=(0, 5))
        ip_entry = tk.Entry(
            dialog, bg=COLORS["surface"], fg=COLORS["text"], width=30
        )
        ip_entry.pack(padx=15, pady=(0, 10), fill=tk.X)

        tk.Label(
            dialog,
            text="Port:",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=15, pady=(0, 5))
        port_entry = tk.Entry(
            dialog, bg=COLORS["surface"], fg=COLORS["text"], width=30
        )
        port_entry.insert(0, "5678")
        port_entry.pack(padx=15, pady=(0, 10), fill=tk.X)

        def save():
            name = name_entry.get().strip()
            ip = ip_entry.get().strip()
            port = port_entry.get().strip()

            if not name or not ip or not port:
                messagebox.showerror("Error", "All fields are required")
                return

            try:
                port = int(port)
            except ValueError:
                messagebox.showerror("Error", "Port must be a number")
                return

            if not (1 <= port <= 65535):
                messagebox.showerror("Error", "Port must be between 1 and 65535")
                return

            try:
                ipaddress.ip_address(ip)
            except ValueError:
                messagebox.showerror("Error", "Invalid IP address")
                return

            try:
                server = Server(name=name, ip=ip, port=port, enabled=True)
                self.db_manager.add_server(server)
                logger.info(f"Server added: {name}")
                self._refresh_servers_list()
                dialog.destroy()
            except sqlite3.IntegrityError:
                messagebox.showerror("Error", f"Server '{name}' already exists")

        buttons_frame = tk.Frame(dialog, bg=COLORS["bg"])
        buttons_frame.pack(fill=tk.X, padx=15, pady=15)

        tk.Button(
            buttons_frame,
            text="Save",
            command=save,
            bg=COLORS["success"],
            fg=COLORS["text_bright"],
            padx=20,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            buttons_frame,
            text="Cancel",
            command=dialog.destroy,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            padx=20,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)

    def _show_edit_server_dialog(self):
        """Show dialog to edit selected server."""
        selection = self.servers_listbox.curselection()
        if not selection:
            return

        servers = self.db_manager.get_servers()
        server = servers[selection[0]]

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Edit Server: {server.name}")
        dialog.geometry("400x250")
        dialog.configure(bg=COLORS["bg"])

        tk.Label(
            dialog,
            text="Server Name:",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=15, pady=(15, 5))
        name_entry = tk.Entry(
            dialog, bg=COLORS["surface"], fg=COLORS["text"], width=30
        )
        name_entry.insert(0, server.name)
        name_entry.pack(padx=15, pady=(0, 10), fill=tk.X)

        tk.Label(
            dialog,
            text="IP Address:",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=15, pady=(0, 5))
        ip_entry = tk.Entry(
            dialog, bg=COLORS["surface"], fg=COLORS["text"], width=30
        )
        ip_entry.insert(0, server.ip)
        ip_entry.pack(padx=15, pady=(0, 10), fill=tk.X)

        tk.Label(
            dialog,
            text="Port:",
            bg=COLORS["bg"],
            fg=COLORS["text"],
            font=("Helvetica", 10),
        ).pack(anchor=tk.W, padx=15, pady=(0, 5))
        port_entry = tk.Entry(
            dialog, bg=COLORS["surface"], fg=COLORS["text"], width=30
        )
        port_entry.insert(0, str(server.port))
        port_entry.pack(padx=15, pady=(0, 10), fill=tk.X)

        enabled_var = tk.BooleanVar(value=server.enabled)
        tk.Checkbutton(
            dialog,
            text="Enabled",
            variable=enabled_var,
            bg=COLORS["bg"],
            fg=COLORS["text"],
        ).pack(anchor=tk.W, padx=15, pady=5)

        def save():
            name = name_entry.get().strip()
            ip = ip_entry.get().strip()
            port = port_entry.get().strip()

            if not name or not ip or not port:
                messagebox.showerror("Error", "All fields are required")
                return

            try:
                port = int(port)
            except ValueError:
                messagebox.showerror("Error", "Port must be a number")
                return

            if not (1 <= port <= 65535):
                messagebox.showerror("Error", "Port must be between 1 and 65535")
                return

            try:
                ipaddress.ip_address(ip)
            except ValueError:
                messagebox.showerror("Error", "Invalid IP address")
                return

            server.name = name
            server.ip = ip
            server.port = port
            server.enabled = enabled_var.get()
            self.db_manager.update_server(server)
            logger.info(f"Server updated: {name}")
            self._refresh_servers_list()
            dialog.destroy()

        buttons_frame = tk.Frame(dialog, bg=COLORS["bg"])
        buttons_frame.pack(fill=tk.X, padx=15, pady=15)

        tk.Button(
            buttons_frame,
            text="Save",
            command=save,
            bg=COLORS["success"],
            fg=COLORS["text_bright"],
            padx=20,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)

        tk.Button(
            buttons_frame,
            text="Cancel",
            command=dialog.destroy,
            bg=COLORS["surface2"],
            fg=COLORS["text"],
            padx=20,
            pady=5,
        ).pack(side=tk.LEFT, padx=5)

    def _remove_server(self):
        """Remove selected server."""
        selection = self.servers_listbox.curselection()
        if not selection:
            return

        servers = self.db_manager.get_servers()
        server = servers[selection[0]]

        if messagebox.askyesno(
            "Confirm",
            f"Remove server '{server.name}'? This cannot be undone.",
        ):
            self.db_manager.delete_server(server.id)
            logger.info(f"Server removed: {server.name}")
            self._refresh_servers_list()

    def _save_settings(self):
        """Save settings to database."""
        try:
            poll_interval = int(self.poll_interval_var.get())
            if poll_interval < 1:
                raise ValueError("Poll interval must be >= 1")

            report_every = int(self.report_every_var.get())
            if report_every < 1:
                raise ValueError("Report frequency must be >= 1")

            self.db_manager.set_config("poll_interval", str(poll_interval))
            self.db_manager.set_config("report_every_n_polls", str(report_every))
            self.db_manager.set_config(
                "polling_token", self.polling_token_var.get().strip()
            )
            self.db_manager.set_config(
                "openclaw_token", self.openclaw_token_var.get()
            )
            self.db_manager.set_config(
                "openclaw_enabled",
                "1" if self.openclaw_enabled_var.get() else "0",
            )

            logger.info("Settings saved")
            messagebox.showinfo("Success", "Settings saved successfully")
        except ValueError as e:
            messagebox.showerror("Error", str(e))

    def _test_openclaw(self):
        """Test OpenClaw connection."""
        token = self.openclaw_token_var.get().strip()
        if not token:
            messagebox.showerror("Error", "Token is required")
            return

        try:
            payload = json.dumps({"text": "TaskPaw Hub: Connection test"}).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:18789/hooks/wake",
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            messagebox.showinfo("Success", "Connected to OpenClaw successfully")
            logger.info("OpenClaw test successful")
        except Exception as e:
            messagebox.showerror("Error", f"Connection failed: {e}")
            logger.error(f"OpenClaw test failed: {e}")

    def _start_polling(self):
        """Start the polling engine."""
        self.polling_engine = PollingEngine(
            self.db_manager, self._on_polling_update
        )
        self.polling_engine.start()

    def _on_polling_update(self, server_statuses: Dict[str, Dict[str, Any]]):
        """Called when polling engine has new data. Updates GUI."""
        self.server_statuses = server_statuses
        self.root.after(0, self._refresh_dashboard)

    def _refresh_dashboard(self):
        """Refresh the dashboard display."""
        # Clear existing cards
        for widget in self.cards_scrollable_frame.winfo_children():
            widget.destroy()

        # Create new cards for each server
        servers = self.db_manager.get_servers()
        for server in servers:
            status = self.server_statuses.get(server.name, {})
            self._create_server_card(server, status)

        # Refresh events
        self._refresh_events()

    def _create_server_card(
        self, server: Server, status: Dict[str, Any]
    ):
        """Create a server status card."""
        card = tk.Frame(
            self.cards_scrollable_frame,
            bg=COLORS["surface"],
            relief=tk.RAISED,
            bd=1,
        )
        card.pack(fill=tk.X, padx=5, pady=5)

        # Header with status indicator
        header_frame = tk.Frame(card, bg=COLORS["surface"])
        header_frame.pack(fill=tk.X, padx=10, pady=10)

        # Status dot: Green=running/processing, Yellow=idle/stopped, Red=error/offline
        if status.get("reachable"):
            last_seen = status.get("last_seen")
            if last_seen:
                ago = datetime.now() - last_seen
                if ago.total_seconds() < 300:
                    # Determine color from monitor activity
                    monitors = status.get("monitors", [])
                    monitor_statuses = ""
                    if isinstance(monitors, list):
                        monitor_statuses = " ".join(
                            m.get("status", "").lower() for m in monitors
                        )
                    elif isinstance(monitors, dict):
                        monitor_statuses = " ".join(
                            str(v).lower() for v in monitors.values()
                        )

                    # Check for stuck states (orange)
                    if "stuck" in monitor_statuses:
                        dot_color = COLORS["stuck"]
                    # Check for error states (red)
                    elif "error" in monitor_statuses or "failed" in monitor_statuses:
                        dot_color = COLORS["error"]
                    # Check for active/running states (green)
                    elif any(kw in monitor_statuses for kw in [
                        "processing", "running", "extracting",
                        "translating", "moving", "pending",
                    ]):
                        dot_color = COLORS["success"]
                    else:
                        # Idle, stopped, listening, waiting (yellow)
                        dot_color = COLORS["warning"]

                    if ago.total_seconds() < 120:
                        time_text = "now"
                    else:
                        minutes = int(ago.total_seconds() // 60)
                        time_text = f"{minutes}m ago"
                else:
                    dot_color = COLORS["error"]
                    minutes = int(ago.total_seconds() // 60)
                    time_text = f"{minutes}m ago"
            else:
                dot_color = COLORS["warning"]
                time_text = "checking..."
        else:
            dot_color = COLORS["error"]
            time_text = "offline"

        # Dot canvas
        dot_canvas = tk.Canvas(
            header_frame, width=12, height=12, bg=COLORS["surface"], highlightthickness=0
        )
        dot_canvas.pack(side=tk.LEFT, padx=(0, 8))
        dot_canvas.create_oval(1, 1, 11, 11, fill=dot_color, outline=dot_color)

        # Server name and time
        info_frame = tk.Frame(header_frame, bg=COLORS["surface"])
        info_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        name_label = tk.Label(
            info_frame,
            text=f"{server.name} ({server.ip}:{server.port})",
            font=("Helvetica", 11, "bold"),
            bg=COLORS["surface"],
            fg=COLORS["text_bright"],
        )
        name_label.pack(anchor=tk.W)

        time_label = tk.Label(
            info_frame,
            text=f"Last seen: {time_text}",
            font=("Helvetica", 9),
            bg=COLORS["surface"],
            fg=COLORS["text_dim"],
        )
        time_label.pack(anchor=tk.W)

        # Monitors
        monitors = status.get("monitors", [])
        if monitors:
            monitors_frame = tk.Frame(card, bg=COLORS["surface2"])
            monitors_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

            # Handle both list format (from API) and dict format
            if isinstance(monitors, list):
                for m in monitors:
                    name = m.get("name", "Unknown")
                    mstatus = m.get("status", "Unknown")
                    monitor_text = f"{name}: {mstatus}"
                    tk.Label(
                        monitors_frame,
                        text=monitor_text,
                        font=("Helvetica", 9),
                        bg=COLORS["surface2"],
                        fg=COLORS["text"],
                    ).pack(anchor=tk.W, padx=5, pady=2)
            elif isinstance(monitors, dict):
                for monitor_name, monitor_status in monitors.items():
                    monitor_text = f"{monitor_name}: {monitor_status}"
                    tk.Label(
                        monitors_frame,
                        text=monitor_text,
                        font=("Helvetica", 9),
                        bg=COLORS["surface2"],
                        fg=COLORS["text"],
                    ).pack(anchor=tk.W, padx=5, pady=2)

    def _refresh_events(self):
        """Refresh the recent updates display with last 4 status logs."""
        self.events_listbox.delete(0, tk.END)
        logs = self.db_manager.get_recent_status_logs(limit=4)

        if logs:
            for log in logs:
                try:
                    timestamp = datetime.fromisoformat(log["timestamp"]).strftime(
                        "%H:%M:%S"
                    )
                except Exception:
                    timestamp = str(log["timestamp"])
                # Truncate long summaries
                summary = log["summary"]
                if len(summary) > 80:
                    summary = summary[:77] + "..."
                text = f"[{timestamp}] {log['server']}: {summary}"
                self.events_listbox.insert(tk.END, text)
        else:
            self.events_listbox.insert(tk.END, "No updates yet - waiting for first poll...")

    def _on_close(self):
        """Handle window close."""
        if self.polling_engine:
            self.polling_engine.stop()
        logger.info("Application closed")
        self.root.destroy()


def main():
    """Main entry point."""
    root = tk.Tk()
    app = TaskPawHub(root)
    root.mainloop()


if __name__ == "__main__":
    main()
