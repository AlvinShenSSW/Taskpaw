# 🐾 TaskPaw

TaskPaw watches the AI tasks and services running on your machines — **LADA**
video restore, **ComfyUI**, download folders, processes — and surfaces their
status, progress, and events in one place, optionally notifying your **OpenClaw**
assistant when work completes.

**V3** is a cross-platform desktop app (Tauri + React) with a two-role design:

- **Agent** — runs on each machine; watches that machine's monitors and exposes a
  small local API. A native console lets you add/edit/start/stop monitors and read
  the local task log (「日志」), retained for 30 days, without touching config files.
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
| `avsubs` | Standalone「AV 翻译」— walk a library folder (recursive by default) and give every MP4 that has no subtitles yet a `<name>.srt` (zh, global LLM API) next to it, via a `<name>.ja.srt` transcript (WhisperJAV) that is deleted once the `.srt` is written (kept as the resume point when translation does not finish); translation is resumable line by line (a Stop or an outage loses at most the request in flight), lines the primary model refuses go to up to two fallback models, a line every model refuses keeps its Japanese text, and a video with no translation service for 2 h is paused and continued at the next Start; a video already has subtitles when its folder holds `<name>.srt` (or `.ass`/`.ssa`/`.vtt`), a `<name>.<tag>.srt` with a non-Japanese tag (e.g. `.chs.srt`), or it is the folder's only video and there is any non-Japanese subtitle — an unreadable folder skips the video for the run and an existing subtitle is never overwritten; takes turns on the GPU with Jasna per file (in-process GPU lease) |
| `jasna` | Jasna video restore — managed (TaskPaw runs one `jasna.exe` per video: skip/resume, per-resolution `unet-4x`, retry + degrade) or passive; file queue with failures, GPU/VRAM, CPU/RAM. Output `<name>-破解.mp4`; a legacy `<name>_restored.mp4` still counts as restored. Optional「AV 翻译」: after each restore, WhisperJAV (ja ASR) + the global LLM API write `<name>-破解.srt` (zh) next to the restored video — always named like that video (`<name>_restored.srt` for a legacy file); the `.ja.srt` transcript is deleted once the `.srt` is written; translation is resumable and uses the fallback models, as in `avsubs` |
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
- **LLM API** (Settings → LLM API, #178): one agent-level base URL / model / key
  (default xAI direct, `https://api.x.ai/v1` + `grok-4.3`) that features such as Jasna's
  AV 翻译 reuse. The key comes from the `TASKPAW_LLM_API_KEY` environment variable
  first, else `agent.yaml`; it is masked everywhere and never logged. Two optional
  **fallback models** (备用模型 1 / 2, #190/#192 — e.g. DeepSeek, then the MiMo Token
  Plan; keys from `TASKPAW_LLM_FALLBACK1_API_KEY` / `TASKPAW_LLM_FALLBACK2_API_KEY`
  first) translate the lines the primary refuses and, with「主模型不可用时改用备用模型」
  on (the default), the lines while it is unavailable. "Test connection" sends one
  real translation request with the current form values of that model, without
  saving them.

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
- **[docs/guides/openclaw-integration.md](docs/guides/openclaw-integration.md)** — read fleet status (CPU/RAM/GPU/queue) from `hub.db` / `status.md`.
- **[Design spec](docs/specs/2026-06-27-taskpaw-v3-design.md)** — V3 architecture.
- **[AGENTS.md](AGENTS.md)** / **[docs/constitution.md](docs/constitution.md)** — repo guide + hard rules (for contributors/agents).
- **[CHANGELOG.md](CHANGELOG.md)** — release notes.

## Author & Copyright

TaskPaw was **initiated, designed, and is maintained by Alvin Shen (304)**.
Copyright © 2026 Alvin Shen (304).

## License

MIT — see [LICENSE](LICENSE).
