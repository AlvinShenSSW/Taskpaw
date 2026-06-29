# 🐾 TaskPaw

TaskPaw watches the AI tasks and services running on your machines — **LADA**
video restore, **ComfyUI**, download folders, processes — and surfaces their
status, progress, and events in one place, optionally notifying your **OpenClaw**
assistant when work completes.

**V3** is a cross-platform desktop app (Tauri + React) with a two-role design:

- **Agent** — runs on each machine; watches that machine's monitors and exposes a
  small local API. A native console lets you add/edit/start/stop monitors and read
  recent events without touching config files.
- **Hub** — a headless aggregator that polls your agents, keeps durable event +
  status history, and forwards completions to OpenClaw. A dashboard view shows the
  whole fleet and its event log.

The UI ships **Simplified Chinese (default) and English**, a Settings tab
(language · agent config · about), a live status dashboard, and native
file/folder pickers for path fields.

> **V2 is frozen.** The original single-file Windows app (`taskpaw.py`) still
> works but is no longer developed. All new work lives under
> [`taskpaw_v3/`](taskpaw_v3/).

## What it monitors

| Monitor | Watches |
|---------|---------|
| `lada` | LADA video restore — managed (TaskPaw launches `lada-cli`, parses progress) or passive (detect an external run); file queue, GPU/VRAM, CPU/RAM |
| `comfyui` | ComfyUI queue (idle = complete) + error diagnostics from its log |
| `folder` | A downloads dir — a file is "done" once its size is stable |
| `process` | Any process by name/pattern (running ↔ exited) |
| `custom_cmd` | Runs a command on a schedule; exit code = status |
| `tcp_check` | A host:port is listening |
| `heartbeat` / `state_file` | A status/heartbeat JSON file stays fresh |
| `host_metrics` | The machine's own CPU/mem/GPU/net (auto-on baseline) |

## Install & run

### Desktop app (recommended)

Download the installer for your OS from the project's **Releases** (Windows `.msi`
/ NSIS `.exe`, macOS `.dmg`). Launch **TaskPaw Agent** on each machine you want to
watch; it self-creates a default config on first run.

- Closing the window fully exits — no orphaned background process (#40).
- The agent's control API is loopback-only. The network API defaults to
  `127.0.0.1` (on-host only); **a fresh config has no token, so auth is disabled**
  — set an API token (Settings → Configuration) before binding it to a LAN address
  so the Hub reaches it over Bearer-authenticated HTTP.

### From source (dev)

Python 3.10+ via [`uv`](https://docs.astral.sh/uv/), Node 22, and the Rust
toolchain (for the Tauri shell).

```bash
# From the repo root:
uv sync --group dev          # Python backend + tests
uv run pytest                # backend test suite

# Headless backend, no GUI (still from the repo root):
uv run python -m taskpaw_v3.bootstrap agent --run     # an agent
uv run python -m taskpaw_v3.bootstrap hub --run       # the Hub

# Build the packaged desktop app (backend sidecar + Tauri bundle). The build
# extra provides PyInstaller for the sidecar:
uv sync --extra build --extra v3
uv run python scripts/build.py

# Frontend tests/build (in its own directory):
cd taskpaw_v3/ui && npm ci && npm test && npm run build
```

## Architecture

```
 Machine A ─ TaskPaw Agent ┐
 Machine B ─ TaskPaw Agent ┼─poll→ TaskPaw Hub ──HTTP POST──→ OpenClaw
 Machine C ─ TaskPaw Agent ┘        (history + status.md)        (Telegram/…)
```

Each agent monitors its own machine; the Hub aggregates them, keeps event/status
history, and notifies OpenClaw on completions. Run as many agents as you like.

## Docs

- **[docs/guides/deployment.md](docs/guides/deployment.md)** — deploy agents + a Hub.
- **[Design spec](docs/specs/2026-06-27-taskpaw-v3-design.md)** — V3 architecture.
- **[AGENTS.md](AGENTS.md)** / **[docs/constitution.md](docs/constitution.md)** — repo guide + hard rules (for contributors/agents).
- **[CHANGELOG.md](CHANGELOG.md)** — release notes.

## Author & Copyright

TaskPaw was **initiated, designed, and is maintained by Alvin Shen (304)**.
Copyright © 2026 Alvin Shen (304).

## License

MIT — see [LICENSE](LICENSE).
