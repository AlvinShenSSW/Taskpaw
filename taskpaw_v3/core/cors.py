"""CORS for the surfaces the local UI client calls (design §3.2).

The agent NETWORK API (/ping /status /events, polled by the Hub) stays CORS-free.
CORS is opened only on the surfaces the desktop UI talks to — the agent CONTROL
API (loopback) and the Hub API — and only for the local UI origins (the Tauri
webview + the Vite dev server), never "*". Authorization is allowed so the
bearer-gated fetches' preflight succeeds.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# The Tauri webview origins (per-OS) + the Vite dev server. The packaged Windows
# webview uses http://tauri.localhost; macOS/Linux use tauri://localhost (and
# https://tauri.localhost in some configs) — include all so the bearer-gated
# preflight passes on every platform.
UI_ORIGINS = [
    "tauri://localhost",
    "https://tauri.localhost",
    "http://tauri.localhost",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def add_ui_cors(app: FastAPI, extra_origins: list[str] | None = None) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=UI_ORIGINS + (extra_origins or []),
        # PATCH/DELETE for the monitor CRUD control API (#57); GET/POST for the
        # rest. The browser preflights PATCH/DELETE, so they must be allowed or
        # the desktop console can't edit/remove monitors.
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )
